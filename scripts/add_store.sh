#!/usr/bin/env bash
# Add a NEW, EMPTY store to memora-all (Item 95): prepare it up to the point
# where the unchanged scripts/cutover_store.sh takes over. Run from the Mac, in
# the checkout whose instances/all.env and instances/deploy.env the deploy
# reads. docs/add-store-runbook.md is the runbook; this script does only its
# steps, with a check at each boundary. It never handles a token value: every
# credential is a file PATH inside the memora-all container.
#
#   1  bootstrap  local_primary.py bootstrap-d1: the app's own schema (and only that) on the
#                 pinned NEW, EMPTY D1 database, after read-only proof of its identity, readability and
#                 emptiness, and that no store uses its id. The store is in no registry: nothing serves it.
#   2  prefreeze  local_primary.py pre-freeze: the persisted freeze, BEFORE the store is ever served
#   3  env        instances/all.env  MEMORA_DATABASES[<db>] = d1://<account>/<id>   (interim, pre-frozen)
#                 instances/deploy.env MEMORA_PROJECTS[<db>] = ["<db>"]
#                 per key, after dated 0600 backups outside the repo; every other line and mapping unchanged
#   4  deploy     scripts/deploy-memora-all.sh (the store comes up FROZEN, served from D1)
#   5  export     the first verified export (under the persisted freeze) with its receipt, copied
#                 to deploy-host ~/memora-lp/exports/<db>/ and this Mac, as cutover_store.sh step b needs
#   -  handoff    the exact cutover_store.sh commands; nothing else is advertised to any client
#
# THE INTERIM STORE IS NOT ADVERTISED: no clmux binding, MCP entry or token scope for <db> may
# exist until the cutover's barrier compare is clean (0 diffs) and the store is thawed as a local primary.
#
# Usage:
#   scripts/add_store.sh <db> --d1-account A --d1-id I --d1-name N --credential-file F      the plan (dry run)
#   ... --execute                          1, 2 (stops before the env edits)
#   ... --execute --from env --apply-env   3, 4, 5 (then prints the cutover commands)
#   ... --execute --from export            5 only
#   ... --execute --rollback-env           S2/S3 rollback, step 1: remove ONLY the <db> keys from both files
#   ... --execute --clear-freeze           S2/S3 rollback, step 2: AFTER the redeploy without <db>, remove its freeze
# --credential-file is the D1 EDIT token file as seen INSIDE the container (an operator credential);
# --read-token-file defaults to the container's own MEMORA_D1_READ_TOKEN(_FILE).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/instances/all.env"
DEPLOY_ENV_FILE="$ROOT/instances/deploy.env"
DEPLOY="$ROOT/scripts/deploy-memora-all.sh"
CONFIGTOOL="$ROOT/scripts/add_store_config.py"
HOST="$(python3 "$ROOT/scripts/deploy_config.py" "$DEPLOY_ENV_FILE" DEPLOY_HOST | tr -d '\0')" || HOST=""
[ -n "$HOST" ] || { echo "refused: DEPLOY_HOST is not set in $DEPLOY_ENV_FILE (see instances/deploy.env.example) — nothing was done" >&2; exit 2; }
RT=docker
CONTAINER=memora-all
TOOL=(python /app/scripts/local_primary.py)
SHM=/dev/shm/memora-addstore
OFFHOST="${CUTOVER_OFFHOST_DIR:-$HOME/memora-lp/exports}"
STEPS="bootstrap prefreeze env deploy export"

DB=""; ACCT=""; DBID=""; D1NAME=""; CRED=""; READ_TOKEN=""
EXECUTE=0; FROM=bootstrap; APPLY_ENV=0; ROLLBACK_ENV=0; CLEAR_FREEZE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --d1-account) ACCT="${2:-}"; shift ;;
    --d1-id) DBID="${2:-}"; shift ;;
    --d1-name) D1NAME="${2:-}"; shift ;;
    --credential-file) CRED="${2:-}"; shift ;;
    --read-token-file) READ_TOKEN="${2:-}"; shift ;;
    --execute) EXECUTE=1 ;;
    --dry-run) EXECUTE=0 ;;
    --from) FROM="${2:-}"; shift ;;
    --apply-env) APPLY_ENV=1 ;;
    --rollback-env) ROLLBACK_ENV=1 ;;
    --clear-freeze) CLEAR_FREEZE=1 ;;
    -h|--help) awk 'NR > 1 && /^set -euo/ { exit } NR > 1' "$0"; exit 0 ;;
    -*) echo "unknown option $1" >&2; exit 2 ;;
    *) [ -z "$DB" ] || { echo "one store at a time" >&2; exit 2; }; DB="$1" ;;
  esac
  shift
done

rollback_hint() {
  echo "  rollback: docs/add-store-runbook.md \"Rollback by stage\" (S0-S6)" >&2
}
fail() {  # fail STEP MESSAGE
  echo "add_store $DB STOPPED at step $1: $2" >&2
  echo "  a persisted freeze already placed stays in place (only the cutover's thaw, or --clear-freeze after the route is gone, lifts it)" >&2
  rollback_hint
  exit 1
}
refuse() { echo "add_store refused: $* — nothing was done" >&2; rollback_hint; exit 2; }

printf '%s' "$DB" | grep -Eqx '[a-z0-9][a-z0-9_-]*' || refuse "usage: $0 <db> --d1-account A --d1-id I --d1-name N --credential-file F [--execute ...]"
case " $STEPS " in *" $FROM "*) ;; *) refuse "--from takes bootstrap, prefreeze, env, deploy or export (not '$FROM')" ;; esac
[ -f "$ENV_FILE" ] || refuse "missing $ENV_FILE"
[ -f "$DEPLOY_ENV_FILE" ] || refuse "missing $DEPLOY_ENV_FILE"
if [ "$ROLLBACK_ENV" != 1 ] && [ "$CLEAR_FREEZE" != 1 ]; then
  [ -n "$ACCT" ] && [ -n "$DBID" ] && [ -n "$D1NAME" ] || refuse "--d1-account, --d1-id and --d1-name (from \`wrangler d1 create\`) are required"
  printf '%s %s' "$ACCT" "$DBID" | grep -Eqx '[A-Za-z0-9_-]{8,64} [A-Za-z0-9_-]{8,64}' || refuse "--d1-account / --d1-id are not Cloudflare ids"
  [ -n "$CRED" ] || refuse "--credential-file (the D1 edit token file inside $CONTAINER) is required"
fi
[ "$ROLLBACK_ENV" != 1 ] || [ "$CLEAR_FREEZE" != 1 ] || refuse "--rollback-env and --clear-freeze are two steps: run them one after the other, with the redeploy between"
URI="d1://$ACCT/$DBID"

after() { case "$STEPS" in *"$1"*"$2"*) return 0 ;; esac; return 1; }
runs() { [ "$1" = "$FROM" ] || after "$FROM" "$1"; }

# ------------------------------------------------------------ remote helpers
q() { printf '%q ' "$@"; }
remote() { ssh "$HOST" "$(q "$@")"; }
cexec() { remote "$RT" exec "$CONTAINER" "$@"; }
AUTH=(--admin-token-file "$SHM/admin.token" --health-token-file "$SHM/health.token")
EXPORT_ARGS=(--account "$ACCT" --database-id "$DBID" --r2-dir /data/exports-r2 --out-dir /data/exports "${AUTH[@]}")

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
tool() {
  TOOL_RC=0
  TOOL_OUT="$(cexec "${TOOL[@]}" "$@")" || TOOL_RC=$?
  printf '  %s\n' "$TOOL_OUT"
}

# the interim stage's health: served from D1, frozen with nothing in flight, no replication block yet
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
print(json.dumps({**body, "http": code, "registry_entry": reg.get(db)}))
' | base64 | tr -d '\n')"
health() { cexec python -c "import base64; exec(base64.b64decode('$HEALTH_PROG'))" "$DB"; }
INTERIM_CHECK="$(cat <<'PY'
import json, sys
db = sys.argv[1]
h = json.loads(sys.stdin.read() or "{}")
fr = h.get("freeze") or {}
bad = []
if h.get("http") != 200 or h.get("status") != "ok":
    bad.append(f"/health/db/{db} is HTTP {h.get('http')} status {h.get('status')!r}")
if fr.get("state") != "frozen" or fr.get("in_flight") != 0 or fr.get("open_intents"):
    bad.append(f"not frozen with 0 in flight (state={fr.get('state')!r} in_flight={fr.get('in_flight')!r} open_intents={fr.get('open_intents')!r})")
if not str(h.get("registry_entry") or "").startswith("d1://"):
    bad.append(f"served from {h.get('registry_entry')!r}, not the interim d1:// entry")
if h.get("replication"):
    bad.append("a replication block is present before the cutover")
if bad:
    sys.exit("; ".join(bad))
print(f"  /health/db/{db}: interim stage ok (frozen, 0 in flight, served from D1, no replication yet)")
PY
)"
check_interim() {
  local out msg
  out="$(health)" || out='{}'
  msg="$(printf '%s' "$out" | python3 -c "$INTERIM_CHECK" "$DB" 2>&1)" || fail "$1" "boundary check (interim stage): $msg"
  echo "$msg"
}

# ------------------------------------------------------------ the backup directory (outside the repo)
BACKUP_DIR="$(python3 "$ROOT/scripts/deploy_config.py" "$DEPLOY_ENV_FILE" DEPLOY_BACKUP_DIR 2>/dev/null | tr -d '\0')" || BACKUP_DIR=""
[ -n "$BACKUP_DIR" ] || BACKUP_DIR="$HOME/memora-backups/cutover-env"

cfg() { python3 "$CONFIGTOOL" "$1" --all-env "$ENV_FILE" --deploy-env "$DEPLOY_ENV_FILE" --db "$DB" --repo "$ROOT" "${@:2}"; }

# ------------------------------------------------------------ rollback steps
if [ "$ROLLBACK_ENV" = 1 ] || [ "$CLEAR_FREEZE" = 1 ]; then
  if [ "$EXECUTE" != 1 ]; then
    if [ "$ROLLBACK_ENV" = 1 ]; then
      echo "rollback plan (S2/S3): remove ONLY the $DB keys from $ENV_FILE and $DEPLOY_ENV_FILE (refused once the cutover's env edits exist), then redeploy ($DEPLOY) and verify /mcp/$DB and /health/db/$DB are gone."
    else
      echo "rollback plan: ${TOOL[*]} clear-freeze $DB inside $CONTAINER, refused while the running service still routes $DB"
    fi
    echo "dry run: nothing was done (add --execute to run it)"
    exit 0
  fi
  if [ "$ROLLBACK_ENV" = 1 ]; then
    echo "== rollback: remove the $DB keys (current files only; no stale backup is restored)"
    cfg remove --backup-dir "$BACKUP_DIR" || exit $?
    echo "next: redeploy ($DEPLOY), verify the route is gone, then: $0 $DB --execute --clear-freeze"
  else
    echo "== rollback: remove the persisted freeze (only once the running service no longer routes $DB)"
    tool clear-freeze "$DB"
    [ "$TOOL_RC" = 0 ] || fail clear-freeze "the freeze was kept (exit $TOOL_RC): the service still routes $DB, or the registry could not be read"
  fi
  exit 0
fi

# ------------------------------------------------------------ the plan
plan() {
  echo "add-store plan for $DB (D1 $D1NAME, d1://$ACCT/$DBID) on $HOST:$CONTAINER, from step $FROM:"
  runs bootstrap && echo "  1  bootstrap: ${TOOL[*]} bootstrap-d1 $DB --account $ACCT --database-id $DBID --d1-name $D1NAME --credential-file $CRED${READ_TOKEN:+ --read-token-file $READ_TOKEN}" \
                       "(pinned identity, read-only proofs, zero user objects, id not configured; then ONLY ensure_schema; durable /data/bootstrap/$DB.json)"
  runs prefreeze && echo "  2  prefreeze: ${TOOL[*]} pre-freeze $DB (persisted /data/freeze/$DB, before the store is first served)"
  if runs env; then
    echo "  3  env:       $ENV_FILE MEMORA_DATABASES[$DB]=$URI ; $DEPLOY_ENV_FILE MEMORA_PROJECTS[$DB]=[\"$DB\"]"
    echo "                (made only with --apply-env; dated 0600 backups in $BACKUP_DIR; every other line and mapping unchanged)"
  fi
  runs deploy && echo "  4  deploy:    $DEPLOY (the store comes up FROZEN, served from D1; writes unsupported by design)"
  runs export && echo "  5  export:    ${TOOL[*]} export $DB ${EXPORT_ARGS[*]} under the persisted freeze; receipt + sql copied to $HOST:~/memora-lp/exports/$DB and $OFFHOST/$DB"
  echo "  then: scripts/cutover_store.sh $DB --execute ; --execute --from d --apply-env ; --execute --from h --thaw   (unchanged)"
  echo "  NOT advertised: no clmux binding, MCP entry or token scope for $DB until the barrier compare is 0 and the store is thawed"
}
plan
# The store must still be NEW in the configuration until step 3 has edited it.
config_check() {
  case "$FROM" in
    bootstrap|prefreeze|env)
      local out
      out="$(cfg check --uri "$URI")" || { echo "add_store refused: $out — nothing was done" >&2; exit 2; } ;;
  esac
}
if [ "$EXECUTE" != 1 ]; then
  config_check
  echo "dry run: nothing was done (add --execute to run it)"
  exit 0
fi
echo "executing from step $FROM"
config_check

# the image must contain Item 95's tools
if ! cexec "${TOOL[@]}" bootstrap-d1 --help >/dev/null 2>&1; then
  refuse "the running $CONTAINER image has no 'bootstrap-d1' (it predates Item 95): deploy a build that contains it first"
fi

# ------------------------------------------------------------ 1, 2
if runs bootstrap; then
  echo "== 1: bootstrap-d1 (schema only, on the pinned new empty D1)"
  tool bootstrap-d1 "$DB" --account "$ACCT" --database-id "$DBID" --d1-name "$D1NAME" --credential-file "$CRED" \
    ${READ_TOKEN:+--read-token-file "$READ_TOKEN"}
  [ "$TOOL_RC" = 0 ] || fail bootstrap "bootstrap-d1 refused or failed (exit $TOOL_RC); the message above names the checkpoint"
fi
if runs prefreeze; then
  echo "== 2: pre-freeze"
  tool pre-freeze "$DB"
  [ "$TOOL_RC" = 0 ] || fail prefreeze "pre-freeze refused (exit $TOOL_RC)"
fi

# ------------------------------------------------------------ 3
if runs env; then
  echo "== 3: instances/all.env and instances/deploy.env"
  cfg check --uri "$URI"
  if [ "$APPLY_ENV" != 1 ]; then
    echo "stopped before the env edits (the store is bootstrapped and pre-frozen, in no registry)."
    echo "next: $0 $DB --d1-account $ACCT --d1-id $DBID --d1-name $D1NAME --credential-file $CRED --execute --from env --apply-env"
    exit 0
  fi
  cfg apply --uri "$URI" --backup-dir "$BACKUP_DIR" || fail env "the edit refused or failed (the message above); nothing else was changed"
fi

# ------------------------------------------------------------ 4
if runs deploy; then
  echo "== 4: redeploy"
  "$DEPLOY" || fail deploy "the redeploy failed (see its output). Rollback by stage: S3 — $0 $DB --execute --rollback-env, redeploy, then --clear-freeze"
  tokens_out; tokens_in deploy
  check_interim deploy
fi

# ------------------------------------------------------------ 5
if runs export; then
  echo "== 5: the first verified export, under the persisted freeze"
  [ "$TOKENS_PLACED" = 1 ] || tokens_in export
  check_interim export
  RHOME="$(remote sh -c 'printf %s "$HOME"')" || fail export "cannot reach $HOST"
  HOST_DIR="$RHOME/memora-lp/exports/$DB"
  tool export "$DB" "${EXPORT_ARGS[@]}"
  [ "$TOOL_RC" = 0 ] || fail export "the export failed (exit $TOOL_RC)"
  USED="$(printf '%s' "$TOOL_OUT" | python3 -c 'import json, sys; print(json.load(sys.stdin)["receipt"])')" \
    || fail export "the tool printed no receipt"
  USED_SQL="$(cexec python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["sql_path"])' "$USED")" \
    || fail export "unreadable receipt $USED"
  remote mkdir -p "$HOST_DIR" || fail export "cannot create $HOST:$HOST_DIR"
  for f in "$USED" "$USED_SQL"; do
    remote "$RT" cp "$CONTAINER:$f" "$HOST_DIR/" || fail export "cannot copy $f out to $HOST:$HOST_DIR"
    mkdir -p "$OFFHOST/$DB" && scp -q "$HOST:$HOST_DIR/$(basename "$f")" "$OFFHOST/$DB/" \
      || fail export "cannot copy $(basename "$f") off-host to $OFFHOST/$DB"
  done
  echo "  export $USED copied to $HOST:$HOST_DIR and $OFFHOST/$DB"
  check_interim export
  tokens_out
  cat <<EOF

$DB is prepared: bootstrapped, pre-frozen, served FROZEN from D1, with a verified off-host export.
Nothing may point at it yet. Now the unchanged cutover (leader):
  scripts/cutover_store.sh $DB --execute                           # a, b, c: freeze (already), recheck, seed
  scripts/cutover_store.sh $DB --execute --from d --apply-env      # d..g: env edits, the redeploy, health, barrier compare
  scripts/cutover_store.sh $DB --execute --from h --thaw           # h: thaw, only after a clean barrier compare
Then and only then: the api-tokens.json scope (user), the clmux binding, and the copy (docs/add-store-runbook.md).
EOF
fi
