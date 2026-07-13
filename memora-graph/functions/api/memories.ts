/**
 * GET /api/memories - Returns memories with optional filters (timeline, issues, favorites).
 * Supports ?db=<configured name> to select a database.
 *
 * Query params:
 *   favorites=1              → only favorited memories (legacy)
 *   type=issue               → only issue memories (metadata.type == 'issue')
 *   status=open|closed       → issue status filter (normalizes legacy in_progress/resolved/wontfix)
 *   severity=critical|major|minor → issue severity (missing defaults to minor)
 *   component=<str>          → exact component match
 *   category=<str>           → exact category match
 *   sort=updated|created|severity → result ordering
 *   limit, offset            → pagination
 */

import { resolveDatabase, selectionErrorResponse, type DatabaseEnv } from "./_db";

interface Env extends DatabaseEnv {}

interface Memory {
  id: number;
  content: string;
  metadata: string;
  tags: string;
  created_at: string;
  updated_at: string | null;
}

function parseJson<T>(str: string | null, defaultValue: T): T {
  if (!str) return defaultValue;
  try {
    return JSON.parse(str);
  } catch {
    return defaultValue;
  }
}

function expandR2Urls(metadata: Record<string, unknown> | null): Record<string, unknown> {
  if (!metadata) return {};

  const images = metadata.images as Array<{ src: string; caption?: string }> | undefined;
  if (images?.length) {
    metadata.images = images.map(img => {
      let src = img.src;
      // Convert r2:// URLs to our proxy path
      if (src?.startsWith("r2://")) {
        src = "/api/r2/" + src.replace("r2://", "");
      }
      return { ...img, src };
    });
  }

  return metadata;
}

// Normalize SQLite "YYYY-MM-DD HH:MM:SS" (naive) to ISO 8601 with Z suffix
// for cross-browser Date parsing compatibility in formatRelative().
function toIsoUtc(ts: string | null | undefined): string | null {
  if (!ts) return null;
  if (ts.includes(" ") && !ts.includes("T")) {
    return ts.replace(" ", "T") + "Z";
  }
  return ts;
}

export const onRequestGet: PagesFunction<Env> = async ({ env, request }) => {
  const url = new URL(request.url);
  const dbName = url.searchParams.get("db");
  const selection = resolveDatabase(env, dbName);
  if (!selection.ok) return selectionErrorResponse(selection);
  const db = selection.binding;

  // Collect filters
  const favoritesOnly = url.searchParams.get("favorites") === "1";
  const typeFilter = url.searchParams.get("type"); // "issue" for now
  const statusFilter = url.searchParams.get("status"); // "open" | "closed"
  const severityFilter = url.searchParams.get("severity"); // "critical" | "major" | "minor"
  const componentFilter = url.searchParams.get("component");
  const categoryFilter = url.searchParams.get("category");
  const sortParam = url.searchParams.get("sort"); // "updated" | "created" | "severity"

  const isIssueQuery = typeFilter === "issue";

  // Default limit bumps when filtering issues (users expect larger page)
  const defaultLimit = favoritesOnly ? 500 : (isIssueQuery ? 200 : 50);
  const maxLimit = favoritesOnly ? 500 : (isIssueQuery ? 500 : 200);
  const limit = Math.min(
    Math.max(parseInt(url.searchParams.get("limit") || String(defaultLimit), 10) || defaultLimit, 1),
    maxLimit,
  );
  const offset = Math.max(parseInt(url.searchParams.get("offset") || "0", 10) || 0, 0);

  // Build WHERE clauses — each in its own parentheses, joined with AND.
  // `AND` binds tighter than `OR` in SQL precedence, so un-grouped OR
  // clauses would leak rows that match only one side.
  const clauses: string[] = [];
  const binds: Array<string | number> = [];

  if (favoritesOnly) {
    clauses.push("(json_extract(metadata, '$.favorite') IN (1, 'true'))");
  }

  if (isIssueQuery) {
    clauses.push("(json_extract(metadata, '$.type') = 'issue')");
  }

  if (statusFilter === "open") {
    // Normalize legacy: in_progress → open, unset → open
    clauses.push("(json_extract(metadata, '$.status') IN ('open', 'in_progress') OR json_extract(metadata, '$.status') IS NULL)");
  } else if (statusFilter === "closed") {
    // Normalize legacy: resolved/wontfix → closed
    clauses.push("(json_extract(metadata, '$.status') IN ('closed', 'resolved', 'wontfix'))");
  }

  if (severityFilter === "critical" || severityFilter === "major" || severityFilter === "minor") {
    clauses.push("(COALESCE(json_extract(metadata, '$.severity'), 'minor') = ?)");
    binds.push(severityFilter);
  }

  if (componentFilter) {
    clauses.push("(json_extract(metadata, '$.component') = ?)");
    binds.push(componentFilter);
  }

  if (categoryFilter) {
    clauses.push("(json_extract(metadata, '$.category') = ?)");
    binds.push(categoryFilter);
  }

  const whereClause = clauses.length > 0 ? " WHERE " + clauses.join(" AND ") : "";

  // ORDER BY
  let orderBy: string;
  switch (sortParam) {
    case "updated":
      orderBy = " ORDER BY COALESCE(updated_at, created_at) DESC";
      break;
    case "severity":
      orderBy =
        " ORDER BY CASE COALESCE(json_extract(metadata, '$.severity'), 'minor') " +
        "WHEN 'critical' THEN 0 WHEN 'major' THEN 1 WHEN 'minor' THEN 2 ELSE 3 END, " +
        "COALESCE(updated_at, created_at) DESC";
      break;
    case "created":
    default:
      orderBy = " ORDER BY created_at DESC";
      break;
  }

  const countSql = "SELECT COUNT(*) as cnt FROM memories" + whereClause;
  const countRow = await db.prepare(countSql).bind(...binds).first<{ cnt: number }>();
  const total = countRow?.cnt ?? 0;

  const listSql =
    "SELECT id, content, metadata, tags, created_at, updated_at FROM memories" +
    whereClause +
    orderBy +
    " LIMIT ? OFFSET ?";

  const result = await db.prepare(listSql).bind(...binds, limit, offset).all<Memory>();

  const memories = (result.results || []).map(m => {
    const meta = parseJson<Record<string, unknown>>(m.metadata, {});
    return {
      id: m.id,
      content: m.content,
      tags: parseJson<string[]>(m.tags, []),
      created: toIsoUtc(m.created_at) ?? "",
      updated: toIsoUtc(m.updated_at),
      metadata: expandR2Urls(meta),
    };
  });

  return Response.json({ memories, total, limit, offset });
};
