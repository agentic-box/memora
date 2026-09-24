#!/usr/bin/env bash
# Scheduled §5.2 compares of memora-all's live local primaries (NC1).
#
# For every store in the RUNNING memora-all's MEMORA_REPLICAS, one at a
# time, it runs the operator tool INSIDE the container:
#   nightly (Mon-Sat): local_primary.py compare <db> --mode nightly
#   weekly  (Sunday):  local_primary.py compare <db> --mode barrier --brief-freeze
#                      (a brief freeze covering every key; an operator's own
#                      freeze is left as it is)
# with the store path and the D1 identity from the container's own
# MEMORA_DATABASES / MEMORA_REPLICAS, and records each outcome through
# memora-all's verified admin path. Admin/health tokens reach the tool as
# 0600 files on the container's tmpfs, written by the container's shell
# from its own environment and removed on exit (as scripts/cutover_store.sh).
#
#   scripts/nightly_compare.sh [--mode nightly|barrier] [--dry-run]
#
# --mode defaults to barrier on Sunday, nightly otherwise. --dry-run reads
# the store list (read-only) and prints the plan; it places no token file
# and runs no compare.
#
# Output: one summary line per store, printed and appended to
# $NC_LOG_DIR/compare-<date>.log (default ~/memora-lp/compare-logs; files
# older than 30 days are removed). Exit 1 when any store is not clean: a
# diff (tool exit 5), a skipped compare (6), a refusal (2), a halt (3) or
# any other failure, missing vectors on D1, a halted replicator, or new
# would_halt events (counted per store in $NC_LOG_DIR/would-halt-<db>.count).
# Exit 75 when another run holds the lock ($NC_LOG_DIR/.lock): two compares
# never run at once.
#
# Where: on the deploy host, next to memora-all (the default), or from
# elsewhere with DEPLOY_HOST=<ssh host>. RUNTIME (docker) and
# DEPLOY_CONTAINER (memora-all) name the runtime and the container. No host,
# store or account is named in this script.
#
# Crontab on the deploy host (local time; the leader installs it):
#   15 3 * * 1-6  $HOME/repos/agentic-box/memora/scripts/nightly_compare.sh --mode nightly >/dev/null 2>&1
#   0  4 * * 0    $HOME/repos/agentic-box/memora/scripts/nightly_compare.sh --mode barrier >/dev/null 2>&1
# (cron's own output is discarded; the summary is in the log. A failing run
# exits 1 for whatever wraps it.)
set -euo pipefail

RT="${RUNTIME:-docker}"
CONTAINER="${DEPLOY_CONTAINER:-memora-all}"
HOST="${DEPLOY_HOST:-localhost}"
LOG_DIR="${NC_LOG_DIR:-$HOME/memora-lp/compare-logs}"
KEEP_DAYS=30
TOOL=(python /app/scripts/local_primary.py)
SHM=/dev/shm/memora-nightly-compare
AUTH=(--admin-token-file "$SHM/admin.token" --health-token-file "$SHM/health.token")

MODE=""; DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --mode) MODE="${2:-}"; shift ;;
    --dry-run) DRY=1 ;;
    -h|--help) awk 'NR > 1 && /^set -euo/ { exit } NR > 1' "$0"; exit 0 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
  shift
done
if [ -z "$MODE" ]; then
  if [ "$(date +%u)" = 7 ]; then MODE=barrier; else MODE=nightly; fi
fi
case "$MODE" in nightly|barrier) ;; *) echo "--mode is nightly or barrier, not '$MODE'" >&2; exit 2 ;; esac

q() { printf '%q ' "$@"; }
remote() { if [ "$HOST" = localhost ]; then "$@"; else ssh "$HOST" "$(q "$@")"; fi; }
cexec() { remote "$RT" exec "$CONTAINER" "$@"; }

# The container's own view: {db: {"uri": d1://..., "store": path}} and one
# store's health. Programs go in base64 (one word through ssh).
PROG="$(base64 <<'PY' | tr -d '\n'
import json, os, sys, urllib.error, urllib.request
what = sys.argv[1]
if what == "stores":
    reg = json.loads(os.environ.get("MEMORA_DATABASES") or "{}")
    rep = json.loads(os.environ.get("MEMORA_REPLICAS") or "{}")
    out = {}
    for db in sorted(rep):
        store = str(reg.get(db, ""))
        out[db] = {"uri": rep[db], "store": store[len("file://"):] if store.startswith("file://") else store}
    print(json.dumps(out))
else:
    req = urllib.request.Request("http://127.0.0.1:8000/health/db/" + sys.argv[2],
                                 headers={"Authorization": "Bearer " + os.environ.get("MEMORA_HEALTH_TOKEN", "")})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        body = {"error": str(e)[:200]}
    print(json.dumps(body.get("replication") or {"error": body.get("error", "no replication block")}))
PY
)"
in_container() { cexec python -c "import base64; exec(base64.b64decode('$PROG'))" "$@"; }

STORES="$(in_container stores)" || { echo "cannot read $CONTAINER's stores (is it running?)" >&2; exit 1; }
PLAN="$(printf '%s' "$STORES" | python3 -c '
import json, re, sys
for db, s in json.load(sys.stdin).items():
    uri, store = s["uri"], s["store"]
    m = re.fullmatch(r"d1://([^/\s]+)/([^/\s]+)", uri)
    if not m or not store.startswith("/") or re.search(r"\s", store) or not re.fullmatch(r"[A-Za-z0-9._-]+", db):
        sys.exit(f"store {db}: cannot use uri {uri!r} / path {store!r}")
    print(db, store, m.group(1), m.group(2))
')" || { echo "the replicated stores' configuration is unusable (above)" >&2; exit 1; }

compare_args() {  # compare_args DB STORE ACCOUNT DATABASE -> CARGS (an array)
  CARGS=(compare "$1" --mode "$MODE" --store "$2" --account "$3" --database-id "$4" "${AUTH[@]}")
  if [ "$MODE" = barrier ]; then CARGS+=(--brief-freeze); fi
}

if [ "$DRY" = 1 ]; then
  echo "nightly_compare plan: mode $MODE, container $CONTAINER on $HOST, one store at a time:"
  while read -r db store acct dbid; do
    [ -n "$db" ] || continue
    compare_args "$db" "$store" "$acct" "$dbid"
    echo "  $db: $RT exec $CONTAINER ${TOOL[*]} ${CARGS[*]}"
  done <<< "$PLAN"
  [ -n "$PLAN" ] || echo "  (no store in MEMORA_REPLICAS)"
  echo "dry run: nothing was run"
  exit 0
fi

mkdir -p "$LOG_DIR"
LOCK="$LOG_DIR/.lock"
mkdir "$LOCK" 2>/dev/null || { echo "another nightly_compare run holds $LOCK" >&2; exit 75; }
TOKENS=0
cleanup() {
  [ "$TOKENS" = 1 ] && { cexec rm -rf "$SHM" >/dev/null 2>&1 || echo "note: remove $SHM in $CONTAINER by hand" >&2; }
  rmdir "$LOCK" 2>/dev/null || true
}
trap cleanup EXIT

LOG="$LOG_DIR/compare-$(date +%Y-%m-%d).log"
find "$LOG_DIR" -maxdepth 1 -name 'compare-*.log' -mtime +"$KEEP_DAYS" -exec rm -f {} + 2>/dev/null || true

if [ -n "$PLAN" ]; then
  TOKENS=1
  cexec sh -c "umask 077 && mkdir -p $SHM && printf %s \"\$MEMORA_ADMIN_TOKEN\" > $SHM/admin.token && printf %s \"\$MEMORA_HEALTH_TOKEN\" > $SHM/health.token" \
    || { echo "$(date -u +%FT%TZ) all mode=$MODE result=error detail=cannot place the tool's token files" | tee -a "$LOG"; exit 1; }
fi

FAILED=0
while read -r db store acct dbid; do
  [ -n "$db" ] || continue
  rc=0
  compare_args "$db" "$store" "$acct" "$dbid"
  out="$(cexec "${TOOL[@]}" "${CARGS[@]}")" || rc=$?
  repl="$(in_container health "$db" 2>/dev/null)" || repl='{"error": "health unreadable"}'
  state_file="$LOG_DIR/would-halt-$db.count"
  line="$(OUT="$out" REPL="$repl" python3 - "$db" "$MODE" "$rc" "$state_file" <<'PY'
import json, os, sys, time
db, mode, rc, state_file = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
lines = [l for l in os.environ["OUT"].splitlines() if l.strip().startswith("{")]
try:
    out = json.loads(lines[-1]) if lines else {}
except ValueError:
    out = {}
try:
    repl = json.loads(os.environ["REPL"] or "{}")
except ValueError:
    repl = {"error": "unparseable health"}
problems = []
result = {0: "clean", 5: "diff", 6: "skipped", 2: "refused", 3: "halted"}.get(rc, "error")
if rc != 0:
    problems.append(result)
missing = out.get("d1_missing_vectors")
if isinstance(missing, int) and missing > 0:
    problems.append(f"missing_vectors={missing}")
if repl.get("halted_reason") or repl.get("status") == "halted":
    problems.append(f"replicator_halted={repl.get('halted_reason')!r}")
count = repl.get("would_halt_count")
new_events = None
if isinstance(count, int):
    try:
        before = int(open(state_file).read().strip())
    except (OSError, ValueError):
        before = 0
    new_events = count - before
    if new_events > 0:
        problems.append(f"would_halt_events=+{new_events}")
    with open(state_file, "w") as fh:
        fh.write(str(count))
elif "error" in repl:
    problems.append(f"health={repl['error']!r}")
detail = out.get("refused") or out.get("halted") or out.get("skipped") or ""
print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {db} mode={mode} result={result if not problems or rc else 'alert'} "
      f"exit={rc} diffs={out.get('diff_count')} missing_vectors={missing} would_halt={count}"
      f"{'' if new_events is None else f'(+{new_events})'} recorded={out.get('recorded')}"
      + (f" problems={','.join(problems)}" if problems else "") + (f" detail={str(detail)[:200]!r}" if detail else ""))
sys.exit(1 if problems else 0)
PY
)" || FAILED=1
  echo "$line" | tee -a "$LOG"
done <<< "$PLAN"
[ -n "$PLAN" ] || echo "$(date +%Y-%m-%dT%H:%M:%S%z) (none) mode=$MODE result=clean detail='no store in MEMORA_REPLICAS'" | tee -a "$LOG"
exit "$FAILED"
