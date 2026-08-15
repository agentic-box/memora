/**
 * ?limit= tests for GET /api/graph. Imports the shipped Pages handler and
 * exercises it against a fake D1.
 * Run: node --experimental-strip-types scripts/test_graph_limit.mjs
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { onRequestGet } from "../functions/api/graph.ts";

let failed = 0;
function assert(cond, message) {
  if (!cond) {
    console.error("FAIL:", message);
    failed++;
  } else {
    console.log("ok:", message);
  }
}

class FakeDb {
  constructor(memories, crossrefs = []) {
    this.memories = memories;
    this.crossrefs = crossrefs;
  }

  prepare(sql) {
    const db = this;
    return {
      async all() {
        if (sql.includes("FROM memories_crossrefs")) {
          return { results: db.crossrefs };
        }
        if (sql.includes("FROM tombstone_components")) {
          return { results: [] };
        }
        if (sql.includes("FROM tombstones")) {
          return { results: [] };
        }
        if (sql.includes("FROM memories")) {
          return { results: db.memories };
        }
        return { results: [] };
      },
    };
  }
}

function memory(id, created_at, content = `memory ${id}`, overrides = {}) {
  return {
    id,
    content,
    metadata: overrides.metadata ?? "{}",
    tags: overrides.tags ?? "[]",
    created_at,
    updated_at: null,
  };
}

async function graph(env, url) {
  return onRequestGet({
    env,
    request: new Request(url),
  });
}

// Shared adversarial grammar fixture (same file the Python suite runs).
const conformance = JSON.parse(
  readFileSync(
    join(dirname(fileURLToPath(import.meta.url)), "../../tests/fixtures/graph_limit_conformance.json"),
    "utf8",
  ),
);

// Default (no ?limit): full graph, not truncated.
{
  const db = new FakeDb([
    memory(1, "2026-01-01T00:00:00Z"),
    memory(2, "2026-02-01T00:00:00Z"),
  ]);
  const res = await graph({ DB_MEMORA: db }, "http://local/api/graph");
  const data = await res.json();
  assert(res.status === 200, "no limit returns 200");
  assert(data.truncated === false, "no limit is not truncated");
  assert(data.total === 2, "no limit total equals node count");
  assert(data.nodes.length === 2, "no limit returns all nodes");
}

// Default cap: without ?limit, a store larger than DEFAULT_GRAPH_LIMIT is
// truncated to the newest DEFAULT_GRAPH_LIMIT nodes (mutation: removing the
// default cap makes this return all 2100 and truncated=false).
{
  const many = [];
  for (let i = 0; i < 2100; i++) {
    many.push(memory(i + 1, new Date(2020, 0, 1 + i).toISOString()));
  }
  const db = new FakeDb(many);
  const res = await graph({ DB_MEMORA: db }, "http://local/api/graph");
  const data = await res.json();
  assert(res.status === 200, "default-cap store returns 200");
  assert(data.truncated === true, "default cap truncates a >2000 store");
  assert(data.total === 2100, "default cap total reports 2100");
  assert(data.nodes.length === 2000, "default cap returns 2000 nodes");
}

// ?limit=N returns the N newest, newest first, truncated=true, total set.
{
  const db = new FakeDb([
    memory(1, "2026-01-01T00:00:00Z"),
    memory(2, "2026-02-01T00:00:00Z"),
    memory(3, "2026-03-01T00:00:00Z"),
    memory(4, "2026-04-01T00:00:00Z"),
    memory(5, "2026-05-01T00:00:00Z"),
  ]);
  const res = await graph({ DB_MEMORA: db }, "http://local/api/graph?limit=3");
  const data = await res.json();
  assert(res.status === 200, "limit=3 returns 200");
  assert(data.truncated === true, "limit=3 is truncated");
  assert(data.total === 5, "limit=3 total reports the full 5");
  assert(data.nodes.length === 3, "limit=3 returns 3 nodes");
  const ids = data.nodes.map((n) => n.id);
  assert(
    JSON.stringify(ids) === JSON.stringify([5, 4, 3]),
    `newest-first order: got ${JSON.stringify(ids)} expected [5,4,3]`,
  );
}

// Deterministic id tiebreak when created_at collides.
{
  const db = new FakeDb([
    memory(1, "2026-01-01T00:00:00Z"),
    memory(2, "2026-01-01T00:00:00Z"),
    memory(3, "2026-01-01T00:00:00Z"),
  ]);
  const res = await graph({ DB_MEMORA: db }, "http://local/api/graph?limit=2");
  const data = await res.json();
  const ids = data.nodes.map((n) => n.id);
  assert(
    JSON.stringify(ids) === JSON.stringify([3, 2]),
    `id tiebreak newest-first: got ${JSON.stringify(ids)} expected [3,2]`,
  );
}

// Hard-max clamp is OBSERVED against the real constant: build a store larger
// than GRAPH_LIMIT_MAX (5000), request above it, assert exactly GRAPH_LIMIT_MAX
// nodes + truncated + total. Red under the leader's mutation (max -> huge)
// because then all 5100 nodes come back untruncated.
{
  const many = [];
  for (let i = 0; i < 5100; i++) {
    many.push(memory(i + 1, new Date(2020, 0, 1 + i).toISOString()));
  }
  const db = new FakeDb(many);
  const res = await graph({ DB_MEMORA: db }, "http://local/api/graph?limit=999999");
  const data = await res.json();
  assert(res.status === 200, "clamped oversized limit returns 200");
  assert(
    data.nodes.length === 5000,
    `clamp keeps exactly GRAPH_LIMIT_MAX=5000 nodes, got ${data.nodes.length}`,
  );
  assert(data.truncated === true, "clamped oversized limit is truncated");
  assert(data.total === 5100, "clamped oversized limit total reports the store count");
}

// The hard max is tunable via env (test-harness injection): a small injected
// max is respected. Red if the env override is ignored (max stays huge).
{
  const db = new FakeDb([
    memory(1, "2026-01-01T00:00:00Z"),
    memory(2, "2026-02-01T00:00:00Z"),
    memory(3, "2026-03-01T00:00:00Z"),
    memory(4, "2026-04-01T00:00:00Z"),
    memory(5, "2026-05-01T00:00:00Z"),
  ]);
  const res = await graph(
    { DB_MEMORA: db, GRAPH_LIMIT_MAX: "3" },
    "http://local/api/graph?limit=999999",
  );
  const data = await res.json();
  assert(res.status === 200, "env-injected max oversized limit returns 200");
  assert(data.nodes.length === 3, "env-injected max respects GRAPH_LIMIT_MAX=3");
  assert(data.truncated === true, "env-injected max oversized limit is truncated");
  const ids = data.nodes.map((n) => n.id);
  assert(JSON.stringify(ids) === JSON.stringify([5, 4, 3]), "env-injected clamp keeps newest");
}

// Explicit limit below the max is not clamped.
{
  const db = new FakeDb([
    memory(1, "2026-01-01T00:00:00Z"),
    memory(2, "2026-02-01T00:00:00Z"),
    memory(3, "2026-03-01T00:00:00Z"),
    memory(4, "2026-04-01T00:00:00Z"),
    memory(5, "2026-05-01T00:00:00Z"),
  ]);
  const res = await graph({ DB_MEMORA: db }, "http://local/api/graph?limit=10");
  const data = await res.json();
  assert(data.truncated === false, "limit=10 not truncated");
  assert(data.total === 5, "limit=10 total=5");
  assert(data.nodes.length === 5, "limit=10 returns all nodes");
}

// Non-numeric, zero, and negative ?limit are rejected with 400 invalid_limit.
for (const bad of ["abc", "0", "-5", "2.5", ""]) {
  const db = new FakeDb([memory(1, "2026-01-01T00:00:00Z")]);
  const res = await graph({ DB_MEMORA: db }, `http://local/api/graph?limit=${bad}`);
  const data = await res.json();
  assert(res.status === 400, `limit=${JSON.stringify(bad)} -> 400`);
  assert(data.error === "invalid_limit", `limit=${JSON.stringify(bad)} -> invalid_limit`);
}

// No dangling edges: with limit keeping only the newest node, a crossref edge
// to the excluded older node must be dropped.
{
  const db = new FakeDb(
    [
      memory(1, "2026-01-01T00:00:00Z", "older"),
      memory(2, "2026-02-01T00:00:00Z", "newer"),
    ],
    [
      { memory_id: 1, related: JSON.stringify([{ id: 2, score: 0.9, edge_type: "related_to" }]) },
      { memory_id: 2, related: "[]" },
    ],
  );
  const res = await graph({ DB_MEMORA: db }, "http://local/api/graph?limit=1");
  const data = await res.json();
  assert(res.status === 200, "limit=1 returns 200");
  assert(data.nodes.length === 1, "limit=1 keeps one node");
  const nodeIds = new Set(data.nodes.map((n) => n.id));
  for (const edge of data.edges) {
    assert(
      nodeIds.has(edge.from) && nodeIds.has(edge.to),
      `edge ${edge.from}->${edge.to} must not dangle to an excluded node`,
    );
  }
}

// Shared adversarial grammar matrix: invalid values must 400, valid values 200.
// When a valid value is a real limit (<= store size) we also check the count.
{
  const db = new FakeDb([
    memory(1, "2026-01-01T00:00:00Z"),
    memory(2, "2026-02-01T00:00:00Z"),
    memory(3, "2026-03-01T00:00:00Z"),
    memory(4, "2026-04-01T00:00:00Z"),
    memory(5, "2026-05-01T00:00:00Z"),
  ]);
  for (const caseRow of conformance) {
    const res = await graph(
      { DB_MEMORA: db },
      `http://local/api/graph?limit=${encodeURIComponent(caseRow.value)}`,
    );
    if (!caseRow.valid) {
      assert(res.status === 400, `conformance ${caseRow.id}: ${JSON.stringify(caseRow.value)} -> 400`);
    } else {
      assert(res.status === 200, `conformance ${caseRow.id}: ${JSON.stringify(caseRow.value)} -> 200`);
      if (caseRow.expected <= 5) {
        const data = await res.json();
        assert(
          data.nodes.length === caseRow.expected,
          `conformance ${caseRow.id}: value ${JSON.stringify(caseRow.value)} yields ${caseRow.expected} nodes, got ${data.nodes.length}`,
        );
      }
    }
  }
}

// Effective default respects a max below the default: GRAPH_LIMIT_MAX=3 with no
// ?limit must cap at 3, not the 2000 default (red if the default ignores max).
{
  const db = new FakeDb([
    memory(1, "2026-01-01T00:00:00Z"),
    memory(2, "2026-02-01T00:00:00Z"),
    memory(3, "2026-03-01T00:00:00Z"),
    memory(4, "2026-04-01T00:00:00Z"),
    memory(5, "2026-05-01T00:00:00Z"),
  ]);
  const res = await graph({ DB_MEMORA: db, GRAPH_LIMIT_MAX: "3" }, "http://local/api/graph");
  const data = await res.json();
  assert(res.status === 200, "max<default no-query returns 200");
  assert(data.nodes.length === 3, "max<default no-query caps at the max (3)");
  assert(data.truncated === true, "max<default no-query is truncated");
  assert(data.total === 5, "max<default no-query total reports 5");
}

// FIX 4: junk env caps fall back to the default, not a partially-parsed value.
{
  const db = new FakeDb([
    memory(1, "2026-01-01T00:00:00Z"),
    memory(2, "2026-02-01T00:00:00Z"),
    memory(3, "2026-03-01T00:00:00Z"),
    memory(4, "2026-04-01T00:00:00Z"),
    memory(5, "2026-05-01T00:00:00Z"),
  ]);
  for (const junk of ["3junk", "2.5", "1e3", "+7"]) {
    const res = await graph(
      { DB_MEMORA: db, GRAPH_LIMIT_MAX: junk },
      "http://local/api/graph?limit=999999",
    );
    const data = await res.json();
    assert(
      data.nodes.length === 5,
      `malformed env GRAPH_LIMIT_MAX=${JSON.stringify(junk)} falls back to default (all 5), got ${data.nodes.length}`,
    );
  }
}

// FIX 5: duplicateIds and tagColors are closed over the included nodes.
{
  const db = new FakeDb(
    [
      memory(1, "2026-01-01T00:00:00Z", "dup a", { tags: '["alpha"]' }),
      memory(2, "2026-02-01T00:00:00Z", "dup b", { tags: '["beta"]' }),
      memory(3, "2026-03-01T00:00:00Z", "newest", { tags: '["gamma"]' }),
    ],
    [
      { memory_id: 1, related: JSON.stringify([{ id: 2, score: 0.9, edge_type: "related_to" }]) },
      { memory_id: 2, related: JSON.stringify([{ id: 1, score: 0.9, edge_type: "related_to" }]) },
      { memory_id: 3, related: "[]" },
    ],
  );
  const res = await graph({ DB_MEMORA: db }, "http://local/api/graph?limit=1");
  const data = await res.json();
  const nodeIds = new Set(data.nodes.map((n) => n.id));
  for (const id of data.duplicateIds || []) {
    assert(nodeIds.has(id), `duplicateIds entry ${id} must be an included node (FIX 5)`);
  }
  for (const tag of Object.keys(data.tagColors || {})) {
    const refs = data.tagToNodes?.[tag];
    assert(
      Array.isArray(refs) && refs.length > 0,
      `tagColors key ${JSON.stringify(tag)} must have an included node (FIX 5)`,
    );
  }
}

if (failed) {
  console.error(`\n${failed} graph-limit test(s) failed`);
  process.exit(1);
}
console.log("\nAll graph-limit tests passed");
