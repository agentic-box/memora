#!/usr/bin/env bash
# Run the local-primary operator tool (scripts/local_primary.py) in a
# one-off container of the image memora-all runs NOW (X3; plan §5.3 and §4).
#
#   LP_TOKEN_DIR=/etc/memora/lp-secrets scripts/lp_container.sh rollback gamma --phase verify \
#       --store /data/gamma.db --lock-barrier --admin-token-file /run/secrets/memora/admin \
#       --health-token-file /run/secrets/memora/health --read-token-file /run/secrets/memora/d1-read ...
#
# - The image is memora-all's own image ID (`inspect .Image`), not a tag that
#   may have moved since it started: the tool matches the memora that serves.
# - /data is the named volume memora-all-data (the store and its primary
#   lock); LP_TOKEN_DIR (0600 token files) is mounted read-only at
#   /run/secrets/memora. The host's docker socket is NEVER mounted, so inside
#   there is no docker: use --lock-barrier (--service-stopped is refused).
# - The PROOF that memora-all is not running is the data volume's service
#   lock (/data/.service.lock, review 7823/7825): memora-all takes it at
#   startup and holds it for its lifetime (MEMORA_SERVICE_LOCK=1 in the
#   image); the tool, for a stopped-required command, takes it first and
#   holds it for the whole run, so memora-all cannot start meanwhile (it
#   exits 2). This script refuses (exit 70) unless memora-all mounts
#   LP_DATA_VOLUME at /data and, for a stopped-required command, runs with
#   MEMORA_SERVICE_LOCK=1 and MEMORA_DATA_DIR unset or /data (review 7830:
#   the same lock FILE on both sides) -- otherwise the lock proves nothing.
# - It classifies the command -- stopped-required (restore, resume,
#   sequence-highwater, rollback --phase verify) or running-required
#   (rollback --phase finish|drain) -- and checks `inspect .State.Running`
#   BEFORE the run (a friendly early refusal, exit 67) and AFTER it (exit 68
#   if it changed). memora-all's own MEMORA_DATABASES is passed in, so
#   --lock-barrier can refuse a store that memora-all routes to d1:// or to
#   another path.
# - Orphans: a SIGKILL of this script skips the EXIT trap. Its container is
#   then left stopped; find and remove it by label, never by name alone:
#     docker ps -a --filter label=memora.lp.run --format '{{.ID}} {{.Names}} {{.Label "memora.lp.run"}}'
#     docker rm <ID>     # only when its label equals its name (memora-lp-<ts>-<pid>)
# - The container is created (`create`, its ID captured only from a
#   successful create), labelled memora.lp.run=<name>, started attached, and
#   removed at exit BY THAT ID after its label is re-checked. A failed create
#   (a name collision) removes nothing. Never --rm; never a volume or image.
#
# Environment: LP_TOKEN_DIR (required), LP_RUNTIME (docker), LP_SERVICE
# (memora-all), LP_DATA_VOLUME (memora-all-data), LP_NETWORK (host).
# Exit: the tool's own status; 64 the image predates the tool; 65 usage;
# 66 memora-all cannot be inspected; 67 memora-all is in the wrong state for
# this command; 68 memora-all's state changed during the run; 69 the
# container could not be created; 70 memora-all does not use this data
# volume, or (stopped-required) does not hold the service lock.
set -euo pipefail

RT="${LP_RUNTIME:-docker}"
SERVICE="${LP_SERVICE:-memora-all}"
VOLUME="${LP_DATA_VOLUME:-memora-all-data}"
NETWORK="${LP_NETWORK:-host}"
LABEL="memora.lp.run"

usage() { echo "usage: LP_TOKEN_DIR=<dir> $0 <local_primary.py arguments...>" >&2; exit 65; }
[ "$#" -gt 0 ] || usage
if [ -z "${LP_TOKEN_DIR:-}" ] || [ ! -d "$LP_TOKEN_DIR" ]; then
  echo "lp_container: LP_TOKEN_DIR must name the host directory of the 0600 token files" >&2
  exit 65
fi
PHASE=""
prev=""
for a in "$@"; do
  if [ "$a" = "--service-stopped" ]; then
    echo "lp_container: --service-stopped needs docker, which is not in the container; use --lock-barrier" >&2
    exit 65
  fi
  if [ "$prev" = "--phase" ]; then PHASE="$a"; fi
  prev="$a"
done
case "$1" in
  restore|resume|sequence-highwater) REQUIRED=stopped ;;
  rollback)
    case "$PHASE" in
      verify) REQUIRED=stopped ;;
      finish|drain) REQUIRED=running ;;
      *) echo "lp_container: rollback needs --phase drain|verify|finish" >&2; exit 65 ;;
    esac ;;
  *) REQUIRED=any ;;
esac

running() { "$RT" inspect -f '{{.State.Running}}' "$SERVICE" 2>/dev/null; }
if ! BEFORE="$(running)" || [ -z "$BEFORE" ]; then
  echo "lp_container: cannot inspect $SERVICE" >&2
  exit 66
fi
if [ "$REQUIRED" = stopped ] && [ "$BEFORE" != false ]; then
  echo "lp_container: $1 needs $SERVICE stopped (State.Running=$BEFORE); nothing was run" >&2
  exit 67
fi
if [ "$REQUIRED" = running ] && [ "$BEFORE" != true ]; then
  echo "lp_container: $1 --phase $PHASE needs $SERVICE running (State.Running=$BEFORE); nothing was run" >&2
  exit 67
fi
if ! IMAGE="$("$RT" inspect -f '{{.Image}}' "$SERVICE" 2>/dev/null)" || [ -z "$IMAGE" ]; then
  echo "lp_container: cannot read the image of $SERVICE" >&2
  exit 66
fi
RUN_USER="$("$RT" inspect -f '{{.Config.User}}' "$SERVICE" 2>/dev/null || true)"
SERVICE_ENV="$("$RT" inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$SERVICE" 2>/dev/null || true)"
DATABASES="$(printf '%s\n' "$SERVICE_ENV" | sed -n 's/^MEMORA_DATABASES=//p' | head -n 1)"
DATA_MOUNT="$("$RT" inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' "$SERVICE" 2>/dev/null || true)"
if [ "$DATA_MOUNT" != "$VOLUME" ]; then
  echo "lp_container: $SERVICE mounts '${DATA_MOUNT:-nothing}' at /data, not $VOLUME: the locks would prove nothing" >&2
  exit 70
fi
if [ "$REQUIRED" = stopped ] && ! printf '%s\n' "$SERVICE_ENV" | grep -qx 'MEMORA_SERVICE_LOCK=1'; then
  echo "lp_container: $SERVICE does not run with MEMORA_SERVICE_LOCK=1 (an image before the service lock?): its absence cannot be proven; nothing was run" >&2
  exit 70
fi
# memora-all locks <MEMORA_DATA_DIR>/.service.lock; this run locks
# /data/.service.lock. They are the same file only when memora-all's data
# dir is /data (unset means /data) -- review 7830.
SERVICE_DATA_DIR="$(printf '%s\n' "$SERVICE_ENV" | sed -n 's/^MEMORA_DATA_DIR=//p' | tail -n 1)"
if [ "$REQUIRED" = stopped ] && [ -n "$SERVICE_DATA_DIR" ] && [ "$SERVICE_DATA_DIR" != /data ]; then
  echo "lp_container: $SERVICE runs with MEMORA_DATA_DIR=$SERVICE_DATA_DIR, not /data: its service lock would be another file; nothing was run" >&2
  exit 70
fi

NAME="memora-lp-$(date -u +%Y%m%dT%H%M%SZ)-$$"
CID=""

cleanup() {
  [ -n "$CID" ] || return 0  # nothing was created by this run
  local got
  got="$("$RT" inspect -f "{{index .Config.Labels \"$LABEL\"}}" "$CID" 2>/dev/null)" || return 0
  if [ "$got" = "$NAME" ]; then
    "$RT" rm -f "$CID" >/dev/null 2>&1 || echo "lp_container: could not remove $CID ($NAME)" >&2
  else
    echo "lp_container: $CID does not carry this run's label; left in place" >&2
  fi
}
trap cleanup EXIT

CREATE_ARGS=(create --name "$NAME" --label "$LABEL=$NAME" --network "$NETWORK"
             -v "$VOLUME:/data" -v "$LP_TOKEN_DIR:/run/secrets/memora:ro"
             -e MEMORA_DATA_DIR=/data -e "LP_SERVICE_DATA_DIR=${SERVICE_DATA_DIR:-/data}" --entrypoint sh)
if [ -n "$RUN_USER" ]; then
  CREATE_ARGS+=(--user "$RUN_USER")
fi
if [ -n "$DATABASES" ]; then
  CREATE_ARGS+=(-e "MEMORA_DATABASES=$DATABASES")
fi
if ! CID="$("$RT" "${CREATE_ARGS[@]}" "$IMAGE" -c \
  'test -f /app/scripts/local_primary.py || { echo "lp_container: this memora-all image predates the operator tool (deploy a build with scripts/local_primary.py)" >&2; exit 64; }; exec python /app/scripts/local_primary.py "$@"' \
  lp "$@")" || [ -z "$CID" ]; then
  CID=""
  echo "lp_container: could not create $NAME; nothing to clean up" >&2
  exit 69
fi
set +e
"$RT" start -a "$CID"
rc=$?
set -e
AFTER="$(running || true)"
if [ "$AFTER" != "$BEFORE" ]; then
  echo "lp_container: $SERVICE State.Running changed during the run ($BEFORE -> ${AFTER:-unknown}); the run's barrier did not hold -- check the store before anything else (tool exit $rc)" >&2
  exit 68
fi
exit "$rc"
