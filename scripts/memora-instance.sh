#!/usr/bin/env bash
# Deploy one memora container + its supervised proxy, from a per-instance config.
#
# One store per instance: a memora process binds ONE database for its lifetime
# (storage.py resolves STORAGE_BACKEND at import), so each database gets its own
# container, its own proxy port, and its own LaunchAgent. Adding a store is a new
# file in instances/, not a fork of this script.
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
#   STORAGE_URI   d1://account/database   (omit for local sqlite)
#   VOLUME        host dir mounted at /data (local sqlite only)
#   CONTAINER     optional: adopt an existing container name instead of memora-<INSTANCE>
#   IMAGE         optional: pin this instance to its own image tag
#   CRED_SOURCE   optional: this instance's own credential file (see below)
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
[ -f "$DEFAULT_CRED_SOURCE" ] || DEFAULT_CRED_SOURCE="$HOME/repos/agentic-box/.mcp.json"
PROXY_BIN="${MEMORA_PROXY_BIN:-$HOME/.local/libexec/memora/memora_proxy.py}"
LOG_DIR="${MEMORA_LOG_DIR:-$HOME/.local/var/log}"
TARGET_PORT="${MEMORA_TARGET_PORT:-8000}"      # port memora listens on INSIDE the container
MAX_CONN="${MEMORA_PROXY_MAX_CONN:-64}"
RESOLVE_TIMEOUT="${MEMORA_PROXY_RESOLVE_TIMEOUT:-2}"
CONNECT_TIMEOUT="${MEMORA_PROXY_CONNECT_TIMEOUT:-2}"

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
  INSTANCE=""; PORT=""; STORAGE_URI=""; VOLUME=""; CONTAINER=""; IMAGE=""; CRED_SOURCE=""
  # shellcheck disable=SC1090
  set -a; . "$f"; set +a
  VOLUME="${VOLUME/#\$HOME/$HOME}"
  [ -n "$INSTANCE" ] || die "$f: INSTANCE missing"
  [ -n "$PORT" ]     || die "$f: PORT missing"
  [ -n "$STORAGE_URI" ] || [ -n "$VOLUME" ] || die "$f: needs STORAGE_URI or VOLUME"
  # CONTAINER may be set by the config to adopt a container created elsewhere.
  CONTAINER="${CONTAINER:-memora-$INSTANCE}"
  # A config may pin its own image tag so rebuilding for one instance cannot
  # change what a different instance gets on its next restart.
  IMAGE="${IMAGE:-$DEFAULT_IMAGE}"
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
skip = {"MEMORA_STORAGE_URI", "MEMORA_DB_PATH"}
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

cmd_up() {
  load "$1"
  container stop "$CONTAINER" >/dev/null 2>&1 || true
  container rm   "$CONTAINER" >/dev/null 2>&1 || true
  local args=(run -d --name "$CONTAINER")
  if [ -n "$STORAGE_URI" ]; then
    # CLOUDFLARE_API_TOKEN comes through cred_args with everything else.
    args+=(-e "MEMORA_STORAGE_URI=$STORAGE_URI")
  else
    mkdir -p "$VOLUME"; args+=(-v "$VOLUME:/data")
  fi
  while IFS= read -r -d '' a; do args+=("$a"); done < <(cred_args)
  args+=("$IMAGE")
  container "${args[@]}" >/dev/null
  echo "$CONTAINER up ($( [ -n "$STORAGE_URI" ] && echo "D1 ${STORAGE_URI##*/}" || echo "sqlite $VOLUME" )) -- proxy :$PORT"
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
    for f in "$INSTANCE_DIR"/*.env; do one_status "$(basename "$f" .env)"; done
  else one_status "$1"; fi
}

cmd_config() {
  load "$1"
  echo "instance      $INSTANCE"
  echo "container     $CONTAINER"
  echo "image         $IMAGE"
  echo "proxy port    $PORT  (http://127.0.0.1:$PORT/mcp)"
  [ -n "$STORAGE_URI" ] && echo "storage       $STORAGE_URI" || echo "storage       sqlite $VOLUME"
  echo "credentials   $CRED_SOURCE (read at run time, never baked in)"
  echo "launchd label $LABEL"
}

case "${1:-}" in
  build)  cmd_build "${2:-}" ;;
  up)     cmd_up "${2:-}" ;;
  proxy)  cmd_proxy "${2:-}" ;;
  status) cmd_status "${2:-all}" ;;
  config) cmd_config "${2:-}" ;;
  down)   load "${2:-}"; container stop "$CONTAINER" >/dev/null 2>&1 || true; echo "$CONTAINER stopped" ;;
  logs)   load "${2:-}"; container logs "$CONTAINER" ;;
  *) sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
