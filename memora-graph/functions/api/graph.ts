/**
 * GET /api/graph - Returns graph nodes, edges, and metadata for visualization
 * Supports ?db=<configured name> to select a database
 */

import { resolveDatabase, selectionErrorResponse, type DatabaseEnv } from "./_db.ts";
import {
  applyRetirement,
  buildAssociationEdges,
  buildLineageMaps,
  classifyRetirementQueryError,
  parseRelatedPayload,
  partitionLineageEdges,
  type CrossRefEntry,
} from "./_lineage.ts";

interface Env extends DatabaseEnv {
  MIN_EDGE_SCORE?: string;
  /** Override the default (no-?limit) node cap; positive int or ignored. */
  GRAPH_DEFAULT_LIMIT?: string;
  /** Override the hard cap for explicit ?limit=; positive int or ignored. */
  GRAPH_LIMIT_MAX?: string;
}

interface Memory {
  id: number;
  content: string;
  metadata: string;
  tags: string;
  created_at: string;
  updated_at: string | null;
}

interface CrossRef {
  memory_id: number;
  related: string;
}

interface GraphNode {
  id: number;
  label: string;
  title: string;
  color: string | { background: string; border: string };
  size: number;
  mass: number;
  borderWidth?: number;
  shape?: string;
  frag?: boolean;
  /** True when another memory supersedes this one (not current). */
  superseded?: boolean;
  /** True when lineage could not be loaded — not "current", not "superseded". */
  authority_unknown?: boolean;
  /** Ids of memories that supersede this node (newer leaves). */
  superseded_by?: number[];
  /** Ids of older memories this node supersedes. */
  supersedes?: number[];
  /** True when this id is in a tombstoned/retired component. */
  retired?: boolean;
}

interface GraphEdge {
  id: number;
  from: number;
  to: number;
  /** Crossref edge type when known. Omitted/related_to = similarity/association. */
  edge_type?: string;
  /** Cosine or link score when available. */
  score?: number;
  /**
   * directed means source/target ORDER IS MEANINGFUL — not a lineage marker.
   * True for every semantically asymmetric relation (supersedes, references,
   * implements, extends, …). Lineage is edge_type === "supersedes", full stop.
   * Clients must not re-derive semantic direction after reverse-half normalisation.
   */
  directed?: boolean;
}

// Tag colors (purple palette)
const TAG_COLORS = [
  "#a855f7", "#c084fc", "#d8b4fe", "#9333ea",
  "#7c3aed", "#8b5cf6", "#a78bfa", "#c4b5fd"
];

// Status colors for issues
const ISSUE_STATUS_COLORS: Record<string, string> = {
  "open": "#ff7b72",
  "closed:complete": "#7ee787",
  "closed:not_planned": "#8b949e",
};

// Status colors for TODOs
const TODO_STATUS_COLORS: Record<string, string> = {
  "open": "#58a6ff",
  "closed:complete": "#7ee787",
  "closed:not_planned": "#8b949e",
};

const DUPLICATE_THRESHOLD = 0.85;

// Node cap for GET /api/graph. Without an explicit ?limit= the response is
// capped at DEFAULT_GRAPH_LIMIT so a large store can't produce an unbounded
// graph that stalls the force-directed renderer or the browser (thousands of
// labeled nodes degrade badly). An explicit ?limit= may raise this up to
// GRAPH_LIMIT_MAX; higher values clamp. Both sit far above the 13-node test
// fixture yet keep memory/time bounded.
const DEFAULT_GRAPH_LIMIT = 2000;
const GRAPH_LIMIT_MAX = 5000;

// Cluster colors (distinct from TAG_COLORS)
const CLUSTER_COLORS = [
  "#ff6b6b", "#ffd93d", "#6bcb77", "#4d96ff",
  "#ff922b", "#cc5de8", "#20c997", "#339af0",
  "#f06595", "#a9e34b", "#22b8cf", "#845ef7",
];

function louvainCommunities(
  adj: Map<number, Map<number, number>>,
  minCommunitySize: number = 3
): Map<number, number> {
  const nodeList = Array.from(adj.keys());
  if (nodeList.length === 0) return new Map();

  // Initialize: each node in its own community
  const community = new Map<number, number>();
  for (const n of nodeList) community.set(n, n);

  // Compute total weight
  let m2 = 0; // 2*m
  for (const [, neighbors] of adj) {
    for (const [, w] of neighbors) m2 += w;
  }
  if (m2 === 0) return community;

  // Node strengths (sum of weights)
  const strength = new Map<number, number>();
  for (const n of nodeList) {
    let s = 0;
    for (const [, w] of adj.get(n)!) s += w;
    strength.set(n, s);
  }

  // Phase 1: Local moves
  let improved = true;
  let iterations = 0;
  while (improved && iterations < 50) {
    improved = false;
    iterations++;

    for (const node of nodeList) {
      const currentComm = community.get(node)!;
      const ki = strength.get(node)!;

      // Sum of weights to each neighboring community
      const commWeights = new Map<number, number>();
      for (const [neighbor, w] of adj.get(node)!) {
        const nc = community.get(neighbor)!;
        commWeights.set(nc, (commWeights.get(nc) || 0) + w);
      }

      // Compute community totals
      const commTotals = new Map<number, number>();
      for (const n of nodeList) {
        const c = community.get(n)!;
        commTotals.set(c, (commTotals.get(c) || 0) + strength.get(n)!);
      }

      // Weight to own community (excluding self)
      const kiIn = commWeights.get(currentComm) || 0;
      const sigmaTot = commTotals.get(currentComm)! - ki;

      // Remove node from current community: compute loss
      const removeLoss = kiIn / m2 - (sigmaTot * ki) / (m2 * m2);

      let bestGain = 0;
      let bestComm = currentComm;

      for (const [targetComm, kiTarget] of commWeights) {
        if (targetComm === currentComm) continue;
        const sigmaTarget = commTotals.get(targetComm) || 0;
        const gain = kiTarget / m2 - (sigmaTarget * ki) / (m2 * m2) - removeLoss;
        if (gain > bestGain) {
          bestGain = gain;
          bestComm = targetComm;
        }
      }

      if (bestComm !== currentComm) {
        community.set(node, bestComm);
        improved = true;
      }
    }
  }

  // Renumber communities starting from 0
  const uniqueComms = [...new Set(community.values())];
  const commMap = new Map<number, number>();
  let idx = 0;
  for (const c of uniqueComms) {
    // Count members
    let count = 0;
    for (const [, v] of community) {
      if (v === c) count++;
    }
    if (count >= minCommunitySize) {
      commMap.set(c, idx++);
    }
  }

  const result = new Map<number, number>();
  for (const [node, comm] of community) {
    if (commMap.has(comm)) {
      result.set(node, commMap.get(comm)!);
    }
  }
  return result;
}

function buildClusterData(
  crossrefsMap: Map<number, Array<{ id: number; score?: number; edge_type?: string }>>,
  memoryIds: number[],
  minScore: number = 0.5,
  minClusterSize: number = 3
): {
  clusterToNodes: Record<string, number[]>;
  clusterColors: Record<string, string>;
  clusterMeta: Record<string, { size: number; label: string }>;
} {
  const empty = { clusterToNodes: {}, clusterColors: {}, clusterMeta: {} };
  if (memoryIds.length < minClusterSize) return empty;

  const idSet = new Set(memoryIds);

  // Build similarity graph from crossrefs (already computed)
  const adj = new Map<number, Map<number, number>>();
  for (const id of memoryIds) adj.set(id, new Map());

  for (const [memId, refs] of crossrefsMap) {
    if (!idSet.has(memId)) continue;
    for (const ref of refs) {
      const score = typeof ref.score === "number" ? ref.score : 0;
      if (score < minScore || !idSet.has(ref.id)) continue;
      adj.get(memId)?.set(ref.id, score);
      adj.get(ref.id)?.set(memId, score);
    }
  }

  // Run Louvain
  const communities = louvainCommunities(adj, minClusterSize);

  // Build cluster mappings
  const clusterToNodes: Record<string, number[]> = {};
  for (const [nodeId, clusterId] of communities) {
    const key = String(clusterId);
    if (!clusterToNodes[key]) clusterToNodes[key] = [];
    clusterToNodes[key].push(nodeId);
  }

  const clusterColors: Record<string, string> = {};
  const clusterMeta: Record<string, { size: number; label: string }> = {};
  const clusterIds = Object.keys(clusterToNodes);

  for (let i = 0; i < clusterIds.length; i++) {
    const cid = clusterIds[i];
    clusterColors[cid] = CLUSTER_COLORS[i % CLUSTER_COLORS.length];
    clusterMeta[cid] = {
      size: clusterToNodes[cid].length,
      label: `Cluster ${parseInt(cid) + 1}`,
    };
  }

  return { clusterToNodes, clusterColors, clusterMeta };
}

function parseJson<T>(str: string | null, defaultValue: T): T {
  if (!str) return defaultValue;
  try {
    return JSON.parse(str);
  } catch {
    return defaultValue;
  }
}

/** Parse a positive-int env override; returns the fallback when unset/invalid. */
function parsePositiveIntEnv(raw: string | undefined, fallback: number): number {
  if (raw === undefined) return fallback;
  const n = parseInt(raw, 10);
  return Number.isInteger(n) && n > 0 ? n : fallback;
}

function isSection(metadata: Record<string, unknown> | null): boolean {
  return metadata?.type === "section";
}

function isDocumentFragment(metadata: Record<string, unknown> | null): boolean {
  return metadata?.type === "document_fragment";
}

function isDocumentRoot(metadata: Record<string, unknown> | null): boolean {
  return metadata?.type === "document_root";
}

function isDuplicateExcluded(metadata: Record<string, unknown> | null): boolean {
  return isSection(metadata) || isDocumentFragment(metadata) || isDocumentRoot(metadata);
}

function isIssue(metadata: Record<string, unknown> | null): boolean {
  return metadata?.type === "issue";
}

function isTodo(metadata: Record<string, unknown> | null): boolean {
  return metadata?.type === "todo";
}

function getIssueStatus(metadata: Record<string, unknown>): string {
  const status = (metadata.status as string) || "open";
  if (status === "resolved") return "closed:complete";
  if (status === "wontfix") return "closed:not_planned";
  if (status === "in_progress") return "open";
  if (status === "closed") {
    const reason = (metadata.closed_reason as string) || "complete";
    return `closed:${reason}`;
  }
  return status;
}

function getTodoStatus(metadata: Record<string, unknown>): string {
  const status = (metadata.status as string) || "open";
  if (status === "completed") return "closed:complete";
  if (status === "blocked") return "closed:not_planned";
  if (status === "in_progress") return "open";
  if (status === "closed") {
    const reason = (metadata.closed_reason as string) || "complete";
    return `closed:${reason}`;
  }
  return status;
}

export const onRequestGet: PagesFunction<Env> = async ({ env, request }) => {
  const url = new URL(request.url);
  const dbName = url.searchParams.get("db");
  const includeDocs = url.searchParams.get("docs") === "1";   // include document fragments as nodes, linked to their doc root
  const selection = resolveDatabase(env, dbName);
  if (!selection.ok) return selectionErrorResponse(selection);
  const db = selection.binding;
  const minScore = parseFloat(env.MIN_EDGE_SCORE || "0.40");

  // Resolve the node caps. DEFAULT_GRAPH_LIMIT / GRAPH_LIMIT_MAX are the
  // production defaults; the corresponding env vars override them for tuning
  // and for the test harness (so the clamp is observable on small fixtures).
  const defaultLimit = parsePositiveIntEnv(env.GRAPH_DEFAULT_LIMIT, DEFAULT_GRAPH_LIMIT);
  const graphLimitMax = parsePositiveIntEnv(env.GRAPH_LIMIT_MAX, GRAPH_LIMIT_MAX);

  // ?limit= node cap. Absent => defaultLimit. Non-numeric or <= 0 => 400
  // invalid_limit. > graphLimitMax clamps to max.
  const limitRaw = url.searchParams.get("limit");
  let limit = defaultLimit;
  if (limitRaw !== null) {
    const trimmed = limitRaw.trim();
    if (!/^-?\d+$/.test(trimmed)) {
      return Response.json({ error: "invalid_limit" }, { status: 400 });
    }
    const parsed = parseInt(trimmed, 10);
    if (parsed <= 0) {
      return Response.json({ error: "invalid_limit" }, { status: 400 });
    }
    limit = Math.min(parsed, graphLimitMax);
  }

  // Fetch all memories
  const memoriesResult = await db.prepare(
    "SELECT id, content, metadata, tags, created_at, updated_at FROM memories"
  ).all<Memory>();

  if (!memoriesResult.results || memoriesResult.results.length === 0) {
    return Response.json({ error: "no_memories", message: "No memories to visualize" });
  }

  let memories = memoriesResult.results;

  // Fetch all crossrefs (table may not exist on some D1 databases).
  // Fail CLOSED for lineage: unknown must not present as "all current".
  const crossrefsMap = new Map<number, CrossRefEntry[]>();
  // G6: crossrefsAvailable covers lineage, dups, associations, clusters — same query.
  let crossrefsAvailable = true;
  let lineageAvailable = true;
  let lineageDegradedReason: string | null = null;
  const corruptCrossrefRows: number[] = [];
  try {
    const crossrefsResult = await db.prepare(
      "SELECT memory_id, related FROM memories_crossrefs"
    ).all<CrossRef>();

    for (const cr of crossrefsResult.results || []) {
      const parsed = parseRelatedPayload(cr.related);
      if (!parsed.ok) {
        // One corrupt row must not silently drop that memory's supersession evidence
        // and paint it as current with no banner (F1 second fail-open).
        lineageAvailable = false;
        crossrefsAvailable = false;
        lineageDegradedReason = lineageDegradedReason || `corrupt_crossref:${parsed.reason}`;
        corruptCrossrefRows.push(cr.memory_id);
        continue;
      }
      crossrefsMap.set(cr.memory_id, parsed.entries);
    }
  } catch {
    lineageAvailable = false;
    crossrefsAvailable = false;
    lineageDegradedReason = "crossrefs_query_failed";
  }

  // Lineage: normalize BOTH halves (supersedes + superseded_by) into canonical
  // newer→older, then dedupe. Associations use SEMANTIC endpoints (G2).
  const retiredIds = new Set<number>();
  let retirementAvailable = true;
  const ingestRetired = async (table: "tombstone_components" | "tombstones") => {
    try {
      const result = await db.prepare(
        `SELECT memory_id FROM ${table}`
      ).all<{ memory_id: number }>();
      for (const row of result.results || []) {
        if (typeof row.memory_id === "number") retiredIds.add(row.memory_id);
      }
    } catch (err) {
      if (classifyRetirementQueryError(err, table) === "absent") return;
      retirementAvailable = false;
      lineageAvailable = false;
      crossrefsAvailable = false;
      lineageDegradedReason = lineageDegradedReason || "retirement_query_failed";
    }
  };
  await ingestRetired("tombstone_components");
  await ingestRetired("tombstones");

  const lineage = lineageAvailable
    ? buildLineageMaps(crossrefsMap.entries())
    : {
        supersededBy: new Map<number, Set<number>>(),
        supersedesMap: new Map<number, Set<number>>(),
        supersedesEdges: [],
        conflicts: [],
        authorityUnknown: new Set<number>(),
      };
  applyRetirement(lineage, retiredIds);
  const supersededBy = lineage.supersededBy;
  const supersedesMap = lineage.supersedesMap;

  // Provisional edge lists — connectionCounts computed AFTER dangling partition (G5).
  const provisionalLineageEdges: GraphEdge[] = [];
  const provisionalAssocEdges: GraphEdge[] = [];
  let edgeId = 0;

  if (lineageAvailable) {
    for (const le of lineage.supersedesEdges) {
      provisionalLineageEdges.push({
        id: edgeId++,
        from: le.from,
        to: le.to,
        edge_type: "supersedes",
        score: le.score,
        directed: true,
      });
    }
  }
  // Associations only when crossrefs available (else we would claim exact link counts).
  if (crossrefsAvailable) {
    for (const ae of buildAssociationEdges(crossrefsMap.entries(), minScore)) {
      provisionalAssocEdges.push({
        id: edgeId++,
        from: ae.from,
        to: ae.to,
        edge_type: ae.edge_type,
        score: ae.score,
        directed: ae.directed,
      });
    }
  }

  // Placeholder counts — recomputed from final drawable edges after nodes exist (G5).
  const connectionCounts = new Map<number, number>();

  // Find duplicate memories from canonical duplicate pairs.
  // Filter rules must match find_duplicate_pairs() in storage.py
  // and the /api/duplicates endpoint:
  //   - exclude structural memories (section, document_fragment,
  //     document_root).
  //   - skip typed link entries (supersedes, references, extends, ...) —
  //     only for DUPLICATE detection, NOT for graph edge assembly.
  //     `related_to` is allowed because compute_crossrefs uses it as the
  //     default tag for score-based refs.
  //   - skip score >= 0.9999 — these are absorb's link_memories writes
  //     with hardcoded 1.0, not real cosine matches. Cosine of non-
  //     identical vectors is mathematically always < 1.0.
  const memoryIds = new Set(memories.filter(m => {
    const meta = parseJson<Record<string, unknown> | null>(m.metadata, null);
    return !isDuplicateExcluded(meta);
  }).map(m => m.id));
  const duplicateIds = new Set<number>();
  const duplicatePairKeys = new Set<string>();

  for (const m of memories) {
    const meta = parseJson<Record<string, unknown>>(m.metadata, {});
    if (isDuplicateExcluded(meta)) continue;

    const refs = crossrefsMap.get(m.id) || [];
    for (const ref of refs) {
      if (ref.edge_type !== undefined && ref.edge_type !== null && ref.edge_type !== "related_to") continue;
      if (typeof ref.score !== "number") continue;
      if (ref.score >= 0.9999) continue;
      if (ref.id === m.id) continue;
      if (ref.score >= DUPLICATE_THRESHOLD && memoryIds.has(ref.id)) {
        duplicatePairKeys.add([Math.min(m.id, ref.id), Math.max(m.id, ref.id)].join("-"));
        duplicateIds.add(m.id);
        duplicateIds.add(ref.id);
      }
    }
  }

  // Build tag colors
  const tagColors: Record<string, string> = {};
  for (const m of memories) {
    const tags = parseJson<string[]>(m.tags, []);
    const primaryTag = tags[0] || "untagged";
    if (!(primaryTag in tagColors)) {
      tagColors[primaryTag] = TAG_COLORS[Object.keys(tagColors).length % TAG_COLORS.length];
    }
  }

  // Build nodes
  // Map document_key -> document_root node id so fragments can link to their root.
  const rootByDocKey = new Map<string, number>();
  for (const m of memories) {
    const meta = parseJson<Record<string, unknown>>(m.metadata, {});
    if (isDocumentRoot(meta) && typeof meta.document_key === "string") rootByDocKey.set(meta.document_key, m.id);
  }

  // Node cap: deterministic "newest first" subset. Eligible = not section and
  // (not document-fragment unless ?docs=1), mirroring the node-building loop
  // below. Order: created_at DESC, id DESC (stable, so a later re-run of the
  // same limit returns the same set). All downstream steps
  // (duplicate/cluster/edges/mappings) run over the truncated `memories`, which
  // keeps edges and mappings closed over the included node set — no dangling
  // edges to excluded nodes.
  const eligibleMemories = memories.filter((m) => {
    const meta = parseJson<Record<string, unknown>>(m.metadata, {});
    if (isSection(meta)) return false;
    if (isDocumentFragment(meta) && !includeDocs) return false;
    return true;
  });
  const total = eligibleMemories.length;
  const truncated = total > limit;
  memories = eligibleMemories
    .slice()
    .sort((a, b) => {
      const ta = a.created_at || "";
      const tb = b.created_at || "";
      if (ta < tb) return 1;
      if (ta > tb) return -1;
      return b.id - a.id;
    })
    .slice(0, limit);

  const nodes: GraphNode[] = [];
  for (const m of memories) {
    const meta = parseJson<Record<string, unknown>>(m.metadata, {});

    // Skip sections always; include document fragments only when ?docs=1
    if (isSection(meta)) continue;
    const isFrag = isDocumentFragment(meta);
    if (isFrag && !includeDocs) continue;

    const tags = parseJson<string[]>(m.tags, []);
    const primaryTag = tags[0] || "untagged";
    const content = m.content;

    const firstLine = content.split("\n")[0].replace(/^#+\s*/, "").trim().slice(0, 60);
    const headline = firstLine.replace(/"/g, "'").replace(/\\/g, "");
    const label = content.slice(0, 35).replace(/[\n#*_`[\]]/g, " ").trim().replace(/"/g, "'").replace(/\\/g, "");

    // Calculate node size based on connections
    const connections = connectionCounts.get(m.id) || 0;
    const nodeSize = 12 + Math.min(28, Math.floor(Math.log1p(connections) * 8));
    const nodeMass = 0.5 + Math.min(2.5, Math.log1p(connections) * 0.8);

    // Build title with type indicator
    let typeLabel = "";
    if (isIssue(meta)) typeLabel = " - Issue";
    else if (isTodo(meta)) typeLabel = " - TODO";

    const isSuperseded = lineageAvailable && supersededBy.has(m.id);
    const isRetired = retiredIds.has(m.id);
    const authorityUnknown =
      !lineageAvailable || (lineageAvailable && lineage.authorityUnknown.has(m.id));
    const supersededByIds: number[] | undefined = isSuperseded
      ? Array.from(supersededBy.get(m.id) as Set<number>)
      : undefined;
    const supersedesIds: number[] | undefined =
      lineageAvailable && supersedesMap.has(m.id)
        ? Array.from(supersedesMap.get(m.id) as Set<number>)
        : undefined;
    const lineageLabel = authorityUnknown
      ? " - AUTHORITY UNKNOWN"
      : isSuperseded
        ? " - SUPERSEDED"
        : (supersedesIds && supersedesIds.length ? " - supersedes older" : "");

    const node: GraphNode = {
      id: m.id,
      label: label.length > 35 ? label + "..." : label,
      title: `#${m.id}${typeLabel}${lineageLabel}\n${headline}`,
      color: authorityUnknown ? "#484f58" : tagColors[primaryTag],
      size: isSuperseded || authorityUnknown ? Math.max(8, Math.floor(nodeSize * 0.7)) : nodeSize,
      mass: nodeMass,
      superseded: isSuperseded || undefined,
      authority_unknown: authorityUnknown || undefined,
      retired: isRetired || undefined,
      superseded_by: supersededByIds,
      supersedes: supersedesIds,
    };

    // Apply issue-specific styling
    if (isIssue(meta)) {
      const status = getIssueStatus(meta);
      node.shape = "dot";
      node.color = ISSUE_STATUS_COLORS[status] || ISSUE_STATUS_COLORS["open"];
      if (meta.severity === "critical") {
        node.borderWidth = 4;
      }
    }

    // Apply TODO-specific styling
    if (isTodo(meta)) {
      const status = getTodoStatus(meta);
      node.shape = "dot";
      node.color = TODO_STATUS_COLORS[status] || TODO_STATUS_COLORS["open"];
      if (meta.priority === "high") {
        node.borderWidth = 4;
      }
    }

    // Apply duplicate indicator
    if (duplicateIds.has(m.id)) {
      node.color = {
        background: typeof node.color === "string" ? node.color : "#a855f7",
        border: "#f85149",
      };
      node.borderWidth = 3;
    }

    // Document fragments: smaller distinct nodes (they cluster around their doc root)
    if (isFrag) {
      node.frag = true;
      node.shape = "dot";
      node.color = "#58a6ff";
      node.size = 9;
      node.mass = 0.4;
      const heading = typeof meta.section_heading === "string" ? meta.section_heading : "";
      const dk = typeof meta.document_key === "string" ? meta.document_key : "";
      node.title = `#${m.id} - fragment\n${heading || dk}`;
      node.label = "";
    }

    nodes.push(node);
  }

  // Document structural edges (always drawable among included nodes).
  const docEdges: GraphEdge[] = [];
  if (includeDocs) {
    const byDoc = new Map<string, Array<{ id: number; ord: number }>>();
    const docSeen = new Set<string>();
    for (const m of memories) {
      const meta = parseJson<Record<string, unknown>>(m.metadata, {});
      if (!isDocumentFragment(meta)) continue;
      const dk = typeof meta.document_key === "string" ? meta.document_key : "";
      if (!dk) continue;
      const ord = typeof meta.ordinal === "number" ? meta.ordinal : 0;
      const arr = byDoc.get(dk) || [];
      arr.push({ id: m.id, ord });
      byDoc.set(dk, arr);
    }
    const addEdge = (a: number, b: number) => {
      if (a === b) return;
      const k = `doc-${Math.min(a, b)}-${Math.max(a, b)}`;
      if (!docSeen.has(k)) {
        docSeen.add(k);
        docEdges.push({
          id: edgeId++,
          from: a,
          to: b,
          edge_type: "document",
          directed: false,
        });
      }
    };
    byDoc.forEach((frags, dk) => {
      frags.sort((a, b) => a.ord - b.ord);
      const rootId = rootByDocKey.get(dk);
      if (rootId !== undefined) frags.forEach(f => addEdge(f.id, rootId));
      else for (let i = 1; i < frags.length; i++) addEdge(frags[i - 1].id, frags[i].id);
    });
  }

  // Build mappings
  const tagToNodes: Record<string, number[]> = {};
  const sectionToNodes: Record<string, number[]> = {};
  const subsectionToNodes: Record<string, number[]> = {};
  const statusToNodes: Record<string, number[]> = {};
  const issueCategoryToNodes: Record<string, number[]> = {};
  const todoStatusToNodes: Record<string, number[]> = {};
  const todoCategoryToNodes: Record<string, number[]> = {};
  const nodeTimestamps: Record<number, string> = {};

  let minDate = "";
  let maxDate = "";
  const dates: string[] = [];

  for (const m of memories) {
    const meta = parseJson<Record<string, unknown>>(m.metadata, {});
    const tags = parseJson<string[]>(m.tags, []);

    // Skip sections and document fragments for mappings
    if (isSection(meta) || isDocumentFragment(meta)) continue;

    // Tags mapping
    for (const tag of tags) {
      if (!tagToNodes[tag]) tagToNodes[tag] = [];
      tagToNodes[tag].push(m.id);
    }

    // Issue mappings
    if (isIssue(meta)) {
      const status = getIssueStatus(meta);
      if (!statusToNodes[status]) statusToNodes[status] = [];
      statusToNodes[status].push(m.id);

      const component = (meta.component as string) || "uncategorized";
      if (!issueCategoryToNodes[component]) issueCategoryToNodes[component] = [];
      issueCategoryToNodes[component].push(m.id);
    }

    // TODO mappings
    if (isTodo(meta)) {
      const status = getTodoStatus(meta);
      if (!todoStatusToNodes[status]) todoStatusToNodes[status] = [];
      todoStatusToNodes[status].push(m.id);

      const category = (meta.category as string) || "uncategorized";
      if (!todoCategoryToNodes[category]) todoCategoryToNodes[category] = [];
      todoCategoryToNodes[category].push(m.id);
    }

    // Section mappings (skip issues and TODOs)
    if (!isIssue(meta) && !isTodo(meta)) {
      const hierarchy = meta.hierarchy as { path?: string[] } | undefined;
      let section = "Uncategorized";
      let parts: string[] = [];

      if (hierarchy?.path?.length) {
        section = hierarchy.path[0];
        parts = hierarchy.path.slice(1);
      } else {
        section = (meta.section as string) || "Uncategorized";
        const subsection = meta.subsection as string;
        if (subsection) parts = subsection.split("/");
      }

      if (!sectionToNodes[section]) sectionToNodes[section] = [];
      sectionToNodes[section].push(m.id);

      if (parts.length) {
        for (let i = 0; i < parts.length; i++) {
          const partialPath = parts.slice(0, i + 1).join("/");
          const fullKey = `${section}/${partialPath}`;
          if (!subsectionToNodes[fullKey]) subsectionToNodes[fullKey] = [];
          subsectionToNodes[fullKey].push(m.id);
        }
      }
    }

    // Timeline data
    if (m.created_at) {
      nodeTimestamps[m.id] = m.created_at;
      dates.push(m.created_at);
    }
  }

  if (dates.length) {
    dates.sort();
    minDate = dates[0];
    maxDate = dates[dates.length - 1];
  }

  // Build cluster data using Louvain on crossrefs
  const nodeIds = nodes.map(n => n.id);
  const nodeIdSet = new Set(nodeIds);
  const clusterData = buildClusterData(crossrefsMap, nodeIds, 0.4, 3);

  // F4: only emit drawable lineage edges when both ends exist as nodes.
  // Older nodes whose superseder is missing stay superseded (conservative).
  const { drawable: drawableLineage, dangling } = partitionLineageEdges(
    lineage.supersedesEdges,
    nodeIdSet,
  );
  // Associations: only if both ends are present nodes.
  const drawableAssoc = provisionalAssocEdges.filter(
    e => nodeIdSet.has(e.from) && nodeIdSet.has(e.to),
  );
  const drawableDocs = docEdges.filter(
    e => nodeIdSet.has(e.from) && nodeIdSet.has(e.to),
  );

  const finalEdges: GraphEdge[] = [
    ...drawableLineage.map((le, i) => ({
      id: i,
      from: le.from,
      to: le.to,
      edge_type: "supersedes" as const,
      score: le.score,
      directed: true as const,
    })),
    ...drawableAssoc.map((e, i) => ({
      ...e,
      id: drawableLineage.length + i,
    })),
    ...drawableDocs.map((e, i) => ({
      ...e,
      id: drawableLineage.length + drawableAssoc.length + i,
    })),
  ];

  // G5: node size/mass from FINAL drawable edges only (not dangling).
  connectionCounts.clear();
  for (const edge of finalEdges) {
    connectionCounts.set(edge.from, (connectionCounts.get(edge.from) || 0) + 1);
    connectionCounts.set(edge.to, (connectionCounts.get(edge.to) || 0) + 1);
  }
  for (const n of nodes) {
    const connections = connectionCounts.get(n.id) || 0;
    const nodeSize = 12 + Math.min(28, Math.floor(Math.log1p(connections) * 8));
    const nodeMass = 0.5 + Math.min(2.5, Math.log1p(connections) * 0.8);
    if (n.superseded || n.authority_unknown) {
      n.size = Math.max(8, Math.floor(nodeSize * 0.7));
    } else if (!n.frag) {
      n.size = nodeSize;
    }
    if (!n.frag) n.mass = nodeMass;
  }

  // O(S) with Set — not O(S×N) linear scan per key.
  // When lineage is unavailable, do NOT emit empty supersededIds/count (clients
  // must not infer "zero superseded" from absence — F1).
  const supersededIds = lineageAvailable
    ? Array.from(supersededBy.keys()).filter(id => nodeIdSet.has(id))
    : null;

  // G6: null exact counts when crossrefs degraded.
  const dupIdsOut = crossrefsAvailable ? Array.from(duplicateIds) : null;
  const dupPairOut = crossrefsAvailable ? duplicatePairKeys.size : null;

  return Response.json({
    nodes,
    edges: finalEdges,
    tagColors,
    tagToNodes,
    sectionToNodes,
    subsectionToNodes,
    statusToNodes,
    issueCategoryToNodes,
    todoStatusToNodes,
    todoCategoryToNodes,
    duplicateIds: dupIdsOut,
    duplicatePairCount: dupPairOut,
    /** False when crossrefs query/parse failed — dups/associations/clusters also untrustworthy. */
    crossrefsAvailable,
    /** False when lineage cannot certify currentness (subset of crossrefs failure or corrupt rows). */
    lineageAvailable,
    lineageDegradedReason,
    corruptCrossrefRows: corruptCrossrefRows.length ? corruptCrossrefRows : undefined,
    /** null when unavailable — never [] that reads as "zero superseded". */
    supersededIds,
    supersededCount: lineageAvailable ? (supersededIds as number[]).length : null,
    retiredIds: Array.from(retiredIds),
    /** Directed lineage edges only (from=newer, to=older), both ends present. */
    supersedesEdges: lineageAvailable
      ? drawableLineage.map(e => ({ from: e.from, to: e.to, score: e.score }))
      : null,
    lineageDangling: lineageAvailable && dangling.length ? dangling : undefined,
    lineageConflicts: lineageAvailable && lineage.conflicts.length ? lineage.conflicts : undefined,
    nodeTimestamps,
    minDate,
    maxDate,
    /** True when ?limit=N truncated the graph; false when unbounded/fully included. */
    truncated,
    /** Renderable nodes in the full (unbounded) graph, so clients can gauge how much ?limit dropped. */
    total,
    clusterToNodes: crossrefsAvailable ? clusterData.clusterToNodes : {},
    clusterColors: crossrefsAvailable ? clusterData.clusterColors : {},
    clusterMeta: crossrefsAvailable ? clusterData.clusterMeta : {},
  });
};
