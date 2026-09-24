#!/usr/bin/env bash
# Deployment REHEARSAL (R1): the L2a deploy and migration, and the L2-L6
# startup, against a REAL container runtime on a rehearsal host (server2,
# podman). No Cloudflare, no nuc8, no tokens that exist anywhere else: every
# store is a local SQLite file, every name is suffixed "-rh", the port is
# 18920, and all state lives under $RH_ROOT.
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
#   5. saves real `podman inspect` samples (token values redacted) into
#      $RH_ROOT/fixtures for tests/fixtures.
set -uo pipefail

OLD_SRC="${1:?usage: rehearse_deploy.sh OLD_SRC}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RT="${RUNTIME:-podman}"
RH_ROOT="${RH_ROOT:-$HOME/rehearsal-r1}"
NAME=memora-rh
VOL=memora-rh-data
IMAGE=memora-rh:latest
PORT=18920
PY="${PYTHON:-python3}"
CFG="$RH_ROOT/config"
ENVF="$RH_ROOT/all.env"
RESULTS="$RH_ROOT/results.txt"
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml")"
REG='{"memora": "/data/memora.db", "ob1": "/data/ob1.db", "bestation": "/data/bestation.db", "re": "/data/re.db"}'

mkdir -p "$RH_ROOT" "$CFG" "$RH_ROOT/fixtures"
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
# Never `run --rm` with a volume mounted by name: podman's --rm deletes an
# ANONYMOUS volume once no container references it (this rehearsal's finding).
# Every helper container is named and removed with a plain `rm`.
once() {  # once NAME ARGS... -- run a helper container, then remove it (never its volumes)
  local name="rh-helper-$1-$$"; shift
  "$RT" run --name "$name" --tmpfs /data "$@"; local rc=$?  # --tmpfs: no anonymous /data volume left behind
  "$RT" rm "$name" >/dev/null 2>&1
  return $rc
}
mig_digest() {  # mig_digest VOLUME -- the migration program's own digest of a volume
  once dig -v "$1:/from:ro" --entrypoint sh "$IMAGE" -c "$(cat "$ROOT/scripts/migrate_data_volume.sh")" \
    migrate_data_volume digest
}
vol_digest() {  # vol_digest VOLUME -- sha256 of every file except the migration's own
  once vd -v "$1:/v:ro" --entrypoint python "$IMAGE" -c '
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
  DEPLOY_HOST=localhost RUNTIME="$RT" DEPLOY_CONTAINER="$NAME" DEPLOY_DATA_VOLUME="$VOL" \
  DEPLOY_IMAGE="$IMAGE" DEPLOY_PORT="$PORT" DEPLOY_CONFIG_DIR="$CFG" DEPLOY_REPO="$ROOT" \
  DEPLOY_SKIP_CHECKOUT=1 DEPLOY_SMOKE_ABSORB=0 DEPLOY_ENV_FILE="$ENVF" DEPLOY_TAG="v$VERSION" \
    bash "$ROOT/scripts/deploy-memora-all.sh"
}

echo "== 0. clean up an earlier rehearsal (only what it created; no prune)"
for c in $("$RT" ps -a --format '{{.Names}}' | grep -E "^($NAME(-grok-[0-9]+|-migrate-[0-9]+)?|rh-helper-.*)$" || true); do
  "$RT" rm -f "$c" >/dev/null
done
for v in "$VOL" memora-rh-scratch memora-rh-scratch2 $(cat "$RH_ROOT/volumes-created.txt" 2>/dev/null); do
  "$RT" volume rm -f "$v" >/dev/null 2>&1 || true
done
: > "$RH_ROOT/volumes-created.txt"

# The rehearsal's own config: throwaway tokens (0600), no Cloudflare anything.
HEALTH_TOKEN="$(mint)"
printf '%s' "$HEALTH_TOKEN" > "$CFG/all.health-token"; chmod 600 "$CFG/all.health-token"
rm -f "$CFG/all.admin-token"   # the deploy mints it
cat > "$CFG/credentials.mcp.json" <<JSON
{"mcpServers": {"memora": {"env": {"MEMORA_EMBEDDING_MODEL": "tfidf", "MEMORA_LLM_MODEL": "none"}}}}
JSON
printf "MEMORA_DATABASES='%s'\n" "$REG" > "$ENVF"

echo "== 1. the OLD image (production's memora:latest today) from $OLD_SRC"
check "build the old image from $OLD_SRC" "$RT" build -t "$IMAGE" "$OLD_SRC"

echo "== 2. the old container, the production way (anonymous /data)"
check "start the old container" "$RT" run -d --name "$NAME" --restart unless-stopped -p "0.0.0.0:$PORT:8000" \
  -e "MEMORA_DATABASES=$REG" -e MEMORA_DEFAULT_DB=memora -e "MEMORA_HEALTH_TOKEN=$HEALTH_TOKEN" \
  -e MEMORA_EMBEDDING_MODEL=tfidf -e MEMORA_ALLOW_ANY_TAG=1 "$IMAGE"
check "the old container answers /health" wait_health
OLD_VOL="$(old_anon "$NAME")"
echo "$OLD_VOL" >> "$RH_ROOT/volumes-created.txt"
if printf '%s' "$OLD_VOL" | grep -Eqx '[0-9a-f]{64}'; then pass "the old container's /data is an anonymous volume ($OLD_VOL)"; \
  else fail "the old container's /data is not an anonymous volume: '$OLD_VOL'"; fi
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
GROK1="$("$RT" ps -a --format '{{.Names}}' | grep -E "^$NAME-grok-[0-9]+$" | sort | tail -1)"
[ -n "$GROK1" ] && pass "the old container is kept as $GROK1" || fail "no renamed old container"
[ "$("$RT" inspect "$GROK1" --format '{{.State.Running}}' 2>/dev/null)" = false ] \
  && pass "the old container is stopped" || fail "the old container is not stopped"
[ "$(old_anon "$GROK1")" = "$OLD_VOL" ] && pass "the old container still mounts its anonymous volume" \
  || fail "the old container's volume changed"
[ "$(old_anon "$NAME")" = "$VOL" ] && pass "the new container mounts the named volume $VOL" \
  || fail "the new container mounts '$(old_anon "$NAME")'"
# The new server has been writing to the named volume since it started (the
# schema upgrade), so it is compared through what the copy recorded, plus an
# independent copy made by the same program before any server touches it.
once mk -v "$VOL:/v:ro" --entrypoint sh "$IMAGE" -c 'cat /v/.memora-volume-source' > "$RH_ROOT/marker-1.txt" 2>&1
grep -q "source=$OLD_VOL" "$RH_ROOT/marker-1.txt" && pass "the marker names the source volume" || fail "marker: $(cat "$RH_ROOT/marker-1.txt")"
OLD_DIGEST="$(mig_digest "$OLD_VOL")"
grep -q "digest=$OLD_DIGEST" "$RH_ROOT/marker-1.txt" \
  && pass "the marker's digest is the old volume's content (the copy verified exactly this source)" \
  || fail "marker digest != the old volume's digest $OLD_DIGEST"
"$RT" volume create memora-rh-scratch >/dev/null
if once mig -v "$OLD_VOL:/from:ro" -v memora-rh-scratch:/to "$IMAGE" sh -c "$(cat "$ROOT/scripts/migrate_data_volume.sh")" \
     migrate_data_volume migrate "$OLD_VOL" >> "$RH_ROOT/commands.log" 2>&1 \
   && [ "$(vol_digest "$OLD_VOL")" = "$(vol_digest memora-rh-scratch)" ]; then
  pass "a copy by the same program on this runtime is byte-identical, file by file (WAL sidecars, intent journal)"
else
  fail "the independent copy differs from the old volume"
fi
once wal -v "$OLD_VOL:/a:ro" -v "$VOL:/b:ro" --entrypoint sh "$IMAGE" -c \
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

echo "== 3b. a second deploy copies nothing"
MARK1="$(cat "$RH_ROOT/marker-1.txt")"
if deploy > "$RH_ROOT/deploy-2.log" 2>&1; then pass "second deploy"; else fail "second deploy (exit $?; see deploy-2.log)"; fi
grep -q migrate_data_volume "$RH_ROOT/deploy-2.log" && fail "the second deploy ran a copy" || pass "the second deploy ran no copy"
once mk -v "$VOL:/v:ro" --entrypoint sh "$IMAGE" -c 'cat /v/.memora-volume-source' > "$RH_ROOT/marker-2.txt" 2>&1
[ "$(cat "$RH_ROOT/marker-2.txt")" = "$MARK1" ] && pass "the marker is unchanged" || fail "the marker changed"
"$RT" inspect "$NAME" > "$RH_ROOT/fixtures/inspect-new.raw.json"

echo "== 3c. rollback to the old container, write to it, redeploy: the copy is redone"
GROK2="$("$RT" ps -a --format '{{.Names}}' | grep -E "^$NAME-grok-[0-9]+$" | sort | tail -1)"
check "rollback: remove the new container" "$RT" rm -f "$NAME"
check "rollback: rename the ORIGINAL old container back" "$RT" rename "$GROK1" "$NAME"
check "rollback: start it" "$RT" start "$NAME"
check "the rolled-back container answers /health" wait_health
check "write to the rolled-back (old) container" "$RT" exec -i "$NAME" python - <<'PY'
from memora import storage
conn = storage.backend_for("ob1").connect()
storage.add_memory(conn, content="written after the rollback", tags=["rehearsal"]); conn.commit(); conn.close()
PY
[ -n "$GROK2" ] && [ "$GROK2" != "$GROK1" ] && "$RT" rm -f "$GROK2" >/dev/null   # the second deploy's leftover
if deploy > "$RH_ROOT/deploy-3.log" 2>&1; then pass "redeploy after the rollback"; else fail "redeploy (exit $?; see deploy-3.log)"; fi
once mk -v "$VOL:/v:ro" --entrypoint sh "$IMAGE" -c 'cat /v/.memora-volume-source' > "$RH_ROOT/marker-3.txt" 2>&1
NEW_OLD_DIGEST="$(mig_digest "$OLD_VOL")"
[ "$NEW_OLD_DIGEST" != "$OLD_DIGEST" ] && grep -q "digest=$NEW_OLD_DIGEST" "$RH_ROOT/marker-3.txt" \
  && pass "the redeploy recopied: the marker now records the old volume WITH the post-rollback writes" \
  || fail "the redeploy did not recopy the post-rollback content (marker: $(cat "$RH_ROOT/marker-3.txt"))"
once ls -v "$VOL:/v:ro" --entrypoint sh "$IMAGE" -c 'ls -a /v' > "$RH_ROOT/volume-listing.txt"
grep -q '^\.memora-previous-' "$RH_ROOT/volume-listing.txt" && pass "the replaced content was moved aside (.memora-previous-*), not overwritten" \
  || fail "no .memora-previous-* after the recopy"

echo "== 3d. podman: run --rm deletes an unreferenced anonymous volume; the migration form does not"
"$RT" run -d --name rh-helper-probe-$$ --entrypoint sleep "$IMAGE" 600 >/dev/null
PROBE="$(old_anon rh-helper-probe-$$)"; echo "$PROBE" >> "$RH_ROOT/volumes-created.txt"
"$RT" exec rh-helper-probe-$$ sh -c 'echo precious > /data/p'
"$RT" rm -f rh-helper-probe-$$ >/dev/null   # the container goes, its volume stays (no -v)
"$RT" volume create memora-rh-scratch2 >/dev/null
once mig2 -v "$PROBE:/from:ro" -v memora-rh-scratch2:/to "$IMAGE" sh -c "$(cat "$ROOT/scripts/migrate_data_volume.sh")" \
  migrate_data_volume migrate "$PROBE" >> "$RH_ROOT/commands.log" 2>&1
"$RT" volume exists "$PROBE" && pass "the migration form (named container + plain rm) keeps an unreferenced source volume" \
  || fail "the migration deleted its unreferenced source volume"
"$RT" run --rm -v "$PROBE:/from:ro" --entrypoint true "$IMAGE" >/dev/null 2>&1
if "$RT" volume exists "$PROBE"; then pass "note: this runtime's run --rm kept the volume"; \
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
check "a write to the frozen store is refused" bash -c "! \"$RT\" exec -i \"$NAME\" python -c '
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

echo
echo "== summary: $(grep -c '^PASS' "$RESULTS") passed, $FAILS failed ($RESULTS)"
exit $((FAILS > 0))
