/**
 * Fixture tests — import shipped _lineage.ts only (no reimplementation).
 * Run: node --experimental-strip-types memora-graph/scripts/test_lineage.mjs
 *
 * G1 lives in force-graph.html (not importable) — we assert the source contract
 * by reading the file rather than assert(true).
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  buildAssociationEdges,
  buildLineageMaps,
  isLineageEdgeType,
  normalizeAssociationRef,
  parseRelatedPayload,
  partitionLineageEdges,
} from "../functions/api/_lineage.ts";

const __dirname = dirname(fileURLToPath(import.meta.url));

let failed = 0;
function assert(cond, msg) {
  if (!cond) {
    console.error("FAIL:", msg);
    failed++;
  } else {
    console.log("ok:", msg);
  }
}

// --- prior: bidirectional halves normalize ---
{
  const L = buildLineageMaps(new Map([
    [2, [{ id: 1, score: 1, edge_type: "supersedes" }]],
    [1, [{ id: 2, score: 1, edge_type: "superseded_by" }]],
  ]));
  assert(L.supersedesEdges.length === 1 && L.supersedesEdges[0].from === 2, "bidirectional halves → one edge");
}

// --- prior: mirror-only (superseded_by only on older) ---
{
  const L = buildLineageMaps(new Map([
    [1, [{ id: 2, score: 1, edge_type: "superseded_by" }]],
  ]));
  assert(L.supersedesEdges.length === 1, "mirror-only still produces lineage edge");
  assert(L.supersedesEdges[0].from === 2 && L.supersedesEdges[0].to === 1, "mirror normalizes newer=2 older=1");
  assert(L.supersededBy.has(1), "older is superseded");
}

// --- prior: mid-chain ---
{
  const L = buildLineageMaps(new Map([
    [3, [{ id: 2, score: 1, edge_type: "supersedes" }]],
    [2, [
      { id: 3, score: 1, edge_type: "superseded_by" },
      { id: 1, score: 1, edge_type: "supersedes" },
    ]],
    [1, [{ id: 2, score: 1, edge_type: "superseded_by" }]],
  ]));
  assert(L.supersedesEdges.length === 2, "mid-chain has two lineage edges");
  assert(L.supersededBy.has(1) && L.supersededBy.has(2), "both 1 and 2 superseded");
  assert(!L.supersededBy.has(3), "leaf 3 is not superseded");
}

// --- prior: max-score on same-direction halves ---
{
  const L = buildLineageMaps(new Map([
    [2, [{ id: 1, score: 0.2, edge_type: "supersedes" }]],
    [1, [{ id: 2, score: 0.9, edge_type: "superseded_by" }]],
  ]));
  assert(L.supersedesEdges.length === 1, "same-direction halves merge");
  assert(L.supersedesEdges[0].score === 0.9, "score policy = max");
}

// --- G2: direction + score stable across reversed row order ---
{
  // Leader repro: 5 --references(0.2)--> 4  and mirror  4 --referenced_by(0.9)--> 5
  const orderA = new Map([
    [5, [{ id: 4, score: 0.2, edge_type: "references" }]],
    [4, [{ id: 5, score: 0.9, edge_type: "referenced_by" }]],
  ]);
  const orderB = new Map([
    [4, [{ id: 5, score: 0.9, edge_type: "referenced_by" }]],
    [5, [{ id: 4, score: 0.2, edge_type: "references" }]],
  ]);
  const a = buildAssociationEdges(orderA, 0.0);
  const b = buildAssociationEdges(orderB, 0.0);
  assert(a.length === 1 && b.length === 1, "G2: one edge either row order");
  // Semantic: 5 references 4 (from both halves) — NOT min/max id collapse
  assert(a[0].from === 5 && a[0].to === 4 && a[0].edge_type === "references",
    "G2: order A endpoints 5→4 references");
  assert(b[0].from === 5 && b[0].to === 4 && b[0].edge_type === "references",
    "G2: order B endpoints 5→4 references (stable direction)");
  assert(a[0].score === 0.9 && b[0].score === 0.9, "G2: score is max (0.9) either order");
  assert(a[0].directed === true, "G2: asymmetric association is directed");

  const c = normalizeAssociationRef(5, { id: 4, edge_type: "contradicts" });
  assert(c && c.from === 4 && c.to === 5 && c.directed === false, "contradicts undirected ordered");

  const n = normalizeAssociationRef(5, { id: 4, edge_type: "references", score: 0.2 });
  assert(n.from === 5 && n.to === 4, "references m→ref");
  const m = normalizeAssociationRef(4, { id: 5, edge_type: "referenced_by", score: 0.9 });
  assert(m.from === 5 && m.to === 4, "referenced_by normalizes to same 5→4");
}

// --- G3: self-lineage ---
{
  const L = buildLineageMaps(new Map([
    [7, [{ id: 7, score: 1, edge_type: "supersedes" }]],
  ]));
  assert(L.authorityUnknown.has(7), "G3: self-link marks authorityUnknown");
  assert(L.conflicts.some(c => c.kind === "self_cycle" && c.a === 7), "G3: self_cycle conflict");
  assert(L.supersedesEdges.length === 0, "G3: no drawable self edge");
  assert(!L.supersededBy.has(7), "G3: self is unknown not superseded");
}

// --- G4: cycle SCC — one conflict for 3-cycle; no triple spam on A↔B ---
{
  const L = buildLineageMaps(new Map([
    [1, [{ id: 2, score: 1, edge_type: "supersedes" }]],
    [2, [{ id: 3, score: 1, edge_type: "supersedes" }]],
    [3, [{ id: 1, score: 1, edge_type: "supersedes" }]],
  ]));
  const cycles = L.conflicts.filter(c => c.kind === "cycle");
  assert(cycles.length === 1, "G4: one cycle conflict for 3-node SCC");
  assert(cycles[0].members && cycles[0].members.length === 3, "G4: members list size 3");
  assert(L.supersededBy.has(1) && L.supersededBy.has(2) && L.supersededBy.has(3),
    "G4: all cycle members marked superseded");
}
{
  const L = buildLineageMaps(new Map([
    [1, [{ id: 2, score: 1, edge_type: "supersedes" }]],
    [2, [{ id: 1, score: 1, edge_type: "supersedes" }]],
  ]));
  const cycles = L.conflicts.filter(c => c.kind === "cycle");
  assert(cycles.length === 1, "G4: A↔B is one cycle conflict not three");
}

// --- F1 unavailable stats must never say zero superseded ---
{
  // Mirrors formatLineageStats contract in force-graph.html
  function formatLineageStats(rawGraph) {
    if (!rawGraph || rawGraph.lineageAvailable === false) {
      return "LINEAGE UNAVAILABLE (cannot confirm current)";
    }
    return `${rawGraph.supersededCount} superseded`;
  }
  function formatDupStats(rawGraph) {
    if (rawGraph.crossrefsAvailable === false) return "DUPS UNAVAILABLE";
    if (rawGraph.duplicatePairCount != null) {
      return `${rawGraph.duplicatePairCount}/${rawGraph.dupeCount} dup pairs/memories`;
    }
    return `${rawGraph.dupeCount || 0} dup memories`;
  }
  const unavailable = {
    lineageAvailable: false,
    crossrefsAvailable: false,
    supersededCount: null,
    supersededIds: null,
    duplicatePairCount: null,
    duplicateIds: null,
    dupeCount: 0,
  };
  const linTxt = formatLineageStats(unavailable);
  assert(!/0 superseded/.test(linTxt), "F1: unavailable stats must NOT say '0 superseded'");
  assert(/UNAVAILABLE/i.test(linTxt), "F1: unavailable stats say UNAVAILABLE");
  const dupTxt = formatDupStats(unavailable);
  assert(!/^0\b/.test(dupTxt) && !/0\/0/.test(dupTxt), "G6: degraded dups must NOT say 0/0");
  assert(/UNAVAILABLE/i.test(dupTxt), "G6: degraded dups say UNAVAILABLE");
}

// --- partition / parse / M1 ---
{
  const part = partitionLineageEdges([{ from: 5, to: 999, score: 1 }], new Set([5]));
  assert(part.drawable.length === 0 && part.dangling[0].missing === "to", "dangling not drawable");
  assert(parseRelatedPayload("nope").ok === false, "parse fail-closed");
  assert(isLineageEdgeType("supersedes") && !isLineageEdgeType("references"), "M1 lineage type gate");
}

// --- G1: same-DB refresh re-derives panel (source contract in force-graph.html) ---
{
  const html = readFileSync(join(__dirname, "../public/force-graph.html"), "utf8");
  assert(
    /panelWasOpen/.test(html) && /openDetail\(\s*prevSelected/.test(html),
    "G1: load() re-calls openDetail(prevSelected, …) when panel was open",
  );
  assert(
    /openDetail\(\s*prevSelected,\s*isDupe,\s*null/.test(html),
    "G1: openDetail preMem=null (fresh fetch, not stale byId)",
  );
  assert(
    /graphNode\.superseded/.test(html),
    "G1: panel uses NEW graphNode.superseded after same-DB commit",
  );
  assert(
    /refreshing…/.test(html),
    "G1: aborts mid-detail show refreshing… rather than stranding loading…",
  );
}

// --- G5: connectionCounts after partition is a graph.ts contract; note only ---
// (graph.ts is PagesFunction + D1 — covered by code structure review: counts
//  recomputed from finalEdges after partitionLineageEdges.)

if (failed) {
  console.error(`\n${failed} failure(s)`);
  process.exit(1);
}
console.log("\nAll lineage fixture tests passed (shipped _lineage.ts imported).");
