#!/usr/bin/env bash
# REHEARSAL of Item 95 (add a store to memora-all, copy memories into it) against a REAL
# container runtime on a rehearsal host (build-host, podman). Local stores only; it can NOT reach a real
# D1 (Cloudflare's URL is fixed in memora/backends.py), so these legs are NOT rehearsed here and
# are covered by tests/ with a fake D1 and, in production, by the cutover's own gates:
#   bootstrap-d1 against a real new D1, the interim d1:// stage, the first export and receipt, the seed from a
#   receipt, write-mode replication to D1, the barrier compare against D1.
# What it DOES prove, with the real image, the real MCP server and the real deploy script:
#   * the image carries bootstrap-d1 / pre-freeze / clear-freeze and the copy tool;
#   * add_store_config.py check/apply/remove on copies of the operator files;
#   * pre-freeze: the persisted freeze makes a store come up FROZEN in a real deploy, and the
#     deploy's own frozen-store check passes; clear-freeze refuses while the service routes the store;
#   * a 5th local store is routed at /mcp/acebar by a real deploy and survives a redeploy;
#   * the copy tool inside the container against the live server: dry-run writes nothing, apply copies
#     (content, tags, metadata, created_at, provenance, links), a rerun and a redeploy copy nothing twice,
#     the source is byte-identical before and after.
# Every runtime object goes through scripts/rehearse_objects.sh (per-run names, labels, captured IDs,
# removal by ID). RH_KEEP=1 keeps them for inspection.
#
#   scripts/rehearse_add_store.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RT="${RUNTIME:-podman}"
RH_ROOT="${RH_ROOT:-$HOME/rehearsal-item95}"
RUN_ID="rh-$(date +%s)-$$"
RUN_START="$(date +%s)"
NAME="memora-$RUN_ID"
VOL="memora-$RUN_ID-data"
IMAGE="memora-$RUN_ID:latest"
PORT="${PORT:-18921}"
PY="${PYTHON:-python3}"
CFG="$RH_ROOT/config"
SECRETS="$RH_ROOT/secrets"
ENVF="$RH_ROOT/all.env"
DEPLOY_CFGF="$RH_ROOT/deploy.env"
RESULTS="$RH_ROOT/results.txt"
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml")"
REG4='{"memora": "/data/memora.db", "alpha": "/data/alpha.db", "beta": "/data/beta.db", "gamma": "/data/gamma.db"}'
REG5='{"memora": "/data/memora.db", "alpha": "/data/alpha.db", "beta": "/data/beta.db", "gamma": "/data/gamma.db", "acebar": "/data/acebar.db"}'
PROJ4='{"memora":["memora"],"alpha":["alpha"],"beta":["beta"],"gamma":["gamma"]}'
PROJ5='{"memora":["memora"],"alpha":["alpha"],"beta":["beta"],"gamma":["gamma"],"acebar":["acebar"]}'

OBJECTS="$RH_ROOT/run-$RUN_ID.objects"
mkdir -p "$RH_ROOT" "$CFG" "$SECRETS"
echo "start $RUN_START $RUN_ID" > "$OBJECTS"
. "$ROOT/scripts/rehearse_objects.sh"
finish() {
  if [ "${RH_KEEP:-0}" = 1 ]; then echo "RH_KEEP=1: objects kept; see $OBJECTS"; return; fi
  echo "== teardown of run $RUN_ID (by captured ID, label re-checked)"
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
check() {
  local what="$1"; shift
  if "$@" >>"$RH_ROOT/commands.log" 2>&1; then pass "$what"; else fail "$what (exit $?; see commands.log)"; fi
}
mint() { ( set +o pipefail; LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 48 ); }
wait_health() {
  for _ in $(seq 1 60); do curl -sf -m 3 "http://127.0.0.1:$PORT/health" >/dev/null && return 0; sleep 2; done
  return 1
}
deploy() {
  DEPLOY_REHEARSAL=1 DEPLOY_REHEARSAL_ROOT="$RH_ROOT" DEPLOY_LABELS="$RUN_LABEL" DEPLOY_HOST=localhost RUNTIME="$RT" DEPLOY_CONTAINER="$NAME" DEPLOY_DATA_VOLUME="$VOL" \
  DEPLOY_IMAGE="$IMAGE" DEPLOY_PORT="$PORT" DEPLOY_CONFIG_DIR="$CFG" DEPLOY_REPO="$ROOT" \
  DEPLOY_GRAPH_BIND=127.0.0.1 DEPLOY_GRAPH_PORT=18767 \
  DEPLOY_SKIP_CHECKOUT=1 DEPLOY_SMOKE_ABSORB=0 DEPLOY_ENV_FILE="$ENVF" DEPLOY_TAG="v$VERSION" DEPLOY_SECRETS_DIR="$SECRETS" \
  DEPLOY_CONFIG_FILE="$DEPLOY_CFGF" \
    bash "$ROOT/scripts/deploy-memora-all.sh"
}
health_db() { curl -s -H "Authorization: Bearer $HEALTH_TOKEN" "http://127.0.0.1:$PORT/health/db/$1"; }
IN=("$RT" exec "$NAME")
SHM=/dev/shm/memora-rehearsal
tokens_in() {
  "$RT" exec "$NAME" sh -c "umask 077 && mkdir -p $SHM && printf %s \"\$MEMORA_ADMIN_TOKEN\" > $SHM/admin.token && printf %s \"\$MEMORA_HEALTH_TOKEN\" > $SHM/health.token"
}
LP=("$RT" exec "$NAME" python /app/scripts/local_primary.py)
COPY=("$RT" exec "$NAME" python /app/scripts/copy_memories.py --source memora --dest acebar --health-token-file "$SHM/health.token"
      --base-url http://127.0.0.1:8000 --state-dir /data/copy)
# a content digest of a store, read through the container's own code (rows + links), for before/after
digest() {  # digest STORE
  "$RT" exec -i "$NAME" python - "$1" <<'PY'
import hashlib, json, sys
from memora import storage
conn = storage.backend_for(sys.argv[1]).connect()
rows = [tuple(r) for r in conn.execute("SELECT id, content, metadata, tags, created_at FROM memories ORDER BY id")]
refs = [tuple(r) for r in conn.execute("SELECT memory_id, related FROM memories_crossrefs ORDER BY memory_id")]
conn.close()
print(hashlib.sha256(json.dumps([rows, refs], default=str).encode()).hexdigest(), len(rows))
PY
}

echo "== run $RUN_ID: container $NAME, volume $VOL, image $IMAGE"
HEALTH_TOKEN="$(mint)"
printf '%s' "$HEALTH_TOKEN" > "$CFG/all.health-token"; chmod 600 "$CFG/all.health-token"
rm -f "$CFG/all.admin-token"
cat > "$CFG/credentials.mcp.json" <<JSON
{"mcpServers": {"memora": {"env": {"MEMORA_EMBEDDING_MODEL": "tfidf", "MEMORA_LLM_MODEL": "none"}}}}
JSON
printf "MEMORA_DATABASES='%s'\n" "$REG4" > "$ENVF"
printf '%s\n' "DEPLOY_HOST=localhost" "DEPLOY_GRAPH_BIND=127.0.0.1" "DEPLOY_REPO=$ROOT" "MEMORA_PROJECTS='$PROJ4'" > "$DEPLOY_CFGF"
chmod 700 "$SECRETS"
for f in cloudflare-api.token d1-read.token; do
  ( umask 077; printf 'rh-%s-%s\n' "$f" "$(mint)" > "$SECRETS/$f" )
done

echo "== 1. build the image from this checkout; the first deploy (four local stores)"
new_image IMG_ID "$IMAGE" "$ROOT" && pass "build the image ($IMG_ID)"
# the deploy script migrates from a running container's anonymous /data (production's shape), so the
# rehearsal starts the way R1 does: a first container with the four stores
new_container OLD_ID --name "$NAME" --restart unless-stopped -p "0.0.0.0:$PORT:8000" \
  -e "MEMORA_DATABASES=$REG4" -e MEMORA_DEFAULT_DB=memora -e "MEMORA_HEALTH_TOKEN=$HEALTH_TOKEN" \
  -e MEMORA_EMBEDDING_MODEL=tfidf -e MEMORA_ALLOW_ANY_TAG=1 "$IMAGE"
pass "start the first container ($OLD_ID)"
check "it answers /health" wait_health
anon_of OLD_VOL "$OLD_ID"
check "create the four store files (the deploy's preflight reads them)" "$RT" exec -i "$NAME" python - <<'PY'
import json, os
from memora import storage
for name in json.loads(os.environ["MEMORA_DATABASES"]):
    conn = storage.backend_for(name).connect()
    try:
        storage.add_memory(conn, content=f"rehearsal {name} baseline memory", tags=["rehearsal"])
        conn.commit()
    finally:
        conn.close()
PY
if deploy > "$RH_ROOT/deploy-1.log" 2>&1; then pass "deploy 1 (four stores; log: deploy-1.log)"; \
  else fail "deploy 1 (exit $?; see deploy-1.log)"; tail -30 "$RH_ROOT/deploy-1.log"; fi
adopt _ image "$IMAGE"; adopt VOL_ID volume "$VOL"; adopt C1 container "$NAME"; adopt_run_tags
check "the container answers /health" wait_health
check "the image carries bootstrap-d1" "${LP[@]}" bootstrap-d1 --help
check "the image carries pre-freeze and clear-freeze" bash -c "'$RT' exec '$NAME' python /app/scripts/local_primary.py pre-freeze --help && '$RT' exec '$NAME' python /app/scripts/local_primary.py clear-freeze --help"
check "the image carries the copy tool" "$RT" exec "$NAME" python /app/scripts/copy_memories.py --help

echo "== 2. seed the source store with memories tagged acebar (and one that is not)"
check "write the source memories, links and an old created_at" "$RT" exec -i "$NAME" python - <<'PY'
from memora import storage
from memora.storage import add_link
conn = storage.backend_for("memora").connect()
ids = {}
for key, content, tags, meta, created in [
    ("a", "rehearsal acebar note one: apples", ["acebar"], {"k": 1, "nested": {"x": [1, 2]}}, "2020-01-02 03:04:05"),
    ("b", "rehearsal acebar note two: bananas", ["acebar"], None, "2021-05-06 07:08:09"),
    ("c", "rehearsal acebar note three: cherries", ["acebar", "extra"], None, None),
    ("d", "rehearsal acebar note four: dates", ["acebar"], None, "2019-12-31 23:59:59"),
    ("out", "rehearsal not acebar: kumquat", ["other"], None, None),
]:
    rec = storage.add_memory(conn, content=content, tags=tags, metadata=meta)
    ids[key] = rec["id"]
    if created:
        conn.execute("UPDATE memories SET created_at = ? WHERE id = ?", (created, rec["id"]))
conn.commit()
add_link(conn, ids["a"], ids["b"], "references", True)
add_link(conn, ids["b"], ids["c"], "implements", True)
add_link(conn, ids["c"], ids["d"], "related_to", True)
add_link(conn, ids["a"], ids["d"], "supersedes", False)
add_link(conn, ids["a"], ids["out"], "references", True)
conn.commit(); conn.close()
print(ids)
PY
SRC_BEFORE="$(digest memora)"

echo "== 3. add_store_config.py on copies of the operator files"
CT="$RH_ROOT/cfgtest"; rm -rf "$CT"; mkdir -p "$CT"
cp "$ENVF" "$CT/all.env"; cp "$DEPLOY_CFGF" "$CT/deploy.env"
printf 'MEMORA_REPLICATION=write\nMEMORA_REPLICATION_INTERVAL_S=60\n' >> "$CT/all.env"
CFGT=("$PY" "$ROOT/scripts/add_store_config.py")
CFGARGS=(--all-env "$CT/all.env" --deploy-env "$CT/deploy.env" --db acebar --repo "$ROOT")
check "check accepts a new store" "${CFGT[@]}" check "${CFGARGS[@]}" --uri d1://rh-acct/rh-newdb-001
cp "$CT/all.env" "$CT/all.env.orig"
check "apply edits the interim entry and the project" "${CFGT[@]}" apply "${CFGARGS[@]}" --uri d1://rh-acct/rh-newdb-001 --backup-dir "$RH_ROOT/cfgbackup"
grep -q 'acebar' "$CT/all.env" && grep -q 'acebar' "$CT/deploy.env" && pass "acebar is in both files" || fail "acebar missing after apply"
check "remove takes only the acebar keys back out" "${CFGT[@]}" remove "${CFGARGS[@]}" --backup-dir "$RH_ROOT/cfgbackup"
cmp -s "$CT/all.env" "$CT/all.env.orig" && pass "the file is byte-identical to the original after apply + remove" || fail "all.env differs after apply + remove"
[ "$(stat -c %a "$RH_ROOT/cfgbackup")" = 700 ] && pass "the backup directory is 0700" || fail "backup dir mode"

echo "== 4. the new store, pre-frozen, then a real deploy that routes it (stand-ins for bootstrap + seed)"
check "create /data/acebar.db with the app's schema (stand-in for bootstrap + seed; a D1 leg is not rehearsable)" "${IN[@]}" python -c '
from pathlib import Path
from memora.backends import LocalSQLiteBackend
from memora import schema
c = LocalSQLiteBackend(Path("/data/acebar.db")).connect(); schema.ensure_schema(c); c.commit(); c.close()'
check "a verified bootstrap state (stand-in), then the REAL pre-freeze command" "${IN[@]}" python -c '
from memora import add_store as a
a._save_state("acebar", {"account_id": "rh-acct", "database_id": "rh-newdb-001", "d1_name": "acebar-rh", "phase": "verified"}, None)'
check "pre-freeze acebar" "${LP[@]}" pre-freeze acebar
check "the persisted freeze file exists" "${IN[@]}" test -e /data/freeze/acebar
printf "MEMORA_DATABASES='%s'\n" "$REG5" > "$ENVF"
printf '%s\n' "DEPLOY_HOST=localhost" "DEPLOY_GRAPH_BIND=127.0.0.1" "DEPLOY_REPO=$ROOT" "MEMORA_PROJECTS='$PROJ5'" > "$DEPLOY_CFGF"
if deploy > "$RH_ROOT/deploy-2.log" 2>&1; then pass "deploy 2: five stores (log: deploy-2.log)"; \
  else fail "deploy 2 (exit $?; see deploy-2.log)"; tail -30 "$RH_ROOT/deploy-2.log"; fi
adopt C2 container "$NAME"; adopt _ image "$IMAGE"; adopt_run_tags
check "the container answers /health" wait_health
grep -q "store acebar: /health/db 200 ok, FROZEN .*memory_stats" "$RH_ROOT/deploy-2.log" \
  && pass "the deploy checked acebar as a FROZEN store and ran memory_stats on it" || fail "deploy 2 did not check acebar as frozen"
health_db acebar > "$RH_ROOT/health-acebar.json"
"$PY" -c 'import json,sys; f=json.load(open(sys.argv[1]))["freeze"]; assert f["state"]=="frozen" and f["in_flight"]==0, f' "$RH_ROOT/health-acebar.json" \
  && pass "/health/db/acebar: frozen from the persisted freeze, 0 in flight (nothing could write it)" || fail "acebar health: $(cat "$RH_ROOT/health-acebar.json")"
check "token files for the tools" tokens_in
OUT="$("${LP[@]}" clear-freeze acebar 2>&1)"; RC=$?
[ "$RC" != 0 ] && echo "$OUT" | grep -q "still routes" && pass "clear-freeze refuses while the service routes acebar (the freeze stays)" || fail "clear-freeze while routed: rc=$RC $OUT"
check "the freeze file is still there" "${IN[@]}" test -e /data/freeze/acebar

echo "== 5. the copy: dry-run (still frozen), a refused apply, thaw, apply, rerun, redeploy"
DEST0="$(digest acebar)"
check "dry-run writes the plan file (a frozen destination is only read)" "${COPY[@]}" --tag acebar --plan /data/copy/plan.json
OUT="$("${COPY[@]}" --plan /data/copy/plan.json --apply 2>&1)"; RC=$?
[ "$RC" = 2 ] && echo "$OUT" | grep -q "frozen" && pass "apply into the FROZEN interim store is refused (exit 2)" || fail "apply into a frozen store: rc=$RC $OUT"
[ "$(digest acebar)" = "$DEST0" ] && pass "the refused apply wrote nothing" || fail "the refused apply changed acebar"
check "thaw acebar (the cutover's last step, here by the operator tool)" "${LP[@]}" thaw acebar --admin-token-file "$SHM/admin.token" --health-token-file "$SHM/health.token"
[ "$(digest acebar)" = "$DEST0" ] && pass "the dry-run wrote nothing to the destination" || fail "the dry-run changed acebar"
[ "$(digest memora)" = "$SRC_BEFORE" ] && pass "the dry-run left the source byte-identical" || fail "the dry-run changed memora"
"${IN[@]}" python -c '
import json
p = json.load(open("/data/copy/plan.json")); r = p["report"]
assert r["selected"] == 4 and len(r["would_copy"]) == 4 and not r["unknown_refs"] and not r["links"]["unrepresentable"], r
assert r["links"]["external_dropped"] >= 1, r
print("plan ok", r["links"])' >>"$RH_ROOT/commands.log" 2>&1 && pass "the plan: 4 selected, 4 to copy, the external edge counted" || fail "the plan report is not as expected"
check "apply" "${COPY[@]}" --plan /data/copy/plan.json --apply
"${IN[@]}" python - <<'PY' >>"$RH_ROOT/commands.log" 2>&1 && pass "acebar holds the four copies: content, created_at, tags, metadata, provenance, project, links" || fail "the copies differ (see commands.log)"
import json
from memora import storage
conn = storage.backend_for("acebar").connect()
rows = {r["content"]: dict(r) for r in conn.execute("SELECT * FROM memories")}
assert len(rows) == 4, sorted(rows)
a = rows["rehearsal acebar note one: apples"]
assert a["created_at"] == "2020-01-02 03:04:05", a["created_at"]
meta = json.loads(a["metadata"])
assert meta["k"] == 1 and meta["nested"] == {"x": [1, 2]} and meta["project"] == "acebar", meta
assert meta["memora_copy"]["store"] == "memora", meta
assert "acebar" in json.loads(a["tags"])
c = rows["rehearsal acebar note three: cherries"]
assert set(json.loads(c["tags"])) >= {"acebar", "extra"}
ids = {k.split(": ")[1]: v["id"] for k, v in rows.items()}
rel = {r["memory_id"]: {(e["id"], e["edge_type"]) for e in json.loads(r["related"] or "[]")}
       for r in conn.execute("SELECT memory_id, related FROM memories_crossrefs")}
assert (ids["bananas"], "references") in rel[ids["apples"]] and (ids["apples"], "referenced_by") in rel[ids["bananas"]]
assert (ids["cherries"], "implements") in rel[ids["bananas"]]
assert (ids["dates"], "related_to") in rel[ids["cherries"]] and (ids["cherries"], "related_to") in rel[ids["dates"]]
assert (ids["dates"], "supersedes") in rel[ids["apples"]] and (ids["apples"], "superseded_by") not in rel.get(ids["dates"], set())
src = storage.backend_for("memora").connect()
assert {r[0] for r in src.execute("SELECT id FROM memories")} >= set(), "source readable"
print("acebar ok")
PY
[ "$(digest memora)" = "$SRC_BEFORE" ] && pass "the source is byte-identical after the copy" || fail "the source changed"
AFTER1="$(digest acebar)"
sleep 65   # the server's memory_import cooldown (60 s) is part of what a rerun meets
check "a rerun copies nothing twice" "${COPY[@]}" --plan /data/copy/plan.json --apply
[ "$(digest acebar)" = "$AFTER1" ] && pass "the rerun left acebar byte-identical (4 rows, no duplicates)" || fail "the rerun changed acebar"
if deploy > "$RH_ROOT/deploy-3.log" 2>&1; then pass "deploy 3 (restart with the copied data)"; else fail "deploy 3 (exit $?; see deploy-3.log)"; fi
adopt C3 container "$NAME"; adopt _ image "$IMAGE"; adopt_run_tags
check "the container answers /health" wait_health
[ "$(digest acebar)" = "$AFTER1" ] && pass "the copies survived the redeploy unchanged" || fail "acebar changed across the redeploy"
curl -s -H "Authorization: Bearer $HEALTH_TOKEN" "http://127.0.0.1:$PORT/health/db/acebar" > "$RH_ROOT/health-acebar2.json"
"$PY" -c 'import json,sys; assert json.load(open(sys.argv[1]))["freeze"]["state"]=="open"' "$RH_ROOT/health-acebar2.json" \
  && pass "acebar stayed thawed across the redeploy (the thaw removed the persisted freeze)" || fail "acebar health after deploy 3: $(cat "$RH_ROOT/health-acebar2.json")"
check "remove the tool's token files" "$RT" exec "$NAME" rm -rf "$SHM"

echo
echo "NOT REHEARSED (no real or fake D1 reachable from a container): bootstrap-d1 on a new D1, the interim d1:// stage,"
echo "the first export/receipt, the seed from a receipt, write-mode replication, the barrier compare against D1."
echo "== summary: $(grep -c '^PASS' "$RESULTS") passed, $FAILS failed ($RESULTS)"
exit $((FAILS > 0))
