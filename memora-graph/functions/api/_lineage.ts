/**
 * Pure lineage assembly for /api/graph.
 * Normalize BOTH crossref halves (supersedes + superseded_by) into canonical
 * newer→older edges, then dedupe. Used by graph.ts and fixture tests.
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
  kind: "cycle" | "score_mismatch";
  scores: number[];
};

export type LineageMaps = {
  /** older id -> set of newer ids that supersede it */
  supersededBy: Map<number, Set<number>>;
  /** newer id -> set of older ids it supersedes */
  supersedesMap: Map<number, Set<number>>;
  /** directed lineage edges, deduped (both endpoints may still be dangling) */
  supersedesEdges: LineageEdge[];
  /** integrity: pairs with conflicting direction or score disagreement */
  conflicts: LineageConflict[];
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
  if (newer === older) return;
  if (!maps.supersedesMap.has(newer)) maps.supersedesMap.set(newer, new Set());
  maps.supersedesMap.get(newer)!.add(older);
  if (!maps.supersededBy.has(older)) maps.supersededBy.set(older, new Set());
  maps.supersededBy.get(older)!.add(newer);

  const key = `${newer}->${older}`;
  const rev = `${older}->${newer}`;

  // F5: opposing directions → cycle; both stay superseded (conservative).
  if (edgeScores.has(rev)) {
    maps.conflicts.push({
      a: newer,
      b: older,
      kind: "cycle",
      scores: [score, edgeScores.get(rev)!],
    });
    // Keep both directed edges recorded so neither looks current.
    if (!edgeScores.has(key)) {
      edgeScores.set(key, score);
      maps.supersedesEdges.push({ from: newer, to: older, score });
    }
    return;
  }

  if (edgeScores.has(key)) {
    const prev = edgeScores.get(key)!;
    if (prev !== score) {
      maps.conflicts.push({
        a: newer,
        b: older,
        kind: "score_mismatch",
        scores: [prev, score],
      });
      // Deterministic: keep max score on the existing edge.
      const max = Math.max(prev, score);
      edgeScores.set(key, max);
      const edge = maps.supersedesEdges.find(e => e.from === newer && e.to === older);
      if (edge) edge.score = max;
    }
    return;
  }

  edgeScores.set(key, score);
  maps.supersedesEdges.push({ from: newer, to: older, score });
}

/**
 * Walk all crossrefs and normalize lineage from either half of a bidirectional pair:
 *   m --supersedes--> ref      ⇒ newer=m, older=ref
 *   m --superseded_by--> ref   ⇒ newer=ref, older=m
 *
 * Score policy on duplicates: keep max score. Cycles: both directions kept;
 * both nodes marked superseded (conservative). Conflicts are listed for the UI.
 */
export function buildLineageMaps(
  crossrefs: Iterable<[number, CrossRefEntry[]]>,
): LineageMaps {
  const maps: LineageMaps = {
    supersededBy: new Map(),
    supersedesMap: new Map(),
    supersedesEdges: [],
    conflicts: [],
  };
  const edgeScores = new Map<string, number>();

  for (const [memoryId, refs] of crossrefs) {
    for (const ref of refs || []) {
      if (!ref || typeof ref.id !== "number" || ref.id === memoryId) continue;
      const score = typeof ref.score === "number" ? ref.score : 1.0;
      const edgeType = ref.edge_type;
      if (edgeType === "supersedes") {
        addPair(maps, memoryId, ref.id, score, edgeScores);
      } else if (edgeType === "superseded_by") {
        addPair(maps, ref.id, memoryId, score, edgeScores);
      }
    }
  }
  return maps;
}

/** True only for supersedes lineage edges — never other typed relations. */
export function isLineageEdgeType(edgeType: string | undefined | null): boolean {
  return edgeType === "supersedes";
}

/** Canonical undirected association type (collapse reverse halves — F6). */
const ASSOCIATION_CANONICAL: Record<string, string> = {
  related_to: "related_to",
  references: "references",
  referenced_by: "references",
  implements: "implements",
  implemented_by: "implements",
  extends: "extends",
  extended_by: "extends",
  contradicts: "contradicts",
};

/**
 * Build non-lineage graph edges.
 * Reverse halves (references/referenced_by, etc.) collapse to one undirected edge
 * keyed by sorted ids + canonical type so connection counts are not doubled (F6).
 * `directed` is always false — only supersedes is directed lineage.
 */
export function buildAssociationEdges(
  crossrefs: Iterable<[number, CrossRefEntry[]]>,
  minScore: number,
): Array<{ from: number; to: number; edge_type: string; score: number; directed: false }> {
  const edges: Array<{
    from: number;
    to: number;
    edge_type: string;
    score: number;
    directed: false;
  }> = [];
  const seen = new Set<string>();

  for (const [memoryId, refs] of crossrefs) {
    for (const ref of refs || []) {
      if (!ref || typeof ref.id !== "number" || ref.id === memoryId) continue;
      const rawType = ref.edge_type || "related_to";
      if (rawType === "supersedes" || rawType === "superseded_by") continue;
      const score = typeof ref.score === "number" ? ref.score : 0;
      const edgeType = ASSOCIATION_CANONICAL[rawType] || rawType;
      if (edgeType === "related_to" || !ref.edge_type) {
        if (score <= minScore) continue;
      }
      const lo = Math.min(memoryId, ref.id);
      const hi = Math.max(memoryId, ref.id);
      const edgeKey = `rel-${lo}-${hi}-${edgeType}`;
      if (seen.has(edgeKey)) continue;
      seen.add(edgeKey);
      edges.push({
        from: lo,
        to: hi,
        edge_type: edgeType,
        score,
        directed: false,
      });
    }
  }
  return edges;
}

/**
 * Split lineage edges into drawable (both ends in node set) vs dangling integrity notes.
 * Older nodes whose superseder is missing remain in supersededBy (conservative).
 */
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
