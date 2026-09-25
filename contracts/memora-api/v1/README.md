# memora API v1 contract

Owned by memora. clmux vendors this directory (with a `SOURCE` file naming the
memora commit) into `src/clmux/testing/memora_api_v1/`, and its fake memora
server replays these fixtures. Design: clmux `docs/PLAN_MEMORA_DAEMON.md`
(commit 28523ab), §3.1-§3.5, §7.1. Implementation: `memora/api_v1.py`.

Contract version: see `VERSION` (also reported by `/health` as
`contract_version`). A backwards-compatible addition (a new optional request
field, a new response field clients may ignore, a new fixture) bumps the
minor version; anything that breaks an existing client is `/api/v2`.
Versioning starts at the first landed (tagged) memora release that carries
this directory: 1.0.0 is that first version, and changes made before it
landed are part of 1.0.0, not versions of their own.
A change to the README or to fixture data only, with no change to any
schema, route or behaviour, bumps the patch version.

- 1.0.1: docs and fixture data only. The example store names are neutral
  (`alpha`); no schema, route or behaviour changed. A 1.0.0 client needs no
  change.

## Routes

| Route | Request schema | Response schema |
|---|---|---|
| `GET /api/v1/{store}/health` | — | `health_response.json` (200, and its 503: a health document, not an error envelope); `error_response.json` for 401/403/404/500 |
| `POST /api/v1/{store}/search` | `search_request.json` | `search_response.json` (200); `error_response.json` for every non-2xx (400, 401, 403, 404, 413, 429, 500, 503) |
| `POST /api/v1/{store}/absorb` | `absorb_request.json` | `absorb_response.json` (200); `error_response.json` for every non-2xx (400, 401, 403, 404, 409, 413, 429, 500, 501) |

**One error envelope** for every non-2xx response except the health route's
503, which is a health document (`health_response.json`, with `status` and
`reason`). The envelope is `{"error": code, "message": text}` plus
route-specific fields: 501 `writes_unsupported` adds `"writes":
"unsupported"`; the search 503s add `"status"`, `"reason"` and `"detail"`
(below). This is a deliberate simplification of the design
note, whose §3.1 gave 409 and 501 a `{"status": ...}` body (leader decision,
msg 7057; the note is being amended). Clients switch on `error`, never on
`message`.

`{store}` matches `[a-z0-9_-]{1,64}`: a name in the server's
`MEMORA_DATABASES`, or `default` on a single-store server. Every response has
`Cache-Control: no-store`. These are plain HTTP routes: no MCP session is
created, used or held.

## Request order (every route)

1. **401 `bad_token`**: the server has a tokens file and the
   `Authorization: Bearer <token>` header is missing or unknown.
2. **404 `unknown_store`**: `{store}` is not a valid store name.
3. **403 `store_forbidden`**: the token does not list `{store}`.
4. **404 `unknown_store`**: `{store}` is not configured on this server.
5. **413 `payload_too_large`** (search, absorb): the body exceeds 65536
   bytes -- from `Content-Length` before anything is read, else counted at
   the ASGI receive boundary, message by message, stopping at the first
   message past the cap (an over-cap message is never kept). Peak buffering
   per request is at most 65536 bytes plus ONE server-delivered message,
   which is one transport read whose size depends on the event loop.
   `memora-server` pins uvicorn to `http="h11"`, `loop="asyncio"`
   (`h11_max_incomplete_event_size=16384` bounds the request line and
   headers, not body messages); with that loop the largest message measured
   was 262144 bytes, on CPython 3.12.8 (the container's minor version) and
   3.13.1 with uvicorn 0.42.0: `scripts/measure_asgi_body_messages.py` serves
   a recording ASGI app with those exact options and sends it a 4 MiB
   chunked body over loopback.
6. **400 `bad_request`** (search, absorb): invalid JSON, a missing or unknown
   field, a value out of range, or a `project` the store does not declare
   (message starts `unknown_project:`).
7. **429 `admission`** (search, absorb): too many API requests executing
   (`MEMORA_API_MAX_INFLIGHT`, default 8). Transient: retry with backoff.
   Admission gates only the execution; steps 5-6 are bounded work that
   never depends on load, so 413 and 400 are never masked by a 429.
8. The route's result: 200; for search **503 `store_degraded` |
   `store_unavailable`**; for absorb **501 `writes_unsupported`** / **409
   `in_progress` | `key_conflict`**; **500 `internal`** on a server fault
   (details only in the server log).

## Auth

- `MEMORA_API_TOKENS_FILE` (absolute path) holds a JSON object mapping the
  lowercase hex `sha256(token)` to a non-empty list of store names, for
  example `{"ad1e…7823": ["memora", "alpha"]}`. The file never holds a token.
- The API **always** needs a tokens file. Without `MEMORA_API_TOKENS_FILE`
  the routes are not registered on any bind address, loopback included, and
  startup logs at INFO (issue 1131) -- an intentional state, not an error.
  Every request needs a token that lists the store, from any address. Local
  smoke tests use a scratch tokens file.
- The tokens file is opened `O_RDONLY | O_NOFOLLOW | O_CLOEXEC` and checked on
  the descriptor: a regular file, owned by the server's effective uid, no
  group/other permission bits, at most 4 KiB. Every parent directory is
  checked with `lstat` (a symlink is refused, and none may be group- or
  world-writable). For a file under `$HOME`, parents up to `$HOME` must be
  owned by the server's user. For any other file (a container secret),
  parents up to `/` must be owned by that user or root, so `/tmp` (world-
  writable) is refused. **When the file sits on a read-only mount** (the
  deploy's secrets mount, where rootful docker reads the operator's file as
  root; `statvfs` reports `ST_RDONLY`), the owner matches -- the file's and
  its read-only parents' -- are not required; every other check still
  applies. At startup, any failure leaves the routes unregistered.
- **Rotation without a restart:** the file is re-read when its mtime, size or
  inode changes, checked at most once per second. A reload that fails any
  check or validation keeps the last good token set and logs an error.

## Health

- 200 with `status: "ok"` when the store's readiness probe (the same probe as
  `/health/db/{store}`) is fresh and succeeded. The probe is read-only: one
  `SELECT 1 FROM memories LIMIT 1` on a connection that runs no schema setup
  and, for a local SQLite store, follows the read-only guarantee below.
  Otherwise 503 with `status: "down"` -- `reason: "store_missing"`
  (`detail: "no_database"`), `"store_locked"` (`detail:
  "wal_sidecars_incomplete"`, `"wal_shm_unusable"` or
  `"store_locked_or_unreadable"`), `"integrity_fault"` (`detail:
  "schema_missing"`), or `"probe_error"` (any other failure) -- or
  `status: "degraded"` (`reason: "unproven"`, not probed yet, or `reason:
  "stale"`, the last proof is too old).
- `writes` is `"transactional"` (absorb allowed) or `"unsupported"`. Phase 0
  reports `"unsupported"` for every store: no backend runs absorb as one
  fenced transaction yet (Phase L). `health_transactional` documents the
  Phase L shape.
- `projects` lists the projects the store declares (`MEMORA_PROJECTS`); empty
  means none are declared.
- `supervisor` is the Phase L lock supervisor's state, `null` until it exists.

**Client binding rules (Phase 1).** `transactional` → writes allowed.
`unsupported` → jobs held (`held_no_tx`). A 503 → the store is down: skip
reads and back off writes, and keep the last known `writes` value, so a 503
never changes the capability. A 200 whose `writes` is missing or unknown →
treat as `unsupported`.

## Search

**Strictly read-only** (Phase 0 has no write path): only SELECTs, on a
connection that runs no schema setup and, for a local SQLite store, opens it
read-only. It never rebuilds embeddings on a model mismatch and never repairs
a missing vector:

- A row with no vector (or a certified-empty one) is simply not scored;
  `unscored` counts them. A coverage signal for the daemon, not an error.
- **503 `store_degraded`**, `"status": "degraded"`, `"reason":
  "model_mismatch"`: the store answers but its vectors cannot be scored
  against the current model (a model or representation mismatch, mixed
  encodings, a rebuild in progress, or no model recorded yet; `detail` says
  which, e.g. `model_or_representation_mismatch`,
  `embedding_model_unrecorded`). A normal (MCP) search or
  `memory_rebuild_embeddings` repairs it.
- **503 `store_unavailable`**, `"status": "down"`: the store cannot serve
  reads -- `"reason": "integrity_fault"` (e.g. `detail: "schema_missing"`, or
  a fault no rebuild repairs), `"store_missing"` (`detail: "no_database"`)
  or `"store_locked"` (below). Nothing is written or created.

**Read-only guarantee (local SQLite stores; memora's own writers).** Reads
never create files -- no directory, database, `-wal` or `-shm`; a read may
instead be refused (`store_locked`). memora is the single writer of its
local stores, and inside the server process every writer connection's open
and close takes the exclusive side of a per-store reader-writer lock while a
read holds the shared side from its open to its close, so no in-process
writer opens or closes during a read. Under that lock: a rollback-journal
database (memora's own format) opens `mode=ro`. A WAL database (per its
header) with an in-process writer open reads `mode=ro` through that
writer's `-wal`/`-shm` (which exist while it is open), so it sees committed
data; with no in-process writer and no sidecars it opens
`mode=ro&immutable=1` -- SQLite then takes no locks and detects no changes,
which is safe only because the lock keeps any in-process writer from
starting during the read. Incomplete sidecars (`wal_sidecars_incomplete`), a
`-shm` the server's user cannot use (`wal_shm_unusable`), or any other open
or lock failure (`store_locked_or_unreadable`) are refused.
**Out of scope:** writers in another process. An external writer to a local
store can defeat both the no-create guarantee (by closing, which deletes the
sidecars, between the check and the open) and immutable correctness (a
file changing under an immutable read can give stale or torn results).
- Clients switch on `reason`; `detail` is diagnostic.
- Memory: the read-only snapshot is loaded once per store (concurrent cold
  searches wait for one load) and held in the server's corpus cache under
  its byte budget (`MEMORA_CORPUS_CACHE_BUDGET_MB`); a complete one is shared
  with the MCP tools.

- `mode: "hybrid-v1"`: semantic (cosine) and keyword legs combined by
  rank fusion, with `follow="active"`, so superseded and retired memories are
  never returned.
- `fused` is the result order (about 0 to 0.08). It is a ranking, **not** a
  relevance score, and must never be thresholded.
- `cosine` is the semantic leg's raw cosine similarity, and **the only relevance
  signal**. It is `null` when the hit came from the keyword leg only.
- `rank` is the 1-based position in `results`, in `fused` order.
- `embedding_model` is the store's recorded embedding model. A change means
  cosine calibration no longer holds.
- `project` restricts results to memories explicitly in that project: a
  `metadata.project` equal to it, or a tag equal to it or prefixed
  `<project>/`. The filter applies before top-k in both legs. It is never
  inferred from content (memora issue #47).
- `preview` is the first `preview_chars` characters of the memory (default
  200, at most 400).

## Absorb and idempotency

`project` is required: the daemon sends the workspace's project. It must be
one of the store's declared projects when the store declares any. On a store
whose health reports `writes: "unsupported"`, absorb returns **501**
`{"error": "writes_unsupported", "message": ..., "writes": "unsupported"}`
after validation and never runs.

On a transactional store (Phase L; §3.2 of the design):

- `idempotency_key` is scoped to the store. `request_sha256` is the sha256 of
  the validated request, with defaults applied and absent optional fields as
  `null`, serialised as JSON with sorted keys and no whitespace
  (`memora.api_v1.canonical_request_sha256`).
- The first request with a key claims it and runs the absorb. **Only a stored
  `done` row with its response counts as completion.** That row is written in
  the same transaction as every effect of the absorb, and before the 200. A
  pure skip goes through the same path.
- The same key with the same `request_sha256` after completion replays the
  stored response: 200, the same body.
- The same key while a live claim runs gets **409** `{"error":
  "in_progress", ...}`. This is transient: retry later.
- The same key with a different `request_sha256` gets **409** `{"error":
  "key_conflict", ...}`. This is a client bug and must not be retried.
- A claim whose absorb failed before its transaction committed left no
  effects, and a retry runs it again.

## Fixtures

`fixtures/<name>.json`:

```
{"name", "description",
 "request":  {"method", "path", "headers", "body"?},
 "response": {"status", "body"},
 "request_schema", "response_schema",   # file names in schemas/ (request_schema may be null)
 "volatile": [field, ...],              # top-level response fields whose values vary (presence and type are still checked)
 "replay":   {...},                     # how memora's own tests reproduce it with stubs; clients ignore it
 "live":     null | {"compare": "exact"|"schema", "store"?}}
```

- The test token is `memora-api-v1-test-token`, and its sha256 is
  `ad1e1c54d5de91f836f918da0c800e77a08b0bcddae8f953b139c05db53d7823`. In the
  fixtures it may use the stores `memora` and `nostore`, and not `alpha`.
- `live` says whether a real Phase 0 server on a scratch store produces the
  fixture: `exact` (the body matches, apart from `volatile`), `schema` (the
  status and schema match; the data differs), or `null` (not producible
  before Phase L, or only with stubs).
- `manifest.json` lists the sha256 of every file here except itself. Check it
  with `scripts/memora_api_contract.py validate`.
