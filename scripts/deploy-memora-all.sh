#!/usr/bin/env bash
# Full deploy of the live memora-all container (nuc8) to v0.4.6: fetch +
# build the tagged image and recreate the container from it, then verify it.
#
# What v0.4.6 changes (see CHANGELOG.md "0.4.6"):
#  - Absorb never supersedes across memory types (a plain fact can no longer
#    retire an open todo or issue); a reused supersede verdict is re-checked
#    when the leaf's type, project or stored vector changed.
#  - Typed tags (<project>/issues, todos, ...) are no longer project
#    evidence; they follow the memory's project once one is declared.
#  - A plain JSON API /api/v1/<store>/{health,search,absorb} (Phase 0 of the
#    clmux memora daemon; contract memora-api-v1.0.0). It is registered ONLY
#    when MEMORA_API_TOKENS_FILE is set. THIS DEPLOY DOES NOT SET IT: the API
#    stays unregistered on memora-all, and step 5 checks that it is (an actual
#    HTTP 404 on /api/v1/memora/health), so an accidental registration -- or
#    a server that stopped answering -- fails the deploy.
#  - memora-server now pins uvicorn to http=h11, loop=asyncio (explicit
#    instead of "auto"); readiness probes (/health/db) no longer run schema
#    setup and never create a database.
#
# NO ENV CHANGE: MEMORA_PROJECTS is already set (v0.4.5) and re-set to the
# same value below; MEMORA_API_TOKENS_FILE is NOT set; MEMORA_LLM_MODEL stays
# openai/gpt-4o-mini (step 2 re-writes the same value, a confirming no-op);
# MEMORA_CORPUS_CACHE_BUDGET_MB stays unset. No schema change.
#
# Steps, all on nuc8:
#  1. git fetch + checkout the v0.4.6 tag in the nuc8 checkout, docker build.
#     The image currently tagged memora:latest is kept as memora:rollback-<ts>
#     before the new one replaces it.
#  2. Edit MEMORA_LLM_MODEL in ~/.config/memora/credentials.mcp.json (already
#     openai/gpt-4o-mini -- a confirming no-op; backup kept).
#  3. Preflight, before any destructive step (unchanged from v0.4.5): validate
#     MEMORA_PROJECTS with the new image, and a read-only count of rows whose
#     metadata contains "import_attempt" in each live store (the startup
#     sweep would complete or remove them). Either failing aborts with the
#     old container untouched and still serving.
#  4. Recreate memora-all -- same image tag, ports, cpu limit, restart policy
#     and env as the v0.4.5 deploy, except (L2a) the named /data volume,
#     MEMORA_DATA_VOLUME, MEMORA_ADMIN_TOKEN and --memory 960m (see below).
#     Old container kept stopped
#     as memora-all-grok-<ts> (the name predates the model switch being a
#     no-op; it still means "the container before this deploy", and the
#     rollback commands below depend on it).
#  5. Wait for GET /health, check it reports version 0.4.6 (proves the new
#     build is the one serving, not a stale image), then run one 3-fact
#     dry-run memory_absorb call, one memory_semantic_search call and one
#     memory_stats call, asserting no JSON-RPC error and a real session id at
#     initialize, no JSON-RPC error / isError at each tools/call (a JSON-RPC
#     error rides HTTP 200 -- an HTTP-status-only check would print and exit
#     zero on a server that answers but can't actually serve requests), an
#     absorb result with a "decisions" list and a "profile" field, a search
#     result with a "results" list and a "profile" field, and a stats result
#     with an integer "import_pending" field. Then EVERY store in
#     MEMORA_DATABASES (not only the default one the calls above use; /health
#     itself touches no database): /health/db/<store> must reach a current
#     (not stale) 200 ok within 90 s, and memory_stats over /mcp/<store> must
#     report that store as its bound database, with an integer
#     import_pending. Any store failing is named and fails the deploy.
#     ("pi" in the memora project list is a tag project inside the memora
#     store, not a store; pi agents have no MCP config.) Finally, GET
#     /api/v1/memora/health must be an actual HTTP 404 (the API is not
#     registered without a tokens file); no HTTP answer within ~20 s, or any
#     other status, fails the deploy.
#
# /data VOLUME (local-primary L2a): memora-all used to reuse its existing data
# volume by id. The image declares VOLUME /data, so that id is normally an
# ANONYMOUS (64-hex) volume, and the server now refuses to serve a store that
# keeps state under /data from one (memora/data_volume.py). This deploy mounts
# the NAMED volume memora-all-data instead and passes MEMORA_DATA_VOLUME.
# Whenever memora-all still mounts another volume, after memora-all is
# stopped, it copies that volume into memora-all-data (scripts/migrate_data_volume.sh: staged, verified,
# swapped in; the marker records the source volume and its content digest,
# so a changed or different source is recopied). It refuses to mount a
# volume whose name is 64-hex, and refuses to copy a source a running
# container still uses. The old container (renamed,
# stopped) keeps its old volume, so the rollback below is unchanged; writes
# made to /data after the switch are not in the old volume.
#
# MEMORY: --memory 960m (was 768m), the local-primary memory gate:
# max(768m, 1.5 x measured peak RSS), scripts/measure_memory_gate.py; see
# docs/local-primary-implementation.md §8 L2a.
#
# ADMIN TOKEN (local-primary §9 (a)): /admin/* routes take MEMORA_ADMIN_TOKEN,
# never the health token. It is read from ~/.config/memora/all.admin-token on
# nuc8 (minted there on first use, 48 alphanumerics, 0600) and must differ
# from the health token.
#
# HARDENED (queue item 23 follow-up, sealed review msg 5698/5699): the
# credentials-env parser used to stream straight into the while loop via
# `done < <(python3 ...)` — a parser failure inside a process substitution
# is invisible to both the while loop and set -e, so ENV_ARGS could
# silently end up empty while the script still stopped/renamed/recreated
# the live container. Same root cause as switch-embedding-host.sh's own
# 2026-09-16 incident (see that script's header), caught here by review
# before ever running. Now captured to a variable and checked (exit status
# + non-empty) before any destructive step; the health-wait loop's
# `$(seq ...)` also replaced with shell arithmetic.
#
# NOT RUN by this repo or any agent — review and run it yourself:
#   scripts/deploy-memora-all.sh
#
# Rollback:
#   ssh nuc8 'docker rm -f memora-all && docker rename memora-all-grok-<ts> memora-all && docker start memora-all'
#   ssh nuc8 'docker tag memora:rollback-<ts> memora:latest'   # only if the image itself needs reverting too
#   restore ~/.config/memora/credentials.mcp.json.bak-llm-<ts> if MEMORA_LLM_MODEL itself needs reverting
set -euo pipefail

TAG="v0.4.6"

# MEMORA_DATABASES names a Cloudflare account + database ids — read from the
# git-ignored instance config rather than written into this (public) script.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/instances/all.env"
[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE — need MEMORA_DATABASES for memora-all" >&2; exit 1; }
MEMORA_DATABASES="$(grep -E "^MEMORA_DATABASES=" "$ENV_FILE" | head -1 | cut -d= -f2- | sed "s/^'//;s/'\$//")"
[ -n "$MEMORA_DATABASES" ] || { echo "$ENV_FILE has no MEMORA_DATABASES" >&2; exit 1; }
# base64 over the wire: the JSON has embedded quotes that ssh's remote
# command re-join would otherwise mangle.
MEMORA_DATABASES_B64="$(printf '%s' "$MEMORA_DATABASES" | base64 | tr -d '\n')"
# The /data migration program (shared with memora-instance.sh), sent from
# THIS checkout: the nuc8 checkout is at $TAG and may predate it.
MIGRATE_B64="$(base64 < "$ROOT/scripts/migrate_data_volume.sh" | tr -d '\n')"

ssh nuc8 bash -s -- "$TAG" "$MEMORA_DATABASES_B64" "$MIGRATE_B64" <<'REMOTE'
set -euo pipefail
TAG="$1"
MEMORA_DATABASES="$(printf '%s' "$2" | base64 -d)"
MIGRATE_SCRIPT="$(printf '%s' "$3" | base64 -d)"
[ -n "$MIGRATE_SCRIPT" ] || { echo "empty /data migration program" >&2; exit 1; }
TS=$(date +%s)
# Keyed by registry store name (MEMORA_DATABASES); see the header for why.
MEMORA_PROJECTS='{"memora":["memora","clmux","acebar","pi"],"ob1":["ob1"],"bestation":["bestation"],"re":["re"]}'

REPO=~/repos/agentic-box/memora
[ -d "$REPO" ] || { echo "missing $REPO checkout on nuc8" >&2; exit 1; }
git -C "$REPO" fetch origin
git -C "$REPO" checkout "$TAG"

# Keep the currently-running image for rollback before building over it.
docker tag memora:latest "memora:rollback-$TS" 2>/dev/null || true
docker build -t memora:latest "$REPO"

CRED=~/.config/memora/credentials.mcp.json
[ -f "$CRED" ] || { echo "missing $CRED" >&2; exit 1; }
cp -p "$CRED" "$CRED.bak-llm-$TS"

python3 - "$CRED" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
env = d["mcpServers"]["memora"]["env"]
before = env.get("MEMORA_LLM_MODEL")
env["MEMORA_LLM_MODEL"] = "openai/gpt-4o-mini"
json.dump(d, open(p, "w"), indent=2)
print(f"MEMORA_LLM_MODEL: {before!r} -> 'openai/gpt-4o-mini' (backup kept alongside)")
PY

HEALTH_TOKEN_FILE=~/.config/memora/all.health-token
[ -f "$HEALTH_TOKEN_FILE" ] || { echo "missing $HEALTH_TOKEN_FILE — refusing to mint a new one for a live container" >&2; exit 1; }
HEALTH_TOKEN=$(cat "$HEALTH_TOKEN_FILE")

# Admin token: read, or mint once. Same shape as the health token (48
# alphanumerics, no newline); an unusable existing file is refused, not
# replaced, because a script may already hold it.
ADMIN_TOKEN_FILE=~/.config/memora/all.admin-token
if [ ! -e "$ADMIN_TOKEN_FILE" ] && [ ! -L "$ADMIN_TOKEN_FILE" ]; then
  tmp="$(mktemp ~/.config/memora/.admin-token.XXXXXX)"
  chmod 600 "$tmp"
  ( set +o pipefail; LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 48 ) > "$tmp"
  mv -f "$tmp" "$ADMIN_TOKEN_FILE"
  echo "minted $ADMIN_TOKEN_FILE"
fi
# An existing token file must be a regular file (not a symlink), owned by
# this user, mode 0600. Otherwise it may have been exposed: refuse, never
# chmod it into shape; removing it lets the next run mint a new value.
python3 - "$ADMIN_TOKEN_FILE" <<'PY' || { echo "$ADMIN_TOKEN_FILE must be a regular file owned by $(id -un) with mode 0600; it may have been exposed: rm it and rerun to mint a new token" >&2; exit 1; }
import os, stat, sys
st = os.lstat(sys.argv[1])
ok = stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) == 0o600
sys.exit(0 if ok else 1)
PY
ADMIN_TOKEN=$(cat "$ADMIN_TOKEN_FILE")
[ "${#ADMIN_TOKEN}" -eq 48 ] && [ -z "$(printf '%s' "$ADMIN_TOKEN" | LC_ALL=C tr -d 'A-Za-z0-9')" ] \
  || { echo "$ADMIN_TOKEN_FILE is not 48 alphanumerics — fix or remove it" >&2; exit 1; }
[ "$ADMIN_TOKEN" != "$HEALTH_TOKEN" ] || { echo "admin token equals the health token — remove $ADMIN_TOKEN_FILE" >&2; exit 1; }

# /data: the NAMED volume memora-all-data (see the header). OLD_VOLUME is what
# the running container mounts at /data today; it is copied after the stop.
DATA_VOLUME=memora-all-data
OLD_VOLUME=$(docker inspect memora-all --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}')
[ -n "$OLD_VOLUME" ] || { echo "could not read memora-all's /data volume" >&2; exit 1; }
# Created and checked BEFORE the stop, so a runtime that cannot create or
# name it fails with memora-all still serving.
docker volume inspect "$DATA_VOLUME" >/dev/null 2>&1 || docker volume create "$DATA_VOLUME" >/dev/null
MOUNTED=$(docker volume inspect "$DATA_VOLUME" --format '{{.Name}}')
if [ "$MOUNTED" != "$DATA_VOLUME" ] || printf '%s' "$MOUNTED" | grep -Eqx '[0-9a-f]{64}'; then
  echo "volume $DATA_VOLUME resolved to '$MOUNTED' — refusing to mount it at /data" >&2; exit 1
fi

# Captured to a variable FIRST, not streamed straight into the while loop
# via process substitution (`done < <(python3 ...)`) — a parser failure
# inside a process substitution is invisible to both the while loop and
# set -e, so ENV_ARGS could silently end up empty and the script would
# still stop/rename/recreate the live container on the next lines (the
# same failure class as the 2026-09-16 incident this script's sibling
# switch-embedding-host.sh already post-mortems). Check exit status AND
# non-empty output explicitly, before any destructive step.
ENV_LINES="$(python3 -c "
import json
env = json.load(open('$CRED'))['mcpServers']['memora']['env']
for k, v in env.items():
    if v != '':
        print(f'{k}={v}')
")" || { echo "credentials parser failed — aborting before touching the live container" >&2; exit 1; }
[ -n "$ENV_LINES" ] || { echo "credentials parser produced no output — aborting before touching the live container" >&2; exit 1; }

ENV_ARGS=()
while IFS='=' read -r key value; do
  [ -z "$key" ] && continue
  case "$key" in
    MEMORA_STORAGE_URI|MEMORA_DB_PATH|MEMORA_DATABASES|MEMORA_DEFAULT_DB|MEMORA_PROJECTS|MEMORA_DATA_VOLUME|MEMORA_ADMIN_TOKEN) continue ;;
  esac
  ENV_ARGS+=(-e "$key=$value")
done <<< "$ENV_LINES"

# Preflight 1: MEMORA_PROJECTS parses with the NEW image's own validator
# (the server refuses to start on a malformed value), and names exactly the
# registry's stores.
docker run --rm -e "MEMORA_PROJECTS=$MEMORA_PROJECTS" -e "MEMORA_DATABASES=$MEMORA_DATABASES" \
  memora:latest python -c '
import json, os, sys
from memora.storage import load_projects_config
projects = load_projects_config()
stores = set(json.loads(os.environ["MEMORA_DATABASES"]))
if not isinstance(projects, dict) or set(projects) != stores:
    sys.exit(f"MEMORA_PROJECTS stores {sorted(projects or [])} != MEMORA_DATABASES stores {sorted(stores)}")
print("MEMORA_PROJECTS ok:", json.dumps(projects, sort_keys=True))
' || { echo "MEMORA_PROJECTS preflight failed — aborting before touching the live container" >&2; exit 1; }

# Preflight 2 (READ-ONLY): the startup sweep (since v0.4.5) completes or removes
# rows whose metadata carries an import_attempt marker. None should exist
# (no released memora wrote the key), but a caller could have set it. Count,
# per live store, rows whose metadata contains the string at all (a superset
# of real markers), through the running (previous) container: a raw backend
# connection, so no schema pass -- one SELECT per store. Any hit, or a
# failed check, aborts before anything is stopped.
docker exec -i memora-all python - <<'PY' || { echo "import_attempt preflight failed — aborting before touching the live container" >&2; exit 1; }
import json, os, sys
from memora import storage
bad = {}
for name in json.loads(os.environ["MEMORA_DATABASES"]):
    conn = storage.backend_for(name).connect()
    try:
        rows = conn.execute(
            "SELECT id FROM memories WHERE instr(metadata, ?) > 0 LIMIT 20", ('"import_attempt"',)
        ).fetchall()
    finally:
        conn.close()
    ids = [int(r[0]) for r in rows]
    print(f"{name}: {len(ids)} row(s) with import_attempt in metadata")
    if ids:
        bad[name] = ids
if bad:
    sys.exit(f"rows the startup sweep could complete or remove: {bad} -- inspect them first")
PY

docker stop memora-all

# Copy the old /data into the named volume while memora-all is stopped
# (scripts/migrate_data_volume.sh: staged into /to/.memora-staging, verified
# by digest, then swapped in; live content is moved aside, never overlaid).
# Its marker records the SOURCE volume and the source's content digest, so
# the copy is skipped only when memora-all-data already holds this exact
# source unchanged. After a rollback (memora-all back on OLD, accruing
# writes) the next deploy recopies. Skipped entirely once memora-all itself
# mounts memora-all-data.
if [ "$OLD_VOLUME" != "$DATA_VOLUME" ]; then
  if [ -n "$(docker ps -q --filter "volume=$OLD_VOLUME")" ]; then
    echo "a running container still uses $OLD_VOLUME — refusing to copy it; memora-all is stopped, restart it with: docker start memora-all" >&2
    exit 1
  fi
  docker run --rm -v "$OLD_VOLUME:/from:ro" -v "$DATA_VOLUME:/to" memora:latest \
    sh -c "$MIGRATE_SCRIPT" migrate_data_volume migrate "$OLD_VOLUME" \
    || { echo "copy $OLD_VOLUME -> $DATA_VOLUME failed — memora-all is stopped, restart it with: docker start memora-all" >&2; exit 1; }
fi

docker rename memora-all "memora-all-grok-$TS"

docker run -d --name memora-all \
  --restart unless-stopped \
  --memory 960m --cpus 4 \
  -p 0.0.0.0:8920:8000 \
  -v "$DATA_VOLUME:/data" \
  -e "MEMORA_DATA_VOLUME=$DATA_VOLUME" \
  -e "MEMORA_TOOL_PROFILE=leader" \
  -e "MEMORA_HEALTH_TOKEN=$HEALTH_TOKEN" \
  -e "MEMORA_ADMIN_TOKEN=$ADMIN_TOKEN" \
  -e "MEMORA_HEALTH_TIMEOUT=30" \
  -e "MEMORA_HEALTH_REFRESH_INTERVAL=15" \
  -e "MEMORA_VECTOR_SCAN_PAGE_SIZE=100" \
  -e "MEMORA_ALLOW_ANY_TAG=1" \
  -e "MEMORA_LOG_LEVEL=INFO" \
  -e "MEMORA_DATABASES=$MEMORA_DATABASES" \
  -e "MEMORA_DEFAULT_DB=memora" \
  -e "MEMORA_PROJECTS=$MEMORA_PROJECTS" \
  "${ENV_ARGS[@]}" \
  memora:latest

echo "memora-all recreated from $TAG (MEMORA_PROJECTS set; MEMORA_LLM_MODEL=openai/gpt-4o-mini unchanged, MEMORA_LOG_LEVEL=INFO, corpus cache budget default 384 MB)"
echo "old container kept stopped as memora-all-grok-$TS; old image kept as memora:rollback-$TS"
echo "rollback: docker rm -f memora-all && docker rename memora-all-grok-$TS memora-all && docker start memora-all"

echo "waiting for /health..."
healthy=0
for ((i = 1; i <= 30; i++)); do
  if curl -sf -m 3 http://127.0.0.1:8920/health >/dev/null 2>&1; then
    healthy=1
    echo "healthy after about $((i * 2))s"
    break
  fi
  sleep 2
done
if [ "$healthy" -ne 1 ]; then
  echo "still not healthy after ~60s — check: docker logs memora-all" >&2
  exit 1
fi

python3 - "${TAG#v}" "$MEMORA_DATABASES" "$HEALTH_TOKEN" <<'PY'
import json, sys, time, urllib.error, urllib.request

EXPECTED_VERSION = sys.argv[1]
STORES = list(json.loads(sys.argv[2]))
HEALTH_TOKEN = sys.argv[3]
ROOT = "http://127.0.0.1:8920"
BASE = f"{ROOT}/mcp/memora"

# The version the RUNNING process reports -- a stale image or a failed
# rebuild would still answer /health, just with the old version.
with urllib.request.urlopen("http://127.0.0.1:8920/health", timeout=10) as resp:
    health = json.loads(resp.read().decode())
if health.get("version") != EXPECTED_VERSION:
    print(f"/health reports version {health.get('version')!r}, expected {EXPECTED_VERSION!r}", file=sys.stderr)
    sys.exit(1)
print(f"/health reports version {EXPECTED_VERSION}")
HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

def _post(body, session_id=None, base=BASE):
    headers = dict(HEADERS)
    if session_id:
        headers["mcp-session-id"] = session_id
    req = urllib.request.Request(base, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        sid = resp.headers.get("mcp-session-id")
        raw = resp.read().decode()
    return sid, raw

def _parse_sse(raw):
    for line in raw.splitlines():
        if line.startswith("data: "):
            return json.loads(line[len("data: "):])
    return json.loads(raw)

# A JSON-RPC error rides HTTP 200 — urllib/curl's own status checks never
# see it. Check the envelope's own "error" key and, for initialize, that a
# session id actually came back; a smoke test that only checks HTTP status
# would print and exit zero on a server that answers but can't actually
# serve requests.
def _initialize(base):
    sid, init_raw = _post({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "deploy-check", "version": "0"}},
    }, base=base)
    init_result = _parse_sse(init_raw)
    if "error" in init_result:
        print(f"initialize at {base} returned a JSON-RPC error: {init_result['error']}", file=sys.stderr)
        sys.exit(1)
    if not sid:
        print(f"initialize at {base} succeeded but no mcp-session-id header was returned", file=sys.stderr)
        sys.exit(1)
    _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session_id=sid, base=base)
    return sid


sid = _initialize(BASE)

def _tool_dict(tool_result, name):
    """The tool's result dict: FastMCP sends it as JSON text content (and as
    structuredContent["result"]). Exits on a missing dict or an error key."""
    out = None
    for item in tool_result.get("content") or []:
        if item.get("type") == "text":
            try:
                out = json.loads(item["text"])
            except ValueError:
                pass
            break
    if out is None:
        out = (tool_result.get("structuredContent") or {}).get("result")
    if not isinstance(out, dict) or "error" in out:
        print(f"{name} returned no result dict or an error: {json.dumps(tool_result)[:2000]}", file=sys.stderr)
        sys.exit(1)
    return out


def _call_tool(req_id, name, arguments, session=None, base=BASE):
    t0 = time.time()
    _, raw = _post({
        "jsonrpc": "2.0", "id": req_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }, session_id=session or sid, base=base)
    elapsed = time.time() - t0
    result = _parse_sse(raw)
    if "error" in result:
        print(f"{name} tools/call returned a JSON-RPC error: {result['error']}", file=sys.stderr)
        sys.exit(1)
    tool_result = result.get("result", {})
    if tool_result.get("isError"):
        print(f"{name} reported isError=true: {json.dumps(tool_result)[:2000]}", file=sys.stderr)
        sys.exit(1)
    return _tool_dict(tool_result, name), elapsed


def _require_profile(name, out):
    profile = out.get("profile")
    if not isinstance(profile, dict) or "total_requests" not in profile:
        print(f"{name} result lacks the profile field: {json.dumps(out)[:2000]}", file=sys.stderr)
        sys.exit(1)
    return profile


facts = [
    "deploy-check fact one about the v0.4.6 rollout",
    "deploy-check fact two about the v0.4.6 rollout",
    "deploy-check fact three about the v0.4.6 rollout",
]
absorb, elapsed = _call_tool(2, "memory_absorb", {"facts": facts, "dry_run": True})
# Not one decision per fact: near-identical facts may be consolidated.
if not isinstance(absorb.get("decisions"), list) or not absorb["decisions"]:
    print(f"memory_absorb result has no decisions: {json.dumps(absorb)[:2000]}", file=sys.stderr)
    sys.exit(1)
profile = _require_profile("memory_absorb", absorb)
print(f"3-fact dry-run absorb via memory store: {elapsed:.1f}s "
      f"({profile['total_requests']} {profile.get('request_unit', 'requests')}, "
      f"server-side {profile['total_seconds']}s)")
print("actions:", [d.get("action") for d in absorb["decisions"]])

search, elapsed = _call_tool(3, "memory_semantic_search", {"query": "memora deploy", "top_k": 3})
if not isinstance(search.get("results"), list):
    print(f"memory_semantic_search result has no results list: {json.dumps(search)[:2000]}", file=sys.stderr)
    sys.exit(1)
profile = _require_profile("memory_semantic_search", search)
print(f"semantic search via memory store: {elapsed:.1f}s, {len(search['results'])} results "
      f"({profile['total_requests']} {profile.get('request_unit', 'requests')}, "
      f"server-side {profile['total_seconds']}s)")

stats, elapsed = _call_tool(4, "memory_stats", {})
pending = stats.get("import_pending")
if not isinstance(pending, int) or isinstance(pending, bool):
    print(f"memory_stats has no integer import_pending field: {json.dumps(stats)[:2000]}", file=sys.stderr)
    sys.exit(1)
print(f"memory_stats via memory store: {elapsed:.1f}s, {stats.get('total_memories')} memories, "
      f"import_pending={pending}")

# EVERY store, not just the default one: /health is liveness only (no
# database), and the calls above all went to the memora store. A store the
# new image cannot reach, or misreads, must fail the deploy by name.
#  a) /health/db/<store>: poll to a CURRENT 200 ok (not stale), bounded.
#  b) memory_stats over /mcp/<store>: a real tool call through the router,
#     bound to that very database, with an integer import_pending.
def _store_health(store):
    req = urllib.request.Request(f"{ROOT}/health/db/{store}",
                                 headers={"Authorization": f"Bearer {HEALTH_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode() or "{}")
        except ValueError:
            return exc.code, {}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, {"error": str(exc)}

failed = []
for store in STORES:
    deadline = time.time() + 90
    healthy = False
    while not healthy:
        status, body = _store_health(store)
        healthy = status == 200 and body.get("status") == "ok" and body.get("stale") is False
        if not healthy and time.time() > deadline:
            break
        if not healthy:
            time.sleep(3)
    if not healthy:
        failed.append(f"{store}: /health/db/{store} not a current 200 ok after 90s "
                      f"(last: HTTP {status}, {json.dumps(body)[:300]})")
        continue
    store_base = f"{ROOT}/mcp/{store}"
    store_sid = _initialize(store_base)
    stats, elapsed = _call_tool(10, "memory_stats", {}, session=store_sid, base=store_base)
    if stats.get("database") != store:
        failed.append(f"{store}: memory_stats is bound to {stats.get('database')!r}, not {store!r}")
        continue
    pending = stats.get("import_pending")
    if not isinstance(pending, int) or isinstance(pending, bool):
        failed.append(f"{store}: memory_stats has no integer import_pending")
        continue
    print(f"store {store}: /health/db 200 ok (latency {body.get('latency_ms')} ms); memory_stats "
          f"{stats.get('total_memories')} memories, import_pending={pending} ({elapsed:.1f}s)")
if failed:
    for line in failed:
        print(f"STORE CHECK FAILED — {line}", file=sys.stderr)
    sys.exit(1)
print(f"all {len(STORES)} stores verified: {', '.join(STORES)}")

# The plain JSON API is registered ONLY with MEMORA_API_TOKENS_FILE, which
# this deploy does not set: /api/v1/... must not exist. REQUIRE an actual
# HTTP 404 from the running server. A refused connection, a timeout or any
# other transport error FAILS (8920 is the same port every check above used,
# so a dead or restarting container here must not pass as "not registered");
# transport errors are retried for at most ~20 s first. Any other status --
# 401 from a registered route, 200, 503 -- means the API is registered.
api_status, last_error = None, None
api_deadline = time.time() + 20
while api_status is None:
    try:
        with urllib.request.urlopen(f"{ROOT}/api/v1/memora/health", timeout=10) as resp:
            api_status = resp.status
    except urllib.error.HTTPError as exc:
        api_status = exc.code
    except (urllib.error.URLError, OSError) as exc:  # refused, reset, timeout
        last_error = exc
        if time.time() > api_deadline:
            print(f"/api/v1/memora/health: no HTTP answer ({last_error}); the server did not respond "
                  "on the port every check above used", file=sys.stderr)
            sys.exit(1)
        time.sleep(2)
if api_status != 404:
    print(f"/api/v1/memora/health answered {api_status}: the API is registered, but this deploy "
          "sets no MEMORA_API_TOKENS_FILE", file=sys.stderr)
    sys.exit(1)
print(f"/api/v1 not registered (/api/v1/memora/health: {api_status})")
PY
REMOTE
