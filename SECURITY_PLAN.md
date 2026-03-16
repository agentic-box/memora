# Security Fixes: Critical and High Priority

## Context

Multi-agent security scan identified 4 critical and 5 high-severity findings. This plan addresses the top 5 actionable items. Cloudflare Access mitigates the "no auth on cloud API" findings (#3, #4), so those are excluded.

## Block 1: Rotate Exposed API Keys

**Problem:** `.mcp.json` exists on disk with plaintext OpenRouter API key and Cloudflare API token. Confirmed: root `.mcp.json` was never committed to git (only `claude-plugin/.mcp.json` which has no secrets). Still good practice to rotate.

**Actions:**
1. Rotate OpenRouter API key at https://openrouter.ai/settings/keys — generate new key, update `.mcp.json` locally
2. Rotate Cloudflare API token at https://dash.cloudflare.com/profile/api-tokens — generate new token with same permissions, update `.mcp.json` locally
3. Verify `.mcp.json` is in `.gitignore` and NOT tracked (`git ls-files .mcp.json` should return empty)
4. Rename tracked `claude-plugin/.mcp.json` to `claude-plugin/.mcp.json.example` to avoid normalizing `.mcp.json` in git
5. Check Cloudflare and OpenRouter access logs for unauthorized usage

## Block 2: Full XSS Sweep — All Three Renderers

**Problem:** XSS exists in **three** rendering paths: `index.html` (live cloud SPA — partially fixed), `templates.py` (static export), and fragment generators (`data.py`, `issues.py`, `todos.py`). The fix must cover all three to close the vulnerability class.

### Scope: Full renderer sweep

**Note:** `memora-graph/public/index.html` is a **symlink** to `memora/graph/index.html` — patching one patches both. Verified: `stat -c '%F' memora-graph/public/index.html` → `symbolic link`.

**A. `index.html` remaining gaps** (live cloud + local SPA — same file via symlink)
- `renderImages()` at line ~703: `img.src` and `img.caption` injected raw into innerHTML
  - Fix: escape caption with `escapeHtmlText()`, validate `img.src` against URL allowlist (`r2://`, `https://`)
- Attribute escaping: `escapeHtmlText()` escapes text nodes but not attributes. Values with `"` can break `data-*` attributes.
  - Fix: Add `escapeHtmlAttr()` function that also escapes `"` and `'`:
    ```javascript
    function escapeHtmlAttr(text) {
        return escapeHtmlText(text).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
    ```
  - Use `escapeHtmlAttr()` for all `data-*` attribute values, `escapeHtmlText()` for display text
- **querySelector selector injection**: filter functions build selectors from user data (e.g., `.legend-item[data-tag="..."]`). Values with `"` or `]` break these.
  - Fix: Use `CSS.escape()` when constructing selectors, OR switch to `dataset` equality matching:
    ```javascript
    // Instead of querySelector('[data-tag="' + tag + '"]')
    document.querySelectorAll('[data-filter-tag]').forEach(function(el) {
        el.classList.toggle('active', el.dataset.filterTag === tag);
    });
    ```

**B. `templates.py`** (static HTML export)
- Tag onclick injection (line ~1054): replace with `data-filter-tag` + `escapeHtmlAttr()`
- Image caption (line ~296): `escapeHtmlText(img.caption)`
- Image src (line ~295): validate URL scheme allowlist
- Tooltip innerHTML (line ~713): escape `idLine` and `descLine`
- Markdown: vendor DOMPurify inline, wrap `marked.parse()` with `DOMPurify.sanitize()`
- Add delegated click handler + `escapeHtmlAttr()` function in generated JS
- **Tab switching**: preserve `switchTab()`/`showPanel()` by using `data-tab` attributes instead of removing onclick wholesale. Only replace dynamic-data onclick handlers, not static navigation.

**C. Fragment generators** (`data.py`, `issues.py`, `todos.py`)
- `data.py:441,455,477` — raw labels in `build_static_html()` fragments
- `issues.py:147,160` — issue legend with inline handlers and raw component names
- `todos.py:141,154` — todo legend with inline handlers and raw category names
- Fix: escape all dynamic text with a Python-side `html_escape()` (use `markupsafe.escape` or `html.escape`), replace inline onclick with data-* attributes

### XSS regression tests
Add to `tests/test_graph_server.py`:
- `test_xss_in_tag_export` — create memory with malicious tag, export graph HTML, assert no unescaped script
- `test_xss_in_image_caption` — create memory with malicious image caption, verify escaping
- `test_xss_in_section_name` — create memory with malicious section metadata, verify escaping

## Block 3: Pin Pillow >= 10.4.0

**Problem:** `pyproject.toml` specifies `Pillow>=10.0.0` which allows versions with CVE-2024-28219 (buffer overflow) and CVE-2023-50447 (arbitrary code execution).

**File:** `pyproject.toml`

**Fix:** Change `"Pillow>=10.0.0"` to `"Pillow>=10.4.0"`. Also pin `openai>=1.6.0`.

**Verification:** `pip install -e .` succeeds with updated constraints.

## Block 4: Rate Limiting — Cloud + Local

**Problem:** Both cloud `/api/chat` (Cloudflare Pages) and local `/api/chat` (Starlette graph server at `server.py:410`) have no rate limiting.

**Cloud fix:** Cloudflare dashboard Rate Limiting rule — 30 req/min per IP for `/api/chat`. Per-IP is sufficient since Cloudflare Access already gates authentication; per-user JWT-claim limits require API Shield which is not configured.

**Local fix:** Add simple middleware to `memora/graph/server.py`:
```python
from collections import defaultdict
import time

_chat_rate = defaultdict(list)  # ip -> [timestamps]
CHAT_RATE_LIMIT = 30  # requests per minute

async def rate_limit_middleware(request, call_next):
    if "/api/chat" in str(request.url):
        ip = request.client.host
        now = time.time()
        _chat_rate[ip] = [t for t in _chat_rate[ip] if now - t < 60]
        if len(_chat_rate[ip]) >= CHAT_RATE_LIMIT:
            return JSONResponse({"error": "rate_limited"}, status_code=429,
                              headers={"Retry-After": "60"})
        _chat_rate[ip].append(now)
    return await call_next(request)
```

Document both rules in `memora-graph/README.md`.

## Block 5: Fix Cache — Permissions, Atomicity, Path Safety

**Problem:** `post_tool_use.py` stores dedup cache in `/tmp/` with world-readable permissions, non-atomic writes, and unsanitized `session_id` in filename.

**File:** `claude-plugin/hooks-handlers/post_tool_use.py`

**A. Secure path with hashed session_id:**
```python
import hashlib

def _cache_path(session_id: str) -> Path:
    safe_id = hashlib.sha256(session_id.encode()).hexdigest()[:16]
    cache_dir = Path.home() / ".cache" / "memora"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"capture_cache_{safe_id}.json"
```

**B. File-locked atomic read-modify-write:**
```python
import tempfile
import fcntl

def save_cache(session_id: str, cache: dict):
    cache_file = _cache_path(session_id)
    lock_file = cache_file.with_suffix(".lock")
    with open(lock_file, 'w') as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            # Write to temp file then atomic rename
            fd, tmp_path = tempfile.mkstemp(dir=cache_file.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, 'w') as f:
                    json.dump(cache, f)
                os.chmod(tmp_path, 0o600)
                os.replace(tmp_path, str(cache_file))
            except Exception:
                os.unlink(tmp_path)
                raise
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)

def load_cache(session_id: str) -> dict:
    cache_file = _cache_path(session_id)
    lock_file = cache_file.with_suffix(".lock")
    with open(lock_file, 'w') as lf:
        fcntl.flock(lf, fcntl.LOCK_SH)  # shared lock for reads
        try:
            if cache_file.exists():
                return json.loads(cache_file.read_text())
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
    return {}
```

This prevents lost updates from concurrent async hook invocations.

## Files Modified
- `memora/graph/index.html` — renderImages() escaping, escapeHtmlAttr() function
- `memora/graph/templates.py` — full XSS fix (data-* attrs, escape functions, DOMPurify, tab safety)
- `memora/graph/data.py` — escape fragment labels
- `memora/graph/issues.py` — escape component names, replace inline handlers
- `memora/graph/todos.py` — escape category names, replace inline handlers
- `memora/graph/server.py` — rate limiting middleware for local /api/chat
- `claude-plugin/hooks-handlers/post_tool_use.py` — secure cache (hashed path, atomic write, 0o600)
- `claude-plugin/.mcp.json` → `claude-plugin/.mcp.json.example` (rename)
- `pyproject.toml` — Pillow >= 10.4.0, openai >= 1.6.0
- `tests/test_graph_server.py` — XSS regression tests

## Manual Actions (No Code)
- Rotate OpenRouter API key and update local `.mcp.json`
- Rotate Cloudflare API token and update local `.mcp.json`
- Add Cloudflare Rate Limiting rule for `/api/chat` in dashboard (per-user via Access identity)

## Verification
1. Keys: Old API keys return 401/403
2. XSS: Create memory with tag `'); alert('xss')`, view in local + cloud graph — no execution
3. XSS: Create memory with image caption `<script>alert(1)</script>` — no execution in any renderer
4. XSS: Create memory with section `<img src=x onerror=alert(1)>` — no execution in static export
5. XSS: `test_xss_in_tag_export`, `test_xss_in_image_caption`, `test_xss_in_section_name` all pass
6. Deps: `pip install -e .` succeeds, `pip show Pillow` shows >= 10.4.0
7. Rate limit (cloud): Hit `/api/chat` > 30 times in 1 minute — get 429
8. Rate limit (local): Same test against local graph server — get 429
9. Cache: Files in `~/.cache/memora/` with `-rw-------`, hashed filenames
10. Tests: `pytest tests/ -q` — all pass

## Review History
- v1: Initial 5-block plan
- v2: Addressed codex findings:
  - Block 1: Confirmed root .mcp.json never in git; rename claude-plugin/.mcp.json to .example
  - Block 2: Expanded from templates.py-only to full 3-renderer sweep (index.html, templates.py, fragment generators); added escapeHtmlAttr for attribute values; added renderImages() src/caption fix; preserved tab switching with data-tab; added XSS regression tests
  - Block 4: Added local rate limiting middleware alongside cloud dashboard rule; use Access identity not IP
  - Block 5: Hash session_id for filename safety; atomic write via tempfile+os.replace; file lock consideration
- v3: Addressed codex v2 findings:
  - Clarified public/index.html is a symlink (not a copy) — patching one patches both
  - Added CSS.escape() / dataset equality matching for querySelector selector injection
  - Block 5: Added fcntl file locking around read-modify-write cycle to prevent async race conditions
  - Block 4: Simplified cloud rate limit to per-IP (per-user JWT requires API Shield, not configured)
