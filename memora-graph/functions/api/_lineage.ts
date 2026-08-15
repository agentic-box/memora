/**
 * Pure lineage + association assembly for /api/graph.
 * Used by graph.ts and fixture tests (must import this module — no copies).
 */

export type CrossRefEntry = { id: number; score?: number; edge_type?: string };

export type LineageEdge = {
  from: number; // newer
  to: number; // older
  score: number;
};

export type LineageConflict = {
  a: number;
  b: number;
  kind: "cycle" | "score_mismatch" | "self_cycle";
  scores: number[];
  /** For cycle SCCs: all member node ids */
  members?: number[];
};

export type LineageMaps = {
  supersededBy: Map<number, Set<number>>;
  supersedesMap: Map<number, Set<number>>;
  supersedesEdges: LineageEdge[];
  conflicts: LineageConflict[];
  /** Nodes that must not render as current (self-links, etc.) */
  authorityUnknown: Set<number>;
};

export type AssociationEdge = {
  from: number;
  to: number;
  edge_type: string;
  score: number;
  /**
   * directed means source/target ORDER IS MEANINGFUL (asymmetric relation).
   * It is NOT a lineage marker. Lineage is edge_type === "supersedes", full stop.
   * True for references/implements/extends; false for related_to/contradicts.
   */
  directed: boolean;
};

/** Parse one memories_crossrefs.related payload. Fail closed on malformed. */
export function parseRelatedPayload(
  raw: string | null | undefined,
): { ok: true; entries: CrossRefEntry[] } | { ok: false; reason: string } {
  if (raw == null || raw === "") return { ok: true, entries: [] };
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return { ok: false, reason: "invalid_json" };
  }
  if (!Array.isArray(parsed)) return { ok: false, reason: "not_array" };
  const entries: CrossRefEntry[] = [];
  for (const item of parsed) {
    if (!item || typeof item !== "object") {
      return { ok: false, reason: "entry_not_object" };
    }
    const id = (item as { id?: unknown }).id;
    if (typeof id !== "number" || !Number.isFinite(id)) {
      return { ok: false, reason: "entry_bad_id" };
    }
    const scoreRaw = (item as { score?: unknown }).score;
    const score = typeof scoreRaw === "number" && Number.isFinite(scoreRaw) ? scoreRaw : undefined;
    const edgeTypeRaw = (item as { edge_type?: unknown }).edge_type;
    const edge_type = typeof edgeTypeRaw === "string" ? edgeTypeRaw : undefined;
    entries.push({ id, score, edge_type });
  }
  return { ok: true, entries };
}

function addPair(
  maps: LineageMaps,
  newer: number,
  older: number,
  score: number,
  edgeScores: Map<string, number>,
): void {
  if (newer === older) {
    // Handled at call site as self_cycle; belt-and-suspenders.
    maps.authorityUnknown.add(newer);
    return;
  }
  if (!maps.supersedesMap.has(newer)) maps.supersedesMap.set(newer, new Set());
  maps.supersedesMap.get(newer)!.add(older);
  if (!maps.supersededBy.has(older)) maps.supersededBy.set(older, new Set());
  maps.supersededBy.get(older)!.add(newer);

  const key = `${newer}->${older}`;
  if (edgeScores.has(key)) {
    const prev = edgeScores.get(key)!;
    if (prev !== score) {
      // Defer score_mismatch recording until finalize (dedupe keys).
      const max = Math.max(prev, score);
      edgeScores.set(key, max);
      const edge = maps.supersedesEdges.find(e => e.from === newer && e.to === older);
      if (edge) edge.score = max;
      maps.conflicts.push({
        a: Math.min(newer, older),
        b: Math.max(newer, older),
        kind: "score_mismatch",
        scores: [prev, score],
      });
    }
    return;
  }

  edgeScores.set(key, score);
  maps.supersedesEdges.push({ from: newer, to: older, score });
}

/** Tarjan SCC on directed graph (edge: from → to). */
function stronglyConnectedComponents(
  nodes: Iterable<number>,
  outs: Map<number, number[]>,
): number[][] {
  let index = 0;
  const indices = new Map<number, number>();
  const lowlink = new Map<number, number>();
  const onStack = new Set<number>();
  const stack: number[] = [];
  const sccs: number[][] = [];

  function strongconnect(v: number) {
    indices.set(v, index);
    lowlink.set(v, index);
    index++;
    stack.push(v);
    onStack.add(v);
    for (const w of outs.get(v) || []) {
      if (!indices.has(w)) {
        strongconnect(w);
        lowlink.set(v, Math.min(lowlink.get(v)!, lowlink.get(w)!));
      } else if (onStack.has(w)) {
        lowlink.set(v, Math.min(lowlink.get(v)!, indices.get(w)!));
      }
    }
    if (lowlink.get(v) === indices.get(v)) {
      const comp: number[] = [];
      for (;;) {
        const w = stack.pop()!;
        onStack.delete(w);
        comp.push(w);
        if (w === v) break;
      }
      sccs.push(comp);
    }
  }

  for (const v of nodes) {
    if (!indices.has(v)) strongconnect(v);
  }
  return sccs;
}

function finalizeLineageConflicts(maps: LineageMaps): void {
  // Dedupe score_mismatch by pair key (keep first with merged scores already applied).
  const scoreSeen = new Set<string>();
  const scoreConflicts: LineageConflict[] = [];
  for (const c of maps.conflicts) {
    if (c.kind !== "score_mismatch") continue;
    const key = `sm-${c.a}-${c.b}`;
    if (scoreSeen.has(key)) continue;
    scoreSeen.add(key);
    scoreConflicts.push(c);
  }

  // G4: one cycle conflict per SCC with size > 1 (includes 2-cycles and longer).
  const outs = new Map<number, number[]>();
  const nodes = new Set<number>();
  for (const e of maps.supersedesEdges) {
    nodes.add(e.from);
    nodes.add(e.to);
    if (!outs.has(e.from)) outs.set(e.from, []);
    outs.get(e.from)!.push(e.to);
  }
  // H1: SCC detection emits ONE conflict with members[] only.
  // Do NOT mutate supersededBy — that invents "X superseded by Y" provenance
  // that was never stored. Every SCC member already has a real incoming edge,
  // so currentness already fails closed without summarising the component.
  const cycleConflicts: LineageConflict[] = [];
  for (const comp of stronglyConnectedComponents(nodes, outs)) {
    if (comp.length < 2) continue;
    const sorted = [...comp].sort((a, b) => a - b);
    cycleConflicts.push({
      a: sorted[0],
      b: sorted[sorted.length - 1],
      kind: "cycle",
      scores: [],
      members: sorted,
    });
  }

  // Keep self_cycle conflicts as recorded.
  const selfConflicts = maps.conflicts.filter(c => c.kind === "self_cycle");
  maps.conflicts = [...selfConflicts, ...scoreConflicts, ...cycleConflicts];

  // Multi-leaf components (forks): fail closed — leaves are authority_unknown.
  // Cycles already have no unsuperseded leaf (every member has an incoming
  // supersedes edge), so this only marks genuine DAG forks.
  const undirected = new Map<number, Set<number>>();
  const addUndirected = (a: number, b: number) => {
    if (!undirected.has(a)) undirected.set(a, new Set());
    if (!undirected.has(b)) undirected.set(b, new Set());
    undirected.get(a)!.add(b);
    undirected.get(b)!.add(a);
  };
  for (const e of maps.supersedesEdges) addUndirected(e.from, e.to);
  const seen = new Set<number>();
  for (const start of undirected.keys()) {
    if (seen.has(start)) continue;
    const stack = [start];
    const comp: number[] = [];
    while (stack.length) {
      const n = stack.pop()!;
      if (seen.has(n)) continue;
      seen.add(n);
      comp.push(n);
      for (const w of undirected.get(n) || []) stack.push(w);
    }
    const leaves = comp.filter(id => !maps.supersededBy.has(id));
    if (leaves.length > 1) {
      for (const leaf of leaves) maps.authorityUnknown.add(leaf);
    }
  }
}

/**
 * Walk all crossrefs and normalize lineage from either half of a bidirectional pair.
 * Score policy: max. Cycles: SCC detection, one conflict per component.
 * Self-links: self_cycle + authorityUnknown.
 */
export function buildLineageMaps(
  crossrefs: Iterable<[number, CrossRefEntry[]]>,
): LineageMaps {
  const maps: LineageMaps = {
    supersededBy: new Map(),
    supersedesMap: new Map(),
    supersedesEdges: [],
    conflicts: [],
    authorityUnknown: new Set(),
  };
  const edgeScores = new Map<string, number>();

  for (const [memoryId, refs] of crossrefs) {
    for (const ref of refs || []) {
      if (!ref || typeof ref.id !== "number") continue;
      const score = typeof ref.score === "number" ? ref.score : 1.0;
      const edgeType = ref.edge_type;

      // G3: self-lineage
      if (ref.id === memoryId) {
        if (edgeType === "supersedes" || edgeType === "superseded_by") {
          maps.authorityUnknown.add(memoryId);
          maps.conflicts.push({
            a: memoryId,
            b: memoryId,
            kind: "self_cycle",
            scores: [score],
          });
        }
        continue;
      }

      if (edgeType === "supersedes") {
        addPair(maps, memoryId, ref.id, score, edgeScores);
      } else if (edgeType === "superseded_by") {
        addPair(maps, ref.id, memoryId, score, edgeScores);
      }
    }
  }
  finalizeLineageConflicts(maps);
  return maps;
}

export function isLineageEdgeType(edgeType: string | undefined | null): boolean {
  return edgeType === "supersedes";
}

/**
 * Normalize one association ref into semantic (from, to, type, directed).
 * references/implements/extends: direction is the meaning.
 * referenced_by(m, ref) means ref references m → from=ref, to=m.
 * related_to / contradicts: undirected (from=min, to=max).
 */
export function normalizeAssociationRef(
  memoryId: number,
  ref: CrossRefEntry,
): { from: number; to: number; edge_type: string; directed: boolean } | null {
  if (!ref || typeof ref.id !== "number" || ref.id === memoryId) return null;
  const rawType = ref.edge_type || "related_to";
  if (rawType === "supersedes" || rawType === "superseded_by") return null;

  switch (rawType) {
    case "references":
      return { from: memoryId, to: ref.id, edge_type: "references", directed: true };
    case "referenced_by":
      return { from: ref.id, to: memoryId, edge_type: "references", directed: true };
    case "implements":
      return { from: memoryId, to: ref.id, edge_type: "implements", directed: true };
    case "implemented_by":
      return { from: ref.id, to: memoryId, edge_type: "implements", directed: true };
    case "extends":
      return { from: memoryId, to: ref.id, edge_type: "extends", directed: true };
    case "extended_by":
      return { from: ref.id, to: memoryId, edge_type: "extends", directed: true };
    case "contradicts": {
      const lo = Math.min(memoryId, ref.id);
      const hi = Math.max(memoryId, ref.id);
      return { from: lo, to: hi, edge_type: "contradicts", directed: false };
    }
    case "related_to":
    default: {
      const lo = Math.min(memoryId, ref.id);
      const hi = Math.max(memoryId, ref.id);
      return { from: lo, to: hi, edge_type: "related_to", directed: false };
    }
  }
}

/**
 * Build association edges with SEMANTIC endpoints and max-score merge (G2).
 * One edge per normalized direction (or undirected pair for symmetric types).
 * Asymmetric edges set directed=true so clients know order is meaningful;
 * UI lineage styling still keys only on edge_type==="supersedes".
 */
export function buildAssociationEdges(
  crossrefs: Iterable<[number, CrossRefEntry[]]>,
  minScore: number,
): AssociationEdge[] {
  const best = new Map<string, AssociationEdge>();

  for (const [memoryId, refs] of crossrefs) {
    for (const ref of refs || []) {
      const norm = normalizeAssociationRef(memoryId, ref);
      if (!norm) continue;
      const score = typeof ref.score === "number" ? ref.score : 0;
      if (norm.edge_type === "related_to" && score <= minScore) continue;

      const key = norm.directed
        ? `dir-${norm.edge_type}-${norm.from}->${norm.to}`
        : `und-${norm.edge_type}-${norm.from}-${norm.to}`;

      const existing = best.get(key);
      if (!existing) {
        best.set(key, {
          from: norm.from,
          to: norm.to,
          edge_type: norm.edge_type,
          score,
          directed: norm.directed,
        });
      } else if (score > existing.score) {
        existing.score = score;
      }
    }
  }
  return Array.from(best.values());
}

export function partitionLineageEdges(
  edges: LineageEdge[],
  nodeIds: Set<number>,
): {
  drawable: LineageEdge[];
  dangling: Array<{ from: number; to: number; missing: "from" | "to" | "both" }>;
} {
  const drawable: LineageEdge[] = [];
  const dangling: Array<{ from: number; to: number; missing: "from" | "to" | "both" }> = [];
  for (const e of edges) {
    const fromOk = nodeIds.has(e.from);
    const toOk = nodeIds.has(e.to);
    if (fromOk && toOk) {
      drawable.push(e);
    } else {
      dangling.push({
        from: e.from,
        to: e.to,
        missing: !fromOk && !toOk ? "both" : !fromOk ? "from" : "to",
      });
    }
  }
  return { drawable, dangling };
}
