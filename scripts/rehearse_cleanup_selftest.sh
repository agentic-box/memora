#!/usr/bin/env bash
# Self-test of the teardown rules in scripts/rehearse_objects.sh against a
# real runtime (server2):  scripts/rehearse_cleanup_selftest.sh IMAGE
# Every object it creates has a name unique to this test run and a captured
# ID. Its decoys are put on the run's own object list on purpose and must
# survive the teardown; the test then removes them by their captured IDs
# after checking their memora.rehearsal.decoy-of label. Exit 0 when every
# rule holds.
set -uo pipefail
IMAGE="${1:?usage: rehearse_cleanup_selftest.sh IMAGE}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RT="${RUNTIME:-podman}"; PY="${PYTHON:-python3}"
RUN_ID="rh-st-$(date +%s)-$$"; RUN_START="$(date +%s)"
T="$(mktemp -d)"; OBJECTS="$T/objects"; echo "start $RUN_START $RUN_ID" > "$OBJECTS"
. "$ROOT/scripts/rehearse_objects.sh"
FAILS=0; ok() { echo "PASS  $*"; }; bad() { echo "FAIL  $*"; FAILS=$((FAILS + 1)); }
DECOY="memora.rehearsal.decoy-of=$RUN_ID"
DECOYS="$T/decoys"; : > "$DECOYS"
decoy_c() {  # decoy_c VAR NAME-SUFFIX RUN-ARGS... -- a captured decoy container
  local _id
  _id="$("$RT" run -d --name "$RUN_ID-$2" --label "$DECOY" "${@:3}")" || die "decoy create failed: $2"
  echo "container $_id" >> "$DECOYS"; printf -v "$1" '%s' "$_id"
}
decoy_v() {  # decoy_v VAR NAME LABEL-ARGS... -- a captured decoy volume
  local _v
  _v="$("$RT" volume create --label "$DECOY" "${@:3}" "$2")" || die "decoy create failed: $2"
  echo "volume $_v" >> "$DECOYS"; printf -v "$1" '%s' "$_v"
}
SL=(--stop-timeout 1 --entrypoint sleep "$IMAGE" 600)
AN=(-v /data)   # an ANONYMOUS /data volume with any image (not only one that declares VOLUME /data)
cleanup_decoys() {  # by captured ID, after checking the decoy-of label
  local kind id av
  while read -r kind id; do
    case "$kind" in
      container) [ "$("$RT" inspect "$id" --format '{{index .Config.Labels "memora.rehearsal.decoy-of"}}' 2>/dev/null)" = "$RUN_ID" ] \
                   && "$RT" rm -f "$id" >/dev/null ;;
      volume) [ "$("$RT" volume inspect "$id" --format '{{index .Labels "memora.rehearsal.decoy-of"}}' 2>/dev/null)" = "$RUN_ID" ] \
                   && "$RT" volume rm "$id" >/dev/null ;;
      image) [ "$("$RT" image inspect "$id" --format '{{index .Labels "memora.rehearsal.decoy-of"}}' 2>/dev/null)" = "$RUN_ID" ] \
               && for t in $("$RT" image inspect "$id" --format '{{range .RepoTags}}{{.}} {{end}}'); do "$RT" rmi "$t" >/dev/null; done ;;
      anon) [ "$("$RT" volume inspect "$id" --format '{{.Anonymous}}' 2>/dev/null)" = true ] \
              && [ -z "$("$RT" ps -a -q --filter "volume=$id")" ] && "$RT" volume rm "$id" >/dev/null ;;
    esac
  done < "$DECOYS"
}
trap 'teardown "$OBJECTS" >/dev/null; cleanup_decoys; rm -rf "$T"' EXIT

tiny_image() {  # tiny_image VAR TAG LABEL... -- a labelled one-line image (no layers of its own)
  local _i
  _i="$(printf 'FROM %s\n' "$IMAGE" | "$RT" build -q "${@:3}" -t "$2" -f - . | tail -1)" || die "build failed: $2"
  printf -v "$1" '%s' "$_i"
}

# this run's own objects: removed
new_container C1 --name "$RUN_ID-c1" --tmpfs /data "${SL[@]}"
new_volume V1 "$RUN_ID-v1"
new_container C2 --name "$RUN_ID-c2" "${AN[@]}" "${SL[@]}"; anon_of A2 "$C2"
tiny_image I1 "$RUN_ID-img1:t" --label "$RUN_LABEL"; track image "$I1"
tiny_image DI_OTHER "$RUN_ID-imgother:t" --label memora.rehearsal=other-run --label "$DECOY"
echo "image $DI_OTHER" >> "$DECOYS"; track image "$DI_OTHER"
# decoys, all deliberately on this run's object list: kept
decoy_c D_OTHER other --label memora.rehearsal=other-run --tmpfs /data "${SL[@]}"; track container "$D_OTHER"
decoy_c D_NOLBL nolabel --tmpfs /data "${SL[@]}"; track container "$D_NOLBL"
decoy_v DV_OTHER "$RUN_ID-vother" --label memora.rehearsal=other-run; track volume "$DV_OTHER"
decoy_v DV_NOLBL "$RUN_ID-vnolabel"; track volume "$DV_NOLBL"
HX="$(printf '%s' "$RUN_ID" | sha256sum | cut -c1-64)"            # a NAMED volume with a 64-hex name
decoy_v DV_HEX "$HX"; track anon "$DV_HEX"
decoy_c D_USER user "${AN[@]}" "${SL[@]}"                           # an anonymous volume still in use
AV_USED="$("$RT" inspect "$D_USER" --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}')"
track anon "$AV_USED"; echo "anon $AV_USED" >> "$DECOYS"

teardown "$OBJECTS"

c() { "$RT" container exists "$1"; }; v() { "$RT" volume exists "$1"; }
! c "$C1" && ok "this run's container removed" || bad "this run's container kept"
! v "$V1" && ok "this run's volume removed" || bad "this run's volume kept"
! c "$C2" && ! v "$A2" && ok "this run's anonymous volume removed with its container" || bad "anonymous volume kept"
c "$D_OTHER" && ok "a container labelled by another run kept" || bad "another run's container removed"
c "$D_NOLBL" && ok "an unlabelled container kept" || bad "an unlabelled container removed"
v "$DV_OTHER" && ok "a volume labelled by another run kept" || bad "another run's volume removed"
v "$DV_NOLBL" && ok "an unlabelled volume kept" || bad "an unlabelled volume removed"
v "$DV_HEX" && ok "a named volume with a 64-hex name kept (not flagged anonymous)" || bad "a named 64-hex volume removed"
v "$AV_USED" && ok "an anonymous volume in use by another container kept" || bad "an in-use anonymous volume removed"
! "$RT" image exists "$I1" && ok "this run's image removed (all its tags)" || bad "this run's image kept"
"$RT" image exists "$DI_OTHER" && ok "an image labelled by another run kept" || bad "another run's image removed"
# adopt refuses an object under a per-run name that another run labelled
decoy_c D_ADOPT adoptme --label memora.rehearsal=other-run --tmpfs /data "${SL[@]}"
( adopt X container "$RUN_ID-adoptme" ) 2>/dev/null && bad "adopt took another run's container" \
  || ok "adopt refuses a container that does not carry this run's label"
grep -q "container $D_ADOPT" "$OBJECTS" && bad "adopt recorded it anyway" || ok "the refused container was not recorded"
# a name that is not unique to this run is refused before anything is created
( new_volume X "memora-rh-scratch8" ) 2>/dev/null && bad "new_volume accepted a fixed name" \
  || ok "new_volume refuses a name without the run id"
grep -q "memora-rh-scratch8" "$OBJECTS" && bad "a fixed-name volume was recorded" || ok "nothing under a fixed name was recorded"
( new_image X "memora-rh:latest" . ) 2>/dev/null && bad "new_image accepted a fixed tag" || ok "new_image refuses a tag without the run id"

# an anonymous volume created BEFORE the run (window): kept
decoy_c D_WIN win "${AN[@]}" "${SL[@]}"
AV_WIN="$("$RT" inspect "$D_WIN" --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}')"
[ "$("$RT" inspect "$D_WIN" --format '{{index .Config.Labels "memora.rehearsal.decoy-of"}}')" = "$RUN_ID" ] \
  && "$RT" rm -f "$D_WIN" >/dev/null   # a decoy: by its captured ID, label checked
echo "anon $AV_WIN" > "$T/objects2"; echo "anon $AV_WIN" >> "$DECOYS"
RUN_START=$(( $(date +%s) + 3600 )) teardown "$T/objects2" >/dev/null
v "$AV_WIN" && ok "an anonymous volume created outside the run's window kept" || bad "window not enforced"

echo "selftest: $FAILS failed"
exit $((FAILS > 0))
