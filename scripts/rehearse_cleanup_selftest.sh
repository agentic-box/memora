#!/usr/bin/env bash
# Self-test of the teardown rules in scripts/rehearse_objects.sh against a
# real runtime (build-host):  scripts/rehearse_cleanup_selftest.sh IMAGE
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
      exttag) set -- $id; [ "$("$RT" image inspect "$2" --format '{{.Id}}' 2>/dev/null)" = "$1" ] \
                && [ "$(label_of image "$1")" = "$RUN_ID" ] && "$RT" rmi "$2" >/dev/null ;;
      dtag) set -- $id; [ "$("$RT" image inspect "$2" --format '{{.Id}}' 2>/dev/null)" = "$1" ] \
              && [ "$("$RT" image inspect "$1" --format '{{index .Labels "memora.rehearsal.decoy-of"}}' 2>/dev/null)" = "$RUN_ID" ] \
              && "$RT" rmi "$2" >/dev/null ;;   # only the one tag this test created
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
tiny_image I1 "$RUN_ID-img1:t" --label "$RUN_LABEL"; track image "$I1"; track tag "$I1" "$RUN_ID-img1:t"
EXT_TAG="ext-$RUN_ID-kept:x"                       # a tag this run did NOT record, on this run's image
"$RT" tag "$I1" "$EXT_TAG" || die "tag failed: $EXT_TAG"
echo "exttag $I1 $EXT_TAG" >> "$DECOYS"
tiny_image I2 "$RUN_ID-img2:t" --label "$RUN_LABEL" --label memora.selftest.n=2; track image "$I2"
[ "$I2" != "$I1" ] || die "the two test images must differ"
"$RT" tag "$I1" "$RUN_ID-img1b:t" || die "tag failed"   # names I1 but is recorded under I2: it must stay
# recorded BEFORE I2's own tag, so I2 (labelled, still present) is what it is checked against
track tag "$I2" "$RUN_ID-img1b:t"; echo "exttag $I1 $RUN_ID-img1b:t" >> "$DECOYS"
track tag "$I2" "$RUN_ID-img2:t"
tiny_image DI_OTHER "$RUN_ID-imgother:t" --label memora.rehearsal=other-run --label "$DECOY"
echo "dtag $DI_OTHER $RUN_ID-imgother:t" >> "$DECOYS"; track image "$DI_OTHER"; track tag "$DI_OTHER" "$RUN_ID-imgother:t"
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
"$RT" image exists "$I1" && ok "this run's image kept while an unrecorded tag remains" || bad "the image was removed despite an external tag"
[ "$("$RT" image inspect "$EXT_TAG" --format '{{.Id}}' 2>/dev/null)" = "$I1" ] && ok "the external tag survived" || bad "the external tag was removed"
! "$RT" image exists "$RUN_ID-img1:t" 2>/dev/null && ok "the recorded tag was removed" || bad "the recorded tag stayed"
[ "$("$RT" image inspect "$RUN_ID-img1b:t" --format '{{.Id}}' 2>/dev/null)" = "$I1" ] \
  && ok "a recorded tag that names another image than the recorded one is kept" || bad "a mismatched tag was removed"
! "$RT" image exists "$I2" && ok "an image whose only tag was recorded is gone with that tag" || bad "image 2 kept"
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
