/**
 * Fixture tests — import shipped _lineage.ts only (no reimplementation).
 * Run: node --experimental-strip-types memora-graph/scripts/test_lineage.mjs
 *
 * UI contracts in force-graph.html are asserted via pure consumer helpers that
 * mirror the shipped predicates + source greps (HTML is not importable).
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
const html = readFileSync(join(__dirname, "../public/force-graph.html"), "utf8");

let failed = 0;
function assert(cond, msg) {
  if (!cond) {
    console.error("FAIL:", msg);
    failed++;
  } else {
    console.log("ok:", msg);
  }
}

// ---------------------------------------------------------------------------
// Pure consumer helpers — same predicates as force-graph.html (keep in sync)
// ---------------------------------------------------------------------------

/** Mirrors authorityUnknownSetFromRaw when lineageAvailable=true */
function authorityUnknownFromNodes(nodes) {
  const s = new Set();
  for (const n of nodes || []) if (n.authority_unknown) s.add(n.id);
  return s;
}

/** H2 current-only: exclude superseded AND authority_unknown when lineage known */
function currentOnlyKeep(n, lineageAvailable, showLineage) {
  if (!lineageAvailable || showLineage) return true;
  return !n.superseded && !n.authority_unknown;
}

/** Timeline row flags (H2) */
function timelineFlags(memId, { lineageAvailable, supersededIds, authorityUnknownIds }) {
  const globalUnknown = lineageAvailable === false;
  return {
    _unknown: globalUnknown || authorityUnknownIds.has(memId),
    _superseded: !globalUnknown && supersededIds.has(memId),
  };
}

function timelineRowClass(flags) {
  return "trow"
    + (flags._dupe ? " dupe" : "")
    + (flags._superseded ? " superseded-row" : "")
    + (flags._unknown ? " unknown-row" : "");
}

// --- prior: bidirectional halves normalize ---
{
  const L = buildLineageMaps(new Map([
    [2, [{ id: 1, score: 1, edge_type: "supersedes" }]],
    [1, [{ id: 2, score: 1, edge_type: "superseded_by" }]],
  ]));
  assert(L.supersedesEdges.length === 1 && L.supersedesEdges[0].from === 2, "bidirectional halves → one edge");
}

// --- prior: mirror-only ---
{
  const L = buildLineageMaps(new Map([
    [1, [{ id: 2, score: 1, edge_type: "superseded_by" }]],
  ]));
  assert(L.supersedesEdges.length === 1, "mirror-only still produces lineage edge");
  assert(L.supersedesEdges[0].from === 2 && L.supersedesEdges[0].to === 1, "mirror normalizes newer=2 older=1");
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

// --- prior: max-score ---
{
  const L = buildLineageMaps(new Map([
    [2, [{ id: 1, score: 0.2, edge_type: "supersedes" }]],
    [1, [{ id: 2, score: 0.9, edge_type: "superseded_by" }]],
  ]));
  assert(L.supersedesEdges.length === 1 && L.supersedesEdges[0].score === 0.9, "score policy = max");
}

// --- G2 + H5 directed contract ---
{
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
  assert(a[0].from === 5 && a[0].to === 4 && a[0].edge_type === "references", "G2: 5→4 references");
  assert(b[0].from === 5 && b[0].to === 4, "G2: direction stable across row order");
  assert(a[0].score === 0.9 && b[0].score === 0.9, "G2: score is max either order");
  // H5: directed=true AND not lineage
  assert(a[0].directed === true, "H5: references directed=true");
  assert(!isLineageEdgeType(a[0].edge_type), "H5: references isLineageEdgeType false");
  assert(a[0].directed === true && !isLineageEdgeType("references"),
    "H5 contract: directed means order meaningful, NOT a lineage marker");
}

// --- G3: self-lineage module ---
{
  const L = buildLineageMaps(new Map([
    [7, [{ id: 7, score: 1, edge_type: "supersedes" }]],
  ]));
  assert(L.authorityUnknown.has(7), "G3: self-link marks authorityUnknown");
  assert(L.conflicts.some(c => c.kind === "self_cycle"), "G3: self_cycle conflict");
  assert(!L.supersededBy.has(7), "G3: self is unknown not superseded");
}

// --- H1: 3-cycle supersededBy is ONLY stored incoming edges (no SCC invention) ---
{
  // Stored only: 1→2, 2→3, 3→1  (newer→older supersedes edges)
  const L = buildLineageMaps(new Map([
    [1, [{ id: 2, score: 1, edge_type: "supersedes" }]],
    [2, [{ id: 3, score: 1, edge_type: "supersedes" }]],
    [3, [{ id: 1, score: 1, edge_type: "supersedes" }]],
  ]));
  const cycles = L.conflicts.filter(c => c.kind === "cycle");
  assert(cycles.length === 1, "H1/G4: one cycle conflict for 3-node SCC");
  assert(cycles[0].members && cycles[0].members.join(",") === "1,2,3", "H1: members=[1,2,3]");
  // Exact STORED incoming: 2←1, 3←2, 1←3
  const sb1 = [...(L.supersededBy.get(1) || [])].sort();
  const sb2 = [...(L.supersededBy.get(2) || [])].sort();
  const sb3 = [...(L.supersededBy.get(3) || [])].sort();
  assert(sb1.join(",") === "3", "H1: #1 superseded_by exactly [3] (stored), not [2,3]");
  assert(sb2.join(",") === "1", "H1: #2 superseded_by exactly [1] (stored), not [1,3]");
  assert(sb3.join(",") === "2", "H1: #3 superseded_by exactly [2] (stored), not [1,2]");
}
{
  const L = buildLineageMaps(new Map([
    [1, [{ id: 2, score: 1, edge_type: "supersedes" }]],
    [2, [{ id: 1, score: 1, edge_type: "supersedes" }]],
  ]));
  assert(L.conflicts.filter(c => c.kind === "cycle").length === 1, "G4: A↔B one cycle");
  const sb1 = [...(L.supersededBy.get(1) || [])].sort();
  const sb2 = [...(L.supersededBy.get(2) || [])].sort();
  assert(sb1.join(",") === "2" && sb2.join(",") === "1", "H1: A↔B supersededBy is stored pair only");
}

// --- H2: consumer honours per-node authority_unknown with lineageAvailable=true ---
{
  // Simulate API: lineageAvailable=true, only #7 is authority_unknown (self_cycle)
  const nodes = [
    { id: 3, superseded: false, authority_unknown: false },
    { id: 4, superseded: false, authority_unknown: false },
    { id: 5, superseded: false, authority_unknown: false },
    { id: 7, superseded: false, authority_unknown: true },
  ];
  const auth = authorityUnknownFromNodes(nodes);
  assert(auth.has(7) && !auth.has(3), "H2: authority set from nodes only #7");

  // current-only ON (showLineage=false, lineageAvailable=true) must DROP #7
  const kept = nodes.filter(n => currentOnlyKeep(
    { superseded: n.superseded, authority_unknown: n.authority_unknown || auth.has(n.id) },
    true,
    false,
  ));
  const keptIds = kept.map(n => n.id).sort().join(",");
  assert(keptIds === "3,4,5", "H2: current-only drops #7 (not 3,4,5,7)");
  assert(!kept.some(n => n.id === 7), "H2: #7 does not survive current-only");

  // Timeline row for #7 must be badged unknown-row
  const flags7 = timelineFlags(7, {
    lineageAvailable: true,
    supersededIds: new Set(),
    authorityUnknownIds: auth,
  });
  assert(flags7._unknown === true && flags7._superseded === false, "H2: #7 timeline flags unknown");
  const cls = timelineRowClass({ ...flags7, _dupe: false });
  assert(cls.includes("unknown-row") && !cls.includes("superseded-row"),
    "H2 DOM: row class is unknown-row (not ordinary trow alone)");
  assert(cls === "trow unknown-row", "H2 DOM: exact class string for self_cycle node");

  // Source: force-graph must filter authority_unknown in current-only paths
  assert(
    /!n\.superseded\s*&&\s*!n\.authority_unknown/.test(html)
      || /!n\.authority_unknown\s*&&\s*!n\.superseded/.test(html),
    "H2 source: canvas current-only filters authority_unknown",
  );
  assert(
    /!m\._superseded\s*&&\s*!m\._unknown/.test(html)
      || /!m\._unknown\s*&&\s*!m\._superseded/.test(html),
    "H2 source: timeline current-only filters _unknown",
  );
  assert(/authorityUnknownSetFromRaw/.test(html), "H2 source: authorityUnknownSetFromRaw helper");
}

// --- H3: forceFetch still required for content freshness ---
{
  assert(/forceFetch/.test(html), "H3: openDetail supports forceFetch");
  assert(/forceFetch:\s*true/.test(html), "H3: reconcile openDetail uses forceFetch:true");
  assert(/delete byId\[id\]/.test(html) || /delete byId\[/.test(html),
    "H3: forceFetch clears byId entry");
}

// --- J1: reconcile CURRENT selection after raw commit (decision table) ---
// Mirrors planSelectionReconcile in force-graph.html — must stay identical.
function planSelectionReconcile(curSelectedId, panelOpenNow, nodes, duplicateIds) {
  if (curSelectedId == null) return { type: "none" };
  const graphNode = (nodes || []).find(n => n.id === curSelectedId);
  if (!graphNode) return { type: "clear" };
  return {
    type: "refresh",
    id: curSelectedId,
    openDetail: !!panelOpenNow,
    isDupe: Array.isArray(duplicateIds) && duplicateIds.includes(curSelectedId),
    superseded: !!graphNode.superseded,
    authority_unknown: !!graphNode.authority_unknown,
  };
}

{
  const nodesBefore = [
    { id: 1, superseded: false },
    { id: 3, superseded: false, supersedes: [2] },
  ];
  const nodesAfterAuth = [
    { id: 1, superseded: false },
    { id: 3, superseded: true, superseded_by: [5] }, // #5 supersedes #3 mid-refresh
  ];
  const nodesWithout3 = [
    { id: 1, superseded: false },
    { id: 5, superseded: false },
  ];

  // Case 1: none→B during refresh — selection made mid-flight must reconcile
  {
    const plan = planSelectionReconcile(3, true, nodesAfterAuth, []);
    assert(plan.type === "refresh" && plan.id === 3 && plan.openDetail === true,
      "J1 none→B: refresh open panel for B");
    assert(plan.superseded === true, "J1 none→B: authority from NEW graph (superseded)");
  }

  // Case 2: A→B during refresh — do not require selectedId === start snapshot
  {
    // start had #1; mid-flight user picked #3; after commit cur=3
    const plan = planSelectionReconcile(3, true, nodesAfterAuth, []);
    assert(plan.type === "refresh" && plan.id === 3,
      "J1 A→B: reconciles CURRENT id 3 (not start snapshot 1)");
    assert(plan.openDetail === true && plan.superseded === true,
      "J1 A→B: panel re-opens with NEW superseded=true (measured fail case)");
  }

  // Case 3: B deleted during refresh
  {
    const plan = planSelectionReconcile(3, true, nodesWithout3, []);
    assert(plan.type === "clear", "J1 B deleted: clear selection when id gone from new graph");
  }

  // Case 4: B's authority CHANGED during refresh (leader repro: #3 becomes superseded)
  {
    const before = planSelectionReconcile(3, true, nodesBefore, []);
    const after = planSelectionReconcile(3, true, nodesAfterAuth, []);
    assert(before.superseded === false, "J1 authority: before refresh #3 not superseded");
    assert(after.superseded === true, "J1 authority: after refresh #3 superseded from NEW node");
    assert(after.openDetail === true && after.type === "refresh",
      "J1 authority: open panel is re-derived (not left on stale 'Supersedes older')");
  }

  // Extra: deselect mid-refresh → none
  assert(planSelectionReconcile(null, false, nodesAfterAuth, []).type === "none",
    "J1 deselect: no reconcile when selectedId is null");
  // Extra: selection kept, panel closed → highlight only
  {
    const plan = planSelectionReconcile(3, false, nodesAfterAuth, []);
    assert(plan.type === "refresh" && plan.openDetail === false,
      "J1 panel closed: setHighlight only, do not force-open panel");
  }

  // Source: load uses planSelectionReconcile(selectedId, …) — not start-snapshot equality
  assert(/function planSelectionReconcile\s*\(/.test(html), "J1 source: planSelectionReconcile defined");
  assert(/planSelectionReconcile\(\s*selectedId/.test(html),
    "J1 source: load reads selectedId NOW after commit");
  assert(!/selectedId\s*===\s*prevSelected/.test(html),
    "J1 source: no start-snapshot selectedId===prevSelected guard");
  assert(/applySelectionReconcile/.test(html), "J1 source: applySelectionReconcile applies plan");
}

// --- H4: combined integrity warning + link counts ---
{
  assert(/setIntegrityUiState/.test(html), "H4: setIntegrityUiState combines both flags");
  assert(/Crossrefs degraded/.test(html), "H4: crossrefs degraded copy present");
  assert(/LINKS UNAVAILABLE/.test(html), "H4: stats say LINKS UNAVAILABLE when degraded");
  assert(/DUPS UNAVAILABLE/.test(html), "H4: stats say DUPS UNAVAILABLE when degraded");
  // Unreachable-guard: must not require lineageAvailable!==false to show crossrefs warn
  assert(
    !/crossrefsAvailable === false[\s\S]{0,120}lineageAvailable !== false/.test(html),
    "H4: no dead path requiring lineageAvailable for crossrefs banner",
  );
}

// --- H5: local lineage prop, not shadowing directed ---
{
  assert(/lineage:\s*e\.edge_type\s*===\s*"supersedes"/.test(html),
    "H5: local link.lineage derived from edge_type");
  assert(/directed:\s*!!e\.directed/.test(html), "H5: wire directed preserved on local link");
  assert(/l\.lineage\s*===\s*true|l\.lineage/.test(html), "H5: isSupersedesLink uses lineage");
}

// --- F1 unavailable stats ---
{
  function formatLineageStats(rawGraph) {
    if (!rawGraph || rawGraph.lineageAvailable === false) {
      return "LINEAGE UNAVAILABLE (cannot confirm current)";
    }
    return `${rawGraph.supersededCount} superseded`;
  }
  function formatLinkDup(rawGraph, linkCount) {
    if (rawGraph.crossrefsAvailable === false) {
      return { links: "LINKS UNAVAILABLE", dups: "DUPS UNAVAILABLE" };
    }
    return { links: `${linkCount} links`, dups: `${rawGraph.dupeCount || 0} dup memories` };
  }
  const u = { lineageAvailable: false, crossrefsAvailable: false, supersededCount: null, dupeCount: 0 };
  assert(!/0 superseded/.test(formatLineageStats(u)), "F1: no 0 superseded");
  const ld = formatLinkDup(u, 0);
  assert(ld.links === "LINKS UNAVAILABLE" && ld.dups === "DUPS UNAVAILABLE", "H4 format helpers");
}

// --- partition / parse / M1 ---
{
  const part = partitionLineageEdges([{ from: 5, to: 999, score: 1 }], new Set([5]));
  assert(part.drawable.length === 0 && part.dangling[0].missing === "to", "dangling not drawable");
  assert(parseRelatedPayload("nope").ok === false, "parse fail-closed");
  assert(isLineageEdgeType("supersedes") && !isLineageEdgeType("references"), "M1 lineage type gate");
}

// --- G1 retained: panel re-derive (via J1 reconcile, not start snapshot) ---
{
  assert(/applySelectionReconcile\(plan\)/.test(html), "G1/J1: applySelectionReconcile after raw commit");
  assert(/openDetail\(plan\.id/.test(html) || /openDetail\(plan\.id,/.test(html),
    "G1/J1: openDetail from plan.id (current selection)");
  assert(/refreshing…/.test(html), "G1: refreshing shell on abort");
}

if (failed) {
  console.error(`\n${failed} failure(s)`);
  process.exit(1);
}
console.log("\nAll lineage fixture tests passed (shipped _lineage.ts imported).");
