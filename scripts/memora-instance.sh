#!/usr/bin/env bash
# Deploy one memora container + its supervised proxy, from a per-instance config.
#
# An instance is one container + one supervised proxy + one LaunchAgent.
# It serves EITHER a single store (STORAGE_URI or VOLUME) or, since memora #965,
# a REGISTRY of stores selected per session by URL path (MEMORA_DATABASES), which
# is how one container serves every workspace. Adding a store is a new file in
# instances/ -- or a new entry in an existing registry -- not a fork of this script.
#
#   ./memora-instance.sh build   [name]        # build the shared image, or one instance's tag
#   ./memora-instance.sh up      <name>        # run the container
#   ./memora-instance.sh proxy   <name>        # generate + print the plist install commands
#   ./memora-instance.sh status  [name|all]
#   ./memora-instance.sh down    <name>
#   ./memora-instance.sh logs    <name>
#   ./memora-instance.sh config  <name>        # show resolved config (secrets redacted)
#
# instances/<name>.env fields:
#   INSTANCE      short name (container becomes memora-<INSTANCE>)
#   PORT          host port the proxy listens on (127.0.0.1:<PORT>)
#   STORAGE_URI   d1://account/database   (single-store instance)
#   VOLUME        host dir mounted at /data (single-store, local sqlite)
#   MEMORA_DATABASES   {"name":"uri",...} registry; serves /mcp/<name> per session
#   MEMORA_DEFAULT_DB  which registry entry a bare /mcp resolves to
#                 Exactly one of STORAGE_URI, VOLUME or MEMORA_DATABASES is required.
#                 Routing is INSTANCE-owned: a credential file may not supply it.
#   CONTAINER     optional: adopt an existing container name instead of memora-<INSTANCE>
#   IMAGE         optional: pin this instance to its own image tag
#   CRED_SOURCE   optional: this instance's own credential file (see below)
#   MEMORY/CPUS   optional: per-instance VM size (defaults 512M / 2)
#   TOOL_PROFILE  optional: full|leader|agent (default leader — see note below)
#
# CREDENTIALS are never in these files, never in the image, never in git. They
# are read at run time from $CRED_SOURCE (a workspace .mcp.json, untracked).
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
# Each container is a VM. 1024MB was the runtime default; measured use inside a
# live container is 116-230MB, and ~250MB of any figure is VM overhead. 512MB is
# generous and halves the per-VM ceiling on a 16GB host running five workspaces.
DEFAULT_MEMORY="${MEMORA_MEMORY:-512M}"
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
skip = {"MEMORA_STORAGE_URI", "MEMORA_DB_PATH",
        "MEMORA_DATABASES", "MEMORA_DEFAULT_DB"}
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
  container build -t "$tag" -f "$ROOT/Dockerfile" "$ROOT"
}

health_token() {  # per-instance secret so an operator can read health DETAIL
  # Requests reach the container through the proxy, so their peer address is
  # the bridge host, never loopback -- without a token the detailed readiness
  # body is unreachable and only an aggregate status is served (memora #996).
  local f="$SECRET_DIR/$INSTANCE.health-token"
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
    if [ -e "$f" ]; then echo "replacing unusable health token at $f" >&2; fi
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

cmd_up() {
  load "$1"
  container stop "$CONTAINER" >/dev/null 2>&1 || true
  container rm   "$CONTAINER" >/dev/null 2>&1 || true
  local args=(run -d --name "$CONTAINER" --memory "$MEMORY" --cpus "$CPUS" -e "MEMORA_TOOL_PROFILE=$TOOL_PROFILE")
  args+=(-e "MEMORA_HEALTH_TOKEN=$(health_token)")
  args+=(-e "MEMORA_HEALTH_TIMEOUT=${MEMORA_HEALTH_TIMEOUT:-15}")
  args+=(-e "MEMORA_HEALTH_REFRESH_INTERVAL=${MEMORA_HEALTH_REFRESH_INTERVAL:-15}")
  # A multi-database instance carries a REGISTRY instead of one storage URI;
  # it is what makes a single container serve every workspace by URL path.
  if [ -n "${MEMORA_DATABASES:-}" ]; then
    args+=(-e "MEMORA_DATABASES=$MEMORA_DATABASES" -e "MEMORA_DEFAULT_DB=${MEMORA_DEFAULT_DB:-}")
  elif [ -n "$STORAGE_URI" ]; then
    # CLOUDFLARE_API_TOKEN comes through cred_args with everything else.
    args+=(-e "MEMORA_STORAGE_URI=$STORAGE_URI")
  else
    mkdir -p "$VOLUME"; args+=(-v "$VOLUME:/data")
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
  local state; state=$(container list 2>/dev/null | awk -v n="$CONTAINER" '$1==n{print $5" "$6}')
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
  down)   load "${2:-}"; container stop "$CONTAINER" >/dev/null 2>&1 || true; echo "$CONTAINER stopped" ;;
  logs)   load "${2:-}"; container logs "$CONTAINER" ;;
  *) sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
