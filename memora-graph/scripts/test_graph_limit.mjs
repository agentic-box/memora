/**
 * ?limit= tests for GET /api/graph. Imports the shipped Pages handler and
 * exercises it against a fake D1.
 * Run: node --experimental-strip-types scripts/test_graph_limit.mjs
 */

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

function memory(id, created_at, content = `memory ${id}`) {
  return {
    id,
    content,
    metadata: "{}",
    tags: "[]",
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

// ?limit=N larger than the node count is not truncated.
{
  const db = new FakeDb([
    memory(1, "2026-01-01T00:00:00Z"),
    memory(2, "2026-02-01T00:00:00Z"),
    memory(3, "2026-03-01T00:00:00Z"),
  ]);
  const res = await graph({ DB_MEMORA: db }, "http://local/api/graph?limit=10");
  const data = await res.json();
  assert(data.truncated === false, "limit=10 not truncated");
  assert(data.total === 3, "limit=10 total=3");
  assert(data.nodes.length === 3, "limit=10 returns all nodes");
}

// Non-numeric, zero, and negative ?limit are rejected with 400 invalid_limit.
for (const bad of ["abc", "0", "-5", "2.5", ""]) {
  const db = new FakeDb([memory(1, "2026-01-01T00:00:00Z")]);
  const res = await graph({ DB_MEMORA: db }, `http://local/api/graph?limit=${bad}`);
  const data = await res.json();
  assert(res.status === 400, `limit=${JSON.stringify(bad)} -> 400`);
  assert(data.error === "invalid_limit", `limit=${JSON.stringify(bad)} -> invalid_limit`);
}

// ?limit= far above the hard cap is clamped, not rejected.
{
  const db = new FakeDb([
    memory(1, "2026-01-01T00:00:00Z"),
    memory(2, "2026-02-01T00:00:00Z"),
    memory(3, "2026-03-01T00:00:00Z"),
  ]);
  const res = await graph({ DB_MEMORA: db }, "http://local/api/graph?limit=999999");
  const data = await res.json();
  assert(res.status === 200, "oversized limit is clamped, not rejected");
  assert(data.truncated === false, "oversized limit over 3 nodes is not truncated");
  assert(data.nodes.length === 3, "oversized limit returns all nodes");
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

if (failed) {
  console.error(`\n${failed} graph-limit test(s) failed`);
  process.exit(1);
}
console.log("\nAll graph-limit tests passed");
