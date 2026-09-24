#!/usr/bin/env bash
# Review 7747: objects carrying the rehearsal's OLD fixed names must survive
# a full rehearsal and the teardown self-test. The two fixed names below are
# deliberate (they are the survivors); they carry this check's own
# memora.survival label, and it removes them by their captured IDs.
#   scripts/rehearse_survival_check.sh OLD_SRC IMAGE_FOR_SELFTEST
set -uo pipefail
OLD_SRC="${1:?usage: rehearse_survival_check.sh OLD_SRC IMAGE}"; IMG="${2:?}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RT="${RUNTIME:-podman}"
SID="surv-$(date +%s)-$$"
C="$("$RT" run -d --name rh-helper-backup --label "memora.survival=$SID" --entrypoint sleep "$IMG" 3600)" \
  || { echo "cannot create rh-helper-backup (does one already exist?)" >&2; exit 2; }
V="$("$RT" volume create --label "memora.survival=$SID" memora-rh-scratch8)" \
  || { [ "$("$RT" inspect "$C" --format '{{index .Config.Labels "memora.survival"}}')" = "$SID" ] && "$RT" rm -f "$C" >/dev/null
       echo "cannot create memora-rh-scratch8" >&2; exit 2; }
bash "$ROOT/scripts/rehearse_deploy.sh" "$OLD_SRC" > "${RH_ROOT:-$HOME/rehearsal-r1}/survival-rehearsal.out" 2>&1; r1=$?
bash "$ROOT/scripts/rehearse_cleanup_selftest.sh" "$IMG" > "${RH_ROOT:-$HOME/rehearsal-r1}/survival-selftest.out" 2>&1; r2=$?
rc=0
"$RT" container exists "$C" && echo "PASS  rh-helper-backup survived (rehearsal exit $r1, self-test exit $r2)" || { echo "FAIL  rh-helper-backup is gone"; rc=1; }
"$RT" volume exists "$V" && echo "PASS  memora-rh-scratch8 survived" || { echo "FAIL  memora-rh-scratch8 is gone"; rc=1; }
[ "$r1" = 0 ] && [ "$r2" = 0 ] || rc=1
# remove what THIS check created: by captured ID, after checking its label
[ "$("$RT" inspect "$C" --format '{{index .Config.Labels "memora.survival"}}' 2>/dev/null)" = "$SID" ] && "$RT" rm -f "$C" >/dev/null
[ "$("$RT" volume inspect "$V" --format '{{index .Labels "memora.survival"}}' 2>/dev/null)" = "$SID" ] && "$RT" volume rm "$V" >/dev/null
exit $rc
