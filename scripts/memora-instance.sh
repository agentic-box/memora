#!/usr/bin/env bash
# Deploy one memora container + its supervised proxy, from a per-instance config.
#
# An instance is one container + one supervised proxy + one LaunchAgent.
# It serves EITHER a single store (STORAGE_URI or VOLUME) or, since memora #965,
# a REGISTRY of stores selected per session by URL path (MEMORA_DATABASES), which
# is how one container serves every workspace. Adding a store is a new file in
# instances/ -- or a new entry in an existing registry -- not a fork of this script.
#
#   ./memora-instance.sh build   NAME          # build NAME's IMAGE tag (omit NAME => memora-pilot)
#   ./memora-instance.sh up      NAME          # run NAME's IMAGE
#   ./memora-instance.sh proxy   NAME          # generate + print the plist install commands
#   ./memora-instance.sh status  [NAME|all]
#   ./memora-instance.sh down    NAME
#   ./memora-instance.sh logs    NAME
#   ./memora-instance.sh config  NAME          # show resolved config (secrets redacted)
#
# instances/NAME.env fields:
#   INSTANCE      short name (container becomes memora-<INSTANCE>)
#   PORT          host port the proxy listens on (127.0.0.1:<PORT>)
#   STORAGE_URI   d1://account/database   (single-store instance)
#   VOLUME        host dir mounted at /data (single-store, local sqlite)
#                 Otherwise, whenever a store keeps state under /data (any
#                 registry entry or STORAGE_URI that is not s3://), up mounts
#                 the NAMED volume memora-<INSTANCE>-data at /data and passes
#                 MEMORA_DATA_VOLUME; the server refuses such stores without it.
#   MEMORA_DATABASES   {"name":"uri",...} registry; serves /mcp/<name> per session
#   MEMORA_DEFAULT_DB  which registry entry a bare /mcp resolves to
#                 At least one of STORAGE_URI, VOLUME, MEMORA_DATABASES.
#                 If several, up uses DATABASES, then STORAGE_URI, then VOLUME.
#   CONTAINER     optional: adopt an existing container name instead of memora-<INSTANCE>
#   IMAGE         optional: pin this instance to its own image tag
#   CRED_SOURCE   optional: this instance's own credential file (see below)
#   MEMORY/CPUS   optional: per-instance VM size (defaults 960M / 2)
#   TOOL_PROFILE  optional: full|leader|agent (default leader — see note below)
#
# CREDENTIALS are never in these files, never in the image, never in git. They
# are read at run time from $CRED_SOURCE (CRED_SOURCE in the instance file, else
# ~/.config/memora/credentials.mcp.json if that file exists, else a
# host-specific fallback). A workspace .mcp.json pointed at the container is
# a bare {type,url} and is not the credential source.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTANCE_DIR="${MEMORA_INSTANCE_DIR:-$ROOT/instances}"
DEFAULT_IMAGE="${MEMORA_IMAGE:-memora-pilot}"
# Credentials live OUTSIDE the workspace .mcp.json, because that file becomes a
# bare {type,url} http entry once a workspace is pointed at a container -- the
# env block would have nowhere to live and nothing would read it.
DEFAULT_CRED_SOURCE="${CRED_SOURCE:-$HOME/.config/memora/credentials.mcp.json}"
# Per-instance health tokens live beside the credentials they are peers of,
# never in the repo and never in an instance .env (those are readable config).
SECRET_DIR="${MEMORA_SECRET_DIR:-$HOME/.config/memora}"
TOKEN_LEN=48                                   # exact health-token length
# Overridable so a test can capture the argv cmd_up would run.
CONTAINER_BIN="${MEMORA_CONTAINER_BIN:-container}"
[ -f "$DEFAULT_CRED_SOURCE" ] || DEFAULT_CRED_SOURCE="$HOME/repos/agentic-box/.mcp.json"
PROXY_BIN="${MEMORA_PROXY_BIN:-$HOME/.local/libexec/memora/memora_proxy.py}"
LOG_DIR="${MEMORA_LOG_DIR:-$HOME/.local/var/log}"
TARGET_PORT="${MEMORA_TARGET_PORT:-8000}"      # port memora listens on INSIDE the container
# Each container is a VM. The default is the local-primary memory gate
# (docs/local-primary-implementation.md §8 L2a): max(768M, 1.5 x the peak RSS
# of one server holding four local stores with FTS and the 384 MB corpus
# cache), rounded up to 64 MiB. scripts/measure_memory_gate.py, run in the
# Linux image (python 3.12.14) on server2: worst peak 629.7 MB (4 x 1500 rows,
# 1536-dim) -> 945 MB -> 960M. The old 512M (measured 116-230 MB on D1-only
# stores, which hold no corpus locally) is too small once stores are local.
DEFAULT_MEMORY="${MEMORA_MEMORY:-960M}"
DEFAULT_CPUS="${MEMORA_CPUS:-2}"
# One container serves EVERY agent in a workspace, leader and workers alike,
# so the profile must be the SUPERSET the leader needs. 'agent' (12 tools)
# would strip create_section/store_document/delete/digest/tags from the leader.
DEFAULT_TOOL_PROFILE="${MEMORA_TOOL_PROFILE:-leader}"
MAX_CONN="${MEMORA_PROXY_MAX_CONN:-64}"
# 2s was calibrated on an idle host. Forking a subprocess under memory pressure
# legitimately takes seconds, and on 2026-08-20 that took every workspace offline
# (memora #982). The lookup is cached, so a larger budget costs almost nothing.
RESOLVE_TIMEOUT="${MEMORA_PROXY_RESOLVE_TIMEOUT:-10}"
CONNECT_TIMEOUT="${MEMORA_PROXY_CONNECT_TIMEOUT:-2}"
STALE_GRACE="${MEMORA_PROXY_STALE_GRACE:-300}"

die() { echo "error: $*" >&2; exit 1; }

load() {  # load instances/<name>.env into INSTANCE/PORT/STORAGE_URI/VOLUME
  local name="${1:-}"
  [ -n "$name" ] || die "instance name required (have: $(ls "$INSTANCE_DIR" | sed 's/\.env$//' | tr '\n' ' '))"
  local f="$INSTANCE_DIR/$name.env"
  [ -f "$f" ] || die "no config at $f"
  # CRED_SOURCE is per-instance: workspaces do NOT all define the same env.
  # bestation and re omit the AWS/R2 backup vars that agentic-box sets, so
  # sharing one credential file would silently switch cloud backup ON for
  # stores that never had it. Reset it every load so one instance cannot
  # inherit the previous one's source during `status all`.
  INSTANCE=""; PORT=""; STORAGE_URI=""; VOLUME=""; CONTAINER=""; IMAGE=""; CRED_SOURCE=""; MEMORY=""; CPUS=""; TOOL_PROFILE=""; MEMORA_DATABASES=""; MEMORA_DEFAULT_DB=""
  # shellcheck disable=SC1090
  set -a; . "$f"; set +a
  VOLUME="${VOLUME/#\$HOME/$HOME}"
  [ -n "$INSTANCE" ] || die "$f: INSTANCE missing"
  [ -n "$PORT" ]     || die "$f: PORT missing"
  [ -n "$STORAGE_URI" ] || [ -n "$VOLUME" ] || [ -n "$MEMORA_DATABASES" ] || die "$f: needs STORAGE_URI, VOLUME or MEMORA_DATABASES"
  # CONTAINER may be set by the config to adopt a container created elsewhere.
  CONTAINER="${CONTAINER:-memora-$INSTANCE}"
  # A config may pin its own image tag so rebuilding for one instance cannot
  # change what a different instance gets on its next restart.
  IMAGE="${IMAGE:-$DEFAULT_IMAGE}"
  TOOL_PROFILE="${TOOL_PROFILE:-$DEFAULT_TOOL_PROFILE}"
  MEMORY="${MEMORY:-$DEFAULT_MEMORY}"
  CPUS="${CPUS:-$DEFAULT_CPUS}"
  CRED_SOURCE="${CRED_SOURCE:-$DEFAULT_CRED_SOURCE}"
  CRED_SOURCE="${CRED_SOURCE/#\$HOME/$HOME}"
  LABEL="com.memora.proxy.$CONTAINER"
}

cred() { python3 -c "import json;print(json.load(open('$CRED_SOURCE'))['mcpServers']['memora']['env'].get('$1',''))"; }

cred_args() {
  # Pass through EVERY env var the credential source defines, not a hand-picked
  # few. The direct config carries LLM keys (memory_absorb's consolidation),
  # cloud-graph settings, and tuning vars; a container started with only the
  # embedding keys would silently lose those features rather than fail loudly.
  # MEMORA_STORAGE_URI and MEMORA_DB_PATH are excluded because the instance
  # config owns which database this container serves.
  [ -f "$CRED_SOURCE" ] || die "no credential source at $CRED_SOURCE"
  python3 - "$CRED_SOURCE" <<'PYEOF'
import json, sys
env = json.load(open(sys.argv[1]))["mcpServers"]["memora"].get("env", {})
# ROUTING IS INSTANCE-OWNED. cmd_up appends the instance's registry FIRST and
# credentials AFTER, so a stale MEMORA_DATABASES left in a credential file
# would win as the later duplicate -e and start the container against the
# wrong set of databases -- silently, and with cross-database consequences.
# The /data volume marker and the admin token are instance-owned for the
# same reason: a stale copy in a credential file would override them.
skip = {"MEMORA_STORAGE_URI", "MEMORA_DB_PATH",
        "MEMORA_DATABASES", "MEMORA_DEFAULT_DB",
        "MEMORA_DATA_VOLUME", "MEMORA_ADMIN_TOKEN"}
out = []
for k, v in env.items():
    if k in skip or v == "":
        continue
    out += ["-e", f"{k}={v}"]
sys.stdout.write("\0".join(out) + ("\0" if out else ""))
PYEOF
}

cmd_build() {  # build [name] -- with a name, build that instance's image tag
  local tag="$DEFAULT_IMAGE"
  if [ -n "${1:-}" ]; then load "$1"; tag="$IMAGE"; fi
  echo "building $tag from $ROOT"
  "$CONTAINER_BIN" build -t "$tag" -f "$ROOT/Dockerfile" "$ROOT"
}

health_token() {  # per-instance secret so an operator can read health DETAIL
  # Requests reach the container through the proxy, so their peer address is
  # the bridge host, never loopback -- without a token the detailed readiness
  # body is unreachable and only an aggregate status is served (memora #996).
  secret_token health
}

admin_token() {  # per-instance secret for /admin/* (memora/admin.py)
  # Separate from the health token, which every prober holds: from L2 on the
  # admin routes place and lift write freezes. The server refuses to start if
  # the two are equal.
  #
  # An EXISTING admin-token file must be a regular file (not a symlink),
  # owned by this user, mode 0600. Anything else is treated as possibly
  # compromised: refuse and say how to re-mint, never chmod it into shape --
  # a token that was world-readable must get a new value.
  local f="$SECRET_DIR/$INSTANCE.admin-token"
  if [ -e "$f" ] || [ -L "$f" ]; then
    secret_file_is_private "$f" \
      || die "$f must be a regular file owned by $(id -un) with mode 0600; it may have been exposed: rm '$f' and rerun to mint a new token"
  fi
  secret_token admin
}

secret_file_is_private() {  # secret_file_is_private PATH -- regular, not a symlink, ours, 0600
  python3 - "$1" <<'PYEOF'
import os, stat, sys
st = os.lstat(sys.argv[1])
ok = stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) == 0o600
sys.exit(0 if ok else 1)
PYEOF
}

secret_token() {  # secret_token KIND -- read or mint $SECRET_DIR/$INSTANCE.KIND-token
  local kind="$1"
  local f="$SECRET_DIR/$INSTANCE.$kind-token"
  mkdir -p "$SECRET_DIR"; chmod 700 "$SECRET_DIR"

  # WHOLE-FILE validation, on EVERY read rather than only at creation. A
  # line-based check accepts a good first line followed by anything at all,
  # and command substitution keeps the embedded newlines -- which would then
  # be written straight into curl's config file. Require exactly TOKEN_LEN
  # alphanumerics and nothing else, no trailing newline.
  local valid=0
  if [ -f "$f" ] && [ "$(wc -c <"$f")" -eq "$TOKEN_LEN" ]; then
    if [ "$(LC_ALL=C tr -d 'A-Za-z0-9' <"$f" | wc -c | tr -d ' ')" -eq 0 ]; then
      valid=1
    fi
  fi

  if [ "$valid" -eq 1 ]; then
    chmod 600 "$f"
  else
    if [ -e "$f" ]; then echo "replacing unusable $kind token at $f" >&2; fi
    # Temp file + rename: a reader must never see a half-written token, and a
    # crash must not leave one behind. The subshell drops pipefail because
    # `head -c` closing the pipe SIGPIPEs `tr`, which would otherwise abort
    # the whole script under `set -euo pipefail`.
    local tmp; tmp="$(mktemp "$SECRET_DIR/.token.XXXXXX")"
    chmod 600 "$tmp"
    ( set +o pipefail
      LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$TOKEN_LEN" ) > "$tmp"
    mv -f "$tmp" "$f"
  fi
  cat "$f"
}

uri_needs_data() {  # uri_needs_data URI -- does this store keep state under /data?
  # Everything but s3:// (whose cache is disposable): a local SQLite path, and
  # a d1:// primary, whose write gate journals to /data/intent/ from L2 on.
  case "$1" in s3://*) return 1 ;; *) return 0 ;; esac
}

registry_needs_data() {  # registry_needs_data JSON -- does any entry?
  local uris
  uris="$(python3 -c 'import json,sys;print("\n".join(json.loads(sys.argv[1]).values()))' "$1")" \
    || die "MEMORA_DATABASES is not a JSON object of name -> uri"
  local u
  while IFS= read -r u; do
    [ -n "$u" ] && uri_needs_data "$u" && return 0
  done <<< "$uris"
  return 1
}

ensure_volume() {  # ensure_volume NAME -- the named volume exists afterwards
  "$CONTAINER_BIN" volume inspect "$1" >/dev/null 2>&1 \
    || "$CONTAINER_BIN" volume create "$1" >/dev/null \
    || die "could not create volume $1"
}

current_data_mount() {  # current_data_mount NAME -- the volume (or host dir) NAME mounts at /data
  # Empty when there is no such container or no /data mount. Parses the
  # runtime's inspect JSON loosely: docker's Mounts[] {Name|Source,
  # Destination} and nested {source|name, destination|target} objects.
  local out
  out="$("$CONTAINER_BIN" inspect "$1" 2>/dev/null)" || return 0
  printf '%s' "$out" | python3 -c '
import json, sys
try:
    doc = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
found = []
def walk(o):
    if isinstance(o, dict):
        dest = o.get("Destination") or o.get("destination") or o.get("target")
        if dest == "/data":
            vol = (o.get("type") or {}).get("volume") if isinstance(o.get("type"), dict) else None
            name = o.get("Name") or o.get("name") or (vol or {}).get("name") or o.get("Source") or o.get("source")
            if name:
                found.append(name)
        for v in o.values():
            walk(v)
    elif isinstance(o, list):
        for v in o:
            walk(v)
walk(doc)
print(found[0] if found else "")
'
}

migrate_data() {  # migrate_data OLD NEW -- staged, verified copy while stopped
  # scripts/migrate_data_volume.sh (shared with deploy-memora-all.sh): skips
  # when NEW already holds a verified copy of OLD's current content.
  "$CONTAINER_BIN" run --rm -v "$1:/from:ro" -v "$2:/to" "$IMAGE" \
    sh -c "$(cat "$ROOT/scripts/migrate_data_volume.sh")" migrate_data_volume migrate "$1"
}

cmd_up() {
  load "$1"
  # Everything that can refuse runs BEFORE the running container is touched:
  # tokens, the registry, the named volume.
  local htok atok
  htok="$(health_token)"; atok="$(admin_token)"
  [ "$htok" != "$atok" ] || die "admin token equals health token; delete $SECRET_DIR/$INSTANCE.admin-token and rerun"
  local data_vol=""
  if [ -n "${MEMORA_DATABASES:-}" ]; then
    if registry_needs_data "$MEMORA_DATABASES"; then data_vol="memora-$INSTANCE-data"; fi
  elif [ -n "$STORAGE_URI" ]; then
    if uri_needs_data "$STORAGE_URI"; then data_vol="memora-$INSTANCE-data"; fi
  fi
  # What the running container keeps at /data now: an anonymous volume on
  # an instance started before L2a (the image declares VOLUME /data).
  local old_vol=""
  old_vol="$(current_data_mount "$CONTAINER")"
  [ -z "$data_vol" ] || ensure_volume "$data_vol"
  # Every runtime call goes through $CONTAINER_BIN, not just the final `run`.
  # Intercepting only `run` left stop/rm hitting the REAL runtime, so a test
  # could delete a genuine container that happened to share the instance name.
  "$CONTAINER_BIN" stop "$CONTAINER" >/dev/null 2>&1 || true
  if [ -n "$data_vol" ] && [ -n "$old_vol" ] && [ "$old_vol" != "$data_vol" ]; then
    # Carry the old /data (SQLite files, freeze files, intent journals) into
    # the named volume while nothing runs on it. The old container is kept
    # (renamed, stopped) with its volume, for rollback.
    if "$CONTAINER_BIN" list 2>/dev/null | awk -v n="$CONTAINER" '$1==n{f=1} END{exit !f}'; then
      die "$CONTAINER is still running; not copying $old_vol"
    fi
    echo "copying /data from $old_vol into $data_vol (staged, verified)"
    migrate_data "$old_vol" "$data_vol" \
      || die "copy $old_vol -> $data_vol failed; $CONTAINER is stopped, unchanged: $CONTAINER_BIN start $CONTAINER"
    local kept="$CONTAINER-pre-data-volume-$(date +%s)"
    if "$CONTAINER_BIN" rename "$CONTAINER" "$kept" >/dev/null 2>&1; then
      echo "old container kept stopped as $kept (volume $old_vol); rollback: rm $CONTAINER, rename $kept back, start it"
    else
      # The runtime cannot rename: remove the container as up always did.
      # Its volume is not removed by rm; the verified copy is in $data_vol.
      "$CONTAINER_BIN" rm "$CONTAINER" >/dev/null 2>&1 || true
      echo "old container removed (runtime has no rename); old volume $old_vol kept"
    fi
  else
    "$CONTAINER_BIN" rm   "$CONTAINER" >/dev/null 2>&1 || true
  fi
  local args=(run -d --name "$CONTAINER" --memory "$MEMORY" --cpus "$CPUS" -e "MEMORA_TOOL_PROFILE=$TOOL_PROFILE")
  args+=(-e "MEMORA_HEALTH_TOKEN=$htok" -e "MEMORA_ADMIN_TOKEN=$atok")
  args+=(-e "MEMORA_HEALTH_TIMEOUT=${MEMORA_HEALTH_TIMEOUT:-15}")
  args+=(-e "MEMORA_HEALTH_REFRESH_INTERVAL=${MEMORA_HEALTH_REFRESH_INTERVAL:-15}")
  # Vector-scan page size. The default of 1000 means a store under 1000 rows
  # returns its ENTIRE corpus -- every embedding blob, plus each row's content
  # and tags -- in ONE D1 response, so pagination never engages where it is
  # most needed. At 902 memories that single response is ~5.4 MB of vectors
  # before content, and it races the 30s ceiling that applies to each request
  # (urllib timeout AND Cloudflare's own per-request limit). absorb then times
  # out mid-write and its compensating delete erases the row it had inserted,
  # reporting "nothing written". 100 keeps each response an order of magnitude
  # smaller for ~0.5-2s of extra round-trip per scan.
  #
  # This is a TOURNIQUET, not the fix: it converts failure into slowness and
  # leaves the O(facts x corpus) transfer untouched. The fix is one skinny
  # corpus snapshot per absorb call, reused across facts and crossrefs.
  args+=(-e "MEMORA_VECTOR_SCAN_PAGE_SIZE=${MEMORA_VECTOR_SCAN_PAGE_SIZE:-100}")
  # A multi-database instance carries a REGISTRY instead of one storage URI;
  # it is what makes a single container serve every workspace by URL path.
  #
  # /data: the image declares VOLUME /data, so without a -v every recreate
  # gets a NEW anonymous volume and whatever was under /data is gone. A store
  # that keeps state there gets the NAMED volume memora-<INSTANCE>-data, which
  # survives stop/rm/run, and MEMORA_DATA_VOLUME names it; the server refuses
  # such a store without both (memora/data_volume.py).
  if [ -n "${MEMORA_DATABASES:-}" ]; then
    args+=(-e "MEMORA_DATABASES=$MEMORA_DATABASES" -e "MEMORA_DEFAULT_DB=${MEMORA_DEFAULT_DB:-}")
  elif [ -n "$STORAGE_URI" ]; then
    # CLOUDFLARE_API_TOKEN comes through cred_args with everything else.
    args+=(-e "MEMORA_STORAGE_URI=$STORAGE_URI")
  else
    # A host directory is a bind mount: a real mount, and it outlives the
    # container. Its path is the marker.
    mkdir -p "$VOLUME"; args+=(-v "$VOLUME:/data" -e "MEMORA_DATA_VOLUME=$VOLUME")
  fi
  if [ -n "$data_vol" ]; then
    args+=(-v "$data_vol:/data" -e "MEMORA_DATA_VOLUME=$data_vol")
  fi
  while IFS= read -r -d '' a; do args+=("$a"); done < <(cred_args)
  args+=("$IMAGE")
  "$CONTAINER_BIN" "${args[@]}" >/dev/null
  local what
  if [ -n "$MEMORA_DATABASES" ]; then
    what="registry: $(python3 -c "import json,sys;print(', '.join(sorted(json.loads(sys.argv[1]))))" "$MEMORA_DATABASES") (default=${MEMORA_DEFAULT_DB:-})"
  elif [ -n "$STORAGE_URI" ]; then
    what="D1 ${STORAGE_URI##*/}"
  else
    what="sqlite $VOLUME"
  fi
  echo "$CONTAINER up ($what) -- proxy :$PORT"
}

cmd_proxy() {
  load "$1"
  local out="$ROOT/launchd/generated/$LABEL.plist"
  mkdir -p "$(dirname "$out")"
  cat > "$out" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>$PROXY_BIN</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <!-- PATH is required: launchd gives a job a minimal PATH that does NOT
             contain the \`container\` CLI, and the proxy shells out to it to
             resolve the container's (constantly changing) IP. -->
        <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
        <key>MEMORA_PROXY_CONTAINER</key><string>$CONTAINER</string>
        <key>MEMORA_PROXY_LISTEN_HOST</key><string>127.0.0.1</string>
        <key>MEMORA_PROXY_LISTEN_PORT</key><string>$PORT</string>
        <key>MEMORA_PROXY_TARGET_PORT</key><string>$TARGET_PORT</string>
        <key>MEMORA_PROXY_MAX_CONN</key><string>$MAX_CONN</string>
        <key>MEMORA_PROXY_RESOLVE_TIMEOUT</key><string>$RESOLVE_TIMEOUT</string>
        <key>MEMORA_PROXY_CONNECT_TIMEOUT</key><string>$CONNECT_TIMEOUT</string>
        <key>MEMORA_PROXY_STALE_GRACE</key><string>$STALE_GRACE</string>
        <key>MEMORA_PROXY_LOG</key><string>$LOG_DIR/memora-proxy-$INSTANCE.log</string>
    </dict>
    <key>ProcessType</key><string>Background</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>5</integer>
    <key>StandardOutPath</key><string>$LOG_DIR/memora-proxy-$INSTANCE.stdout.log</string>
    <key>StandardErrorPath</key><string>$LOG_DIR/memora-proxy-$INSTANCE.stderr.log</string>
</dict>
</plist>
PLIST
  plutil -lint "$out" >/dev/null || die "generated plist failed lint"
  echo "wrote $out"
  echo
  echo "install (you run this -- it loads a supervised service):"
  echo "  U=\$(id -u); cp '$out' ~/Library/LaunchAgents/ && \\"
  echo "    launchctl bootstrap gui/\$U ~/Library/LaunchAgents/$LABEL.plist && \\"
  echo "    launchctl enable gui/\$U/$LABEL && sleep 2 && lsof -nP -iTCP:$PORT -sTCP:LISTEN"
  echo
  echo "workspace .mcp.json:  {\"mcpServers\":{\"memora\":{\"type\":\"http\",\"url\":\"http://127.0.0.1:$PORT/mcp\"}}}"
}

one_status() {
  load "$1"
  local state; state=$("$CONTAINER_BIN" list 2>/dev/null | awk -v n="$CONTAINER" '$1==n{print $5" "$6}')
  # On a connection failure curl still PRINTS 000 and exits non-zero, so a
  # `|| echo 000` fallback would concatenate into "000000". Swallow the status
  # instead and let the printed code stand on its own.
  local code; code=$(curl -sS -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/mcp" 2>/dev/null; true)
  local health; case "$code" in
    406) health="OK (406 = MCP answering)" ;;   # bare GET is rejected by MCP; the server answered
    000) health="DOWN (no answer)" ;;
    *)   health="HTTP $code" ;;
  esac
  printf '%-18s container: %-28s proxy :%-5s %s\n' "$INSTANCE" "${state:-not running}" "$PORT" "$health"
}

cmd_status() {
  if [ "${1:-all}" = "all" ]; then
    for f in "$INSTANCE_DIR"/*.env; do
      local n; n="$(basename "$f" .env)"
      [ "$n" = "example" ] && continue   # a template, not a deployment
      one_status "$n"
    done
  else one_status "$1"; fi
}

cmd_health() {  # health [name] -- per-database readiness, with detail
  load "$1"
  # The token goes through a 0600 curl config file, never argv: process
  # arguments are readable by any local process for the life of the call.
  local cfg; cfg="$(mktemp "${TMPDIR:-/tmp}/memora-health.XXXXXX")"
  chmod 600 "$cfg"
  trap 'rm -f "$cfg"' RETURN
  printf 'header = "Authorization: Bearer %s"\n' "$(health_token)" > "$cfg"
  curl -fsS --max-time 30 --config "$cfg" \
       "http://127.0.0.1:$PORT/health/db" 2>/dev/null \
    | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("  no readiness body (is the container up?)"); raise SystemExit(1)
if "databases" not in d:
    # Aggregate-only means the token did not authorise -- say so rather than
    # printing a bare "unknown", which is what #996 looked like.
    print("  status %s (aggregate only -- token not accepted)" % d.get("status"))
    raise SystemExit(1)
print("  status %s   age %ss%s" % (d.get("status"), d.get("age_seconds"),
                                   "  STALE" if d.get("stale") else ""))
for n, v in sorted(d["databases"].items()):
    line = "  %-12s %-8s %sms" % (n, v.get("status"), v.get("latency_ms"))
    if v.get("message"): line += "  " + str(v["message"])[:80]
    print(line)
' || echo "  readiness unavailable on :$PORT"
}

cmd_config() {
  load "$1"
  echo "instance      $INSTANCE"
  echo "container     $CONTAINER"
  echo "image         $IMAGE"
  echo "resources     memory=$MEMORY cpus=$CPUS"
  echo "tool profile  $TOOL_PROFILE"
  echo "proxy port    $PORT  (http://127.0.0.1:$PORT/mcp)"
  if [ -n "$MEMORA_DATABASES" ]; then
    echo "storage       registry: $(python3 -c "import json,sys;print(', '.join(sorted(json.loads(sys.argv[1]))))" "$MEMORA_DATABASES") (default=$MEMORA_DEFAULT_DB)"
  elif [ -n "$STORAGE_URI" ]; then
    echo "storage       $STORAGE_URI"
  else
    echo "storage       sqlite $VOLUME"
  fi
  echo "credentials   $CRED_SOURCE (read at run time, never baked in)"
  echo "launchd label $LABEL"
}

# Sourceable: `source memora-instance.sh` exposes the functions without
# running the dispatcher, which is what lets the token logic be tested.
if [ "${BASH_SOURCE[0]}" != "${0}" ]; then return 0 2>/dev/null || true; fi

case "${1:-}" in
  build)  cmd_build "${2:-}" ;;
  up)     cmd_up "${2:-}" ;;
  proxy)  cmd_proxy "${2:-}" ;;
  status) cmd_status "${2:-all}" ;;
  config) cmd_config "${2:-}" ;;
  health) cmd_health "${2:-}" ;;
  down)   load "${2:-}"; "$CONTAINER_BIN" stop "$CONTAINER" >/dev/null 2>&1 || true; echo "$CONTAINER stopped" ;;
  logs)   load "${2:-}"; "$CONTAINER_BIN" logs "$CONTAINER" ;;
  *) sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
