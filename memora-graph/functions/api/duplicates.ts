/**
 * GET /api/duplicates - Returns paginated duplicate pairs.
 *
 * Computes canonical duplicates directly from D1 (memories +
 * memories_crossrefs) using the same pair definition as
 * memora.storage.find_duplicate_pairs.
 *
 * Query params:
 *   db=memora|ob1     — database selection (default: env.DEFAULT_DB or "memora")
 *   min_similarity=N  — score floor (default 0.85)
 *   limit=N           — max pairs returned (default 50, max 200)
 *   offset=N          — pagination offset (default 0)
 *   tag=foo           — optional: only pairs where both memories carry this tag
 *
 * Response:
 *   {
 *     pairs: [{
 *       a: { id, preview, tags, created_at, metadata_type? },
 *       b: { id, preview, tags, created_at, metadata_type? },
 *       score,
 *       tier: "high" | "candidate"
 *     }],
 *     total: N,
 *     affected_node_count: N,
 *     thresholds: { high: 0.92, candidate: 0.85 },
 *     min_similarity: N,
 *     limit: N,
 *     offset: N
 *   }
 *
 * Tier thresholds match the existing memora constants:
 *   - high      : score >= 0.92  (red, near-certain duplicate)
 *   - candidate : 0.85 <= score < 0.92  (yellow, needs review)
 */

interface Env {
  DB_MEMORA: D1Database;
  DB_OB1: D1Database;
  DEFAULT_DB?: string;
}

interface MemoryRow {
  id: number;
  content: string;
  metadata: string;
  tags: string;
  created_at: string;
}

interface CrossRefRow {
  memory_id: number;
  related: string;
}

interface CrossRefEntry {
  id: number;
  score: number;
  edge_type?: string;
}

interface PairMemory {
  id: number;
  preview: string;
  tags: string[];
  created_at: string;
  metadata_type?: string;
}

interface DuplicatePair {
  a: PairMemory;
  b: PairMemory;
  score: number;
  tier: "high" | "candidate";
}

// Tier thresholds for OpenAI dense embeddings (text-embedding-3-small, 1536d).
// Empirically validated against real memora data after the OpenRouter
// provider fix that unblocked OpenAI embeddings.
//
//   high      : score >= 0.92  (red, near-certain duplicate)
//   candidate : 0.85 <= score < 0.92  (yellow, worth reviewing)
//   <0.85     : ignored — too loose to be a useful candidate
const HIGH_THRESHOLD = 0.92;
const CANDIDATE_THRESHOLD = 0.85;
const MAX_LIMIT = 200;
const PREVIEW_CHARS = 200;

function getDatabase(env: Env, dbName: string | null): D1Database {
  const name = dbName || env.DEFAULT_DB || "memora";
  if (name === "ob1") return env.DB_OB1;
  return env.DB_MEMORA;
}

function parseJson<T>(str: string | null, defaultValue: T): T {
  if (!str) return defaultValue;
  try {
    return JSON.parse(str);
  } catch {
    return defaultValue;
  }
}

function isExcludedType(metadata: Record<string, unknown> | null): boolean {
  const t = metadata?.type;
  return t === "section" || t === "document_fragment" || t === "document_root";
}

function buildPreview(content: string): string {
  if (!content) return "";
  if (content.length <= PREVIEW_CHARS) return content;
  return content.slice(0, PREVIEW_CHARS) + "\u2026";
}

export const onRequestGet: PagesFunction<Env> = async ({ env, request }) => {
  const url = new URL(request.url);
  const dbName = url.searchParams.get("db");
  const db = getDatabase(env, dbName);

  const minSimRaw = parseFloat(url.searchParams.get("min_similarity") || "");
  const minSimilarity = Number.isFinite(minSimRaw) && minSimRaw > 0
    ? Math.min(1.0, minSimRaw)
    : CANDIDATE_THRESHOLD;

  const limitRaw = parseInt(url.searchParams.get("limit") || "", 10);
  const limit = Number.isFinite(limitRaw) && limitRaw > 0
    ? Math.min(MAX_LIMIT, limitRaw)
    : 50;

  const offsetRaw = parseInt(url.searchParams.get("offset") || "", 10);
  const offset = Number.isFinite(offsetRaw) && offsetRaw >= 0 ? offsetRaw : 0;

  const tagFilter = url.searchParams.get("tag");

  // Fetch all memories (we need metadata + content + tags for the response)
  const memoriesResult = await db.prepare(
    "SELECT id, content, metadata, tags, created_at FROM memories"
  ).all<MemoryRow>();

  if (!memoriesResult.results || memoriesResult.results.length === 0) {
    return Response.json({
      pairs: [],
      total: 0,
      affected_node_count: 0,
      thresholds: { high: HIGH_THRESHOLD, candidate: CANDIDATE_THRESHOLD },
      min_similarity: minSimilarity,
      limit,
      offset,
    });
  }

  const memById = new Map<number, MemoryRow>();
  const excluded = new Set<number>();
  for (const m of memoriesResult.results) {
    memById.set(m.id, m);
    const meta = parseJson<Record<string, unknown>>(m.metadata, {});
    if (isExcludedType(meta)) excluded.add(m.id);
  }

  // Fetch all crossrefs
  let crossrefsResult: D1Result<CrossRefRow>;
  try {
    crossrefsResult = await db.prepare(
      "SELECT memory_id, related FROM memories_crossrefs"
    ).all<CrossRefRow>();
  } catch {
    return Response.json({
      pairs: [],
      total: 0,
      affected_node_count: 0,
      thresholds: { high: HIGH_THRESHOLD, candidate: CANDIDATE_THRESHOLD },
      min_similarity: minSimilarity,
      limit,
      offset,
    });
  }

  // Build unique pair set: (lo, hi) -> max score
  const pairScores = new Map<string, { lo: number; hi: number; score: number }>();

  for (const cr of crossrefsResult.results || []) {
    if (excluded.has(cr.memory_id)) continue;
    const refs = parseJson<CrossRefEntry[]>(cr.related, []);
    for (const ref of refs) {
      if (!ref || typeof ref.id !== "number") continue;
      // Skip typed link entries (supersedes, references, extends, etc.).
      // `related_to` is overloaded: compute_crossrefs writes it as a
      // default tag alongside real cosine scores, but absorb's
      // link_memories ALSO writes it with hardcoded score=1.0 for
      // "linked-but-not-duplicate" facts. Distinguish by score: cosine
      // of non-identical vectors is always < 1.0 mathematically, so
      // score >= 0.9999 means it's an absorb link, not a real duplicate.
      if (ref.edge_type && ref.edge_type !== "related_to") continue;
      if (excluded.has(ref.id)) continue;
      if (ref.id === cr.memory_id) continue;
      if (typeof ref.score !== "number") continue;
      if (ref.score < minSimilarity) continue;
      if (ref.score >= 0.9999) continue;

      const lo = Math.min(cr.memory_id, ref.id);
      const hi = Math.max(cr.memory_id, ref.id);
      const key = `${lo}-${hi}`;
      const existing = pairScores.get(key);
      if (!existing || ref.score > existing.score) {
        pairScores.set(key, { lo, hi, score: ref.score });
      }
    }
  }

  // Sort by score descending, then build full pair objects
  const sorted = Array.from(pairScores.values()).sort((x, y) => y.score - x.score);

  // Apply tag filter (if any) — needs both memories to carry the tag
  const filtered = tagFilter
    ? sorted.filter((p) => {
        const a = memById.get(p.lo);
        const b = memById.get(p.hi);
        if (!a || !b) return false;
        const aTags = parseJson<string[]>(a.tags, []);
        const bTags = parseJson<string[]>(b.tags, []);
        return aTags.includes(tagFilter) && bTags.includes(tagFilter);
      })
    : sorted;

  const total = filtered.length;
  const affectedNodeIds = new Set<number>();
  for (const p of filtered) {
    affectedNodeIds.add(p.lo);
    affectedNodeIds.add(p.hi);
  }
  const page = filtered.slice(offset, offset + limit);

  const pairs: DuplicatePair[] = [];
  for (const p of page) {
    const a = memById.get(p.lo);
    const b = memById.get(p.hi);
    if (!a || !b) continue;
    const aMeta = parseJson<Record<string, unknown>>(a.metadata, {});
    const bMeta = parseJson<Record<string, unknown>>(b.metadata, {});
    pairs.push({
      a: {
        id: a.id,
        preview: buildPreview(a.content),
        tags: parseJson<string[]>(a.tags, []),
        created_at: a.created_at,
        metadata_type: aMeta?.type as string | undefined,
      },
      b: {
        id: b.id,
        preview: buildPreview(b.content),
        tags: parseJson<string[]>(b.tags, []),
        created_at: b.created_at,
        metadata_type: bMeta?.type as string | undefined,
      },
      score: p.score,
      tier: p.score >= HIGH_THRESHOLD ? "high" : "candidate",
    });
  }

  return Response.json({
    pairs,
    total,
    affected_node_count: affectedNodeIds.size,
    thresholds: { high: HIGH_THRESHOLD, candidate: CANDIDATE_THRESHOLD },
    min_similarity: minSimilarity,
    limit,
    offset,
  });
};
