/**
 * Fixture-level tests for lineage assembly — imports the SHIPPED module.
 *
 * Run: node --experimental-strip-types memora-graph/scripts/test_lineage.mjs
 *
 * Must import functions/api/_lineage.ts. A reimplementation that stays green
 * when the real file regresses is not a test.
 *
 * UI predicates that live only inside force-graph.html cannot be imported;
 * those cases are labelled MIRROR below and are NOT coverage of the HTML.
 */

import {
  buildAssociationEdges,
  buildLineageMaps,
  isLineageEdgeType,
} from "../functions/api/_lineage.ts";

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

// --- NON-lineage typed edges must NOT be treated as lineage (M1) ---
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
    assert(!isLineageEdgeType(e.edge_type), `assoc edge ${e.edge_type} is not lineage per isLineageEdgeType`);
  }
  assert(assoc.some(e => e.edge_type === "references"), "references still present as association");
  assert(assoc.some(e => e.edge_type === "contradicts"), "contradicts present, undirected");
  // M1: shipped predicate — only "supersedes"
  assert(isLineageEdgeType("supersedes") === true, "isLineageEdgeType(supersedes)");
  assert(isLineageEdgeType("references") === false, "isLineageEdgeType(references) false");
  assert(isLineageEdgeType("contradicts") === false, "isLineageEdgeType(contradicts) false");
  assert(isLineageEdgeType("implements") === false, "isLineageEdgeType(implements) false");
  assert(isLineageEdgeType(undefined) === false, "isLineageEdgeType(undefined) false");
}

// ---------------------------------------------------------------------------
// MIRROR cases — re-state logic that lives in force-graph.html and cannot be
// imported. These do NOT cover the shipped HTML; they document intended
// behaviour only. If force-graph.html drifts, these stay green.
// ---------------------------------------------------------------------------
{
  // MIRROR of force-graph.html isSupersedesLink: edge_type === "supersedes"
  const isSupersedesLinkMirror = (l) => l.edge_type === "supersedes";
  assert(isSupersedesLinkMirror({ edge_type: "supersedes", directed: true }),
    "MIRROR UI: supersedes is lineage link");
  assert(!isSupersedesLinkMirror({ edge_type: "references", directed: true }),
    "MIRROR UI: directed alone is NOT lineage (force-graph.html not imported)");
  assert(!isSupersedesLinkMirror({ edge_type: "contradicts", directed: true }),
    "MIRROR UI: contradicts not lineage (force-graph.html not imported)");
}

{
  // MIRROR of drawTimeline lineage filter: show all if lineage on, else drop _superseded
  const mems = [
    { id: 1, _superseded: true },
    { id: 2, _superseded: false },
    { id: 3, _superseded: true },
  ];
  const filterMirror = (showLin) => mems.filter(m => showLin || !m._superseded);
  assert(filterMirror(true).length === 3, "MIRROR UI: timeline lineage on shows all");
  assert(filterMirror(false).length === 1 && filterMirror(false)[0].id === 2,
    "MIRROR UI: timeline lineage off hides superseded (force-graph.html not imported)");
}

{
  // MIRROR of loadGen stale discard (M4 pattern) — not force-graph.html
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
  const pA = fakeLoad("A", 50, { supersededIds: [1] });
  const pB = fakeLoad("B", 5, { supersededIds: [99] });
  await Promise.all([pA, pB]);
  const kept = responses.filter(r => !r.discarded);
  assert(kept.length === 1 && kept[0].db === "B", "MIRROR UI: out-of-order only latest DB gen commits");
  assert(responses.some(r => r.db === "A" && r.discarded), "MIRROR UI: slower A discarded");
}

if (failed) {
  console.error(`\n${failed} failure(s)`);
  process.exit(1);
}
console.log("\nAll lineage fixture tests passed (shipped _lineage.ts imported).");
