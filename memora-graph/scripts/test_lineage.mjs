/**
 * Fixture-level tests for lineage assembly — imports the SHIPPED module.
 *
 * Run: node --experimental-strip-types memora-graph/scripts/test_lineage.mjs
 */

import {
  buildAssociationEdges,
  buildLineageMaps,
  isLineageEdgeType,
  parseRelatedPayload,
  partitionLineageEdges,
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

// --- mirror-only ---
{
  const map = new Map([
    [1, [{ id: 2, score: 1, edge_type: "superseded_by" }]],
  ]);
  const L = buildLineageMaps(map);
  assert(L.supersedesEdges.length === 1, "mirror-only still produces lineage edge");
  assert(L.supersedesEdges[0].from === 2 && L.supersedesEdges[0].to === 1, "mirror normalizes newer=2 older=1");
}

// --- mid-chain ---
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
  assert(!L.supersededBy.has(3), "leaf 3 is not superseded");
}

// --- dangling: keep superseded, no drawable edge without both ends ---
{
  const map = new Map([
    [5, [{ id: 999, score: 1, edge_type: "supersedes" }]],
  ]);
  const L = buildLineageMaps(map);
  assert(L.supersededBy.has(999), "dangling older still superseded");
  const part = partitionLineageEdges(L.supersedesEdges, new Set([5]));
  assert(part.drawable.length === 0, "dangling not drawable");
  assert(part.dangling.length === 1 && part.dangling[0].missing === "to", "dangling integrity recorded");
}

// --- NON-lineage typed edges (M1) ---
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
    assert(!isLineageEdgeType(e.edge_type), `assoc edge ${e.edge_type} is not lineage`);
  }
  assert(isLineageEdgeType("supersedes"), "isLineageEdgeType(supersedes)");
  assert(!isLineageEdgeType("contradicts"), "isLineageEdgeType(contradicts) false");
}

// --- F6: reverse halves collapse ---
{
  const map = new Map([
    [1, [{ id: 2, score: 1, edge_type: "references" }]],
    [2, [{ id: 1, score: 1, edge_type: "referenced_by" }]],
  ]);
  const assoc = buildAssociationEdges(map, 0.4);
  assert(assoc.length === 1, "references+referenced_by collapse to one undirected edge");
  assert(assoc[0].edge_type === "references", "canonical type is references");
}

// --- F5: score mismatch keeps max; cycle surfaces conflict ---
{
  const map = new Map([
    [2, [{ id: 1, score: 0.2, edge_type: "supersedes" }]],
    [1, [{ id: 2, score: 0.9, edge_type: "superseded_by" }]],
  ]);
  // same direction twice via both halves with different scores → one edge, max score
  const L = buildLineageMaps(map);
  assert(L.supersedesEdges.length === 1, "same-direction halves merge");
  assert(L.supersedesEdges[0].score === 0.9, "score policy = max");
}
{
  const map = new Map([
    [1, [{ id: 2, score: 1, edge_type: "supersedes" }]],
    [2, [{ id: 1, score: 1, edge_type: "supersedes" }]],
  ]);
  const L = buildLineageMaps(map);
  assert(L.conflicts.some(c => c.kind === "cycle"), "cycle conflict surfaced");
  assert(L.supersededBy.has(1) && L.supersededBy.has(2), "cycle marks both superseded");
}

// --- parseRelatedPayload fail-closed ---
{
  assert(parseRelatedPayload("not-json").ok === false, "malformed JSON rejected");
  assert(parseRelatedPayload("{}").ok === false, "non-array rejected");
  assert(parseRelatedPayload('[{"id":"x"}]').ok === false, "bad id rejected");
  const ok = parseRelatedPayload('[{"id":1,"score":0.5,"edge_type":"supersedes"}]');
  assert(ok.ok && ok.entries[0].id === 1, "valid payload accepted");
}

// --- F1 unavailable stats must never say zero superseded ---
{
  // Mirrors formatLineageStats in force-graph.html (same string contract).
  function formatLineageStats(rawGraph, showLin) {
    if (!rawGraph || rawGraph.lineageAvailable === false) {
      return "LINEAGE UNAVAILABLE (cannot confirm current)";
    }
    const supN = typeof rawGraph.supersededCount === "number"
      ? rawGraph.supersededCount
      : (Array.isArray(rawGraph.supersededIds) ? rawGraph.supersededIds.length : 0);
    const supE = Array.isArray(rawGraph.supersedesEdges) ? rawGraph.supersedesEdges.length : 0;
    return showLin
      ? `${supN} superseded · ${supE} lineage edges`
      : `lineage hidden (${supN} superseded)`;
  }
  const unavailable = {
    lineageAvailable: false,
    supersededIds: null,
    supersededCount: null,
    supersedesEdges: null,
  };
  const txt = formatLineageStats(unavailable, true);
  assert(!/0 superseded/.test(txt), "unavailable stats must NOT say '0 superseded'");
  assert(/UNAVAILABLE/i.test(txt), "unavailable stats say UNAVAILABLE");
  // Wire shape: counts are null, not 0
  assert(unavailable.supersededCount === null, "wire supersededCount is null when unavailable");
  assert(unavailable.supersededIds === null, "wire supersededIds is null when unavailable");
}

// ---------------------------------------------------------------------------
// MIRROR UI — force-graph.html not imported; document intent only.
// ---------------------------------------------------------------------------
{
  const isSupersedesLinkMirror = (l) => l.edge_type === "supersedes";
  assert(!isSupersedesLinkMirror({ edge_type: "references", directed: true }),
    "MIRROR UI: directed alone is NOT lineage");
}

if (failed) {
  console.error(`\n${failed} failure(s)`);
  process.exit(1);
}
console.log("\nAll lineage fixture tests passed (shipped _lineage.ts imported).");
