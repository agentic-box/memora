#!/usr/bin/env bash
# Full deploy of the live memora-all container (on DEPLOY_HOST) to v0.5.2: fetch +
# build the tagged image and recreate the container from it, then verify it.
#
# CONFIGURATION (CFG1): the deploy host, the graph's publish address, the
# checkout path on that host and MEMORA_PROJECTS come from the operator's
# git-ignored instances/deploy.env (keys and placeholders in
# instances/deploy.env.example); the store registry from instances/all.env.
# The script refuses to run without them. "deploy-host" below stands for
# DEPLOY_HOST, and 100.64.0.10 for DEPLOY_GRAPH_BIND.
#
# v0.5.0 (CHANGELOG.md "0.5.0") is the local-primary release. What THIS
# deploy changes on memora-all:
#  - CLOUDFLARE TOKENS AS MOUNTED FILES (REL1, review 7758). The token
#    directory ~/.config/memora-lp on deploy-host (DEPLOY_SECRETS_DIR) is mounted
#    READ-ONLY at /run/secrets/memora, and the container gets only the paths:
#      CLOUDFLARE_API_TOKEN_FILE       <- cloudflare-api.token  (DEPLOY_CLOUDFLARE_TOKEN_FILE)
#      MEMORA_D1_READ_TOKEN_FILE       <- d1-read.token         (DEPLOY_D1_READ_TOKEN_FILE)
#      MEMORA_D1_REPLICATOR_TOKEN_FILE <- d1-read.token         (DEPLOY_D1_REPLICATOR_TOKEN_FILE)
#    The three DEPLOY_*_TOKEN_FILE overrides are file NAMES inside that
#    directory (the only directory the container sees). No token value is on
#    a command line, in the container's configuration (docker inspect) or in
#    this repo; CLOUDFLARE_API_TOKEN, CF_API_TOKEN and the D1 token variables
#    in credentials.mcp.json are NOT passed through any more. Each file must
#    be a regular file (not a symlink) owned by the deploying user, mode
#    0600, not empty -- checked before anything is built or stopped, and
#    again by the NEW image through the read-only mount (memora/secret_files.py)
#    before the old container is stopped. After the start, the container's
#    env is checked to carry no token value and the mount to be read-only.
#    ONE-TIME, before the first v0.5.0 deploy (the user runs it on deploy-host; the
#    value is never printed):
#      ( umask 077; python3 -c 'import json,os; e=json.load(open(os.path.expanduser("~/.config/memora/credentials.mcp.json")))["mcpServers"]["memora"]["env"]; print(e.get("CLOUDFLARE_API_TOKEN") or e["CF_API_TOKEN"])' > ~/.config/memora-lp/cloudflare-api.token )
#    Everything in ~/.config/memora-lp is visible (read-only) to the
#    container: keep only token files there. The deploy lists its entries.
#  - THE GRAPH UI (G1): memora-all's graph server (container port 8765) is
#    published ONLY on deploy-host's Tailscale address, DEPLOY_GRAPH_BIND
#    (100.64.0.10) : DEPLOY_GRAPH_PORT (8766), never on 0.0.0.0 -- it can
#    edit memories. Every graph route needs the graph token
#    (~/.config/memora-lp/graph.token, minted here on first use, 0600,
#    distinct from the health and admin tokens; the container gets
#    MEMORA_GRAPH_TOKEN_FILE). Open http://100.64.0.10:8766/graph, enter
#    the token once (an HttpOnly cookie), pick the store in the selector.
#    The smoke check verifies it refuses without the token and lists the
#    stores with it.
#  - LOCAL-PRIMARY SWITCHES from instances/all.env, passed through when
#    present: MEMORA_REPLICAS (a JSON map store -> d1://account/database; its
#    stores must be local in MEMORA_DATABASES) and MEMORA_REPLICATION
#    (log|write), and the replicator's timing MEMORA_REPLICATION_INTERVAL_S,
#    MEMORA_REPLICATION_POLL_S, MEMORA_REPLICATION_BATCH_ROWS (validated
#    with the server's ranges). Absent = dark / the server's defaults.
#    scripts/cutover_store.sh sets them, one store at a time
#    (docs/cutover-runbook.md).
#
# Unchanged from the v0.4.6 deploy (the text below): the named /data volume
# and its migration, --memory 960m, the health/admin token files, the smoke
# checks. What v0.4.6 changed (CHANGELOG.md "0.4.6"):
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
# Steps, all on deploy-host:
#  1. git fetch + checkout the v0.5.2 tag in the deploy-host checkout, docker build.
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
#  5. Wait for GET /health, check it reports version 0.5.2 (proves the new
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
# deploy-host (minted there on first use, 48 alphanumerics, 0600) and must differ
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
# Rollback (for a store already cut over to local primary, follow the L6
# runbook in docs/local-primary-implementation.md instead):
#   ssh deploy-host 'docker rm -f memora-all && docker rename memora-all-grok-<ts> memora-all && docker start memora-all'
#   ssh deploy-host 'docker tag memora:rollback-<ts> memora:latest'   # only if the image itself needs reverting too
#   restore ~/.config/memora/credentials.mcp.json.bak-llm-<ts> if MEMORA_LLM_MODEL itself needs reverting
set -euo pipefail

TAG="${DEPLOY_TAG:-v0.5.2}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The production values that identify real infrastructure (CFG1) are NOT in
# this public script: they come from the operator's git-ignored
# instances/deploy.env (see instances/deploy.env.example). Without it, or
# with a key missing, the deploy refuses -- it never guesses a host.
DEPLOY_CONFIG_FILE_DEFAULT="$ROOT/instances/deploy.env"
CONFIG_FILE="${DEPLOY_CONFIG_FILE:-$DEPLOY_CONFIG_FILE_DEFAULT}"
CFG=()
while IFS= read -r -d '' v; do CFG+=("$v"); done \
  < <(python3 "$ROOT/scripts/deploy_config.py" "$CONFIG_FILE" DEPLOY_HOST DEPLOY_GRAPH_BIND DEPLOY_REPO MEMORA_PROJECTS)
[ "${#CFG[@]}" -eq 4 ] || { echo "refused: the deploy configuration $CONFIG_FILE is missing or incomplete" \
  "(copy instances/deploy.env.example to instances/deploy.env and fill it in) — nothing was done" >&2; exit 1; }
CFG_DEPLOY_HOST="${CFG[0]}"; CFG_DEPLOY_GRAPH_BIND="${CFG[1]}"; CFG_DEPLOY_REPO="${CFG[2]}"
MEMORA_PROJECTS="${CFG[3]}"
python3 -c 'import json, sys; d = json.loads(sys.argv[1]); assert isinstance(d, dict) and all(isinstance(v, list) for v in d.values())' \
  "$MEMORA_PROJECTS" 2>/dev/null \
  || { echo "refused: MEMORA_PROJECTS in $CONFIG_FILE is not a JSON object of lists — nothing was done" >&2; exit 1; }
# Rehearsal parameters (R1): every default is the production value (from the
# deploy configuration), so an unparameterised run is exactly the production
# deploy. scripts/rehearse_deploy.sh sets them to run the same steps against
# a local podman on a rehearsal host.
DEPLOY_HOST="${DEPLOY_HOST:-$CFG_DEPLOY_HOST}"            # "localhost": run here, no ssh
RUNTIME="${RUNTIME:-docker}"                  # the container runtime binary
DEPLOY_CONTAINER="${DEPLOY_CONTAINER:-memora-all}"
DEPLOY_DATA_VOLUME="${DEPLOY_DATA_VOLUME:-memora-all-data}"
DEPLOY_IMAGE="${DEPLOY_IMAGE:-memora:latest}"
DEPLOY_PORT="${DEPLOY_PORT:-8920}"
DEPLOY_CONFIG_DIR="${DEPLOY_CONFIG_DIR:-~/.config/memora}"   # expanded on the target host
DEPLOY_REPO="${DEPLOY_REPO:-$CFG_DEPLOY_REPO}"               # expanded on the target host
DEPLOY_SKIP_CHECKOUT="${DEPLOY_SKIP_CHECKOUT:-0}"            # 1: build DEPLOY_REPO as it is
DEPLOY_SMOKE_ABSORB="${DEPLOY_SMOKE_ABSORB:-1}"              # 0: no LLM-backed absorb in the smoke check
DEPLOY_LABELS="${DEPLOY_LABELS:-}"                          # rehearsal only: k=v labels on what it creates
DEPLOY_SECRETS_DIR="${DEPLOY_SECRETS_DIR:-~/.config/memora-lp}"  # token dir, mounted :ro (expanded on the target host)
DEPLOY_STORE_WAIT_S="${DEPLOY_STORE_WAIT_S:-90}"            # per-store readiness wait after the start (s)
# The graph UI (G1): published ONLY on the deploy host's Tailscale address, never 0.0.0.0.
DEPLOY_GRAPH_BIND="${DEPLOY_GRAPH_BIND:-$CFG_DEPLOY_GRAPH_BIND}"   # the host address the graph port is published on
DEPLOY_GRAPH_PORT="${DEPLOY_GRAPH_PORT:-8766}"               # the host port (the container's graph is 8765)
# Production overrides (documented, no sentinel): token file NAMES inside
# DEPLOY_SECRETS_DIR. The same file may serve both D1 roles (the pilot does).
DEPLOY_CLOUDFLARE_TOKEN_FILE="${DEPLOY_CLOUDFLARE_TOKEN_FILE:-cloudflare-api.token}"
DEPLOY_D1_READ_TOKEN_FILE="${DEPLOY_D1_READ_TOKEN_FILE:-d1-read.token}"
DEPLOY_D1_REPLICATOR_TOKEN_FILE="${DEPLOY_D1_REPLICATOR_TOKEN_FILE:-d1-read.token}"
DEPLOY_GRAPH_TOKEN_FILE="${DEPLOY_GRAPH_TOKEN_FILE:-graph.token}"   # minted on first use (G1)
for v in DEPLOY_CLOUDFLARE_TOKEN_FILE DEPLOY_D1_READ_TOKEN_FILE DEPLOY_D1_REPLICATOR_TOKEN_FILE DEPLOY_GRAPH_TOKEN_FILE; do
  printf '%s' "${!v}" | grep -Eqx '[A-Za-z0-9_-][A-Za-z0-9._-]*' \
    || { echo "refused: $v must be a plain file name inside DEPLOY_SECRETS_DIR (got '${!v}') — nothing was done" >&2; exit 1; }
done

# MEMORA_DATABASES names a Cloudflare account + database ids — read from the
# git-ignored instance config rather than written into this (public) script.
ENV_FILE="${DEPLOY_ENV_FILE:-$ROOT/instances/all.env}"

# Guard (review 7725): an override of ANY of the above needs the explicit
# rehearsal sentinel, and then everything must be rehearsal-scoped. A stray
# variable in an operator's shell can never re-target the production deploy.
# (bash 3.2 on macOS runs this: no associative arrays.)
DEPLOY_ENV_FILE_EFFECTIVE="$ENV_FILE"
DEPLOY_CONFIG_FILE_EFFECTIVE="$CONFIG_FILE"
OVERRIDDEN=()
while IFS='|' read -r var label default; do
  [ "${!var}" = "$default" ] || OVERRIDDEN+=("$label")
done <<DEFAULTS
TAG|DEPLOY_TAG|v0.5.2
DEPLOY_HOST|DEPLOY_HOST|$CFG_DEPLOY_HOST
RUNTIME|RUNTIME|docker
DEPLOY_CONTAINER|DEPLOY_CONTAINER|memora-all
DEPLOY_DATA_VOLUME|DEPLOY_DATA_VOLUME|memora-all-data
DEPLOY_IMAGE|DEPLOY_IMAGE|memora:latest
DEPLOY_PORT|DEPLOY_PORT|8920
DEPLOY_CONFIG_DIR|DEPLOY_CONFIG_DIR|~/.config/memora
DEPLOY_REPO|DEPLOY_REPO|$CFG_DEPLOY_REPO
DEPLOY_SKIP_CHECKOUT|DEPLOY_SKIP_CHECKOUT|0
DEPLOY_SMOKE_ABSORB|DEPLOY_SMOKE_ABSORB|1
DEPLOY_ENV_FILE_EFFECTIVE|DEPLOY_ENV_FILE|$ROOT/instances/all.env
DEPLOY_CONFIG_FILE_EFFECTIVE|DEPLOY_CONFIG_FILE|$DEPLOY_CONFIG_FILE_DEFAULT
DEPLOY_LABELS|DEPLOY_LABELS|
DEPLOY_SECRETS_DIR|DEPLOY_SECRETS_DIR|~/.config/memora-lp
DEPLOY_STORE_WAIT_S|DEPLOY_STORE_WAIT_S|90
DEPLOY_GRAPH_BIND|DEPLOY_GRAPH_BIND|$CFG_DEPLOY_GRAPH_BIND
DEPLOY_GRAPH_PORT|DEPLOY_GRAPH_PORT|8766
DEFAULTS
if [ "${DEPLOY_REHEARSAL:-}" = 1 ]; then
  RH_ROOT="${DEPLOY_REHEARSAL_ROOT:-}"
  refuse() { echo "rehearsal refused: $* — nothing was done" >&2; exit 1; }
  [ -n "$RH_ROOT" ] && [ -d "$RH_ROOT" ] || refuse "DEPLOY_REHEARSAL_ROOT must name an existing directory"
  [ "$DEPLOY_HOST" = localhost ] || refuse "DEPLOY_HOST must be localhost (not '$DEPLOY_HOST')"
  for v in DEPLOY_CONTAINER DEPLOY_DATA_VOLUME DEPLOY_IMAGE; do
    case "${!v}" in *-rh*) ;; *) refuse "$v '${!v}' is not rehearsal-scoped (must contain -rh)" ;; esac
  done
  [ "$DEPLOY_PORT" != 8920 ] || refuse "DEPLOY_PORT 8920 is production's"
  [ "$DEPLOY_GRAPH_PORT" != 8766 ] || refuse "DEPLOY_GRAPH_PORT 8766 is production's"
  under() { python3 -c 'import os, sys; r, p = map(os.path.realpath, sys.argv[1:]); sys.exit(0 if os.path.commonpath([r, p]) == r else 1)' "$1" "$2"; }
  under "$RH_ROOT" "$DEPLOY_CONFIG_DIR" || refuse "DEPLOY_CONFIG_DIR is not under $RH_ROOT"
  under "$RH_ROOT" "$ENV_FILE" || refuse "DEPLOY_ENV_FILE is not under $RH_ROOT"
  under "$RH_ROOT" "$CONFIG_FILE" || refuse "DEPLOY_CONFIG_FILE is not under $RH_ROOT"
  under "$RH_ROOT" "$DEPLOY_SECRETS_DIR" || refuse "DEPLOY_SECRETS_DIR is not under $RH_ROOT"
  case "$DEPLOY_LABELS" in *memora.rehearsal=?*) ;; *) refuse "DEPLOY_LABELS must carry memora.rehearsal=<run-id>" ;; esac
elif [ "${#OVERRIDDEN[@]}" -gt 0 ]; then
  echo "refused: ${OVERRIDDEN[*]} differ(s) from the production deploy; overrides are for rehearsals only" \
       "(DEPLOY_REHEARSAL=1, scripts/rehearse_deploy.sh) — nothing was done" >&2
  exit 1
fi
# The graph can edit memories: it is published on ONE specific address
# (the deploy host's Tailscale IP), never on all interfaces, rehearsals included.
python3 - "$DEPLOY_GRAPH_BIND" "$DEPLOY_GRAPH_PORT" <<'PY' || { echo "refused: DEPLOY_GRAPH_BIND / DEPLOY_GRAPH_PORT — nothing was done" >&2; exit 1; }
import ipaddress, sys
bind, port = sys.argv[1:]
try:
    ip = ipaddress.IPv4Address(bind)
except ValueError:
    sys.exit(f"DEPLOY_GRAPH_BIND {bind!r} is not an IPv4 address")
if ip.is_unspecified:
    sys.exit("DEPLOY_GRAPH_BIND 0.0.0.0 would publish the graph on every interface")
if not port.isdigit() or not 1 <= int(port) <= 65535:
    sys.exit(f"DEPLOY_GRAPH_PORT {port!r} is not a port")
PY
echo "deploy target: host=$DEPLOY_HOST runtime=$RUNTIME container=$DEPLOY_CONTAINER volume=$DEPLOY_DATA_VOLUME" \
     "image=$DEPLOY_IMAGE port=$DEPLOY_PORT graph=$DEPLOY_GRAPH_BIND:$DEPLOY_GRAPH_PORT tag=$TAG secrets=$DEPLOY_SECRETS_DIR${DEPLOY_REHEARSAL:+ (REHEARSAL)}"
[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE — need MEMORA_DATABASES for memora-all" >&2; exit 1; }
MEMORA_DATABASES="$(grep -E "^MEMORA_DATABASES=" "$ENV_FILE" | head -1 | cut -d= -f2- | sed "s/^'//;s/'\$//")"
[ -n "$MEMORA_DATABASES" ] || { echo "$ENV_FILE has no MEMORA_DATABASES" >&2; exit 1; }
# base64 over the wire: the JSON has embedded quotes that ssh's remote
# command re-join would otherwise mangle.
MEMORA_DATABASES_B64="$(printf '%s' "$MEMORA_DATABASES" | base64 | tr -d '\n')"
MEMORA_PROJECTS_B64="$(printf '%s' "$MEMORA_PROJECTS" | base64 | tr -d '\n')"
# The local-primary switches (absent = dark). Checked here, before anything
# runs: MEMORA_REPLICATION is log|write; MEMORA_REPLICAS maps stores of
# MEMORA_DATABASES whose registry entry is LOCAL to d1://account/database.
env_value() { { grep -E "^$1=" "$ENV_FILE" || true; } | head -1 | cut -d= -f2- | sed "s/^'//;s/'\$//"; }
# MEMORA_DATA_DIR is pinned to /data in the container (leader 7834: X3's
# service lock, /data/.service.lock, must be the one memora-all holds).
DATA_DIR_SET="$(env_value MEMORA_DATA_DIR)"
if [ -n "$DATA_DIR_SET" ] && [ "$DATA_DIR_SET" != /data ]; then
  echo "refused: $ENV_FILE sets MEMORA_DATA_DIR=$DATA_DIR_SET; memora-all's data dir is pinned to /data (the service lock) — nothing was done" >&2
  exit 1
fi
MEMORA_REPLICAS="$(env_value MEMORA_REPLICAS)"
MEMORA_REPLICATION="$(env_value MEMORA_REPLICATION)"
# The replicator's timing (leader 7762), the server's own ranges; unset = its default.
REPL_TIMING=""
for v in MEMORA_REPLICATION_INTERVAL_S MEMORA_REPLICATION_POLL_S MEMORA_REPLICATION_BATCH_ROWS; do
  val="$(env_value "$v")"
  [ -z "$val" ] || REPL_TIMING="$REPL_TIMING $v=$val"
done
python3 - "$MEMORA_DATABASES" "$MEMORA_REPLICAS" "$MEMORA_REPLICATION" $REPL_TIMING <<'PY' || { echo "refused: $ENV_FILE's MEMORA_REPLICAS / MEMORA_REPLICATION / MEMORA_REPLICATION_* — nothing was done" >&2; exit 1; }
import json, math, re, sys
dbs, replicas, mode = sys.argv[1:4]
registry = json.loads(dbs)
for kv in sys.argv[4:]:
    k, v = kv.split("=", 1)
    if k == "MEMORA_REPLICATION_BATCH_ROWS":
        if not v.isdigit() or not 1 <= int(v) <= 1000:
            sys.exit(f"{k}={v!r}: must be an integer 1..1000")
    else:
        try:
            f = float(v)
        except ValueError:
            f = float("nan")
        if not math.isfinite(f) or f < 0 or (k == "MEMORA_REPLICATION_POLL_S" and f == 0):
            sys.exit(f"{k}={v!r}: must be a finite number {'> 0' if k.endswith('POLL_S') else '>= 0'}")
    print(f"local-primary: {k}={v}")
if mode and mode not in ("log", "write"):
    sys.exit(f"MEMORA_REPLICATION must be log or write, not {mode!r}")
if replicas:
    rep = json.loads(replicas)
    if not isinstance(rep, dict) or not rep:
        sys.exit("MEMORA_REPLICAS must be a non-empty JSON object {store: d1://account/database}")
    for name, uri in rep.items():
        if name not in registry:
            sys.exit(f"MEMORA_REPLICAS names {name!r}, not a store of MEMORA_DATABASES")
        if not re.fullmatch(r"d1://[^/\s]+/[^/\s]+", str(uri)):
            sys.exit(f"MEMORA_REPLICAS[{name!r}] must be d1://account/database, not {uri!r}")
        entry = str(registry[name])
        if "://" in entry and not entry.startswith("file://"):
            sys.exit(f"MEMORA_REPLICAS names {name!r}, but MEMORA_DATABASES serves it from {entry!r}: "
                     "a replicated store must be a local path")
    print(f"local-primary: MEMORA_REPLICAS={sorted(rep)} MEMORA_REPLICATION={mode or '(unset: dark)'}")
elif mode:
    print(f"local-primary: MEMORA_REPLICATION={mode} with no MEMORA_REPLICAS (no store replicated)")
else:
    print("local-primary: MEMORA_REPLICAS / MEMORA_REPLICATION absent (dark)")
PY
MEMORA_REPLICAS_B64="$(printf '%s' "$MEMORA_REPLICAS" | base64 | tr -d '\n')"
# The /data migration program (shared with memora-instance.sh), sent from
# THIS checkout: the deploy-host checkout is at $TAG and may predate it.
MIGRATE_B64="$(base64 < "$ROOT/scripts/migrate_data_volume.sh" | tr -d '\n')"

# The remote script's parameters (REL2): ssh JOINS its arguments into one
# command line for the remote shell, which re-splits it -- an EMPTY argument
# vanishes and every later one shifts (production died with "$18: unbound
# variable": DEPLOY_LABELS and MEMORA_REPLICAS are empty there). So all of
# them travel as ONE base64 blob of NUL-terminated values: a single
# non-empty word of [A-Za-z0-9+/=], which no word-joining can split or drop,
# preceded by the blob's sha256 as a second word (review 7889: a changed
# character that keeps the field count must refuse too). The remote script
# checks the digest, decodes (a decoder error refuses), requires exactly
# the 25 parameters it reads, and only then restores $1..$25. A localhost
# rehearsal sends the SAME command line through `sh -c`, i.e. the same
# re-parsing a remote shell does.
REMOTE_ARGS=("$TAG" "$MEMORA_DATABASES_B64" "$MIGRATE_B64" "$RUNTIME" "$DEPLOY_CONTAINER"
  "$DEPLOY_DATA_VOLUME" "$DEPLOY_IMAGE" "$DEPLOY_PORT" "$DEPLOY_CONFIG_DIR" "$DEPLOY_REPO"
  "$DEPLOY_SKIP_CHECKOUT" "$DEPLOY_SMOKE_ABSORB" "$DEPLOY_LABELS" "$DEPLOY_SECRETS_DIR"
  "$DEPLOY_CLOUDFLARE_TOKEN_FILE" "$DEPLOY_D1_READ_TOKEN_FILE" "$DEPLOY_D1_REPLICATOR_TOKEN_FILE"
  "$MEMORA_REPLICAS_B64" "$MEMORA_REPLICATION" "$REPL_TIMING" "$DEPLOY_STORE_WAIT_S"
  "$DEPLOY_GRAPH_BIND" "$DEPLOY_GRAPH_PORT" "$DEPLOY_GRAPH_TOKEN_FILE" "$MEMORA_PROJECTS_B64")
PARAMS_B64="$(printf '%s\0' "${REMOTE_ARGS[@]}" | base64 | tr -d '\n')"
PARAMS_SHA="$(printf '%s' "$PARAMS_B64" | python3 -c 'import hashlib, sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())')"
[ "${#REMOTE_ARGS[@]}" -eq 25 ] || { echo "deploy: internal error: ${#REMOTE_ARGS[@]} remote parameters, not 25" >&2; exit 1; }
REMOTE_CMD="bash -s -- $PARAMS_SHA $PARAMS_B64"
if [ "$DEPLOY_HOST" = localhost ]; then
  TARGET=(sh -c "$REMOTE_CMD")
else
  TARGET=(ssh "$DEPLOY_HOST" "$REMOTE_CMD")
fi
"${TARGET[@]}" <<'REMOTE'
set -euo pipefail
# The parameters: $1 the blob's sha256, $2 the blob (see REMOTE_CMD above).
broken() { echo "deploy: $* -- argument transport broken; nothing was done" >&2; exit 1; }
[ "$#" -eq 2 ] || broken "the remote command arrived with $# words, not 2"
GOT_SHA="$(printf '%s' "$2" | python3 -c 'import hashlib, sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())')" \
  || broken "cannot hash the parameters"
[ "$GOT_SHA" = "$1" ] || broken "the parameters' sha256 does not match"
DECODED="$(mktemp)"
printf '%s' "$2" | base64 -d > "$DECODED" 2>/dev/null || { rm -f "$DECODED"; broken "the parameters do not decode"; }
P=()
while IFS= read -r -d '' v; do P+=("$v"); done < "$DECODED"
rm -f "$DECODED"
[ "${#P[@]}" -eq 25 ] || broken "${#P[@]} parameters arrived, not 25"
set -- "${P[@]}"
TAG="$1"
MEMORA_DATABASES="$(printf '%s' "$2" | base64 -d)"
MIGRATE_SCRIPT="$(printf '%s' "$3" | base64 -d)"
RT="$4"; CONTAINER="$5"; DATA_VOLUME="$6"; IMAGE="$7"; PORT="$8"
CONFIG_DIR="${9/#\~/$HOME}"; REPO_DIR="${10/#\~/$HOME}"; SKIP_CHECKOUT="${11}"; SMOKE_ABSORB="${12}"
LABEL_ARGS=()   # rehearsal only (the guard allows DEPLOY_LABELS under DEPLOY_REHEARSAL=1 alone)
for l in ${13:-}; do LABEL_ARGS+=(--label "$l"); done
[ -n "$MIGRATE_SCRIPT" ] || { echo "empty /data migration program" >&2; exit 1; }
SECRETS_DIR="${14/#\~/$HOME}"
CF_TOKEN_NAME="${15}"; READ_TOKEN_NAME="${16}"; REPL_TOKEN_NAME="${17}"
MEMORA_REPLICAS="$(printf '%s' "${18}" | base64 -d)"; MEMORA_REPLICATION="${19}"
GRAPH_BIND="${22}"; GRAPH_PORT="${23}"; GRAPH_TOKEN_NAME="${24}"
MEMORA_PROJECTS="$(printf '%s' "${25}" | base64 -d)"
SECRETS_MOUNT=/run/secrets/memora
TS=$(date +%s)

# The graph token (G1): minted once in the token directory, like the admin
# token (48 alphanumerics, 0600, never printed); the checks below cover it.
GRAPH_TOKEN_PATH="$SECRETS_DIR/$GRAPH_TOKEN_NAME"
if [ -d "$SECRETS_DIR" ] && [ ! -L "$SECRETS_DIR" ] && [ ! -e "$GRAPH_TOKEN_PATH" ] && [ ! -L "$GRAPH_TOKEN_PATH" ]; then
  tmp="$(mktemp "$SECRETS_DIR/.graph-token.XXXXXX")"
  chmod 600 "$tmp"
  ( set +o pipefail; LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 48 ) > "$tmp"
  mv -f "$tmp" "$GRAPH_TOKEN_PATH"
  echo "minted $GRAPH_TOKEN_PATH (the graph UI's token)"
fi

# Token files (REL1, review 7758), checked BEFORE anything is fetched, built
# or stopped: the directory is a real directory (not a symlink) owned by this
# user; each file a regular file (not a symlink) owned by this user, mode
# exactly 0600, not empty. Refused otherwise, never chmod-ed. Values are
# never read into this shell.
python3 - "$SECRETS_DIR" "$CF_TOKEN_NAME" "$READ_TOKEN_NAME" "$REPL_TOKEN_NAME" "$GRAPH_TOKEN_NAME" <<'PY' || exit 1
import os, stat, sys
d, names = sys.argv[1], sys.argv[2:]
hint = ("  write it on this host first, e.g. for the Cloudflare token (value never printed):\n"
        "  ( umask 077; python3 -c 'import json,os; e=json.load(open(os.path.expanduser(\"~/.config/memora/credentials.mcp.json\")))"
        "[\"mcpServers\"][\"memora\"][\"env\"]; print(e.get(\"CLOUDFLARE_API_TOKEN\") or e[\"CF_API_TOKEN\"])' > FILE )")
def refuse(msg):
    sys.exit(f"refused: {msg} — nothing was stopped")
try:
    st = os.lstat(d)
except OSError:
    refuse(f"token directory {d} is missing\n{hint}")
if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
    refuse(f"token directory {d} must be a directory (not a symlink) owned by this user")
for name in dict.fromkeys(names):
    p = os.path.join(d, name)
    try:
        st = os.lstat(p)
    except OSError:
        refuse(f"token file {p} is missing\n{hint.replace('FILE', p)}")
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600:
        refuse(f"token file {p} must be a regular file owned by this user with mode 0600; it may have been exposed")
    if not open(p).read().strip():
        refuse(f"token file {p} is empty")
print(f"token files ok in {d} (mounted read-only at /run/secrets/memora); entries visible to the container: "
      f"{', '.join(sorted(os.listdir(d)))}")
PY
# SELinux (enforcing, e.g. the Fedora rehearsal host): a home directory's
# label is unreadable from a container, so the mount relabels the token
# directory SHARED (:z) -- still read-only, and still readable by the old
# container kept for rollback (a private :Z would cut it off). Elsewhere
# (deploy-host) the options are plain ro.
SECRETS_OPTS=ro
if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce 2>/dev/null)" = Enforcing ]; then
  SECRETS_OPTS=ro,z
  echo "SELinux is enforcing on $(hostname): $SECRETS_DIR is mounted :ro,z (relabelled container-readable, shared)"
fi
TOKEN_FILE_ARGS=(-e "CLOUDFLARE_API_TOKEN_FILE=$SECRETS_MOUNT/$CF_TOKEN_NAME"
                 -e "MEMORA_D1_READ_TOKEN_FILE=$SECRETS_MOUNT/$READ_TOKEN_NAME"
                 -e "MEMORA_D1_REPLICATOR_TOKEN_FILE=$SECRETS_MOUNT/$REPL_TOKEN_NAME"
                 -e "MEMORA_GRAPH_TOKEN_FILE=$SECRETS_MOUNT/$GRAPH_TOKEN_NAME")
LP_ARGS=()   # the local-primary switches, only when all.env sets them (absent = dark)
[ -z "$MEMORA_REPLICAS" ] || LP_ARGS+=(-e "MEMORA_REPLICAS=$MEMORA_REPLICAS")
[ -z "$MEMORA_REPLICATION" ] || LP_ARGS+=(-e "MEMORA_REPLICATION=$MEMORA_REPLICATION")
for kv in ${20:-}; do LP_ARGS+=(-e "$kv"); done   # MEMORA_REPLICATION_{INTERVAL_S,POLL_S,BATCH_ROWS}, validated above
# Keyed by registry store name (MEMORA_DATABASES); see the header for why.
# From the operator's deploy configuration (parameter 25), not this script.
[ -n "$MEMORA_PROJECTS" ] || { echo "empty MEMORA_PROJECTS" >&2; exit 1; }

REPO="$REPO_DIR"
[ -d "$REPO" ] || { echo "missing $REPO checkout on $(hostname)" >&2; exit 1; }
if [ "$SKIP_CHECKOUT" != 1 ]; then
  git -C "$REPO" fetch origin
  git -C "$REPO" checkout "$TAG"
fi

# Keep the currently-running image for rollback before building over it.
"$RT" tag "$IMAGE" "${IMAGE%%:*}:rollback-$TS" 2>/dev/null || true
"$RT" build ${LABEL_ARGS[@]+"${LABEL_ARGS[@]}"} -t "$IMAGE" "$REPO"

CRED=$CONFIG_DIR/credentials.mcp.json
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

HEALTH_TOKEN_FILE=$CONFIG_DIR/all.health-token
{ [ -e "$HEALTH_TOKEN_FILE" ] || [ -L "$HEALTH_TOKEN_FILE" ]; } || { echo "missing $HEALTH_TOKEN_FILE — refusing to mint a new one for a live container" >&2; exit 1; }
# Same rule as the admin token below: regular file, not a symlink, owned by
# this user, mode 0600; otherwise refuse, never chmod (review 7636).
python3 - "$HEALTH_TOKEN_FILE" <<'PY' || { echo "$HEALTH_TOKEN_FILE must be a regular file owned by $(id -un) with mode 0600; it may have been exposed: re-mint it (and restart memora-all with it)" >&2; exit 1; }
import os, stat, sys
st = os.lstat(sys.argv[1])
ok = stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) == 0o600
sys.exit(0 if ok else 1)
PY
HEALTH_TOKEN=$(cat "$HEALTH_TOKEN_FILE")

# Admin token: read, or mint once. Same shape as the health token (48
# alphanumerics, no newline); an unusable existing file is refused, not
# replaced, because a script may already hold it.
ADMIN_TOKEN_FILE=$CONFIG_DIR/all.admin-token
if [ ! -e "$ADMIN_TOKEN_FILE" ] && [ ! -L "$ADMIN_TOKEN_FILE" ]; then
  tmp="$(mktemp "$CONFIG_DIR/.admin-token.XXXXXX")"
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
# The graph token is its own credential (G1): never the health or admin token.
HEALTH_TOKEN="$HEALTH_TOKEN" ADMIN_TOKEN="$ADMIN_TOKEN" python3 - "$GRAPH_TOKEN_PATH" <<'PY' \
  || { echo "the graph token equals the health or admin token — remove $GRAPH_TOKEN_PATH to mint a new one" >&2; exit 1; }
import os, sys
g = open(sys.argv[1]).read().strip()
sys.exit(1 if g in (os.environ["HEALTH_TOKEN"], os.environ["ADMIN_TOKEN"]) else 0)
PY

# /data: the NAMED volume memora-all-data (see the header). OLD_VOLUME is what
# the running container mounts at /data today; it is copied after the stop.
OLD_VOLUME=$("$RT" inspect "$CONTAINER" --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}')
[ -n "$OLD_VOLUME" ] || { echo "could not read $CONTAINER's /data volume" >&2; exit 1; }
# Created and checked BEFORE the stop, so a runtime that cannot create or
# name it fails with memora-all still serving.
"$RT" volume inspect "$DATA_VOLUME" >/dev/null 2>&1 || "$RT" volume create ${LABEL_ARGS[@]+"${LABEL_ARGS[@]}"} "$DATA_VOLUME" >/dev/null
MOUNTED=$("$RT" volume inspect "$DATA_VOLUME" --format '{{.Name}}')
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
CRED_DATA_DIR="$(printf '%s\n' "$ENV_LINES" | sed -n 's/^MEMORA_DATA_DIR=//p' | head -1)"
if [ -n "$CRED_DATA_DIR" ] && [ "$CRED_DATA_DIR" != /data ]; then
  echo "refused: $CRED sets MEMORA_DATA_DIR=$CRED_DATA_DIR; memora-all's data dir is pinned to /data (the service lock) — aborting before touching the live container" >&2
  exit 1
fi

ENV_ARGS=()
while IFS='=' read -r key value; do
  [ -z "$key" ] && continue
  case "$key" in
    MEMORA_STORAGE_URI|MEMORA_DB_PATH|MEMORA_DATABASES|MEMORA_DEFAULT_DB|MEMORA_PROJECTS|MEMORA_DATA_VOLUME|MEMORA_ADMIN_TOKEN) continue ;;
    MEMORA_DATA_DIR) continue ;;   # pinned below (checked above)
    # Cloudflare tokens come ONLY from the mounted files (REL1); the
    # local-primary switches ONLY from all.env.
    CLOUDFLARE_API_TOKEN|CF_API_TOKEN|MEMORA_D1_READ_TOKEN|MEMORA_D1_REPLICATOR_TOKEN) continue ;;
    CLOUDFLARE_API_TOKEN_FILE|CF_API_TOKEN_FILE|MEMORA_D1_READ_TOKEN_FILE|MEMORA_D1_REPLICATOR_TOKEN_FILE) continue ;;
    MEMORA_REPLICAS|MEMORA_REPLICATION|MEMORA_SHADOW_LOCAL) continue ;;
    MEMORA_REPLICATION_INTERVAL_S|MEMORA_REPLICATION_POLL_S|MEMORA_REPLICATION_BATCH_ROWS) continue ;;
  esac
  ENV_ARGS+=(-e "$key=$value")
done <<< "$ENV_LINES"

# Preflight 1: MEMORA_PROJECTS parses with the NEW image's own validator
# (the server refuses to start on a malformed value), and names exactly the
# registry's stores.
"$RT" run --rm -e "MEMORA_PROJECTS=$MEMORA_PROJECTS" -e "MEMORA_DATABASES=$MEMORA_DATABASES" \
  "$IMAGE" python -c '
import json, os, sys
from memora.storage import load_projects_config
projects = load_projects_config()
stores = set(json.loads(os.environ["MEMORA_DATABASES"]))
if not isinstance(projects, dict) or set(projects) != stores:
    sys.exit(f"MEMORA_PROJECTS stores {sorted(projects or [])} != MEMORA_DATABASES stores {sorted(stores)}")
print("MEMORA_PROJECTS ok:", json.dumps(projects, sort_keys=True))
' || { echo "MEMORA_PROJECTS preflight failed — aborting before touching the live container" >&2; exit 1; }

# Preflight 1b: the NEW image reads every token file through the read-only
# mount, under the server's own rule (memora/secret_files.py) -- this is
# where a uid mapping that cannot read the files shows up, with the old
# container still serving. Only "ok" is printed, never a value.
"$RT" run --rm -v "$SECRETS_DIR:$SECRETS_MOUNT:$SECRETS_OPTS" "${TOKEN_FILE_ARGS[@]}" "$IMAGE" python -c '
from memora.secret_files import check_secret_files
missing = [k for k, ok in check_secret_files().items() if not ok]
assert not missing, missing
print("token files readable by the new image through the read-only mount")
' || { echo "token-file preflight failed — aborting before touching the live container" >&2; exit 1; }

# Preflight 2 (READ-ONLY): the startup sweep (since v0.4.5) completes or removes
# rows whose metadata carries an import_attempt marker. None should exist
# (no released memora wrote the key), but a caller could have set it. Count,
# per live store, rows whose metadata contains the string at all (a superset
# of real markers), through the running (previous) container: a raw backend
# connection, so no schema pass -- one SELECT per store. Any hit, or a
# failed check, aborts before anything is stopped. READ-ONLY (v0.5.1): a
# live local primary's writer lock (/data/<db>.db.primary-lock) is held by
# the running memora-all, so a writer open here would be refused; the
# read-only open takes no writer or primary lock and reads a WAL primary
# through the server's own -wal/-shm. A d1:// store's read path is its
# connect() (the base class's connect_read_only).
"$RT" exec -i "$CONTAINER" python - <<'PY' || { echo "import_attempt preflight failed — aborting before touching the live container" >&2; exit 1; }
import json, os, sys
from memora import storage
bad = {}
for name in json.loads(os.environ["MEMORA_DATABASES"]):
    backend = storage.backend_for(name)
    conn = getattr(backend, "connect_read_only", backend.connect)()
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

# The new container's environment, shared by Preflight 3 and the run below
# (one list, so the preflight checks exactly what the new server will see).
CONTAINER_ENV=(
  -e "MEMORA_DATA_VOLUME=$DATA_VOLUME"
  -e "MEMORA_DATA_DIR=/data"
  -e "MEMORA_TOOL_PROFILE=leader"
  -e "MEMORA_HEALTH_TOKEN=$HEALTH_TOKEN"
  -e "MEMORA_ADMIN_TOKEN=$ADMIN_TOKEN"
  -e "MEMORA_HEALTH_TIMEOUT=30"
  -e "MEMORA_HEALTH_REFRESH_INTERVAL=15"
  -e "MEMORA_VECTOR_SCAN_PAGE_SIZE=100"
  -e "MEMORA_ALLOW_ANY_TAG=1"
  -e "MEMORA_LOG_LEVEL=INFO"
  -e "MEMORA_DATABASES=$MEMORA_DATABASES"
  -e "MEMORA_DEFAULT_DB=memora"
  -e "MEMORA_PROJECTS=$MEMORA_PROJECTS"
  "${TOKEN_FILE_ARGS[@]}"
  ${LP_ARGS[@]+"${LP_ARGS[@]}"}
  "${ENV_ARGS[@]}"
)

# Preflight 3 (E1b): would every store still answer semantic search under
# the NEW image's embedding model? `python -m memora.embedding_preflight`
# in the new image, with the new container's environment, the token mount
# and the volume the RUNNING container serves (/data; read-write, because a
# live primary's WAL is read through the writer's -shm -- the module opens
# stores only read-only and takes no lock). Created, started attached and
# removed by its ID, never `run --rm` (podman's --rm deletes an anonymous
# volume it mounts: the old data, R1). Exit 0 = searchable; anything else
# refuses before the old container is stopped.
PF_ID="$("$RT" create --name "$CONTAINER-embedpf-$TS" ${LABEL_ARGS[@]+"${LABEL_ARGS[@]}"} \
  -v "$OLD_VOLUME:/data" -v "$SECRETS_DIR:$SECRETS_MOUNT:$SECRETS_OPTS" \
  "${CONTAINER_ENV[@]}" "$IMAGE" python -m memora.embedding_preflight < /dev/null)" \
  || { echo "cannot create the embedding preflight container — aborting before touching the live container" >&2; exit 1; }
[ -n "$PF_ID" ] || { echo "the embedding preflight container has no ID — aborting before touching the live container" >&2; exit 1; }
PF_RC=0
# < /dev/null: this script is itself bash's stdin (`bash -s`), and podman's
# `start -a` attaches stdin -- it would swallow the rest of the script.
"$RT" start -a "$PF_ID" < /dev/null || PF_RC=$?
"$RT" rm "$PF_ID" >/dev/null 2>&1 || echo "note: remove the embedding preflight container $PF_ID by hand ($RT rm $PF_ID)" >&2
[ "$PF_RC" -eq 0 ] || { echo "embedding preflight refused (exit $PF_RC): a store would refuse semantic search under the new image (the lines above name it and the fix) — aborting before touching the live container" >&2; exit 1; }

"$RT" stop "$CONTAINER"

# Copy the old /data into the named volume while memora-all is stopped
# (scripts/migrate_data_volume.sh: staged into /to/.memora-staging, verified
# by digest, then swapped in; live content is moved aside, never overlaid).
# Its marker records the SOURCE volume and the source's content digest, so
# the copy is skipped only when memora-all-data already holds this exact
# source unchanged. After a rollback (memora-all back on OLD, accruing
# writes) the next deploy recopies. Skipped entirely once memora-all itself
# mounts memora-all-data.
if [ "$OLD_VOLUME" != "$DATA_VOLUME" ]; then
  # The status is captured on its own: a FAILED query must refuse, never read
  # as "nothing uses it" (review 7637 P1-2).
  IN_USE="$("$RT" ps -q --filter "volume=$OLD_VOLUME")" \
    || { echo "cannot tell whether a container uses $OLD_VOLUME ($RT ps failed) — refusing to copy it; $CONTAINER is stopped, restart it with: $RT start $CONTAINER" >&2; exit 1; }
  if [ -n "$IN_USE" ]; then
    echo "a running container still uses $OLD_VOLUME — refusing to copy it; $CONTAINER is stopped, restart it with: $RT start $CONTAINER" >&2
    exit 1
  fi
  # NOT `run --rm`: podman's --rm deletes an ANONYMOUS volume mounted with -v
  # once no container references it -- the old data (found by the R1
  # rehearsal on build-host). A plain `rm` never removes volumes, on either runtime;
  # --tmpfs /data keeps the image's VOLUME /data from leaving an anonymous one.
  MIGRATOR="$CONTAINER-migrate-$TS"
  if ! "$RT" run --name "$MIGRATOR" ${LABEL_ARGS[@]+"${LABEL_ARGS[@]}"} --tmpfs /data -v "$OLD_VOLUME:/from:ro" -v "$DATA_VOLUME:/to" "$IMAGE" \
       sh -c "$MIGRATE_SCRIPT" migrate_data_volume migrate "$OLD_VOLUME"; then
    "$RT" rm "$MIGRATOR" >/dev/null 2>&1 || true
    echo "copy $OLD_VOLUME -> $DATA_VOLUME failed — $CONTAINER is stopped, restart it with: $RT start $CONTAINER" >&2
    exit 1
  fi
  "$RT" rm "$MIGRATOR" >/dev/null || echo "note: the finished migration container $MIGRATOR was not removed" >&2
fi

"$RT" rename "$CONTAINER" "$CONTAINER-grok-$TS"

"$RT" run -d --name "$CONTAINER" ${LABEL_ARGS[@]+"${LABEL_ARGS[@]}"} \
  --restart unless-stopped \
  --memory 960m --cpus 4 \
  -p "0.0.0.0:$PORT:8000" \
  -p "$GRAPH_BIND:$GRAPH_PORT:8765" \
  -v "$DATA_VOLUME:/data" \
  -v "$SECRETS_DIR:$SECRETS_MOUNT:$SECRETS_OPTS" \
  "${CONTAINER_ENV[@]}" \
  "$IMAGE"

# The new container's configuration carries no token VALUE, and the token
# mount is read-only. Checked against the files themselves; prints no value.
# The inspect output reaches python through its environment (it holds other
# credentials, so not argv; stdin is the program's heredoc).
EXPOSED="the new $CONTAINER exposes a token or mounts the tokens writable — remove it: $RT rm -f $CONTAINER, then roll back (below)"
NEW_INSPECT="$("$RT" inspect "$CONTAINER")" || { echo "cannot inspect the new $CONTAINER. $EXPOSED" >&2; exit 1; }
NEW_INSPECT="$NEW_INSPECT" python3 - "$SECRETS_DIR" "$SECRETS_MOUNT" "$CF_TOKEN_NAME" "$READ_TOKEN_NAME" "$REPL_TOKEN_NAME" "$GRAPH_TOKEN_NAME" <<'PY' \
  || { echo "$EXPOSED" >&2; exit 1; }
import json, os, sys
d, mount, names = sys.argv[1], sys.argv[2], sys.argv[3:]
info = json.loads(os.environ.pop("NEW_INSPECT"))[0]
values = {open(os.path.join(d, n)).read().strip() for n in names}
env = "\n".join(info["Config"].get("Env") or [])
if any(v and v in env for v in values):
    sys.exit("a token value is in the container's environment")
ro = [m for m in info.get("Mounts") or [] if m.get("Destination") == mount]
if len(ro) != 1 or ro[0].get("RW") is not False:
    sys.exit(f"{mount} is not mounted exactly once read-only")
print(f"container env carries no token value; {mount} is mounted read-only")
PY

echo "$CONTAINER recreated from $TAG (MEMORA_PROJECTS set; MEMORA_LLM_MODEL=openai/gpt-4o-mini unchanged, MEMORA_LOG_LEVEL=INFO, corpus cache budget default 384 MB)"
echo "old container kept stopped as $CONTAINER-grok-$TS; old image kept as ${IMAGE%%:*}:rollback-$TS"
echo "rollback: $RT rm -f $CONTAINER && $RT rename $CONTAINER-grok-$TS $CONTAINER && $RT start $CONTAINER"

echo "waiting for /health..."
healthy=0
for ((i = 1; i <= 30; i++)); do
  if curl -sf -m 3 http://127.0.0.1:$PORT/health >/dev/null 2>&1; then
    healthy=1
    echo "healthy after about $((i * 2))s"
    break
  fi
  sleep 2
done
if [ "$healthy" -ne 1 ]; then
  echo "still not healthy after ~60s — check: $RT logs $CONTAINER" >&2
  exit 1
fi

python3 - "${TAG#v}" "$MEMORA_DATABASES" "$HEALTH_TOKEN" "$PORT" "$SMOKE_ABSORB" "$MEMORA_REPLICAS" "$MEMORA_REPLICATION" "${21}" "$GRAPH_BIND" "$GRAPH_PORT" "$GRAPH_TOKEN_PATH" <<'PY'
import json, sys, time, urllib.error, urllib.request

EXPECTED_VERSION = sys.argv[1]
STORES = list(json.loads(sys.argv[2]))
HEALTH_TOKEN = sys.argv[3]
ROOT = f"http://127.0.0.1:{sys.argv[4]}"
SMOKE_ABSORB = sys.argv[5] == "1"
REPLICAS = json.loads(sys.argv[6] or "{}")   # the local-primary switches this deploy set
REPLICATION = sys.argv[7]
STORE_WAIT_S = float(sys.argv[8])   # 90 in production (DEPLOY_STORE_WAIT_S: rehearsals only)
GRAPH_ROOT = f"http://{sys.argv[9]}:{sys.argv[10]}"
GRAPH_TOKEN_PATH = sys.argv[11]
L6 = ("rollback: a store already cut over (in MEMORA_REPLICAS) follows docs/cutover-runbook.md \"Rollback\" "
      "and the L6 runbook, docs/local-primary-implementation.md §5.3")
BASE = f"{ROOT}/mcp/memora"

# The version the RUNNING process reports -- a stale image or a failed
# rebuild would still answer /health, just with the old version.
with urllib.request.urlopen(f"{ROOT}/health", timeout=10) as resp:
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


# The dry-run absorb needs the configured LLM; a rehearsal without one skips it.
if SMOKE_ABSORB:
    facts = [
        "deploy-check fact one about the v0.5.0 rollout",
        "deploy-check fact two about the v0.5.0 rollout",
        "deploy-check fact three about the v0.5.0 rollout",
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
else:
    print("memory_absorb smoke check skipped (DEPLOY_SMOKE_ABSORB=0)")

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

def _replication_problem(store, body):
    """None when a store of MEMORA_REPLICAS replicates as configured (leader
    7789): a replication block, the configured mode and replica URI, not
    refused or halted, the sync schema read (sync_state present) at the
    current trigger version. Otherwise (terminal, reason): terminal means
    waiting will not fix it."""
    if body.get("refused"):
        return True, f"the store is refused: {body['refused']}"
    rep = body.get("replication")
    if not isinstance(rep, dict):
        return False, "no replication block (the replicator has not started)"
    if rep.get("status") == "refused":
        return True, f"replication refused: {rep.get('error')}"
    if rep.get("status") == "halted" or rep.get("halted_reason"):
        return True, f"replication halted: {rep.get('halted_reason')}"
    if "head_seq" not in rep or "trigger_version" not in rep:
        return False, f"the replicator has not read its sync state yet (status {rep.get('status')!r})"
    if rep.get("mode") != REPLICATION:
        return True, f"replication mode {rep.get('mode')!r}, configured {REPLICATION!r}"
    if rep.get("replica_uri") != REPLICAS[store]:
        return True, f"replica_uri {rep.get('replica_uri')!r}, configured {REPLICAS[store]!r}"
    if rep.get("trigger_version") != rep.get("trigger_version_expected"):
        return True, (f"sync trigger version {rep.get('trigger_version')!r}, "
                      f"this build expects {rep.get('trigger_version_expected')!r}")
    if rep.get("status") != "running":
        # e.g. "backoff" after a D1 auth or network error (review 7841 P1):
        # waited for (it may recover), then a failure naming the last error
        return False, (f"replication status {rep.get('status')!r}, not running "
                       f"(last_error {rep.get('last_error')!r}, lag_rows {rep.get('lag_rows')!r})")
    return None


failed = []
for store in STORES:
    deadline = time.time() + STORE_WAIT_S
    healthy = False
    problem = None
    while not healthy:
        status, body = _store_health(store)
        healthy = status == 200 and body.get("status") == "ok" and body.get("stale") is False
        if healthy and store in REPLICAS:
            problem = _replication_problem(store, body)
            healthy = problem is None
            if problem and problem[0]:
                break  # terminal: waiting will not fix it
        if not healthy and time.time() > deadline:
            break
        if not healthy:
            time.sleep(3)
    if problem:
        failed.append(f"{store}: {problem[1]} (/health/db/{store}: {json.dumps(body)[:400]}); {L6}")
        continue
    if not healthy:
        failed.append(f"{store}: /health/db/{store} not a current 200 ok after {STORE_WAIT_S:g}s "
                      f"(last: HTTP {status}, {json.dumps(body)[:300]})")
        continue
    if store in REPLICAS:
        rep = body["replication"]
        print(f"store {store}: replicating ({rep['mode']}) to {rep['replica_uri']}, status {rep.get('status')}, "
              f"trigger version {rep['trigger_version']}, lag_rows {rep.get('lag_rows')}")
    # A store that comes up FROZEN (the persisted freeze of a cutover,
    # scripts/cutover_store.sh step e) serves reads (X2), so memory_stats
    # below checks it like any other; a replicated one also passed the
    # replication checks above. Frozen-UNSAFE (open intents) fails.
    freeze = (body.get("freeze") or {}).get("state")
    if freeze == "frozen-unsafe":
        failed.append(f"{store}: frozen-unsafe after the restart: {json.dumps(body.get('freeze'))[:300]}")
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
    print(f"store {store}: /health/db 200 ok{', FROZEN' if freeze == 'frozen' else ''} "
          f"(latency {body.get('latency_ms')} ms); memory_stats "
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
# The graph UI (G1), on its published address: refuses without the token,
# serves the registry's stores with it. The token is read here, never printed.
def _graph_get(path, token=None):
    req = urllib.request.Request(GRAPH_ROOT + path, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}

graph_deadline = time.time() + 30
while True:
    try:
        anon = _graph_get("/api/databases")
        break
    except (urllib.error.URLError, OSError) as exc:
        if time.time() > graph_deadline:
            print(f"graph UI not reachable at {GRAPH_ROOT} ({exc})", file=sys.stderr)
            sys.exit(1)
        time.sleep(2)
if anon[0] != 401 or not anon[1].get("memora_graph"):
    print(f"graph UI at {GRAPH_ROOT} answered {anon[0]} without the token: it must refuse (401)", file=sys.stderr)
    sys.exit(1)
graph_token = open(GRAPH_TOKEN_PATH).read().strip()
status, dbs = _graph_get("/api/databases", graph_token)
if status != 200 or sorted(dbs.get("databases") or []) != sorted(STORES):
    print(f"graph UI /api/databases with the token: HTTP {status}, {json.dumps(dbs)[:300]}", file=sys.stderr)
    sys.exit(1)
print(f"graph UI at {GRAPH_ROOT}: refuses without the token; serves {len(STORES)} stores with it")

if api_status != 404:
    print(f"/api/v1/memora/health answered {api_status}: the API is registered, but this deploy "
          "sets no MEMORA_API_TOKENS_FILE", file=sys.stderr)
    sys.exit(1)
print(f"/api/v1 not registered (/api/v1/memora/health: {api_status})")
PY
REMOTE
