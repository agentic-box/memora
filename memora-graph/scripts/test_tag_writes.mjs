/**
 * Tag-policy write-path tests. Imports shipped Pages modules directly.
 * Run: node --experimental-strip-types scripts/test_tag_writes.mjs
 */

import { executeToolCall } from "../functions/api/chat.ts";
import { onRequestPatch } from "../functions/api/memories/[id].ts";

const VALID_POLICY = JSON.stringify({
  version: 1,
  allow_any: false,
  tags: ["deploy", "project.*"],
});

let failed = 0;
function assert(condition, message) {
  if (!condition) {
    console.error("FAIL:", message);
    failed++;
  } else {
    console.log("ok:", message);
  }
}

class FakeDb {
  constructor(policy = VALID_POLICY) {
    this.policy = policy;
    this.memory = {
      id: 1,
      content: "existing memory",
      metadata: "{}",
      tags: '["deploy"]',
      created_at: "2026-01-01T00:00:00Z",
      updated_at: null,
    };
    this.writeCount = 0;
  }

  prepare(sql) {
    const db = this;
    return {
      params: [],
      bind(...params) {
        this.params = params;
        return this;
      },
      async first() {
        if (sql.includes("FROM memories_meta")) {
          return db.policy === null ? null : { value: db.policy };
        }
        if (sql.includes("FROM memories WHERE id")) return { ...db.memory };
        return null;
      },
      async run() {
        db.writeCount++;
        if (sql.startsWith("UPDATE memories SET metadata")) {
          db.memory.metadata = this.params[0];
          db.memory.tags = this.params[1];
        } else if (sql.startsWith("UPDATE memories SET content")) {
          db.memory.content = this.params[0];
          db.memory.tags = this.params[1];
        }
        return { meta: { last_row_id: 2 } };
      },
    };
  }
}

async function patch(db, tags) {
  return onRequestPatch({
    env: { DB_MEMORA: db },
    params: { id: "1" },
    request: new Request("http://local/api/memories/1", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tags }),
    }),
  });
}

// NC1: removing PATCH validation makes this write the disallowed tag.
{
  const db = new FakeDb();
  const before = db.memory.tags;
  const response = await patch(db, ["not-allowed"]);
  assert(response.status === 400, "NC1 PATCH rejects a tag outside policy");
  assert(db.writeCount === 0 && db.memory.tags === before, "NC1 PATCH rejection performs no write");
}

// Shape checks mirror Python: strings only and non-empty after trim.
{
  const nonString = new FakeDb();
  const empty = new FakeDb();
  assert((await patch(nonString, [7])).status === 400, "PATCH rejects non-string tags");
  assert((await patch(empty, ["  "])).status === 400, "PATCH rejects empty tags");
  assert(nonString.writeCount === 0 && empty.writeCount === 0, "invalid PATCH shapes perform no write");
}

// Valid exact/wildcard tags are normalized with Python's trim behavior.
{
  const db = new FakeDb();
  const response = await patch(db, [" deploy ", "project.child"]);
  assert(response.status === 200, "PATCH accepts exact and wildcard policy tags");
  assert(db.memory.tags === '["deploy","project.child"]', "PATCH stores trimmed validated tags");
}

// NC2: policy absence/malformed content must fail closed, never allow-any.
for (const [name, policy] of [["missing", null], ["malformed", "{bad"]]) {
  const db = new FakeDb(policy);
  const response = await patch(db, ["deploy"]);
  assert(response.status === 503, `NC2 PATCH fails closed for ${name} policy`);
  assert(db.writeCount === 0, `NC2 ${name} policy performs no write`);
}

const tagPolicy = JSON.parse(VALID_POLICY);
let embeddingFetches = 0;
const originalFetch = globalThis.fetch;
globalThis.fetch = async () => {
  embeddingFetches++;
  throw new Error("embedding fetch must not run");
};

try {
  // NC3: create validation must precede INSERT and embedding generation.
  {
    embeddingFetches = 0;
    const db = new FakeDb();
    const result = JSON.parse(await executeToolCall(
      db, "create_memory", { content: "new", tags: ["blocked"] }, "key", "model", tagPolicy,
    ));
    assert(result.success === false && result.error === "invalid_tags", "NC3 chat create rejects disallowed tags");
    assert(db.writeCount === 0 && embeddingFetches === 0, "NC3 chat create rejects before write/embedding");
  }

  // NC4: update validation must precede UPDATE and embedding generation.
  {
    embeddingFetches = 0;
    const db = new FakeDb();
    const result = JSON.parse(await executeToolCall(
      db, "update_memory", { memory_id: 1, tags: [" "] }, "key", "model", tagPolicy,
    ));
    assert(result.success === false && result.error === "invalid_tags", "NC4 chat update rejects empty tags");
    assert(db.writeCount === 0 && embeddingFetches === 0, "NC4 chat update rejects before write/embedding");
  }
} finally {
  globalThis.fetch = originalFetch;
}

const baseUrl = process.argv[2];
if (baseUrl) {
  const rejected = await fetch(`${baseUrl}/api/memories/4`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tags: ["not-allowed"] }),
  });
  assert(rejected.status === 400, "seeded local D1 policy rejects a disallowed PATCH tag");

  const accepted = await fetch(`${baseUrl}/api/memories/4`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tags: ["api", "project.child"] }),
  });
  assert(accepted.status === 200, "seeded local D1 policy accepts exact and wildcard tags");
}

if (failed) {
  console.error(`\n${failed} tag-write test(s) failed`);
  process.exit(1);
}
console.log("\nAll tag-write tests passed");
