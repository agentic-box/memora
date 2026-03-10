#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

source ~/.bashrc

worktree_port() {
  local base="$1"
  local span="$2"
  local key
  local sum

  key="$(pwd -P)"
  sum="$(printf '%s' "$key" | cksum | awk '{print $1}')"
  echo $((base + (sum % span)))
}

HOST="${MEMORA_HOST:-127.0.0.1}"
PORT="${MEMORA_PORT:-$(worktree_port 18000 1000)}"
GRAPH_PORT="${MEMORA_GRAPH_PORT:-$(worktree_port 28000 1000)}"
TRANSPORT="${MEMORA_TRANSPORT:-streamable-http}"

CONNECT_HOST="$HOST"
if [[ "$CONNECT_HOST" == "0.0.0.0" || "$CONNECT_HOST" == "::" || -z "$CONNECT_HOST" ]]; then
  CONNECT_HOST="127.0.0.1"
fi

STATE_DIR="$SCRIPT_DIR/.memora-run"
PID_FILE="$STATE_DIR/memora-server.pid"
LOG_FILE="$STATE_DIR/memora-server.log"
GRAPH_URL="http://${CONNECT_HOST}:${GRAPH_PORT}/graph"
GRAPH_API_URL="http://${CONNECT_HOST}:${GRAPH_PORT}/api/graph"

mkdir -p "$STATE_DIR"

graph_ready() {
  python3 - "$GRAPH_API_URL" <<'PY'
import json
import sys
import urllib.request

url = sys.argv[1]
try:
    with urllib.request.urlopen(url, timeout=2) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if isinstance(payload, dict) and ("nodes" in payload or "count" in payload):
        raise SystemExit(0)
except Exception:
    pass
raise SystemExit(1)
PY
}

stop_pid() {
  local pid="$1"
  local label="$2"
  if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
    return 0
  fi

  echo "Stopping $label PID $pid"
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 10); do
    if ! kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done

  echo "Force killing $label PID $pid"
  kill -9 "$pid" 2>/dev/null || true
}

port_owner_pid() {
  lsof -ti TCP:"$GRAPH_PORT" -sTCP:LISTEN 2>/dev/null | head -n 1
}

if [[ -f "$PID_FILE" ]]; then
  EXISTING_PID="$(cat "$PID_FILE")"
  stop_pid "$EXISTING_PID" "memora server"
  rm -f "$PID_FILE"
fi

if graph_ready; then
  PORT_PID="$(port_owner_pid || true)"
  if [[ -n "${PORT_PID:-}" ]]; then
    CMDLINE="$(ps -p "$PORT_PID" -o args= 2>/dev/null || true)"
    if [[ "$CMDLINE" == *"memora-server"* ]]; then
      stop_pid "$PORT_PID" "graph port owner"
    else
      echo "Port $GRAPH_PORT is in use by a non-memora process: ${CMDLINE:-unknown}" >&2
      exit 1
    fi
  fi
fi

setsid "$SCRIPT_DIR/.venv/bin/memora-server" \
  --transport "$TRANSPORT" \
  --host "$HOST" \
  --port "$PORT" \
  --graph-port "$GRAPH_PORT" \
  </dev/null >"$LOG_FILE" 2>&1 &
SERVER_PID=$!
echo "$SERVER_PID" >"$PID_FILE"
echo "Started memora server PID $SERVER_PID"

for _ in $(seq 1 30); do
  if graph_ready; then
    break
  fi
  sleep 1
done

if ! graph_ready; then
  echo "Graph server did not become ready. Check $LOG_FILE" >&2
  exit 1
fi

if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$GRAPH_URL" >/dev/null 2>&1 &
elif command -v open >/dev/null 2>&1; then
  open "$GRAPH_URL" >/dev/null 2>&1 &
else
  python3 -m webbrowser "$GRAPH_URL" >/dev/null 2>&1 || true
fi

echo "Graph URL: $GRAPH_URL"
echo "Graph API: $GRAPH_API_URL"
echo "Log file: $LOG_FILE"
echo "PID file: $PID_FILE"
