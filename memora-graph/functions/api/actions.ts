/**
 * GET /api/actions - Returns action history for the History tab
 * Supports ?db=<configured name> to select a database
 * Supports ?limit=200 to control number of results
 */

import { resolveDatabase, selectionErrorResponse, type DatabaseEnv } from "./_db";

interface Env extends DatabaseEnv {}

interface Action {
  id: number;
  memory_id: number | null;
  action: string;
  summary: string;
  timestamp: string;
}

export const onRequestGet: PagesFunction<Env> = async ({ env, request }) => {
  const url = new URL(request.url);
  const dbName = url.searchParams.get("db");
  const selection = resolveDatabase(env, dbName);
  if (!selection.ok) return selectionErrorResponse(selection);
  const db = selection.binding;
  const limit = Math.min(parseInt(url.searchParams.get("limit") || "200", 10), 500);

  try {
    const result = await db.prepare(
      "SELECT id, memory_id, action, summary, timestamp FROM memories_actions ORDER BY id DESC LIMIT ?"
    ).bind(limit).all<Action>();

    return Response.json({ actions: result.results || [] });
  } catch {
    // Table may not exist yet
    return Response.json({ actions: [] });
  }
};
