#!/usr/bin/env bash
# Deployment REHEARSAL (R1): the L2a deploy and migration, and the L2-L6
# startup, against a REAL container runtime on a rehearsal host (server2,
# podman). No Cloudflare, no nuc8, no tokens that exist anywhere else: every
# store is a local SQLite file, the port is 18920, and all state lives under
# $RH_ROOT. Every runtime object it creates goes through
# scripts/rehearse_objects.sh: a name unique to this run (memora-rh-<epoch>-<pid>…),
# the label memora.rehearsal=<run id>, its ID captured from a successful
# create, and removal at the end of the run by that ID after the label is
# re-checked (review 7747). RH_KEEP=1 keeps the objects for inspection; they
# are then listed in $RH_ROOT/run-<run id>.objects for a later
# `scripts/rehearse_teardown.sh <run id>`.
#
#   scripts/rehearse_deploy.sh OLD_SRC
#
# OLD_SRC is a source tree of the release production runs today (v0.4.6:
# `git archive v0.4.6 | tar -x -C OLD_SRC`). The new image is built from the
# checkout this script is in. docs/deploy-rehearsal.md explains each step.
#
# Steps (each prints PASS/FAIL; the summary is $RH_ROOT/results.txt):
#   1. build the OLD image as $IMAGE (production's memora:latest today);
#   2. start the old container the production way: VOLUME /data only, i.e.
#      an ANONYMOUS volume; write data to four local stores, leave SQLite
#      WAL sidecars behind (a process killed with the container) and an
#      intent journal file;
#   3. run scripts/deploy-memora-all.sh itself in rehearsal mode
#      (DEPLOY_HOST=localhost RUNTIME=podman, the -rh names): it migrates to
#      the named volume and recreates the container (960m, admin/health
#      tokens); then verify: data byte-identical, marker, old container kept
#      (renamed, stopped), /health, /admin/data-volume (admin token 200 with
#      kinds; health token refused), the startup data-volume check, memory
#      limit; a second deploy copies nothing; a rollback (old container
#      restarted, written to) then a redeploy recopies;
#   4. local_primary.py freeze / thaw through the live admin routes; the
#      D1-dependent compare is only shown to refuse (no D1 here);
#   4b. (REL1) the cutover mechanics inside the container: the operator
#      tool at /app/scripts/local_primary.py with 0600 token files on its
#      tmpfs, freeze / fk-audit; sync installed on the local "re" store
#      (standing in for the seed, which needs D1); a deploy with
#      MEMORA_REPLICAS / MEMORA_REPLICATION=log from all.env: the store comes
#      up frozen, replicating in LOG mode (log mode sends nothing to D1;
#      write mode is not rehearsed: its D1 endpoint is Cloudflare's);
#   5. saves real `podman inspect` samples (token values redacted) into
#      $RH_ROOT/fixtures for tests/fixtures.
# The Cloudflare tokens (REL1) are throwaway files under $RH_ROOT/secrets,
# mounted read-only; the checks prove no value reaches the container env.
set -uo pipefail

OLD_SRC="${1:?usage: rehearse_deploy.sh OLD_SRC}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RT="${RUNTIME:-podman}"
RH_ROOT="${RH_ROOT:-$HOME/rehearsal-r1}"
RUN_ID="rh-$(date +%s)-$$"
RUN_START="$(date +%s)"
NAME="memora-$RUN_ID"              # memora-rh-<epoch>-<pid>: unique to this run
VOL="memora-$RUN_ID-data"
IMAGE="memora-$RUN_ID:latest"
PORT=18920
PY="${PYTHON:-python3}"
CFG="$RH_ROOT/config"
SECRETS="$RH_ROOT/secrets"
ENVF="$RH_ROOT/all.env"
RESULTS="$RH_ROOT/results.txt"
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml")"
REG='{"memora": "/data/memora.db", "ob1": "/data/ob1.db", "bestation": "/data/bestation.db", "re": "/data/re.db"}'

OBJECTS="$RH_ROOT/run-$RUN_ID.objects"
mkdir -p "$RH_ROOT" "$CFG" "$SECRETS" "$RH_ROOT/fixtures"
echo "start $RUN_START $RUN_ID" > "$OBJECTS"
. "$ROOT/scripts/rehearse_objects.sh"   # new_container/new_volume/new_image/adopt/once/teardown
finish() {
  if [ "${RH_KEEP:-0}" = 1 ]; then echo "RH_KEEP=1: objects kept; see $OBJECTS"; return; fi
  echo "== teardown of run $RUN_ID (by captured ID, label re-checked)"
  # A run that stopped early may not have adopted what the deploy created:
  # adopt the per-run names now (each refused unless it carries this run's label).
  ( adopt _ container "$NAME" ) >/dev/null 2>&1
  ( adopt _ volume "$VOL" ) >/dev/null 2>&1
  ( adopt _ image "$IMAGE" ) >/dev/null 2>&1
  adopt_run_tags
  teardown "$OBJECTS"
}
trap finish EXIT
chmod 700 "$CFG"
: > "$RESULTS"
FAILS=0
pass() { echo "PASS  $*" | tee -a "$RESULTS"; }
fail() { echo "FAIL  $*" | tee -a "$RESULTS"; FAILS=$((FAILS + 1)); }
check() {  # check "description" command... -- PASS/FAIL on the exit status
  local what="$1"; shift
  if "$@" >>"$RH_ROOT/commands.log" 2>&1; then pass "$what"; else fail "$what (exit $?; see commands.log)"; fi
}
mint() { ( set +o pipefail; LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 48 ); }
# (Never `run --rm` with a volume mounted by name: podman's --rm deletes an
# ANONYMOUS volume once no container references it -- this rehearsal's
# finding. Helpers are `once`: create, start attached, remove by ID.)
mig_digest() {  # mig_digest VOLUME -- the migration program's own digest of a volume
  once -v "$1:/from:ro" --entrypoint sh "$IMAGE" -c "$(cat "$ROOT/scripts/migrate_data_volume.sh")" \
    migrate_data_volume digest
}
vol_digest() {  # vol_digest VOLUME -- sha256 of every file except the migration's own
  once -v "$1:/v:ro" --entrypoint python "$IMAGE" -c '
import hashlib, os, json
out = {}
for d, _, fs in os.walk("/v"):
    for f in fs:
        p = os.path.join(d, f); rel = os.path.relpath(p, "/v")
        if rel.startswith(".memora-") or "/.memora-" in rel:
            continue
        out[rel] = hashlib.sha256(open(p, "rb").read()).hexdigest()
print(json.dumps(out, sort_keys=True))'
}
old_anon() { "$RT" inspect "$1" --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}'; }
wait_health() {
  for _ in $(seq 1 60); do curl -sf -m 3 "http://127.0.0.1:$PORT/health" >/dev/null && return 0; sleep 2; done
  return 1
}
admin() {  # admin TOKEN PATH -- the HTTP status of GET PATH with TOKEN
  curl -s -o "$RH_ROOT/last-body.json" -w '%{http_code}' -H "Authorization: Bearer $1" "http://127.0.0.1:$PORT$2"
}
deploy() {
  DEPLOY_REHEARSAL=1 DEPLOY_REHEARSAL_ROOT="$RH_ROOT" DEPLOY_LABELS="$RUN_LABEL" DEPLOY_HOST=localhost RUNTIME="$RT" DEPLOY_CONTAINER="$NAME" DEPLOY_DATA_VOLUME="$VOL" \
  DEPLOY_IMAGE="$IMAGE" DEPLOY_PORT="$PORT" DEPLOY_CONFIG_DIR="$CFG" DEPLOY_REPO="$ROOT" \
  DEPLOY_GRAPH_BIND=127.0.0.1 DEPLOY_GRAPH_PORT=18766 \
  DEPLOY_SKIP_CHECKOUT=1 DEPLOY_SMOKE_ABSORB=0 DEPLOY_ENV_FILE="$ENVF" DEPLOY_TAG="v$VERSION" DEPLOY_SECRETS_DIR="$SECRETS" \
    bash "$ROOT/scripts/deploy-memora-all.sh"
}

echo "== run $RUN_ID: container $NAME, volume $VOL, image $IMAGE"

# The rehearsal's own config: throwaway tokens (0600), no Cloudflare anything.
HEALTH_TOKEN="$(mint)"
printf '%s' "$HEALTH_TOKEN" > "$CFG/all.health-token"; chmod 600 "$CFG/all.health-token"
rm -f "$CFG/all.admin-token"   # the deploy mints it
cat > "$CFG/credentials.mcp.json" <<JSON
{"mcpServers": {"memora": {"env": {"MEMORA_EMBEDDING_MODEL": "tfidf", "MEMORA_LLM_MODEL": "none"}}}}
JSON
printf "MEMORA_DATABASES='%s'\n" "$REG" > "$ENVF"
# Throwaway Cloudflare-shaped tokens (REL1): never valid anywhere, no store
# is on d1://, so nothing uses them; the checks are about where they go.
chmod 700 "$SECRETS"
for f in cloudflare-api.token d1-read.token; do
  ( umask 077; printf 'rh-%s-%s\n' "$f" "$(mint)" > "$SECRETS/$f" )
done

echo "== 1. the OLD image (production's memora:latest today) from $OLD_SRC"
new_image OLD_IMAGE_ID "$IMAGE" "$OLD_SRC" && pass "build the old image from $OLD_SRC ($OLD_IMAGE_ID)"

echo "== 2. the old container, the production way (anonymous /data)"
new_container OLD_ID --name "$NAME" --restart unless-stopped -p "0.0.0.0:$PORT:8000" \
  -e "MEMORA_DATABASES=$REG" -e MEMORA_DEFAULT_DB=memora -e "MEMORA_HEALTH_TOKEN=$HEALTH_TOKEN" \
  -e MEMORA_EMBEDDING_MODEL=tfidf -e MEMORA_ALLOW_ANY_TAG=1 "$IMAGE"
pass "start the old container ($OLD_ID)"
check "the old container answers /health" wait_health
anon_of OLD_VOL "$OLD_ID"   # dies unless it is an anonymous 64-hex volume
pass "the old container's /data is an anonymous volume ($OLD_VOL)"
check "write memories to all four stores" "$RT" exec -i "$NAME" python - <<'PY'
import json, os
from memora import storage
for name in json.loads(os.environ["MEMORA_DATABASES"]):
    conn = storage.backend_for(name).connect()
    try:
        for i in range(5):
            storage.add_memory(conn, content=f"rehearsal {name} memory {i}", tags=["rehearsal"])
        conn.commit()
    finally:
        conn.close()
    print(name, "ok")
PY
check "leave an intent journal file under /data/intent" "$RT" exec "$NAME" sh -c \
  'mkdir -p /data/intent && printf "%s\n" "{\"type\":\"intent\",\"id\":1,\"target\":\"memories\"}" > /data/intent/memora.jsonl'
# A process holding a WAL connection to a probe database no server opens,
# with a committed write that is not checkpointed: the container stop kills
# it, so -wal/-shm stay behind in the old volume. (A store the server also
# has open would be checkpointed by the server's own last close.)
check "start a WAL holder in the old container" "$RT" exec -d "$NAME" python -c '
import sqlite3, time
db = sqlite3.connect("/data/wal-probe.db")
db.execute("PRAGMA journal_mode=WAL"); db.execute("PRAGMA wal_autocheckpoint=0")
db.execute("CREATE TABLE IF NOT EXISTS rehearsal_wal (x)"); db.execute("INSERT INTO rehearsal_wal VALUES (1)"); db.commit()
time.sleep(100000)'
sleep 2
check "the WAL sidecars exist" "$RT" exec "$NAME" sh -c 'test -s /data/wal-probe.db-wal && test -e /data/wal-probe.db-shm'
"$RT" inspect "$NAME" > "$RH_ROOT/fixtures/inspect-old.raw.json"

echo "== 3. deploy (scripts/deploy-memora-all.sh in rehearsal mode)"
if deploy > "$RH_ROOT/deploy-1.log" 2>&1; then pass "first deploy (log: deploy-1.log)"; \
  else fail "first deploy (exit $?; see deploy-1.log)"; tail -30 "$RH_ROOT/deploy-1.log"; fi
adopt NEW_IMAGE_ID image "$IMAGE"; adopt VOL_ID volume "$VOL"; adopt NEW1_ID container "$NAME"; adopt_run_tags
GROK1="$("$RT" inspect "$OLD_ID" --format '{{.Name}}')"   # the old container, by its ID
[[ "$GROK1" == "$NAME-grok-"* ]] && pass "the old container is kept as $GROK1 (same ID)" || fail "the old container is named '$GROK1'"
[ "$("$RT" inspect "$OLD_ID" --format '{{.State.Running}}' 2>/dev/null)" = false ] \
  && pass "the old container is stopped" || fail "the old container is not stopped"
[ "$(old_anon "$OLD_ID")" = "$OLD_VOL" ] && pass "the old container still mounts its anonymous volume" \
  || fail "the old container's volume changed"
[ "$(old_anon "$NAME")" = "$VOL" ] && pass "the new container mounts the named volume $VOL" \
  || fail "the new container mounts '$(old_anon "$NAME")'"
# The new server has been writing to the named volume since it started (the
# schema upgrade), so it is compared through what the copy recorded, plus an
# independent copy made by the same program before any server touches it.
once -v "$VOL:/v:ro" --entrypoint sh "$IMAGE" -c 'cat /v/.memora-volume-source' > "$RH_ROOT/marker-1.txt" 2>&1
grep -q "source=$OLD_VOL" "$RH_ROOT/marker-1.txt" && pass "the marker names the source volume" || fail "marker: $(cat "$RH_ROOT/marker-1.txt")"
OLD_DIGEST="$(mig_digest "$OLD_VOL")"
grep -q "digest=$OLD_DIGEST" "$RH_ROOT/marker-1.txt" \
  && pass "the marker's digest is the old volume's content (the copy verified exactly this source)" \
  || fail "marker digest != the old volume's digest $OLD_DIGEST"
new_volume SCRATCH "memora-$RUN_ID-scratch"
if once -v "$OLD_VOL:/from:ro" -v "$SCRATCH:/to" "$IMAGE" sh -c "$(cat "$ROOT/scripts/migrate_data_volume.sh")" \
     migrate_data_volume migrate "$OLD_VOL" >> "$RH_ROOT/commands.log" 2>&1 \
   && [ "$(vol_digest "$OLD_VOL")" = "$(vol_digest "$SCRATCH")" ]; then
  pass "a copy by the same program on this runtime is byte-identical, file by file (WAL sidecars, intent journal)"
else
  fail "the independent copy differs from the old volume"
fi
once -v "$OLD_VOL:/a:ro" -v "$VOL:/b:ro" --entrypoint sh "$IMAGE" -c \
  'test -s /a/wal-probe.db-wal && cmp /a/wal-probe.db-wal /b/wal-probe.db-wal && cmp /a/intent/memora.jsonl /b/intent/memora.jsonl' \
  && pass "the WAL sidecar and the intent journal reached the named volume byte-identical" \
  || fail "the WAL sidecar or the intent journal differs in the named volume"
MEM="$("$RT" inspect "$NAME" --format '{{.HostConfig.Memory}}')"
[ "$MEM" = "$((960 * 1024 * 1024))" ] && pass "memory limit 960m ($MEM bytes)" || fail "memory limit is '$MEM'"
check "the new container answers /health" wait_health
curl -s "http://127.0.0.1:$PORT/health" > "$RH_ROOT/health.json"
grep -q "\"version\": *\"$VERSION\"" "$RH_ROOT/health.json" && pass "/health reports $VERSION" || fail "/health: $(cat "$RH_ROOT/health.json")"
ADMIN_TOKEN="$(cat "$CFG/all.admin-token")"
[ "$(stat -c %a "$CFG/all.admin-token")" = 600 ] && pass "the minted admin token file is 0600" || fail "admin token mode"
[ "$(admin "$ADMIN_TOKEN" /admin/data-volume)" = 200 ] && pass "/admin/data-volume answers 200 to the admin token" \
  || fail "/admin/data-volume with the admin token"
cp "$RH_ROOT/last-body.json" "$RH_ROOT/data-volume.json"
"$PY" - "$RH_ROOT/data-volume.json" "$VOL" <<'PY' && pass "/admin/data-volume: the named volume, every store sqlite and not refused (the startup /data check passed)" || fail "/admin/data-volume body: $(cat "$RH_ROOT/data-volume.json")"
import json, sys
d = json.load(open(sys.argv[1]))
assert d["volume"] == sys.argv[2], d
assert set(d["stores"]) == {"memora", "ob1", "bestation", "re"}, d
for name, s in d["stores"].items():
    assert s["kind"] == "sqlite" and s["needs_data_volume"] is True and s["refused"] is None, (name, s)
PY
code="$(admin "$HEALTH_TOKEN" /admin/data-volume)"
[ "$code" = 401 ] || [ "$code" = 403 ] && pass "/admin/data-volume refuses the health token ($code)" \
  || fail "/admin/data-volume answered $code to the health token"
code="$(admin "" /admin/data-volume)"
[ "$code" = 401 ] || [ "$code" = 403 ] && pass "/admin/data-volume refuses no token ($code)" || fail "no token: $code"
# REL1: the Cloudflare tokens are files of a read-only mount; the container's
# configuration carries only their paths.
"$RT" inspect "$NAME" > "$RH_ROOT/inspect-tokens.json"
"$PY" - "$RH_ROOT/inspect-tokens.json" "$SECRETS" <<'PY' && pass "the container env has only *_FILE paths, no token value; /run/secrets/memora is read-only" || fail "token env/mount: see commands.log"
import json, os, sys
info = json.load(open(sys.argv[1]))[0]
env = info["Config"]["Env"]
values = [open(os.path.join(sys.argv[2], f)).read().strip() for f in os.listdir(sys.argv[2])]
assert not any(v in e for v in values for e in env), "a token value is in the env"
for k in ("CLOUDFLARE_API_TOKEN_FILE", "MEMORA_D1_READ_TOKEN_FILE", "MEMORA_D1_REPLICATOR_TOKEN_FILE"):
    assert any(e.startswith(k + "=/run/secrets/memora/") for e in env), k
assert not any(e.startswith(("CLOUDFLARE_API_TOKEN=", "MEMORA_D1_READ_TOKEN=", "MEMORA_D1_REPLICATOR_TOKEN=")) for e in env)
m = [m for m in info["Mounts"] if m["Destination"] == "/run/secrets/memora"]
assert len(m) == 1 and m[0]["RW"] is False, m
PY
rm -f "$RH_ROOT/inspect-tokens.json"
check "the server's rule reads every token file through the mount (rootless uid mapping)" "$RT" exec "$NAME" python -c '
from memora.secret_files import check_secret_files
assert all(check_secret_files().values())'
check "the mount is read-only inside the container" "$RT" exec "$NAME" sh -c '! touch /run/secrets/memora/x 2>/dev/null'

echo "== 3b. a second deploy copies nothing"
MARK1="$(cat "$RH_ROOT/marker-1.txt")"
if deploy > "$RH_ROOT/deploy-2.log" 2>&1; then pass "second deploy"; else fail "second deploy (exit $?; see deploy-2.log)"; fi
grep -q migrate_data_volume "$RH_ROOT/deploy-2.log" && fail "the second deploy ran a copy" || pass "the second deploy ran no copy"
once -v "$VOL:/v:ro" --entrypoint sh "$IMAGE" -c 'cat /v/.memora-volume-source' > "$RH_ROOT/marker-2.txt" 2>&1
GROK2="$("$RT" inspect "$NEW1_ID" --format '{{.Name}}')"   # the first new container, renamed by the second deploy
adopt NEW2_ID container "$NAME"; adopt NEW_IMAGE_ID image "$IMAGE"; adopt_run_tags
[ "$(cat "$RH_ROOT/marker-2.txt")" = "$MARK1" ] && pass "the marker is unchanged" || fail "the marker changed"
"$RT" inspect "$NAME" > "$RH_ROOT/fixtures/inspect-new.raw.json"

echo "== 3c. rollback to the old container, write to it, redeploy: the copy is redone"
remove_one container "$NEW2_ID" | grep -q removed && pass "rollback: remove the new container (by its ID, label checked)" \
  || fail "rollback: the new container was not removed"
check "rollback: rename the ORIGINAL old container back" "$RT" rename "$OLD_ID" "$NAME"
check "rollback: start it" "$RT" start "$OLD_ID"
check "the rolled-back container answers /health" wait_health
check "write to the rolled-back (old) container" "$RT" exec -i "$OLD_ID" python - <<'PY'
from memora import storage
conn = storage.backend_for("ob1").connect()
storage.add_memory(conn, content="written after the rollback", tags=["rehearsal"]); conn.commit(); conn.close()
PY
remove_one container "$NEW1_ID" >> "$RH_ROOT/commands.log"   # the second deploy's leftover ($GROK2), by ID
if deploy > "$RH_ROOT/deploy-3.log" 2>&1; then pass "redeploy after the rollback"; else fail "redeploy (exit $?; see deploy-3.log)"; fi
adopt NEW3_ID container "$NAME"; adopt NEW_IMAGE_ID image "$IMAGE"; adopt_run_tags
once -v "$VOL:/v:ro" --entrypoint sh "$IMAGE" -c 'cat /v/.memora-volume-source' > "$RH_ROOT/marker-3.txt" 2>&1
NEW_OLD_DIGEST="$(mig_digest "$OLD_VOL")"
[ "$NEW_OLD_DIGEST" != "$OLD_DIGEST" ] && grep -q "digest=$NEW_OLD_DIGEST" "$RH_ROOT/marker-3.txt" \
  && pass "the redeploy recopied: the marker now records the old volume WITH the post-rollback writes" \
  || fail "the redeploy did not recopy the post-rollback content (marker: $(cat "$RH_ROOT/marker-3.txt"))"
once -v "$VOL:/v:ro" --entrypoint sh "$IMAGE" -c 'ls -a /v' > "$RH_ROOT/volume-listing.txt"
grep -q '^\.memora-previous-' "$RH_ROOT/volume-listing.txt" && pass "the replaced content was moved aside (.memora-previous-*), not overwritten" \
  || fail "no .memora-previous-* after the recopy"

echo "== 3d. podman: run --rm deletes an unreferenced anonymous volume; the migration form does not"
new_container PROBE_ID --name "memora-$RUN_ID-probe" --entrypoint sleep "$IMAGE" 600
anon_of PROBE "$PROBE_ID"
"$RT" exec "$PROBE_ID" sh -c 'echo precious > /data/p'
remove_one container "$PROBE_ID" >> "$RH_ROOT/commands.log"   # the container goes, its volume stays (no -v)
new_volume SCRATCH2 "memora-$RUN_ID-scratch2"
once -v "$PROBE:/from:ro" -v "$SCRATCH2:/to" "$IMAGE" sh -c "$(cat "$ROOT/scripts/migrate_data_volume.sh")" \
  migrate_data_volume migrate "$PROBE" >> "$RH_ROOT/commands.log" 2>&1
"$RT" volume exists "$PROBE" && pass "the migration form (named container + plain rm) keeps an unreferenced source volume" \
  || fail "the migration deleted its unreferenced source volume"
# The hazard itself, on this run's own probe volume. It is specific to
# `run --rm` (a `create --rm` + `start` keeps the volume and leaks its own
# anonymous /data). `run -d --rm` prints the ID of a successful create.
DEMO="$("$RT" run -d --rm --label "$RUN_LABEL" -v "$PROBE:/from:ro" --entrypoint true "$IMAGE")" \
  || die "create failed: --rm demo"
track container "$DEMO"
"$RT" wait "$DEMO" >/dev/null 2>&1; sleep 2
if "$RT" volume exists "$PROBE"; then fail "this runtime's run --rm kept the probe volume (the hazard did not reproduce)"; \
  else pass "confirmed the hazard: '$RT run --rm -v <anonymous>:/from' deleted the unreferenced probe volume (the fixed scripts never do this)"; fi

echo "== 4. local_primary.py through the live admin routes"
printf '%s' "$ADMIN_TOKEN" > "$RH_ROOT/admin.tok"; chmod 600 "$RH_ROOT/admin.tok"
cp "$CFG/all.health-token" "$RH_ROOT/health.tok"; chmod 600 "$RH_ROOT/health.tok"
check "wait for /health after the redeploy" wait_health
LP=("$PY" "$ROOT/scripts/local_primary.py")
TOK=(--memora-url "http://127.0.0.1:$PORT" --admin-token-file "$RH_ROOT/admin.tok" --health-token-file "$RH_ROOT/health.tok")
check "local_primary.py freeze memora" "${LP[@]}" freeze memora "${TOK[@]}"
code="$(curl -s -o "$RH_ROOT/health-db.json" -w '%{http_code}' -H "Authorization: Bearer $HEALTH_TOKEN" "http://127.0.0.1:$PORT/health/db/memora")"
"$PY" -c 'import json,sys; f=json.load(open(sys.argv[1]))["freeze"]; assert f["state"]=="frozen" and f["in_flight"]==0, f' \
  "$RH_ROOT/health-db.json" && pass "/health/db/memora shows frozen, 0 in flight ($code)" || fail "health/db: $(cat "$RH_ROOT/health-db.json")"
check "a write to the frozen store is refused" bash -c "! \"$RT\" exec -i \"$NEW3_ID\" python -c '
from memora import storage
c = storage.backend_for(\"memora\").connect(); c.execute(\"INSERT INTO memories (content) VALUES (1)\"); c.commit()'"
STORE_HOST="$("$RT" volume inspect "$VOL" --format '{{.Mountpoint}}')/memora.db"
MEMORA_D1_READ_TOKEN=unused "${LP[@]}" compare memora --mode barrier --store "$STORE_HOST" --account none \
  --database-id none --no-record "${TOK[@]}" --out-dir "$RH_ROOT/compare" > "$RH_ROOT/compare.json" 2>&1
grep -q "replication is not installed" "$RH_ROOT/compare.json" \
  && pass "compare --mode barrier reaches the live store and refuses: no replication installed (D1 steps skipped: no D1 here)" \
  || fail "compare: $(tail -3 "$RH_ROOT/compare.json")"
check "local_primary.py thaw memora" "${LP[@]}" thaw memora "${TOK[@]}"
curl -s -H "Authorization: Bearer $HEALTH_TOKEN" "http://127.0.0.1:$PORT/health/db/memora" > "$RH_ROOT/health-db2.json"
"$PY" -c 'import json,sys; assert json.load(open(sys.argv[1]))["freeze"]["state"]=="open"' "$RH_ROOT/health-db2.json" \
  && pass "thawed: /health/db/memora shows open" || fail "after thaw: $(cat "$RH_ROOT/health-db2.json")"

echo "== 4b. the cutover mechanics in the container; a replicated local store in log mode"
SHM=/dev/shm/memora-cutover
IN_TOOL=("$RT" exec "$NAME" python /app/scripts/local_primary.py)
IN_TOK=(--admin-token-file "$SHM/admin.token" --health-token-file "$SHM/health.token")
tokens_in() {  # exactly scripts/cutover_store.sh's form: the container's shell writes its own env
  "$RT" exec "$NAME" sh -c "umask 077 && mkdir -p $SHM && printf %s \"\$MEMORA_ADMIN_TOKEN\" > $SHM/admin.token && printf %s \"\$MEMORA_HEALTH_TOKEN\" > $SHM/health.token"
}
health_re() { curl -s -H "Authorization: Bearer $HEALTH_TOKEN" "http://127.0.0.1:$PORT/health/db/re"; }
check "place the tool's token files on the container's tmpfs (0600)" tokens_in
check "they are 0600 and on tmpfs" "$RT" exec "$NAME" sh -c \
  "test \"\$(stat -c %a $SHM/admin.token)\" = 600 && test \"\$(stat -f -c %T $SHM)\" = tmpfs"
check "the image carries the operator tool: freeze re from inside the container" "${IN_TOOL[@]}" freeze re "${IN_TOK[@]}"
check "fk-audit re from inside the container (clean)" "${IN_TOOL[@]}" fk-audit re --store /data/re.db
check "install sync on /data/re.db (standing in for the seed, which needs D1)" "$RT" exec "$NAME" python -c '
import sqlite3
from memora.schema import install_sync
c = sqlite3.connect("/data/re.db"); install_sync(c, "d1://rh-acct/rh-db", 1); c.close()'
printf "MEMORA_DATABASES='%s'\nMEMORA_REPLICAS='%s'\nMEMORA_REPLICATION=log\nMEMORA_REPLICATION_INTERVAL_S=2\nMEMORA_REPLICATION_BATCH_ROWS=50\n" \
  "$REG" '{"re": "d1://rh-acct/rh-db"}' > "$ENVF"
if deploy > "$RH_ROOT/deploy-4.log" 2>&1; then pass "deploy with MEMORA_REPLICAS / MEMORA_REPLICATION=log from all.env"; \
  else fail "deploy 4 (exit $?; see deploy-4.log)"; tail -30 "$RH_ROOT/deploy-4.log"; fi
adopt NEW4_ID container "$NAME"; adopt NEW_IMAGE_ID image "$IMAGE"; adopt_run_tags
grep -q "store re: replicating (log) to d1://rh-acct/rh-db" "$RH_ROOT/deploy-4.log" \
  && grep -q "store re: /health/db 200 ok, FROZEN .*memory_stats" "$RH_ROOT/deploy-4.log" \
  && pass "the deploy checked re's replication and ran memory_stats on it while frozen (X2: frozen stores serve reads)" \
  || fail "deploy 4 did not check re as a frozen replicated store"
check "wait for /health after deploy 4" wait_health
"$RT" inspect "$NAME" --format '{{json .Config.Env}}' | grep -q 'MEMORA_REPLICATION=log' \
  && pass "the container env carries the local-primary switches from all.env" || fail "MEMORA_REPLICATION not passed through"
sleep 5
health_re > "$RH_ROOT/health-re.json"
"$PY" -c '
import json, sys
h = json.load(open(sys.argv[1]))
assert h["freeze"]["state"] == "frozen", h["freeze"]
r = h["replication"]
assert r["mode"] == "log" and r["status"] == "running" and not r.get("halted_reason"), r
assert (r["interval_s"], r["poll_s"], r["batch_rows"]) == (2.0, 5.0, 50), r
' "$RH_ROOT/health-re.json" && pass "re came up frozen (persisted freeze), replicating in log mode, not halted; timing 2 s / 5 s / 50 rows from all.env" \
  || fail "/health/db/re after deploy 4: $(cat "$RH_ROOT/health-re.json")"
check "token files again in the new container" tokens_in
check "thaw re from inside the container" "${IN_TOOL[@]}" thaw re "${IN_TOK[@]}"
"$PY" - "$PORT" <<'PY' >> "$RH_ROOT/commands.log" 2>&1 && pass "a write to re through /mcp/re" || fail "write to re (see commands.log)"
import json, sys, urllib.request
base = f"http://127.0.0.1:{sys.argv[1]}/mcp/re"
H = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
def post(body, sid=None):
    h = dict(H, **({"mcp-session-id": sid} if sid else {}))
    with urllib.request.urlopen(urllib.request.Request(base, json.dumps(body).encode(), h), timeout=30) as r:
        return r.headers.get("mcp-session-id"), r.read().decode()
sid, _ = post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05",
              "capabilities": {}, "clientInfo": {"name": "rehearsal", "version": "0"}}})
post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
_, raw = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
               "params": {"name": "memory_create", "arguments": {"content": "rehearsal write under log mode", "tags": ["rehearsal"]}}}, sid)
assert '"isError":true' not in raw.replace(" ", "") and '"error"' not in raw[:200], raw[:500]
print(raw[:300])
PY
LOGGED=0
for _ in $(seq 1 20); do
  health_re > "$RH_ROOT/health-re2.json"
  "$PY" -c 'import json,sys; r=json.load(open(sys.argv[1]))["replication"]; sys.exit(0 if r["lag_rows"] == 0 and r["log_cursor_seq"] > 0 else 1)' \
    "$RH_ROOT/health-re2.json" && { LOGGED=1; break; }
  sleep 2
done
[ "$LOGGED" = 1 ] && pass "the write was logged: lag_rows 0, log cursor moved (no D1 involved)" \
  || fail "log mode did not catch up: $(cat "$RH_ROOT/health-re2.json")"
check "remove the tool's token files" "$RT" exec "$NAME" rm -rf "$SHM"
printf "MEMORA_DATABASES='%s'\n" "$REG" > "$ENVF"

echo "== 5. podman inspect samples (token values redacted)"
for f in old new; do
  "$PY" - "$RH_ROOT/fixtures/inspect-$f.raw.json" "$RH_ROOT/fixtures/podman_inspect_$f.json" <<'PY'
import json, re, sys
doc = json.load(open(sys.argv[1]))
def scrub(o):
    if isinstance(o, dict):
        return {k: scrub(v) for k, v in o.items()}
    if isinstance(o, list):
        return [scrub(v) for v in o]
    if isinstance(o, str):
        return re.sub(r"((?:TOKEN|SECRET|KEY)[A-Z_]*=)[^\s\"]+", r"\1<redacted>", o)
    return o
json.dump(scrub(doc), open(sys.argv[2], "w"), indent=2, sort_keys=True)
PY
  rm -f "$RH_ROOT/fixtures/inspect-$f.raw.json"
done
grep -l "$HEALTH_TOKEN\|$ADMIN_TOKEN" "$RH_ROOT"/fixtures/*.json && fail "a token survived the redaction" \
  || pass "inspect samples saved without token values"

echo "== 6. the teardown rules (scripts/rehearse_cleanup_selftest.sh, per-run decoys)"
RUNTIME="$RT" PYTHON="$PY" bash "$ROOT/scripts/rehearse_cleanup_selftest.sh" "$IMAGE" >> "$RH_ROOT/commands.log" 2>&1 \
  && pass "the teardown removes only this run's labelled, captured objects (self-test)" \
  || fail "the teardown self-test failed (see commands.log)"

echo
echo "== summary: $(grep -c '^PASS' "$RESULTS") passed, $FAILS failed ($RESULTS)"
exit $((FAILS > 0))
