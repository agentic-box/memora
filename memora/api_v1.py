"""memora /api/v1: the plain JSON API clmuxd talks to.

Design: clmux docs/PLAN_MEMORA_DAEMON.md (commit 28523ab) §3.1, §3.2, §3.3,
§3.5 and §7.1. The contract -- JSON Schemas, named fixtures, manifest -- is
contracts/memora-api/v1/ in this repo; its README states every rule this
module implements. Phase 0 scope: health, search, and the absorb route's
full contract; the absorb route never writes (every store reports
``writes: "unsupported"`` until the transactional Phase L exists).

Why plain routes and not MCP: these are Starlette custom routes on FastMCP's
app. They never create, touch or wait on an MCP session (one session per
call was memora OOM #999); the /mcp/<db> router and the pre-session guard
pass non-MCP paths through untouched.

Every non-2xx response has ONE envelope: {"error": code, "message": text}
plus route-specific fields (501 adds "writes"). This deliberately replaces
the design note's {"status": ...} bodies for 409/501 (leader decision, msg
7057; the note is being amended).

Request order, identical for every route:
  1. 401 bad_token        -- a tokens file is configured and the bearer token
                             is missing or unknown;
  2. 404 unknown_store    -- the store name is not a valid name;
  3. 403 store_forbidden  -- the token does not list the store;
  4. 404 unknown_store    -- the store is not configured on this server;
  5. 400 bad_request      -- (search, absorb) invalid JSON or fields;
  6. 429 admission        -- (search, absorb) too many API calls in flight;
  7. the route's own result.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import stat
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Tuple

logger = logging.getLogger("memora.api_v1")

API_VERSION = "v1"
CONTRACT_VERSION = "1.0.1"
SEARCH_MODE = "hybrid-v1"
# Single-store deployments (no MEMORA_DATABASES) expose their one store as:
DEFAULT_STORE = "default"

NAME_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

TOKEN_FILE_MAX_BYTES = 4096
MAX_TOP_K = 20
DEFAULT_TOP_K = 5
MAX_PREVIEW_CHARS = 400
DEFAULT_PREVIEW_CHARS = 200
MAX_QUERY_CHARS = 2000
MAX_FACTS = 20
MAX_FACT_CHARS = 10000
MAX_TAGS = 20
MAX_TAG_CHARS = 100
MAX_SOURCE_CHARS = 64
MAX_CONTEXT_CHARS = 2000
DEFAULT_MAX_INFLIGHT = 8
# Request body cap (search and absorb): 413 payload_too_large above it.
MAX_BODY_BYTES = 64 * 1024


class ApiConfigError(RuntimeError):
    """The API's configuration is unusable; the routes are not registered."""


# --------------------------------------------------------------------------
# Token file (§3.3, §7.1)
# --------------------------------------------------------------------------

def _read_only_mount(path: str) -> bool:
    """Is `path` on a read-only mounted filesystem (statvfs ST_RDONLY)? API1
    (leader 8213): the deploy mounts its secrets directory read-only into
    the container, so the tokens file keeps the host user's uid while the
    server runs as root (rootful docker). On such a mount, and only there,
    the OWNER matches are dropped; every other check stays."""
    try:
        return bool(os.statvfs(path).f_flag & getattr(os, "ST_RDONLY", 1))
    except OSError:
        return False


def _check_parent_dirs(path: str) -> None:
    """Every parent directory must be trustworthy (§7.1).

    §7.1 walks "up to $HOME", which fits a user's file. memora's tokens file
    usually sits outside any home (a container secret), so: for a file under
    $HOME, each directory from the file's up to and including $HOME must be
    owned by this user; for any other file, each directory up to "/" must be
    owned by this user or root. In both cases no directory may be group- or
    world-writable, and no component may be a symlink (lstat, never followed).
    """
    euid = os.geteuid()
    home = os.path.abspath(os.path.expanduser("~"))
    directory = os.path.dirname(path)
    under_home = directory == home or directory.startswith(home + os.sep)
    while True:
        st = os.lstat(directory)
        if stat.S_ISLNK(st.st_mode):
            raise ApiConfigError(f"tokens file parent {directory} is a symlink")
        if not stat.S_ISDIR(st.st_mode):
            raise ApiConfigError(f"tokens file parent {directory} is not a directory")
        allowed_owners = {euid} if under_home else {euid, 0}
        if st.st_uid not in allowed_owners and not _read_only_mount(directory):
            raise ApiConfigError(f"tokens file parent {directory} has owner uid {st.st_uid}")
        if st.st_mode & 0o022:
            raise ApiConfigError(
                f"tokens file parent {directory} is group/world-writable (mode {st.st_mode & 0o777:o})"
            )
        if under_home and directory == home:
            return
        parent = os.path.dirname(directory)
        if parent == directory:
            return
        directory = parent


def read_token_file(path: str) -> bytes:
    """Open and check the tokens file exactly as §7.1 says, return its bytes.

    O_RDONLY | O_NOFOLLOW | O_CLOEXEC, then fstat the descriptor: a regular
    file, owned by this effective uid, no group/other permission bits, at
    most 4 KiB; and every parent directory checked (_check_parent_dirs).
    On a read-only mount (the deploy's secrets mount) the owner matches of
    the file and of its read-only parents are not required (API1).
    """
    if not os.path.isabs(path):
        raise ApiConfigError("MEMORA_API_TOKENS_FILE must be an absolute path")
    path = os.path.normpath(path)
    _check_parent_dirs(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        # ELOOP: the final component is a symlink (O_NOFOLLOW).
        raise ApiConfigError(f"cannot open tokens file: {exc.strerror}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ApiConfigError("tokens file is not a regular file")
        if st.st_uid != os.geteuid() and not _read_only_mount(path):
            raise ApiConfigError(f"tokens file owner uid {st.st_uid} is not this user")
        if st.st_mode & 0o077:
            raise ApiConfigError(f"tokens file mode {st.st_mode & 0o777:o} allows group/other")
        if st.st_size > TOKEN_FILE_MAX_BYTES:
            raise ApiConfigError(f"tokens file is larger than {TOKEN_FILE_MAX_BYTES} bytes")
        data = os.read(fd, TOKEN_FILE_MAX_BYTES + 1)
        if len(data) > TOKEN_FILE_MAX_BYTES:
            raise ApiConfigError(f"tokens file is larger than {TOKEN_FILE_MAX_BYTES} bytes")
        return data
    finally:
        os.close(fd)


def parse_token_table(data: bytes) -> Dict[str, FrozenSet[str]]:
    """{sha256(token) hex: allowed stores}. The file holds hashes, never tokens.

    Format (contracts/memora-api/v1/README.md): a JSON object mapping a
    lowercase 64-hex sha256 of the token to a non-empty list of store names.
    """
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiConfigError(f"tokens file is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict) or not raw:
        raise ApiConfigError("tokens file must be a non-empty JSON object")
    table: Dict[str, FrozenSet[str]] = {}
    for digest, stores in raw.items():
        if not isinstance(digest, str) or not SHA256_HEX_RE.match(digest):
            raise ApiConfigError("tokens file keys must be lowercase sha256 hex digests")
        if (
            not isinstance(stores, list) or not stores
            or not all(isinstance(s, str) and NAME_RE.match(s) for s in stores)
        ):
            raise ApiConfigError("tokens file values must be non-empty lists of store names")
        table[digest] = frozenset(stores)
    return table


def load_token_table(path: str) -> Dict[str, FrozenSet[str]]:
    return parse_token_table(read_token_file(path))


# --------------------------------------------------------------------------
# Store and project configuration
# --------------------------------------------------------------------------

def store_projects(store: str) -> Tuple[str, ...]:
    """Projects the store declares (MEMORA_PROJECTS, storage.configured_projects
    -- one parser for the whole server, issue #47). Empty: none declared, so
    any valid project name is accepted."""
    from .storage import configured_projects

    return configured_projects(store)


def configured_stores() -> Tuple[Dict[str, Optional[str]], Optional[str]]:
    """({api store name: registry name or None for the legacy backend},
    error). An unreadable registry is reported, never guessed around."""
    from .storage import DatabaseRegistryError, database_registry

    try:
        registry = database_registry()
    except DatabaseRegistryError as exc:
        return {}, str(exc)
    if registry:
        return {name: name for name in registry if NAME_RE.match(name)}, None
    return {DEFAULT_STORE: None}, None


# --------------------------------------------------------------------------
# Seams for Phase L (and for the contract tests)
# --------------------------------------------------------------------------

def writes_capability(store: str) -> str:
    """"transactional" or "unsupported". API2: "transactional" iff the store
    is on a local SQLite primary, the only backend whose writer can hold
    absorb's phase 3 in one transaction (the same condition as L4's
    storage._has_transactions). D1, cloud and refused stores stay
    "unsupported". This opens no connection, so /health stays read-only."""
    from .storage import store_is_transactional

    return "transactional" if store_is_transactional(store) else "unsupported"


# API2 (docs/api-v1-writes.md): the (store, request_dict) -> (http_status,
# body) executor implementing §3.2's claim / fenced-done protocol. It is
# installed when the routes register and is only reached for a store whose
# writes_capability is "transactional".
absorb_executor: Optional[Callable[[str, Dict[str, Any]], Tuple[int, Dict[str, Any]]]] = None


def supervisor_state() -> Optional[Dict[str, Any]]:
    """§3.2 bound 4's `supervisor` block, read from the lock supervisor's
    state directory. That supervisor is Phase L infrastructure and does not
    exist yet, so Phase 0 reports null."""
    return None


# --------------------------------------------------------------------------
# Request validation (mirrors the contract schemas)
# --------------------------------------------------------------------------

class BadRequest(ValueError):
    pass


class PayloadTooLarge(Exception):
    """The request body exceeds MAX_BODY_BYTES."""


def _require_object(body: Any) -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise BadRequest("body must be a JSON object")
    return body


def _check_fields(body: Dict[str, Any], allowed: set) -> None:
    extra = sorted(set(body) - allowed)
    if extra:
        raise BadRequest(f"unknown field(s): {', '.join(extra)}")


def _string(body, name, *, required, max_len, min_len=1, pattern=None):
    if name not in body:
        if required:
            raise BadRequest(f"{name} is required")
        return None
    value = body[name]
    if not isinstance(value, str) or not (min_len <= len(value) <= max_len):
        raise BadRequest(f"{name} must be a string of {min_len}..{max_len} characters")
    if pattern is not None and not pattern.match(value):
        raise BadRequest(f"{name} has invalid characters")
    return value


def _integer(body, name, *, default, low, high):
    if name not in body:
        return default
    value = body[name]
    if type(value) is not int or not (low <= value <= high):
        raise BadRequest(f"{name} must be an integer in {low}..{high}")
    return value


def _string_list(body, name, *, max_items, max_len, min_items=0):
    if name not in body:
        return None
    value = body[name]
    if (
        not isinstance(value, list) or not (min_items <= len(value) <= max_items)
        or not all(isinstance(v, str) and 1 <= len(v) <= max_len for v in value)
    ):
        raise BadRequest(f"{name} must be a list of {min_items}..{max_items} strings")
    return value


def validate_search_request(body: Any) -> Dict[str, Any]:
    body = _require_object(body)
    _check_fields(body, {"query", "top_k", "tags_any", "preview_chars", "project"})
    query = _string(body, "query", required=True, max_len=MAX_QUERY_CHARS)
    if not query.strip():
        raise BadRequest("query must not be blank")
    return {
        "query": query,
        "top_k": _integer(body, "top_k", default=DEFAULT_TOP_K, low=1, high=MAX_TOP_K),
        "tags_any": _string_list(body, "tags_any", max_items=MAX_TAGS, max_len=MAX_TAG_CHARS, min_items=1),
        "preview_chars": _integer(body, "preview_chars", default=DEFAULT_PREVIEW_CHARS,
                                  low=0, high=MAX_PREVIEW_CHARS),
        "project": _string(body, "project", required=False, max_len=64, pattern=NAME_RE),
    }


def validate_absorb_request(body: Any) -> Dict[str, Any]:
    body = _require_object(body)
    _check_fields(body, {"idempotency_key", "project", "facts", "source", "context", "metadata", "tags"})
    facts = _string_list(body, "facts", max_items=MAX_FACTS, max_len=MAX_FACT_CHARS, min_items=1)
    if facts is None:
        raise BadRequest("facts is required")
    metadata = body.get("metadata")
    if "metadata" in body and not isinstance(metadata, dict):
        raise BadRequest("metadata must be a JSON object")
    return {
        "idempotency_key": _string(body, "idempotency_key", required=True, max_len=128,
                                   pattern=IDEMPOTENCY_KEY_RE),
        "project": _string(body, "project", required=True, max_len=64, pattern=NAME_RE),
        "facts": facts,
        "source": _string(body, "source", required=True, max_len=MAX_SOURCE_CHARS),
        "context": _string(body, "context", required=False, max_len=MAX_CONTEXT_CHARS),
        "metadata": metadata,
        "tags": _string_list(body, "tags", max_items=MAX_TAGS, max_len=MAX_TAG_CHARS),
    }


def canonical_request_sha256(request: Mapping[str, Any]) -> str:
    """request_sha256 for the idempotency row (§3.2): sha256 of the validated
    request (defaults applied) serialised with sorted keys and no spaces.
    The same key with a different sha256 is 409 key_conflict."""
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

TOKEN_RELOAD_INTERVAL_S = 1.0


def _file_signature(path: str) -> Optional[Tuple[int, int, int]]:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


@dataclass
class ApiState:
    tokens: Dict[str, FrozenSet[str]]
    tokens_path: str
    max_inflight: int
    tokens_signature: Optional[Tuple[int, int, int]] = None
    tokens_checked_at: float = 0.0
    inflight: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def current_tokens(self) -> Dict[str, FrozenSet[str]]:
        """The token table, re-read when the file changed (mtime, size or
        inode), checked at most once per TOKEN_RELOAD_INTERVAL_S -- so a
        token rotates without a restart. A reload that fails any check or
        validation keeps the last good table and logs an error."""
        now = time.monotonic()
        with self.lock:
            if now - self.tokens_checked_at < TOKEN_RELOAD_INTERVAL_S:
                return self.tokens
            self.tokens_checked_at = now
            signature = _file_signature(self.tokens_path)
            if signature == self.tokens_signature:
                return self.tokens
            try:
                table = load_token_table(self.tokens_path)
            except (ApiConfigError, OSError) as exc:
                logger.error("api/v1 tokens file reload failed; keeping the last good set: %s", exc)
                self.tokens_signature = signature
                return self.tokens
            self.tokens = table
            self.tokens_signature = signature
            logger.info("api/v1 tokens file reloaded (%d tokens)", len(table))
            return self.tokens


def _json(body: Mapping[str, Any], status: int = 200):
    from starlette.responses import JSONResponse

    return JSONResponse(dict(body), status_code=status, headers={"Cache-Control": "no-store"})


def _error(http_status: int, code: str, message: str, **extra: Any):
    return _json({"error": code, "message": message, **extra}, http_status)


def _bearer(request) -> Optional[str]:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token if 0 < len(token) <= 512 else None


def _admit_store(state: ApiState, request) -> Tuple[Optional[str], Optional[Any]]:
    """Steps 1-4 of the request order. Returns (store, None) or (None, error)."""
    store = request.path_params.get("store", "")
    token = _bearer(request)
    allowed = (
        state.current_tokens().get(hashlib.sha256(token.encode("utf-8")).hexdigest())
        if token is not None else None
    )
    if allowed is None:
        return None, _error(401, "bad_token", "missing or unknown bearer token")
    if not NAME_RE.match(store):
        return None, _error(404, "unknown_store", "unknown store")
    if store not in allowed:
        return None, _error(403, "store_forbidden", "this token does not allow the store")
    stores, registry_error = configured_stores()
    if registry_error is not None:
        logger.error("api: database registry unusable: %s", registry_error)
        return None, _error(500, "internal", "server configuration error")
    if store not in stores:
        return None, _error(404, "unknown_store", "unknown store")
    return store, None


def _check_project(state: ApiState, store: str, project: Optional[str]) -> None:
    declared = store_projects(store)
    if project is not None and declared and project not in declared:
        raise BadRequest(f"unknown_project: store {store!r} does not hold project {project!r}")


@contextlib.contextmanager
def _admission(state: ApiState):
    with state.lock:
        if state.inflight >= state.max_inflight:
            yield False
            return
        state.inflight += 1
    try:
        yield True
    finally:
        with state.lock:
            state.inflight -= 1


@contextlib.contextmanager
def _bound_store(store: str):
    """Bind the store for storage calls on this thread (CURRENT_DB)."""
    from .storage import CURRENT_DB

    stores, _err = configured_stores()
    registry_name = stores.get(store)
    token = CURRENT_DB.set(registry_name) if registry_name is not None else None
    try:
        yield
    finally:
        if token is not None:
            CURRENT_DB.reset(token)


def run_search(store: str, req: Dict[str, Any]) -> Dict[str, Any]:
    """Blocking search body (runs in a worker thread).

    hybrid_search_scored with follow="active" -- the same lineage default as
    the MCP search tools: superseded and retired memories are not returned.
    STRICTLY READ-ONLY (Phase 0 has no write path): no rebuild on a model
    mismatch (storage.SearchUnavailable -> 503), no repair of missing
    vectors (those rows are unscored and counted in "unscored").
    """
    from . import storage

    started = time.perf_counter()
    with _bound_store(store):
        # No schema setup either: it writes (CREATE/ALTER/INSERT OR IGNORE);
        # and a missing local database is never created by a read.
        from .backends import StoreLockedError, StoreMissingError

        try:
            conn = storage.connect_without_schema()
        except StoreMissingError as exc:
            raise storage.SearchUnavailable("store_missing", "no_database") from exc
        except StoreLockedError as exc:
            raise storage.SearchUnavailable("store_locked", str(exc)) from exc
        try:
            coverage: Dict[str, Any] = {}
            try:
                items = storage.hybrid_search_scored(
                    conn, req["query"], top_k=req["top_k"], min_score=0.0,
                    tags_any=req["tags_any"], follow="active", project=req["project"],
                    read_only=True, auto_rebuild=False, coverage=coverage,
                )
            except Exception as exc:
                if "no such table" in str(exc):
                    raise storage.SearchUnavailable("integrity_fault", "schema_missing") from exc
                raise
            model = storage._read_meta_keys(conn, ["embedding_model"]).get("embedding_model")
        finally:
            conn.close()
    results = []
    for rank, item in enumerate(items, start=1):
        mem = item["memory"]
        cosine = item.get("cosine")
        results.append({
            "id": mem["id"],
            "created_at": mem.get("created_at"),
            "tags": list(mem.get("tags") or []),
            "preview": (mem.get("content") or "")[: req["preview_chars"]],
            "cosine": round(float(cosine), 6) if cosine is not None else None,
            "fused": float(item["score"]),
            "rank": rank,
        })
    return {
        "count": len(results),
        "mode": SEARCH_MODE,
        "embedding_model": model or storage.EMBEDDING_MODEL,
        "results": results,
        "unscored": int(coverage.get("unscored", 0)),
        "took_ms": int(round((time.perf_counter() - started) * 1000)),
    }


def _health_body(state: ApiState, store: str, status: str) -> Dict[str, Any]:
    from . import __version__

    return {
        "status": status,
        "store": store,
        "version": __version__,
        "api_version": API_VERSION,
        "contract_version": CONTRACT_VERSION,
        "writes": writes_capability(store),
        "projects": list(store_projects(store)),
        "mode": SEARCH_MODE,
        "supervisor": supervisor_state(),
    }


def register_api_routes(mcp: Any, *, bind_host: str, env: Optional[Mapping[str, str]] = None) -> bool:
    """Register /api/v1/{store}/{health,search,absorb}. Returns whether it did.

    Fails closed (§3.3, leader decision msg 7057): the API always needs a
    tokens file. Without MEMORA_API_TOKENS_FILE the routes are NOT
    registered, on any bind address, loopback included, and an error is
    logged; so is an unreadable or unsafe tokens file, or an invalid
    MEMORA_PROJECTS. Every request needs a token that lists its store, from
    any address. bind_host is only logged.
    """
    from .storage import ProjectConfigError

    env = os.environ if env is None else env
    tokens_path = (env.get("MEMORA_API_TOKENS_FILE") or "").strip()
    if not tokens_path:
        logger.info(  # an intentional state: the deploy sets it only with a tokens file (issue 1131)
            "api/v1 NOT registered: MEMORA_API_TOKENS_FILE is unset (the API always requires "
            "store-scoped tokens; bind %s)", bind_host,
        )
        return False
    try:
        tokens = load_token_table(tokens_path)
        # A malformed MEMORA_PROJECTS fails closed at startup, not per request.
        for store in configured_stores()[0]:
            store_projects(store)
    except (ApiConfigError, ProjectConfigError, OSError) as exc:
        logger.error("api/v1 NOT registered: %s", exc)
        return False
    try:
        max_inflight = int(env.get("MEMORA_API_MAX_INFLIGHT") or DEFAULT_MAX_INFLIGHT)
    except ValueError:
        max_inflight = DEFAULT_MAX_INFLIGHT
    state = ApiState(
        tokens=tokens, tokens_path=os.path.normpath(tokens_path), max_inflight=max(1, max_inflight),
        tokens_signature=_file_signature(os.path.normpath(tokens_path)),
        tokens_checked_at=time.monotonic(),
    )
    mcp._memora_api_state = state  # for tests and diagnostics

    from .health import ensure_refresher, readiness_payload_async

    @mcp.custom_route("/api/v1/{store}/health", methods=["GET"])
    async def _api_health(request):
        store, err = _admit_store(state, request)
        if err is not None:
            return err
        ensure_refresher()
        stores, _ = configured_stores()
        probe_name = stores.get(store) or "(default)"
        payload = await readiness_payload_async(may_refresh=True)
        entry = (payload.get("databases") or {}).get(probe_name)
        if entry is not None and entry.get("status") == "ok" and not payload.get("too_stale"):
            return _json(_health_body(state, store, "ok"))
        # /health/db/<store> semantics: 503 when the store is not proven ok.
        # A store whose probe errored is "down"; one not yet proven, or whose
        # proof is too old, is "degraded".
        down = entry is not None and entry.get("status") == "error"
        body = _health_body(state, store, "down" if down else "degraded")
        if down:
            # The probe is read-only: it neither creates a missing local
            # database nor sets up a missing schema; say which it was.
            if entry.get("error") == "StoreMissingError":
                body["reason"], body["detail"] = "store_missing", "no_database"
            elif entry.get("error") == "StoreLockedError":
                body["reason"], body["detail"] = "store_locked", str(entry.get("message") or "store_locked")[:100]
            elif "no such table" in str(entry.get("message") or ""):
                body["reason"], body["detail"] = "integrity_fault", "schema_missing"
            else:
                body["reason"] = "probe_error"
        else:
            body["reason"] = "stale" if entry is not None else "unproven"
        return _json(body, 503)

    async def _read_body(request):
        """The request body, bounded at the ASGI receive boundary.

        Content-Length over MAX_BODY_BYTES is refused before anything is read.
        Otherwise each http.request message is counted as it is received and
        the read stops (413) as soon as the total passes the cap; an over-cap
        message is never kept. HONEST BOUND: what is held at once is at most
        MAX_BODY_BYTES plus ONE server-delivered message -- the server hands
        the body over in its own pieces, one transport read each, whose size
        depends on the event loop. memora-server pins uvicorn to http="h11",
        loop="asyncio": measured at <= 262144 bytes per message
        (scripts/measure_asgi_body_messages.py), so the peak is
        65536 + 262144 bytes per request.
        """
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > MAX_BODY_BYTES:
                    raise PayloadTooLarge()
            except ValueError:
                raise BadRequest("invalid Content-Length")
        chunks, total = [], 0
        while True:
            message = await request.receive()
            if message.get("type") == "http.disconnect":
                raise BadRequest("client disconnected")
            if message.get("type") != "http.request":
                continue
            body = message.get("body") or b""
            total += len(body)
            if total > MAX_BODY_BYTES:
                raise PayloadTooLarge()
            chunks.append(body)
            if not message.get("more_body", False):
                break
        raw = b"".join(chunks)
        try:
            return json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BadRequest("body is not valid JSON")

    def _too_large():
        return _error(413, "payload_too_large", f"request body exceeds {MAX_BODY_BYTES} bytes")

    @mcp.custom_route("/api/v1/{store}/search", methods=["POST"])
    async def _api_search(request):
        from .storage import SearchUnavailable

        store, err = _admit_store(state, request)
        if err is not None:
            return err
        # Bounded pre-admission work (at most MAX_BODY_BYTES read, then
        # validation): 413 and 400 never depend on load. Admission gates only
        # the execution.
        try:
            req = validate_search_request(await _read_body(request))
            _check_project(state, store, req["project"])
        except PayloadTooLarge:
            return _too_large()
        except BadRequest as exc:
            return _error(400, "bad_request", str(exc))
        with _admission(state) as admitted:
            if not admitted:
                return _error(429, "admission", "too many API requests in flight")
            try:
                # A worker thread: the event loop (and /health) stay responsive.
                body = await asyncio.to_thread(run_search, store, req)
            except SearchUnavailable as exc:
                if exc.reason == "model_mismatch":  # answers, but cannot be scored
                    return _error(503, "store_degraded",
                                  f"search cannot score this store ({exc.detail}); nothing was written",
                                  status="degraded", reason=exc.reason, detail=exc.detail)
                return _error(503, "store_unavailable",  # cannot serve at all
                              f"this store cannot serve reads ({exc.detail}); nothing was written or created",
                              status="down", reason=exc.reason, detail=exc.detail)
            except Exception:
                logger.exception("api/v1 search failed for store %s", store)
                return _error(500, "internal", "search failed")
        return _json(body)

    @mcp.custom_route("/api/v1/{store}/absorb", methods=["POST"])
    async def _api_absorb(request):
        store, err = _admit_store(state, request)
        if err is not None:
            return err
        try:  # bounded pre-admission work, as for search
            req = validate_absorb_request(await _read_body(request))
            _check_project(state, store, req["project"])
        except PayloadTooLarge:
            return _too_large()
        except BadRequest as exc:
            return _error(400, "bad_request", str(exc))
        with _admission(state) as admitted:
            if not admitted:
                return _error(429, "admission", "too many API requests in flight")
            executor = absorb_executor
            if writes_capability(store) != "transactional" or executor is None:
                # Never runs an absorb on a non-transactional store (§3.2).
                return _error(501, "writes_unsupported",
                              "this store is not on a transactional backend; nothing was written",
                              writes="unsupported")
            try:
                status, body = await asyncio.to_thread(executor, store, req)
            except Exception:
                logger.exception("api/v1 absorb failed for store %s", store)
                return _error(500, "internal", "absorb failed")
        return _json(body, status)

    from . import api_absorb

    global absorb_executor
    absorb_executor = api_absorb.execute  # only reached when the store is transactional
    logger.info("api/v1 registered on %s (%d tokens)", bind_host, len(tokens))
    return True
