#!/usr/bin/env bash
# Run the local-primary operator tool (scripts/local_primary.py) in a
# one-off container of the image memora-all runs NOW (X3; plan §5.3 and §4).
#
#   LP_TOKEN_DIR=/etc/memora/lp-secrets scripts/lp_container.sh rollback re --phase verify \
#       --store /data/re.db --lock-barrier --admin-token-file /run/secrets/memora/admin \
#       --health-token-file /run/secrets/memora/health --read-token-file /run/secrets/memora/d1-read ...
#
# - The image is memora-all's own image ID (`inspect .Image`), not a tag that
#   may have moved since it started: the tool matches the memora that serves.
# - /data is the named volume memora-all-data (the store and its primary
#   lock); LP_TOKEN_DIR (0600 token files) is mounted read-only at
#   /run/secrets/memora. The host's docker socket is NEVER mounted, so inside
#   there is no docker: use --lock-barrier, which holds the store's primary
#   lock for the whole run (memora-all holds it while it serves the store).
#   --service-stopped needs docker and is refused here.
# - The container is named memora-lp-<ts>-<pid> and labelled with that name.
#   It is NOT run with --rm: on exit this script removes that one container,
#   by name, and only while it still carries this run's label (R1's lesson:
#   never remove what this run did not create). Volumes are never removed.
#
# Environment: LP_TOKEN_DIR (required), LP_RUNTIME (docker), LP_SERVICE
# (memora-all), LP_DATA_VOLUME (memora-all-data), LP_NETWORK (host: D1, R2
# and memora-all's published port are reachable as on the host).
# Exit: the tool's own status; 64 when the image predates the tool; 65 on a
# usage error; 66 when memora-all's image cannot be read.
set -euo pipefail

RT="${LP_RUNTIME:-docker}"
SERVICE="${LP_SERVICE:-memora-all}"
VOLUME="${LP_DATA_VOLUME:-memora-all-data}"
NETWORK="${LP_NETWORK:-host}"

usage() { echo "usage: LP_TOKEN_DIR=<dir> $0 <local_primary.py arguments...>" >&2; exit 65; }
[ "$#" -gt 0 ] || usage
if [ -z "${LP_TOKEN_DIR:-}" ] || [ ! -d "$LP_TOKEN_DIR" ]; then
  echo "lp_container: LP_TOKEN_DIR must name the host directory of the 0600 token files" >&2
  exit 65
fi
for a in "$@"; do
  if [ "$a" = "--service-stopped" ]; then
    echo "lp_container: --service-stopped needs docker, which is not in the container; use --lock-barrier" >&2
    exit 65
  fi
done

if ! IMAGE="$("$RT" inspect -f '{{.Image}}' "$SERVICE" 2>/dev/null)" || [ -z "$IMAGE" ]; then
  echo "lp_container: cannot read the image of $SERVICE" >&2
  exit 66
fi
RUN_USER="$("$RT" inspect -f '{{.Config.User}}' "$SERVICE" 2>/dev/null || true)"

NAME="memora-lp-$(date -u +%Y%m%dT%H%M%SZ)-$$"
LABEL="memora.lp.run"

cleanup() {
  local got
  got="$("$RT" inspect -f "{{index .Config.Labels \"$LABEL\"}}" "$NAME" 2>/dev/null)" || return 0
  if [ "$got" = "$NAME" ]; then
    "$RT" rm -f "$NAME" >/dev/null 2>&1 || echo "lp_container: could not remove $NAME" >&2
  else
    echo "lp_container: $NAME does not carry this run's label; left in place" >&2
  fi
}
trap cleanup EXIT

RUN_ARGS=(run --name "$NAME" --label "$LABEL=$NAME" --network "$NETWORK"
          -v "$VOLUME:/data" -v "$LP_TOKEN_DIR:/run/secrets/memora:ro"
          -e MEMORA_DATA_DIR=/data --entrypoint sh)
if [ -n "$RUN_USER" ]; then
  RUN_ARGS+=(--user "$RUN_USER")
fi
set +e
"$RT" "${RUN_ARGS[@]}" "$IMAGE" -c \
  'test -f /app/scripts/local_primary.py || { echo "lp_container: this memora-all image predates the operator tool (deploy a build with scripts/local_primary.py)" >&2; exit 64; }; exec python /app/scripts/local_primary.py "$@"' \
  lp "$@"
rc=$?
set -e
exit "$rc"
