/**
 * Pure truncation-banner / limit-raise helpers for the graph UIs.
 * Shipped module — both UIs import it and tests MUST import this file (no
 * mirrors / no reimplementation in tests). Mirrors the /api/graph ?limit=
 * contract (default cap 2000, hard max 5000).
 */

export const GRAPH_DEFAULT_LIMIT = 2000;
export const GRAPH_LIMIT_MAX = 5000;

// Deterministic raise step: an unraised graph (2000) goes 2000 -> 3500 -> 5000.
const GRAPH_LIMIT_STEP = 1500;

/**
 * Compute the truncation banner from a graph response.
 * @param {{ truncated?: boolean, shown?: number, total?: number }} input
 * @returns {{ visible: boolean, text: string }}
 */
export function formatTruncationBanner({ truncated, shown, total }) {
  if (truncated !== true) return { visible: false, text: "" };
  return { visible: true, text: `showing ${shown} of ${total} nodes` };
}

/**
 * Next ?limit= value to request after a raise.
 * current defaults conceptually to 2000, hardMax to 5000. Returns an integer in
 * (current, hardMax] when current < hardMax, or hardMax when current >= hardMax.
 * Never returns > hardMax; never returns <= current unless already at max.
 * @param {{ current?: number, hardMax?: number }} input
 */
export function nextGraphLimit({ current, hardMax = GRAPH_LIMIT_MAX }) {
  const base = Number.isFinite(Number(current)) ? Number(current) : GRAPH_DEFAULT_LIMIT;
  const cap = Number.isFinite(Number(hardMax)) && Number(hardMax) > 0
    ? Number(hardMax)
    : GRAPH_LIMIT_MAX;
  if (base >= cap) return cap;
  const stepped = base + GRAPH_LIMIT_STEP;
  return Math.min(cap, Math.max(base + 1, stepped));
}

/**
 * Build the /api/graph URL, preserving db/docs and adding ?limit= only when the
 * user has raised it. Clamps limit to GRAPH_LIMIT_MAX.
 * @param {{ pathname?: string, db?: string|null, docs?: boolean, limit?: number|null }} input
 */
export function buildGraphUrl({ pathname = "/api/graph", db, docs, limit } = {}) {
  const params = new URLSearchParams();
  if (db) params.set("db", db);
  if (docs) params.set("docs", "1");
  if (limit != null) {
    const n = Math.floor(Number(limit));
    if (Number.isFinite(n) && n > 0) params.set("limit", String(Math.min(n, GRAPH_LIMIT_MAX)));
  }
  const qs = params.toString();
  return qs ? `${pathname}?${qs}` : pathname;
}
