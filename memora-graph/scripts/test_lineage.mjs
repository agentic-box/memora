/**
 * Fixture-level tests for lineage assembly (M1–M3 core).
 * Run: node memora-graph/scripts/test_lineage.mjs
 *
 * Mirrors memora-graph/functions/api/_lineage.ts (kept as plain JS so we do not
 * need a TS runner in CI). If these diverge, fix both.
 */

function buildLineageMaps(crossrefs) {
  const supersededBy = new Map();
  const supersedesMap = new Map();
  const supersedesEdges = [];
  const edgeKeys = new Set();
  const addPair = (newer, older, score) => {
    if (newer === older) return;
    if (!supersedesMap.has(newer)) supersedesMap.set(newer, new Set());
    supersedesMap.get(newer).add(older);
    if (!supersededBy.has(older)) supersededBy.set(older, new Set());
    supersededBy.get(older).add(newer);
    const key = `${newer}->${older}`;
    if (!edgeKeys.has(key)) {
      edgeKeys.add(key);
      supersedesEdges.push({ from: newer, to: older, score });
    }
  };
  for (const [memoryId, refs] of crossrefs) {
    for (const ref of refs || []) {
      if (!ref || typeof ref.id !== "number" || ref.id === memoryId) continue;
      const score = typeof ref.score === "number" ? ref.score : 1.0;
      if (ref.edge_type === "supersedes") addPair(memoryId, ref.id, score);
      else if (ref.edge_type === "superseded_by") addPair(ref.id, memoryId, score);
    }
  }
  return { supersededBy, supersedesMap, supersedesEdges };
}

function buildAssociationEdges(crossrefs, minScore) {
  const edges = [];
  const seen = new Set();
  for (const [memoryId, refs] of crossrefs) {
    for (const ref of refs || []) {
      if (!ref || typeof ref.id !== "number" || ref.id === memoryId) continue;
      const edgeType = ref.edge_type || "related_to";
      if (edgeType === "supersedes" || edgeType === "superseded_by") continue;
      const score = typeof ref.score === "number" ? ref.score : 0;
      if ((edgeType === "related_to" || !ref.edge_type) && score <= minScore) continue;
      const edgeKey = `rel-${Math.min(memoryId, ref.id)}-${Math.max(memoryId, ref.id)}-${edgeType}`;
      if (seen.has(edgeKey)) continue;
      seen.add(edgeKey);
      edges.push({ from: memoryId, to: ref.id, edge_type: edgeType, score, directed: false });
    }
  }
  return edges;
}

function isSupersedesLink(l) {
  return l.edge_type === "supersedes"; // M1 — never l.directed alone
}

let failed = 0;
function assert(cond, msg) {
  if (!cond) {
    console.error("FAIL:", msg);
    failed++;
  } else {
    console.log("ok:", msg);
  }
}

// --- healthy bidirectional ---
{
  const map = new Map([
    [2, [{ id: 1, score: 1, edge_type: "supersedes" }]],
    [1, [{ id: 2, score: 1, edge_type: "superseded_by" }]],
  ]);
  const L = buildLineageMaps(map);
  assert(L.supersedesEdges.length === 1, "bidirectional dedupes to one edge");
  assert(L.supersedesEdges[0].from === 2 && L.supersedesEdges[0].to === 1, "newer=2 older=1");
  assert(L.supersededBy.has(1) && L.supersededBy.get(1).has(2), "1 is superseded by 2");
}

// --- mirror-only (superseded_by half only — drift case) ---
{
  const map = new Map([
    [1, [{ id: 2, score: 1, edge_type: "superseded_by" }]],
  ]);
  const L = buildLineageMaps(map);
  assert(L.supersedesEdges.length === 1, "mirror-only still produces lineage edge");
  assert(L.supersedesEdges[0].from === 2 && L.supersedesEdges[0].to === 1, "mirror normalizes newer=2 older=1");
  assert(L.supersededBy.has(1), "older marked superseded from mirror only");
}

// --- mid-chain: 3 supersedes 2 supersedes 1 ---
{
  const map = new Map([
    [3, [{ id: 2, score: 1, edge_type: "supersedes" }]],
    [2, [
      { id: 3, score: 1, edge_type: "superseded_by" },
      { id: 1, score: 1, edge_type: "supersedes" },
    ]],
    [1, [{ id: 2, score: 1, edge_type: "superseded_by" }]],
  ]);
  const L = buildLineageMaps(map);
  assert(L.supersedesEdges.length === 2, "mid-chain has two lineage edges");
  assert(L.supersededBy.has(1) && L.supersededBy.has(2), "both 1 and 2 superseded");
  assert(L.supersedesMap.has(3) && L.supersedesMap.has(2), "2 and 3 both supersede something");
  assert(!L.supersededBy.has(3), "leaf 3 is not superseded");
}

// --- dangling endpoint (points at missing id) still records maps ---
{
  const map = new Map([
    [5, [{ id: 999, score: 1, edge_type: "supersedes" }]],
  ]);
  const L = buildLineageMaps(map);
  assert(L.supersedesEdges[0].to === 999, "dangling older id preserved");
  assert(L.supersededBy.has(999), "dangling older still in supersededBy");
}

// --- NON-lineage typed edges must NOT get directed lineage styling ---
{
  const map = new Map([
    [10, [
      { id: 11, score: 1, edge_type: "references" },
      { id: 12, score: 1, edge_type: "contradicts" },
      { id: 13, score: 1, edge_type: "implements" },
      { id: 14, score: 0.8, edge_type: "related_to" },
      { id: 15, score: 1, edge_type: "supersedes" },
    ]],
  ]);
  const assoc = buildAssociationEdges(map, 0.4);
  const L = buildLineageMaps(map);
  assert(L.supersedesEdges.length === 1 && L.supersedesEdges[0].to === 15, "only supersedes is lineage");
  for (const e of assoc) {
    assert(e.directed === false, `assoc edge ${e.edge_type} is not directed`);
    assert(!isSupersedesLink(e), `assoc edge ${e.edge_type} is not supersedes-link for UI`);
  }
  assert(assoc.some(e => e.edge_type === "references"), "references still present as association");
  assert(assoc.some(e => e.edge_type === "contradicts"), "contradicts present, undirected");
  // UI guard
  assert(isSupersedesLink({ edge_type: "supersedes", directed: true }), "supersedes is lineage link");
  assert(!isSupersedesLink({ edge_type: "references", directed: true }), "M1: directed alone is NOT lineage");
  assert(!isSupersedesLink({ edge_type: "contradicts", directed: true }), "M1: contradicts not lineage");
}

// --- timeline filter logic (mirrors force-graph drawTimeline) ---
{
  const mems = [
    { id: 1, _superseded: true },
    { id: 2, _superseded: false },
    { id: 3, _superseded: true },
  ];
  const lineageOn = true;
  const lineageOff = false;
  const filter = (showLin) => mems.filter(m => showLin || !m._superseded);
  assert(filter(lineageOn).length === 3, "timeline lineage on shows all");
  assert(filter(lineageOff).length === 1 && filter(lineageOff)[0].id === 2, "timeline lineage off hides superseded");
}

// --- out-of-order DB response simulation ---
{
  let loadGen = 0;
  const responses = [];
  async function fakeLoad(db, delay, payload) {
    const gen = ++loadGen;
    await new Promise(r => setTimeout(r, delay));
    if (gen !== loadGen) {
      responses.push({ db, discarded: true });
      return null;
    }
    responses.push({ db, discarded: false, payload });
    return payload;
  }
  // Switch A (slow) then B (fast): B should win; A discarded
  const pA = fakeLoad("A", 50, { supersededIds: [1] });
  const pB = fakeLoad("B", 5, { supersededIds: [99] });
  await Promise.all([pA, pB]);
  const kept = responses.filter(r => !r.discarded);
  assert(kept.length === 1 && kept[0].db === "B", "out-of-order: only latest DB gen commits");
  assert(responses.some(r => r.db === "A" && r.discarded), "out-of-order: slower A discarded");
}

if (failed) {
  console.error(`\n${failed} failure(s)`);
  process.exit(1);
}
console.log("\nAll lineage fixture tests passed.");
