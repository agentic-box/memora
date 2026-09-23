/**
 * GET /api/memories/:id - Returns a single memory by ID
 * Supports ?db=<configured name> to select a database
 * PATCH/PUT/POST/DELETE - 405: the viewer is read-only (§6 F1)
 */

import { resolveDatabase, selectionErrorResponse, type DatabaseEnv } from "../_db.ts";

interface Env extends DatabaseEnv {}

interface Memory {
  id: number;
  content: string;
  metadata: string;
  tags: string;
  created_at: string;
  updated_at: string | null;
}

interface MemoryResponse {
  id: number;
  content: string;
  tags: string[];
  created: string;
  updated: string | null;
  metadata: Record<string, unknown>;
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

// Normalize SQLite naive datetime to ISO 8601 with Z suffix for
// cross-browser Date parsing. Matches memories.ts.
function toIsoUtc(ts: string | null | undefined): string | null {
  if (!ts) return null;
  if (ts.includes(" ") && !ts.includes("T")) {
    return ts.replace(" ", "T") + "Z";
  }
  return ts;
}

function toMemoryResponse(result: Memory): MemoryResponse {
  const meta = parseJson<Record<string, unknown>>(result.metadata, {});
  return {
    id: result.id,
    content: result.content,
    tags: parseJson<string[]>(result.tags, []),
    created: toIsoUtc(result.created_at) ?? "",
    updated: toIsoUtc(result.updated_at),
    metadata: expandR2Urls(meta),
  };
}

// The viewer is read-only (docs/local-primary-implementation.md §6 F1, slice
// L7): every write method answers 405 before touching the database. Memories
// are edited through memora itself. Exported explicitly rather than left to
// the platform, so the answer is the same in every runtime and is tested.
const readOnly: PagesFunction<Env> = async () =>
  Response.json(
    {
      error: "read_only",
      message: "The memora viewer is read-only; edit memories through memora.",
    },
    { status: 405, headers: { Allow: "GET, HEAD" } },
  );

export const onRequestPatch = readOnly;
export const onRequestPut = readOnly;
export const onRequestPost = readOnly;
export const onRequestDelete = readOnly;

export const onRequestGet: PagesFunction<Env> = async ({ env, params, request }) => {
  const url = new URL(request.url);
  const dbName = url.searchParams.get("db");
  const selection = resolveDatabase(env, dbName);
  if (!selection.ok) return selectionErrorResponse(selection);
  const db = selection.binding;

  const id = parseInt(params.id as string, 10);

  if (isNaN(id)) {
    return Response.json({ error: "invalid_id" }, { status: 400 });
  }

  const result = await db.prepare(
    "SELECT id, content, metadata, tags, created_at, updated_at FROM memories WHERE id = ?"
  ).bind(id).first<Memory>();

  if (!result) {
    return Response.json({ error: "not_found" }, { status: 404 });
  }

  return Response.json(toMemoryResponse(result));
};
