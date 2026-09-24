#!/usr/bin/env bash
# Self-test of scripts/rehearse_cleanup.sh against a real runtime (server2):
#   scripts/rehearse_cleanup_selftest.sh IMAGE
# Creates only its own labelled/decoy objects and removes them at the end.
# Exit 0 when every rule holds.
set -uo pipefail
IMAGE="${1:?usage: rehearse_cleanup_selftest.sh IMAGE}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RT="${RUNTIME:-podman}"; PY="${PYTHON:-python3}"
. "$ROOT/scripts/rehearse_cleanup.sh"
T="$(mktemp -d)"; FAILS=0
ok() { echo "PASS  $*"; }; bad() { echo "FAIL  $*"; FAILS=$((FAILS + 1)); }
S="rh-selftest-$$"; OTHER="rh-other-$$"
echo "run $S $(date +%s)" > "$T/ledger"
sl() { "$RT" run -d --name "$1" "${@:2}" --entrypoint sleep "$IMAGE" 600 >/dev/null; }
sl rh-helper-pos-$$ --label "memora.rehearsal=$S" --tmpfs /data            # removed
"$RT" volume create --label "memora.rehearsal=$S" memora-rh-scratch91 >/dev/null   # removed
sl rh-helper-anonpos-$$ --label "memora.rehearsal=$S"                     # removed, with its anonymous volume
AP="$("$RT" inspect rh-helper-anonpos-$$ --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}')"
echo "anon $S $("$RT" inspect rh-helper-anonpos-$$ --format '{{.Id}}') $AP" >> "$T/ledger"
sl rh-helper-backup-$$ --tmpfs /data                                        # kept: no label
"$RT" volume create --label "memora.rehearsal=$OTHER" memora-rh-scratch92 >/dev/null   # kept: another run
sl not-rehearsal-$$ --label "memora.rehearsal=$S" --tmpfs /data           # kept: our label, unexpected name
"$RT" volume create --label "memora.rehearsal=$S" rh-not-expected-$$ >/dev/null        # kept: our label, unexpected name
sl rh-helper-anonkeep-$$                                                    # its anonymous volume: kept (other user)
AK="$("$RT" inspect rh-helper-anonkeep-$$ --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}')"
echo "anon $S not-this-container $AK" >> "$T/ledger"
HX="$(printf '%s' "rh-selftest-$$" | sha256sum | cut -c1-64)"             # kept: a NAMED volume with a 64-hex name
"$RT" volume create "$HX" >/dev/null
echo "anon $S - $HX" >> "$T/ledger"
S2="rh-selftest-future-$$"; echo "run $S2 $(( $(date +%s) + 3600 ))" >> "$T/ledger"
sl rh-helper-anonwin-$$ --label "memora.rehearsal=$S2"                     # container removed; volume kept (window)
AW="$("$RT" inspect rh-helper-anonwin-$$ --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}')"
echo "anon $S2 $("$RT" inspect rh-helper-anonwin-$$ --format '{{.Id}}') $AW" >> "$T/ledger"

cleanup_ledger "$T/ledger"

c() { "$RT" container exists "$1"; }; v() { "$RT" volume exists "$1"; }
! c rh-helper-pos-$$ && ok "labelled container with an expected name removed" || bad "labelled container kept"
! v memora-rh-scratch91 && ok "labelled volume with an expected name removed" || bad "labelled volume kept"
! c rh-helper-anonpos-$$ && ! v "$AP" && ok "recorded anonymous volume removed with its container" || bad "anonymous volume kept"
c rh-helper-backup-$$ && ok "unlabelled container kept" || bad "unlabelled container removed"
v memora-rh-scratch92 && ok "another run's volume kept" || bad "another run's volume removed"
c not-rehearsal-$$ && ok "labelled container with an unexpected name kept" || bad "unexpected name removed"
v rh-not-expected-$$ && ok "labelled volume with an unexpected name kept" || bad "unexpected volume removed"
v "$AK" && ok "anonymous volume used by another container kept" || bad "in-use anonymous volume removed"
v "$HX" && ok "a named volume with a 64-hex name is kept (not flagged anonymous)" || bad "a named 64-hex volume removed"
! c rh-helper-anonwin-$$ && v "$AW" && ok "anonymous volume created outside its run's window kept" || bad "window not enforced"

"$RT" rm -f rh-helper-backup-$$ not-rehearsal-$$ rh-helper-anonkeep-$$ >/dev/null 2>&1   # teardown: this test's own
"$RT" volume rm memora-rh-scratch92 rh-not-expected-$$ "$AK" "$AW" "$HX" >/dev/null 2>&1
rm -rf "$T"
echo "selftest: $FAILS failed"
exit $((FAILS > 0))
