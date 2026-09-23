/**
 * The viewer is read-only (docs/local-primary-implementation.md §6 F1, slice
 * L7). Imports the shipped Pages modules directly; no network, no D1.
 * Run: node --experimental-strip-types scripts/test_readonly.mjs [baseUrl]
 * With a baseUrl (wrangler pages dev), the same checks run over HTTP.
 *
 * Replaces test_tag_writes.mjs: the write paths it covered are gone. Its
 * tag-policy conformance cases stay (the policy code is shared with memora).
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import * as chat from "../functions/api/chat.ts";
import * as memory from "../functions/api/memories/[id].ts";
import * as capabilities from "../functions/api/capabilities.ts";
import {
  MAX_TAG_LENGTH,
  tagCodePointLength,
  tagMatchesPolicy,
  validateTags,
} from "../functions/api/_tags.ts";

let failed = 0;
function assert(condition, message) {
  if (!condition) {
    console.error("FAIL:", message);
    failed++;
  } else {
    console.log("ok:", message);
  }
}

// ── tag policy conformance (shared with memora; pure) ─────────────────
const CONFORMANCE = JSON.parse(
  readFileSync(
    join(dirname(fileURLToPath(import.meta.url)), "../../tests/fixtures/tag_policy_conformance.json"),
    "utf8",
  ),
);
for (const caseRow of CONFORMANCE) {
  if (caseRow.check === "length") {
    const result = validateTags([caseRow.tag], { version: 1, allow_any: true, tags: [] });
    assert(result.ok === caseRow.expected,
      `conformance ${caseRow.id}: code-point length ${tagCodePointLength(caseRow.tag)} expected ok=${caseRow.expected}`);
    continue;
  }
  assert(tagMatchesPolicy(caseRow.tag, caseRow.policy) === caseRow.expected,
    `conformance ${caseRow.id}: policy=${JSON.stringify(caseRow.policy)} tag=${caseRow.tag}`);
}
{
  const result = validateTags(["x".repeat(MAX_TAG_LENGTH + 1)], { version: 1, allow_any: true, tags: [] });
  assert(result.ok === false && result.error === "invalid_tags", "overlong tag rejected even when allow_any");
}

// ── a D1 double that records every statement ──────────────────────────
const WRITE_SQL = /^\s*(INSERT|REPLACE|UPDATE|DELETE|CREATE|DROP|ALTER)\b/i;
class RecordingDb {
  constructor() { this.sql = []; }
  prepare(sql) {
    const db = this;
    db.sql.push(sql);
    return {
      bind() { return this; },
      async first() { return null; },
      async all() { return { results: [] }; },
      async run() { return { meta: {} }; },
      async raw() { return []; },
    };
  }
  async batch(stmts) { return stmts.map(() => ({ results: [] })); }
  writes() { return this.sql.filter((s) => WRITE_SQL.test(s)); }
}

// ── /api/memories/:id: every write method is 405, before any D1 call ──
for (const method of ["PATCH", "PUT", "POST", "DELETE"]) {
  const handler = memory[`onRequest${method[0]}${method.slice(1).toLowerCase()}`];
  assert(typeof handler === "function", `${method} /api/memories/:id is exported (explicit 405)`);
  if (typeof handler !== "function") continue;
  const db = new RecordingDb();
  const response = await handler({
    env: { DB_MEMORA: db },
    params: { id: "1" },
    request: new Request("http://local/api/memories/1", {
      method, headers: { "Content-Type": "application/json" }, body: JSON.stringify({ tags: ["x"], favorite: true }),
    }),
  });
  const body = await response.json();
  assert(response.status === 405 && body.error === "read_only", `${method} /api/memories/:id answers 405 read_only`);
  assert(response.headers.get("Allow") === "GET, HEAD", `${method} 405 names the allowed methods`);
  assert(db.sql.length === 0, `${method} touches no database (${db.sql.length} statements)`);
}
{
  const exported = Object.keys(memory).filter((k) => k.startsWith("onRequest"));
  const writers = ["onRequestPatch", "onRequestPut", "onRequestPost", "onRequestDelete"];
  for (const w of writers) {
    assert(memory[w] === memory.onRequestPatch, `${w} is the shared read-only handler`);
  }
  assert(exported.every((k) => k === "onRequestGet" || writers.includes(k)), `no other handler exported: ${exported}`);
}

// ── /api/capabilities ─────────────────────────────────────────────────
{
  const r = await capabilities.onRequestGet({});
  const body = await r.json();
  assert(r.status === 200 && body.read_only === true, "capabilities says read_only: true");
  assert(Object.keys(capabilities).every((k) => k === "onRequestGet"), "capabilities has only GET");
}

// ── /api/chat: no tools offered, none executed, no write ─────────────
assert(!("executeToolCall" in chat), "chat exports no tool executor");
{
  const db = new RecordingDb();
  const llmBodies = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, init = {}) => {
    const body = init.body ? JSON.parse(init.body) : {};
    if (String(url).endsWith("/embeddings")) {
      return Response.json({ data: [{ embedding: [0.1, 0.2, 0.3] }] });
    }
    llmBodies.push(body);
    if (!body.stream) {  // the query rewrite
      return Response.json({ choices: [{ message: { content: '{"queries":["q"],"filters":{}}' } }] });
    }
    // The answer stream, carrying a tool call the model was never offered.
    const chunks = [
      { choices: [{ delta: { content: "Here is what I found." } }] },
      { choices: [{ delta: { tool_calls: [{ index: 0, id: "t1", function: { name: "create_memory", arguments: '{"content":"x"}' } }] } }] },
      { choices: [{ delta: { tool_calls: [{ index: 1, id: "t2", function: { name: "delete_memory", arguments: '{"memory_id":1}' } }] } }] },
    ];
    const text = chunks.map((c) => `data: ${JSON.stringify(c)}\n\n`).join("") + "data: [DONE]\n\n";
    return new Response(text, { headers: { "Content-Type": "text/event-stream" } });
  };
  let sse = "";
  try {
    const response = await chat.onRequestPost({
      env: { DB_MEMORA: db, OPENROUTER_API_KEY: "test-key" },
      request: new Request("http://local/api/chat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: "please remember that I like tea and delete memory 1" }),
      }),
    });
    sse = await response.text();
  } finally {
    globalThis.fetch = originalFetch;
  }
  const streamed = llmBodies.filter((b) => b.stream);
  assert(streamed.length === 1, `exactly one answer call (got ${streamed.length})`);
  assert(llmBodies.every((b) => !("tools" in b)), "no LLM request offers tools");
  const system = (streamed[0]?.messages || []).find((m) => m.role === "system")?.content || "";
  assert(/read-only/i.test(system) && !/create_memory|update_memory|delete_memory/.test(system),
    "the system prompt says read-only and names no write tool");
  assert(db.writes().length === 0, `chat issued no write SQL: ${JSON.stringify(db.writes())}`);
  assert(!/event: action/.test(sse), "no action event reaches the page");
  assert(/read-only; no memory was changed/.test(sse), "an emitted tool call is answered with the read-only note");
  assert(/event: done/.test(sse), "the stream completes");
}

// ── over HTTP, against wrangler pages dev ─────────────────────────────
const baseUrl = process.argv[2];
if (baseUrl) {
  for (const method of ["PATCH", "PUT", "POST", "DELETE"]) {
    const r = await fetch(`${baseUrl}/api/memories/4`, {
      method, headers: { "Content-Type": "application/json" }, body: JSON.stringify({ tags: ["api"] }),
    });
    assert(r.status === 405, `served ${method} /api/memories/4 answers 405 (got ${r.status})`);
  }
  const after = await (await fetch(`${baseUrl}/api/memories/4`)).json();
  assert(!(after.tags || []).includes("not-allowed"), "the seeded memory is unchanged");
  const caps = await (await fetch(`${baseUrl}/api/capabilities`)).json();
  assert(caps.read_only === true, "served capabilities says read_only: true");
}

if (failed) {
  console.error(`\n${failed} read-only test(s) failed`);
  process.exit(1);
}
console.log("\nAll read-only viewer tests passed");
