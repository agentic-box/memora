# Security Fixes: High Priority Issues

## Context

Multi-agent security scan identified 5 high-severity findings. This plan addresses all of them.

## Block 1: R2 Proxy — Prefix Restriction + Content-Type Validation

**Problem:** Both cloud (`r2/[[path]].ts:32`) and local (`graph/server.py:378`) R2 proxies accept arbitrary object keys. `../` is not the real issue — any key in the bucket is fetchable. Images are stored under `images/{memory_id}/` prefix.

**Files:**
- `memora-graph/functions/api/r2/[[path]].ts:22-76`
- `memora/graph/server.py:378-400`

**Fix (both paths):**
1. **Prefix restriction:** Only allow keys starting with `images/` (or `memora/images/`, `ob1/images/` after db prefix stripping)
2. **Block `..` sequences** as defense in depth
3. **Validate Content-Type** is `image/*` before returning the body
4. **Block non-image extensions** as secondary check

**TypeScript (cloud):**
```typescript
if (objectKey.includes("..") || objectKey.startsWith("/")) {
  return new Response("Invalid path", { status: 400 });
}
// After db prefix stripping, require images/ prefix
if (!objectKey.startsWith("images/")) {
  return new Response("Not found", { status: 404 });
}
// After fetching, validate content type
if (object && !object.httpMetadata?.contentType?.startsWith("image/")) {
  return new Response("Not found", { status: 404 });
}
```

**Python (local):**
```python
if ".." in key or not key.startswith("images/"):
    return JSONResponse({"error": "not_found"}, status_code=404)
# After fetching, check content type
content_type = response.get("ContentType", "")
if not content_type.startswith("image/"):
    return JSONResponse({"error": "not_found"}, status_code=404)
```

## Block 2: MCP Tool Rate Limiting — Operation-Specific

**Problem:** No rate limiting on expensive MCP tools. An agent could exhaust resources via rebuild/export/import loops.

**File:** `memora/server.py`

**Specific tools to throttle:**

| Tool | Cooldown | Reason |
|---|---|---|
| `memory_rebuild_embeddings` | 300s | Recomputes all embeddings (OpenAI API calls) |
| `memory_rebuild_crossrefs` | 300s | Full crossref recomputation |
| `memory_find_duplicates` | 120s | Scans all crossrefs + optional LLM calls |
| `memory_migrate_images` | 300s | Bulk image migration |
| `memory_generate_insights` | 120s | Full analysis with LLM |
| `memory_export` | 60s | Full DB read |
| `memory_import` | 60s | Bulk write |

**Implementation:** Single-flight per tool name (not per identity — MCP server is single-process):
```python
_tool_last_call: Dict[str, float] = {}

def _check_tool_cooldown(tool_name: str) -> Optional[str]:
    cooldown = _TOOL_COOLDOWNS.get(tool_name)
    if not cooldown:
        return None
    last = _tool_last_call.get(tool_name, 0)
    elapsed = time.time() - last
    if elapsed < cooldown:
        return f"Rate limited. Try again in {int(cooldown - elapsed)}s."
    _tool_last_call[tool_name] = time.time()
    return None
```

Add check at the top of each expensive tool function. Document which tools are throttled in the tool docstrings.

## Block 3: Secure Cache — ALREADY IMPLEMENTED

**Status:** Completed in SECURITY_PLAN.md implementation. Changes already in worktree:
- `_cache_path()` with SHA-256 hashed session ID
- `~/.cache/memora/` with 0o600 permissions
- `fcntl` exclusive lock covering full read-modify-write in `is_duplicate()`
- Atomic `tempfile` + `os.replace()` writes

**No further action needed.** Remove from active scope.

## Block 4: Single-User Security Model Documentation

**Problem:** Flagged as "no row-level security" but this is by design — Memora is a single-user memory system.

**File:** `memora-graph/README.md`

**Action:** Add security model section documenting:
- Single-user design — no tenant isolation
- Cloud: Cloudflare Access gates all endpoints; `?db=` parameter selects between user's own databases (not multi-tenant)
- Local: graph server binds to localhost by default
- Not designed for multi-tenant or shared-user deployments

```markdown
## Security Model

Memora is a **single-user** memory system. All memories in a database are
accessible to any authenticated user. Multi-user/multi-tenant isolation is
not supported and the `?db=` parameter is not a tenant boundary — it selects
between the owner's own databases.

Access control is enforced at the infrastructure level:
- **Cloud:** Cloudflare Access gates all Pages endpoints (authentication required)
- **Local:** Graph server binds to localhost by default
- **MCP:** Server runs as a local process under the user's own permissions
```

## Block 5: SSE/WebSocket Origin Validation (Defense in Depth)

**Problem:** SSE endpoint in local graph server has no origin validation.

**File:** `memora/graph/server.py:293`

**Note:** This is defense in depth, not the primary security control. The local server typically binds to localhost. Non-browser clients bypass Origin checks entirely.

**Fix:** Add origin validation to **both** SSE and `/api/chat` endpoints:
```python
def _check_origin(request: Request) -> bool:
    """Validate Origin header for browser requests."""
    origin = request.headers.get("origin", "")
    if not origin:
        return True  # Non-browser clients don't send Origin
    host = request.headers.get("host", "localhost")
    return (origin.startswith("http://localhost") or
            origin.startswith("http://127.0.0.1") or
            origin.startswith(f"http://{host}") or
            origin.startswith(f"https://{host}"))
```

Apply to `graph_events()` and `api_chat()`. Return 403 on failure.

## Files Modified
- `memora-graph/functions/api/r2/[[path]].ts` — prefix + content-type validation
- `memora/graph/server.py` — local R2 proxy prefix validation, SSE origin check, chat origin check
- `memora/server.py` — operation-specific rate limiting on 7 expensive MCP tools
- `memora-graph/README.md` — security model documentation

## Verification
1. R2 cloud: `GET /api/r2/memora/secret.txt` → 404 (not under images/)
2. R2 cloud: `GET /api/r2/memora/../../../etc/passwd` → 400
3. R2 local: same tests via local graph server
4. Rate limit: Call `memory_rebuild_embeddings` twice in 5 min → second returns cooldown message
5. Docs: README has Security Model section
6. SSE: Cross-origin fetch to `/api/graph/events` → 403
7. Chat origin: Cross-origin POST to `/api/chat` → 403
8. Tests: `pytest tests/ -q` — all pass

## Review History
- v1: Initial 5-block plan
- v2: Addressed codex findings:
  - Block 1: Changed from `..` rejection to prefix restriction (`images/`) + content-type validation; added local graph server R2 proxy (server.py:378) to scope; removed extension-only check as primary control
  - Block 2: Made operation-specific with 7 named tools and cooldowns; single-flight per tool name
  - Block 3: Marked as ALREADY IMPLEMENTED, removed from active scope
  - Block 4: Clarified `?db=` is not a tenant boundary; explicit about unsupported multi-tenant use
  - Block 5: Framed as defense in depth; applied to both SSE and `/api/chat`; documented non-browser bypass
