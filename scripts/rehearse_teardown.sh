#!/usr/bin/env bash
# Tear down a rehearsal run kept with RH_KEEP=1:
#   scripts/rehearse_teardown.sh RH_ROOT/run-<run id>.objects
# Removes only the objects that run captured, by ID, after re-checking that
# each still carries memora.rehearsal=<that run id> (scripts/rehearse_objects.sh).
set -uo pipefail
OBJECTS="${1:?usage: rehearse_teardown.sh RUN_OBJECTS_FILE}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RT="${RUNTIME:-podman}"; PY="${PYTHON:-python3}"
read -r tag RUN_START RUN_ID < "$OBJECTS" || { echo "unreadable $OBJECTS" >&2; exit 2; }
[ "$tag" = start ] && [ -n "$RUN_ID" ] || { echo "$OBJECTS is not a run object list" >&2; exit 2; }
. "$ROOT/scripts/rehearse_objects.sh"
teardown "$OBJECTS"
