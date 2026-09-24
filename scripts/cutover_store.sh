#!/usr/bin/env bash
# Local-primary cutover of ONE store of memora-all (REL1): the store moves
# from d1:// to a seeded local SQLite file on memora-all's /data volume,
# replicated to the same D1 database in write mode. Run from the Mac, in the
# checkout whose instances/all.env the deploy reads. docs/cutover-runbook.md
# is the runbook; this script does only its steps, with a check at each
# boundary:
#
#   a  freeze <db> (POST /admin/freeze/<db>, persisted on /data)
#   b  recheck the latest export receipt on deploy-host under that freeze; a fresh
#      export (still frozen) if D1 moved or the receipt is older than 24 h
#   c  seed /data/<db>.db INSIDE memora-all (the named volume is root-owned
#      on the host), then the fk audit (clean required)
#   d  print the all.env edits; --apply-env makes them (0600 backup first):
#        MEMORA_DATABASES[<db>] = /data/<db>.db
#        MEMORA_REPLICAS[<db>]  = d1://<account>/<database-id>
#        MEMORA_REPLICATION     = write
#        MEMORA_REPLICATION_INTERVAL_S = --interval (default 60: the gamma pilot,
#                                 leader 7763; one send per minute at most)
#   e  redeploy (scripts/deploy-memora-all.sh); the store comes up frozen
#   f  health: served locally, replication block present, write mode, the
#      configured interval, not halted, last_acked_seq reaching head within
#      2 x interval + 30 s, still frozen
#   g  barrier compare (clean required, recorded in the store); its drain
#      wait is at least 2 x interval + 30 s
#   h  thaw, only with --thaw, and only after a recorded clean barrier compare
#
# Usage:
#   scripts/cutover_store.sh <db> [--interval S]          the plan (default: --dry-run; S default 60)
#   scripts/cutover_store.sh <db> --execute               run a..d (stops before the env edits)
#   scripts/cutover_store.sh <db> --execute --from d --apply-env   d..g (stops before the thaw)
#   scripts/cutover_store.sh <db> --execute --from h --thaw        h
# --from takes a, d, e, f, g or h (a..c run as one unit: the seed rechecks
# its receipt under the freeze itself). Every failure message names the
# rollback: docs/cutover-runbook.md "Rollback", the L6 runbook.
#
# Tokens: the operator tool runs inside memora-all and gets the admin and
# health tokens as 0600 files under /dev/shm/memora-cutover (tmpfs), written
# there from the container's own environment by the container's shell and
# removed on exit. No token value is on a command line of the Mac or the deploy host,
# or printed. The D1 read token is the container's MEMORA_D1_READ_TOKEN_FILE.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/instances/all.env"
DEPLOY="$ROOT/scripts/deploy-memora-all.sh"
# The memora-all host comes from the operator's git-ignored deploy
# configuration (CFG1, instances/deploy.env.example); no host is guessed.
CONFIG_FILE="$ROOT/instances/deploy.env"
HOST="$(python3 "$ROOT/scripts/deploy_config.py" "$CONFIG_FILE" DEPLOY_HOST | tr -d '\0')" || HOST=""
[ -n "$HOST" ] || { echo "refused: DEPLOY_HOST is not set in $CONFIG_FILE (see instances/deploy.env.example) — nothing was done" >&2; exit 2; }
RT=docker
CONTAINER=memora-all
TOOL=(python /app/scripts/local_primary.py)
SHM=/dev/shm/memora-cutover
OFFHOST="${CUTOVER_OFFHOST_DIR:-$HOME/memora-lp/exports}"   # this Mac: off-host copies of a fresh export
STEPS="a b c d e f g h"

DB=""; EXECUTE=0; FROM=a; APPLY_ENV=0; THAW=0; INTERVAL=60
while [ $# -gt 0 ]; do
  case "$1" in
    --execute) EXECUTE=1 ;;
    --dry-run) EXECUTE=0 ;;
    --from) FROM="${2:-}"; shift ;;
    --apply-env) APPLY_ENV=1 ;;
    --thaw) THAW=1 ;;
    --interval) INTERVAL="${2:-}"; shift ;;
    -h|--help) awk 'NR > 1 && /^set -euo/ { exit } NR > 1' "$0"; exit 0 ;;
    -*) echo "unknown option $1" >&2; exit 2 ;;
    *) [ -z "$DB" ] || { echo "one store at a time" >&2; exit 2; }; DB="$1" ;;
  esac
  shift
done

rollback_hint() {
  echo "  rollback: docs/cutover-runbook.md \"Rollback\"; after step e this is the L6 runbook," >&2
  echo "            docs/local-primary-implementation.md §5.3: local_primary.py rollback $DB --phase drain|verify|finish" >&2
}
fail() {  # fail STEP MESSAGE
  echo "cutover $DB STOPPED at step $1: $2" >&2
  echo "  a freeze already placed stays in place (only step h, or local_primary.py thaw, lifts it)" >&2
  rollback_hint
  exit 1
}
refuse() { echo "cutover refused: $* — nothing was done" >&2; rollback_hint; exit 2; }

printf '%s' "$DB" | grep -Eqx '[a-z0-9][a-z0-9_-]*' || refuse "usage: $0 <db> [--execute] [--from a|d|e|f|g|h] [--apply-env] [--thaw]"
case " a d e f g h " in *" $FROM "*) ;; *) refuse "--from takes a, d, e, f, g or h (not '$FROM')" ;; esac
[ -f "$ENV_FILE" ] || refuse "missing $ENV_FILE"
printf '%s' "$INTERVAL" | grep -Eqx '[0-9]+(\.[0-9]+)?' || refuse "--interval takes seconds >= 0 (not '$INTERVAL')"
after() { case "$STEPS" in *"$1"*"$2"*) return 0 ;; esac; return 1; }   # after X Y: Y comes after X
runs() { [ "$1" = "$FROM" ] || after "$FROM" "$1"; }                     # runs STEP: STEP >= --from

# ------------------------------------------------------------ all.env (local)
ENVTOOL="$(cat <<'PY'
import json, os, re, sys

def parse(path):
    lines = open(path).read().splitlines()
    vals = {}
    for ln in lines:
        m = re.match(r"([A-Z_][A-Z0-9_]*)=(.*)$", ln)
        if m and m.group(1) not in vals:
            v = m.group(2)
            if len(v) >= 2 and v[0] == v[-1] == "'":
                v = v[1:-1]
            vals[m.group(1)] = v
    return lines, vals

cmd, path, db, frm, interval = sys.argv[1:6]
lines, vals = parse(path)
try:
    registry = json.loads(vals.get("MEMORA_DATABASES", ""))
    replicas = json.loads(vals.get("MEMORA_REPLICAS") or "{}")
except ValueError as exc:
    sys.exit(f"{path}: MEMORA_DATABASES / MEMORA_REPLICAS is not JSON: {exc}")
if db not in registry:
    sys.exit(f"{db!r} is not a store of MEMORA_DATABASES ({sorted(registry)})")
entry, local = str(registry[db]), f"/data/{db}.db"
before_env = frm in ("a", "d")
if before_env:
    m = re.fullmatch(r"d1://([^/\s]+)/([^/\s]+)", entry)
    if not m:
        sys.exit(f"MEMORA_DATABASES[{db!r}] is {entry!r}, not d1://<account>/<database-id>: "
                 f"not a store to cut over (already local? then --from e)")
    if db in replicas:
        sys.exit(f"MEMORA_REPLICAS already names {db!r}")
    account, database = m.groups()
else:
    if entry not in (local, "file://" + local):
        sys.exit(f"--from {frm} needs the env edits (step d) made: MEMORA_DATABASES[{db!r}] is {entry!r}, not {local!r}")
    m = re.fullmatch(r"d1://([^/\s]+)/([^/\s]+)", str(replicas.get(db, "")))
    if not m:
        sys.exit(f"--from {frm} needs MEMORA_REPLICAS[{db!r}] = d1://<account>/<database-id> (step d)")
    if vals.get("MEMORA_REPLICATION") != "write":
        sys.exit(f"--from {frm} needs MEMORA_REPLICATION=write (step d)")
    account, database = m.groups()
    interval = vals.get("MEMORA_REPLICATION_INTERVAL_S") or "0"  # what the deploy passes
uri = f"d1://{account}/{database}"
new_registry = dict(registry, **{db: local})
new_replicas = dict(replicas, **{db: uri})
edits = [("MEMORA_DATABASES", "'" + json.dumps(new_registry) + "'"),
         ("MEMORA_REPLICAS", "'" + json.dumps(new_replicas) + "'"),
         ("MEMORA_REPLICATION", "write"),
         ("MEMORA_REPLICATION_INTERVAL_S", interval)]
if cmd == "ids":
    print(account, database, interval)
elif cmd == "edits":
    for k, v in edits:
        print(f"    {k}={v}")
elif cmd == "apply":
    out, done = [], set()
    for ln in lines:
        m = re.match(r"([A-Z_][A-Z0-9_]*)=", ln)
        hit = next(((k, v) for k, v in edits if m and m.group(1) == k), None)
        if hit and hit[0] in done:
            continue  # a duplicate key: the first occurrence was replaced
        if hit:
            out.append(f"{hit[0]}={hit[1]}")
            done.add(hit[0])
        else:
            out.append(ln)
    out += [f"{k}={v}" for k, v in edits if k not in done]
    tmp = path + ".cutover-tmp"
    with open(tmp, "w") as fh:
        fh.write("\n".join(out) + "\n")
    os.chmod(tmp, os.stat(path).st_mode & 0o777)
    os.replace(tmp, path)
    _, again = parse(path)
    got = (json.loads(again["MEMORA_DATABASES"]).get(db), json.loads(again["MEMORA_REPLICAS"]).get(db),
           again.get("MEMORA_REPLICATION"), again.get("MEMORA_REPLICATION_INTERVAL_S"))
    if got != (local, uri, "write", interval):
        sys.exit(f"{path} does not read back as edited: {got}")
PY
)"
envtool() { python3 -c "$ENVTOOL" "$1" "$ENV_FILE" "$DB" "$FROM" "$INTERVAL"; }

IDS="$(envtool ids)" || refuse "$ENV_FILE does not fit --from $FROM (above)"
read -r ACCT DBID INTERVAL <<< "$IDS"
# The interval's allowance (leader 7763): a send may wait one interval, so
# the ack reaching head, and the compare's drain, get 2 x interval + 30 s.
ALLOW_S="$(python3 -c 'import math, sys; print(math.ceil(2 * float(sys.argv[1]) + 30))' "$INTERVAL")"
DRAIN_S=$(( ALLOW_S > 600 ? ALLOW_S : 600 ))
LOCAL="/data/$DB.db"

# ------------------------------------------------------------ remote helpers
q() { printf '%q ' "$@"; }
remote() { ssh "$HOST" "$(q "$@")"; }
cexec() { remote "$RT" exec "$CONTAINER" "$@"; }
AUTH=(--admin-token-file "$SHM/admin.token" --health-token-file "$SHM/health.token")
D1=(--account "$ACCT" --database-id "$DBID")
COMMON=("${D1[@]}" "${AUTH[@]}" --r2-dir /data/exports-r2 --out-dir /data/exports)

TOKENS_PLACED=0
tokens_in() {  # the container's own shell writes its env tokens to 0600 tmpfs files
  cexec sh -c "umask 077 && mkdir -p $SHM && printf %s \"\$MEMORA_ADMIN_TOKEN\" > $SHM/admin.token && printf %s \"\$MEMORA_HEALTH_TOKEN\" > $SHM/health.token" \
    || fail "$1" "cannot place the tool's token files in $CONTAINER"
  TOKENS_PLACED=1
}
tokens_out() {
  if [ "$TOKENS_PLACED" = 1 ]; then
    cexec rm -rf "$SHM" >/dev/null 2>&1 || echo "note: remove $SHM in $CONTAINER by hand" >&2
    TOKENS_PLACED=0
  fi
}
trap tokens_out EXIT

TOOL_OUT=""; TOOL_RC=0
tool() {  # tool ARGS... -> TOOL_OUT (its JSON line), TOOL_RC
  TOOL_RC=0
  TOOL_OUT="$(cexec "${TOOL[@]}" "$@")" || TOOL_RC=$?
  printf '  %s\n' "$TOOL_OUT"
}

HEALTH_PROG="$(printf '%s' '
import json, os, sys, urllib.error, urllib.request
db = sys.argv[1]
req = urllib.request.Request("http://127.0.0.1:8000/health/db/" + db,
                             headers={"Authorization": "Bearer " + os.environ.get("MEMORA_HEALTH_TOKEN", "")})
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        code, body = r.status, json.loads(r.read() or b"{}")
except urllib.error.HTTPError as e:
    code, body = e.code, {}
except (urllib.error.URLError, OSError, ValueError) as e:
    code, body = None, {"error": str(e)}
reg = json.loads(os.environ.get("MEMORA_DATABASES") or "{}")
print(json.dumps({**body, "http": code, "registry_entry": reg.get(db),
                  "env_replicas": json.loads(os.environ.get("MEMORA_REPLICAS") or "{}"),
                  "env_replication": os.environ.get("MEMORA_REPLICATION")}))
' | base64 | tr -d '\n')"
health() { cexec python -c "import base64; exec(base64.b64decode('$HEALTH_PROG'))" "$DB"; }

CHECK_PROG="$(cat <<'PY'
import json, sys
kind, db, interval = sys.argv[1:4]
h = json.loads(sys.stdin.read() or "{}")
fr, rep = h.get("freeze") or {}, h.get("replication")
bad = []
if h.get("http") != 200 or h.get("status") != "ok":
    bad.append(f"/health/db/{db} is HTTP {h.get('http')} status {h.get('status')!r}")
if kind == "thawed":
    if fr.get("state") != "open":
        bad.append(f"freeze state is {fr.get('state')!r}, not open")
else:
    if fr.get("state") != "frozen" or fr.get("in_flight") != 0 or fr.get("open_intents"):
        bad.append(f"not frozen with 0 in flight (state={fr.get('state')!r} in_flight={fr.get('in_flight')!r} "
                   f"open_intents={fr.get('open_intents')!r})")
if kind in ("live", "compared"):
    entry = str(h.get("registry_entry"))
    if entry not in (f"/data/{db}.db", f"file:///data/{db}.db"):
        bad.append(f"served from {entry!r}, not the local /data/{db}.db")
    if h.get("env_replication") != "write" or db not in (h.get("env_replicas") or {}):
        bad.append("memora-all's env lacks MEMORA_REPLICATION=write / MEMORA_REPLICAS[db]")
    if not isinstance(rep, dict):
        bad.append("no replication block (the replicator did not start: see docker logs)")
    else:
        if rep.get("mode") != "write":
            bad.append(f"replication mode {rep.get('mode')!r}, not write")
        if rep.get("status") == "halted" or rep.get("halted_reason"):
            bad.append(f"replication halted: {rep.get('halted_reason')!r}")
        elif rep.get("status") != "running":  # e.g. backoff on a D1 error (review 7841); polled
            bad.append(f"replication status {rep.get('status')!r}, not running (last_error {rep.get('last_error')!r})")
        if rep.get("interval_s") != float(interval):
            bad.append(f"replication interval_s {rep.get('interval_s')!r}, not the configured {float(interval)}")
        if rep.get("last_acked_seq") != rep.get("head_seq") or rep.get("lag_rows") != 0:
            bad.append(f"last_acked_seq {rep.get('last_acked_seq')!r} has not reached head {rep.get('head_seq')!r} "
                       f"(lag_rows {rep.get('lag_rows')!r})")
if kind == "compared" and isinstance(rep, dict):
    if rep.get("last_compare_mode") != "barrier" or rep.get("last_compare_clean") is not True:
        bad.append(f"the last recorded compare is {rep.get('last_compare_mode')!r} clean={rep.get('last_compare_clean')!r}, "
                   "not a clean barrier compare")
    elif rep.get("compare_consumed_seq") != rep.get("head_seq"):
        bad.append(f"rows were written after the compare (consumed {rep.get('compare_consumed_seq')} < head {rep.get('head_seq')})")
if bad:
    sys.exit("; ".join(bad))
print(f"  /health/db/{db}: {kind} ok (freeze {fr.get('state')}"
      + (f", replication {rep.get('mode')} lag {rep.get('lag_rows')}" if isinstance(rep, dict) else "") + ")")
PY
)"
check() {  # check STEP KIND [TRIES]: /health/db/<db> must be KIND (frozen|live|compared|thawed)
  local tries="${3:-1}" i out msg
  for ((i = 1; i <= tries; i++)); do
    out="$(health)" || out='{}'
    if msg="$(printf '%s' "$out" | python3 -c "$CHECK_PROG" "$2" "$DB" "$INTERVAL" 2>&1)"; then
      echo "$msg"; return 0
    fi
    [ "$i" -lt "$tries" ] && sleep 3
  done
  fail "$1" "boundary check ($2): $msg"
}

# ------------------------------------------------------------ the plan
plan() {
  echo "cutover plan for store $DB (D1 d1://$ACCT/$DBID) on $HOST:$CONTAINER, from step $FROM:"
  runs a && echo "  a  freeze:   $RT exec $CONTAINER ${TOOL[*]} freeze $DB ${AUTH[*]}; check frozen, 0 in flight"
  runs a && echo "  b  recheck:  the newest ~/memora-lp/exports/$DB/*.receipt.json on $HOST (+ its .sql), copied to" \
                 "/data/exports/$DB/; ${TOOL[*]} recheck $DB --receipt <it> ${COMMON[*]}; a fresh export when D1 moved" \
                 "or it is older than 24 h (then copied out to $HOST:~/memora-lp/exports/$DB and $OFFHOST/$DB)"
  runs a && echo "  c  seed:     ${TOOL[*]} seed $DB --receipt <b's receipt> --out $LOCAL ${COMMON[*]};" \
                 "then fk-audit $DB --store $LOCAL (clean required); check still frozen"
  if runs d; then
    echo "  d  env:      $ENV_FILE edits (made only with --apply-env; timestamped 0600 backup first):"
    envtool edits
  fi
  runs e && echo "  e  redeploy: $DEPLOY (production defaults; the store comes up frozen from /data/freeze)"
  runs f && echo "  f  health:   /health/db/$DB: served from $LOCAL, replication block, mode write, interval_s $INTERVAL," \
                 "not halted, last_acked_seq reaching head within ${ALLOW_S} s (2 x interval + 30), frozen"
  runs g && echo "  g  compare:  ${TOOL[*]} compare $DB --mode barrier --store $LOCAL ${D1[*]} ${AUTH[*]} --drain-timeout $DRAIN_S" \
                 "(exit 0 = clean required)"
  runs h && echo "  h  thaw:     only with --thaw, after a recorded clean barrier compare: ${TOOL[*]} thaw $DB ${AUTH[*]}"
  echo "  the tool runs INSIDE $CONTAINER; its admin/health token files live in $SHM (tmpfs, 0600) and are removed on exit"
}

plan
if [ "$EXECUTE" != 1 ]; then
  echo "dry run: nothing was done (add --execute to run it)"
  exit 0
fi
echo "executing from step $FROM"

# ------------------------------------------------------------ a..c
if runs a; then
  echo "== a: freeze"
  tokens_in a
  tool freeze "$DB" "${AUTH[@]}"
  [ "$TOOL_RC" = 0 ] || fail a "the freeze was refused (exit $TOOL_RC)"
  check a frozen

  echo "== b: recheck the latest receipt under the freeze"
  RHOME="$(remote sh -c 'printf %s "$HOME"')" || fail b "cannot reach $HOST"
  HOST_DIR="$RHOME/memora-lp/exports/$DB"
  LATEST="$(remote sh -c 'ls -1 "$1"/*.receipt.json 2>/dev/null | sort | tail -1' _ "$HOST_DIR")" || true
  [ -n "$LATEST" ] || fail b "no receipt under $HOST:$HOST_DIR"
  SQL="$(remote python3 -c 'import json, os, sys; print(os.path.basename(json.load(open(sys.argv[1]))["sql_path"]))' "$LATEST")" \
    || fail b "unreadable receipt $LATEST"
  cexec mkdir -p "/data/exports/$DB" || fail b "cannot create /data/exports/$DB in $CONTAINER"
  remote "$RT" cp "$LATEST" "$CONTAINER:/data/exports/$DB/" || fail b "cannot copy $LATEST into $CONTAINER"
  remote "$RT" cp "$HOST_DIR/$SQL" "$CONTAINER:/data/exports/$DB/" || fail b "cannot copy $HOST_DIR/$SQL into $CONTAINER"
  RECEIPT="/data/exports/$DB/$(basename "$LATEST")"
  tool recheck "$DB" --receipt "$RECEIPT" "${COMMON[@]}"
  if [ "$TOOL_RC" != 0 ] && printf '%s' "$TOOL_OUT" | grep -q 'older than 24 h'; then
    echo "  the receipt is older than 24 h: a fresh export under the same freeze"
    tool export "$DB" "${COMMON[@]}"
  fi
  [ "$TOOL_RC" = 0 ] || fail b "no usable receipt (exit $TOOL_RC)"
  USED="$(printf '%s' "$TOOL_OUT" | python3 -c 'import json, sys; print(json.load(sys.stdin)["receipt"])')" \
    || fail b "the tool printed no receipt"
  if [ "$USED" != "$RECEIPT" ]; then  # a fresh export: off-host copies before anything relies on it
    USED_SQL="$(cexec python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["sql_path"])' "$USED")" \
      || fail b "unreadable fresh receipt $USED"
    for f in "$USED" "$USED_SQL"; do
      remote "$RT" cp "$CONTAINER:$f" "$HOST_DIR/" || fail b "cannot copy $f out to $HOST:$HOST_DIR"
      mkdir -p "$OFFHOST/$DB" && scp -q "$HOST:$HOST_DIR/$(basename "$f")" "$OFFHOST/$DB/" \
        || fail b "cannot copy $(basename "$f") off-host to $OFFHOST/$DB"
    done
    echo "  fresh export $USED copied to $HOST:$HOST_DIR and $OFFHOST/$DB"
  fi
  check b frozen

  echo "== c: seed $LOCAL inside $CONTAINER"
  tool seed "$DB" --receipt "$USED" --out "$LOCAL" "${COMMON[@]}"
  [ "$TOOL_RC" = 0 ] || fail c "the seed failed (exit $TOOL_RC); $LOCAL was not placed unless the message says so"
  tool fk-audit "$DB" --store "$LOCAL"
  [ "$TOOL_RC" = 0 ] || fail c "the fk audit of $LOCAL is not clean (exit $TOOL_RC)"
  check c frozen
  tokens_out
fi

# ------------------------------------------------------------ d
if runs d; then
  echo "== d: $ENV_FILE"
  envtool edits
  if [ "$APPLY_ENV" != 1 ]; then
    echo "stopped before the env edits (the store stays frozen, still served from D1)."
    echo "next: $0 $DB --execute --from d --apply-env"
    exit 0
  fi
  BACKUP="$ENV_FILE.bak-cutover-$DB-$(date +%Y%m%dT%H%M%S)"
  ( umask 077; cp "$ENV_FILE" "$BACKUP" ) && chmod 600 "$BACKUP" || fail d "cannot back up $ENV_FILE"
  envtool apply || fail d "the edit of $ENV_FILE failed; the original is $BACKUP"
  echo "  edited; backup $BACKUP (0600)"
  FROM=e  # the edits are in place: the rest reads them
  IDS="$(envtool ids)" || fail d "$ENV_FILE does not read back as edited"
fi

# ------------------------------------------------------------ e..h
if runs e; then
  echo "== e: redeploy"
  "$DEPLOY" || fail e "the redeploy failed (see its output; it prints its own rollback)"
fi
if runs f; then
  echo "== f: health"
  tokens_out; tokens_in f
  # polled every 3 s for 2 x interval + 30 s: the first ack may wait one interval
  check f live "${CUTOVER_HEALTH_TRIES:-$(( (ALLOW_S + 2) / 3 ))}"
fi
if runs g; then
  echo "== g: barrier compare"
  [ "$TOKENS_PLACED" = 1 ] || tokens_in g
  tool compare "$DB" --mode barrier --store "$LOCAL" "${D1[@]}" "${AUTH[@]}" --drain-timeout "$DRAIN_S"
  case "$TOOL_RC" in
    0) ;;
    5) fail g "the barrier compare found differences (report under /data/compare)" ;;
    6) fail g "the barrier compare was skipped (not drained in time)" ;;
    *) fail g "the barrier compare failed (exit $TOOL_RC)" ;;
  esac
  check g compared
fi
if runs h; then
  if [ "$THAW" != 1 ]; then
    echo "stopped before the thaw: $DB is served locally, replicating in write mode, and FROZEN."
    echo "next: $0 $DB --execute --from h --thaw"
    exit 0
  fi
  echo "== h: thaw"
  [ "$TOKENS_PLACED" = 1 ] || tokens_in h
  check h compared
  tool thaw "$DB" "${AUTH[@]}"
  [ "$TOOL_RC" = 0 ] || fail h "the thaw was refused (exit $TOOL_RC)"
  check h thawed
  echo "cutover of $DB done: local primary, replicating to d1://$ACCT/$DBID in write mode, thawed."
fi
