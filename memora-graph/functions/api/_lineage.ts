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

export type LineageMaps = {
  /** older id -> set of newer ids that supersede it */
  supersededBy: Map<number, Set<number>>;
  /** newer id -> set of older ids it supersedes */
  supersedesMap: Map<number, Set<number>>;
  /** directed lineage edges, deduped */
  supersedesEdges: LineageEdge[];
};

function addPair(
  maps: LineageMaps,
  newer: number,
  older: number,
  score: number,
  edgeKeys: Set<string>,
): void {
  if (newer === older) return;
  if (!maps.supersedesMap.has(newer)) maps.supersedesMap.set(newer, new Set());
  maps.supersedesMap.get(newer)!.add(older);
  if (!maps.supersededBy.has(older)) maps.supersededBy.set(older, new Set());
  maps.supersededBy.get(older)!.add(newer);
  const key = `${newer}->${older}`;
  if (!edgeKeys.has(key)) {
    edgeKeys.add(key);
    maps.supersedesEdges.push({ from: newer, to: older, score });
  }
}

/**
 * Walk all crossrefs and normalize lineage from either half of a bidirectional pair:
 *   m --supersedes--> ref      ⇒ newer=m, older=ref
 *   m --superseded_by--> ref   ⇒ newer=ref, older=m
 */
export function buildLineageMaps(
  crossrefs: Iterable<[number, CrossRefEntry[]]>,
): LineageMaps {
  const maps: LineageMaps = {
    supersededBy: new Map(),
    supersedesMap: new Map(),
    supersedesEdges: [],
  };
  const edgeKeys = new Set<string>();

  for (const [memoryId, refs] of crossrefs) {
    for (const ref of refs || []) {
      if (!ref || typeof ref.id !== "number" || ref.id === memoryId) continue;
      const score = typeof ref.score === "number" ? ref.score : 1.0;
      const edgeType = ref.edge_type;
      if (edgeType === "supersedes") {
        addPair(maps, memoryId, ref.id, score, edgeKeys);
      } else if (edgeType === "superseded_by") {
        addPair(maps, ref.id, memoryId, score, edgeKeys);
      }
    }
  }
  return maps;
}

/** True only for supersedes lineage edges — never other typed relations. */
export function isLineageEdgeType(edgeType: string | undefined | null): boolean {
  return edgeType === "supersedes";
}

/**
 * Build non-lineage graph edges (related_to + other typed non-supersede relations).
 * `directed` is always false here — only supersedes edges are directed lineage.
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
      const edgeType = ref.edge_type || "related_to";
      if (edgeType === "supersedes" || edgeType === "superseded_by") continue;
      const score = typeof ref.score === "number" ? ref.score : 0;
      if (edgeType === "related_to" || !ref.edge_type) {
        if (score <= minScore) continue;
      }
      // Undirected key for association edges (including contradicts — symmetric).
      const edgeKey = `rel-${Math.min(memoryId, ref.id)}-${Math.max(memoryId, ref.id)}-${edgeType}`;
      if (seen.has(edgeKey)) continue;
      seen.add(edgeKey);
      edges.push({
        from: memoryId,
        to: ref.id,
        edge_type: edgeType,
        score,
        directed: false,
      });
    }
  }
  return edges;
}
