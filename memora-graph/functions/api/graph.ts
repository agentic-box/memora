/**
 * GET /api/graph - Returns graph nodes, edges, and metadata for visualization
 * Supports ?db=memora or ?db=ob1 parameter to select database
 */

interface Env {
  DB_MEMORA: D1Database;
  DB_OB1: D1Database;
  MIN_EDGE_SCORE?: string;
  DEFAULT_DB?: string;
  DB_CONFIG?: string;
}

function getDatabase(env: Env, dbName: string | null): D1Database {
  const name = dbName || env.DEFAULT_DB || "memora";
  if (name === "ob1") return env.DB_OB1;
  return env.DB_MEMORA;
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
}

interface GraphEdge {
  id: number;
  from: number;
  to: number;
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
  crossrefsMap: Map<number, Array<{ id: number; score: number }>>,
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
      if (ref.score < minScore || !idSet.has(ref.id)) continue;
      adj.get(memId)?.set(ref.id, ref.score);
      adj.get(ref.id)?.set(memId, ref.score);
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
  const db = getDatabase(env, dbName);
  const minScore = parseFloat(env.MIN_EDGE_SCORE || "0.40");

  // Fetch all memories
  const memoriesResult = await db.prepare(
    "SELECT id, content, metadata, tags, created_at, updated_at FROM memories"
  ).all<Memory>();

  if (!memoriesResult.results || memoriesResult.results.length === 0) {
    return Response.json({ error: "no_memories", message: "No memories to visualize" });
  }

  const memories = memoriesResult.results;

  // Fetch all crossrefs (table may not exist on some D1 databases)
  const crossrefsMap = new Map<number, Array<{ id: number; score: number; edge_type?: string }>>();
  try {
    const crossrefsResult = await db.prepare(
      "SELECT memory_id, related FROM memories_crossrefs"
    ).all<CrossRef>();

    for (const cr of crossrefsResult.results || []) {
      const related = parseJson<Array<{ id: number; score: number; edge_type?: string }>>(cr.related, []);
      crossrefsMap.set(cr.memory_id, related);
    }
  } catch {
    // Table doesn't exist yet — proceed without crossrefs
  }

  // Build edges
  const edges: GraphEdge[] = [];
  const seen = new Set<string>();
  let edgeId = 0;

  for (const m of memories) {
    const refs = crossrefsMap.get(m.id) || [];
    for (const ref of refs) {
      if (ref.score <= minScore) continue;
      const edgeKey = [Math.min(m.id, ref.id), Math.max(m.id, ref.id)].join("-");
      if (!seen.has(edgeKey)) {
        seen.add(edgeKey);
        edges.push({ id: edgeId++, from: m.id, to: ref.id });
      }
    }
  }

  // Count connections per node
  const connectionCounts = new Map<number, number>();
  for (const edge of edges) {
    connectionCounts.set(edge.from, (connectionCounts.get(edge.from) || 0) + 1);
    connectionCounts.set(edge.to, (connectionCounts.get(edge.to) || 0) + 1);
  }

  // Find duplicate memories from canonical duplicate pairs.
  // Filter rules must match find_duplicate_pairs() in storage.py
  // and the /api/duplicates endpoint:
  //   - exclude structural memories (section, document_fragment,
  //     document_root).
  //   - skip typed link entries (supersedes, references, extends, ...) —
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
  const nodes: GraphNode[] = [];
  for (const m of memories) {
    const meta = parseJson<Record<string, unknown>>(m.metadata, {});

    // Skip section memories and document fragments
    if (isSection(meta) || isDocumentFragment(meta)) continue;

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

    const node: GraphNode = {
      id: m.id,
      label: label.length > 35 ? label + "..." : label,
      title: `#${m.id}${typeLabel}\n${headline}`,
      color: tagColors[primaryTag],
      size: nodeSize,
      mass: nodeMass,
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

    nodes.push(node);
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
  const clusterData = buildClusterData(crossrefsMap, nodeIds, 0.4, 3);

  return Response.json({
    nodes,
    edges,
    tagColors,
    tagToNodes,
    sectionToNodes,
    subsectionToNodes,
    statusToNodes,
    issueCategoryToNodes,
    todoStatusToNodes,
    todoCategoryToNodes,
    duplicateIds: Array.from(duplicateIds),
    duplicatePairCount: duplicatePairKeys.size,
    nodeTimestamps,
    minDate,
    maxDate,
    clusterToNodes: clusterData.clusterToNodes,
    clusterColors: clusterData.clusterColors,
    clusterMeta: clusterData.clusterMeta,
  });
};
