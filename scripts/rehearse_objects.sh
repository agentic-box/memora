# Sourced by scripts/rehearse_deploy.sh, rehearse_cleanup_selftest.sh and
# rehearse_survival_check.sh (R1, review 7747): the ONLY way these scripts
# create or remove runtime objects.
#
#   * every object this run creates has a per-run unique name (it contains
#     $RUN_ID) and the label memora.rehearsal=$RUN_ID;
#   * its ID is captured only from a successful create (`id=$(… ) || die`)
#     and recorded;
#   * teardown removes only recorded objects, by ID, after checking that the
#     label still equals $RUN_ID. Anonymous /data volumes (the image creates
#     them, so they cannot be labelled) are removed only when the runtime
#     flags them anonymous, no container uses them, and they were created
#     during this run. An image is never removed by ID: only the tags this
#     run created and recorded are untagged, each while it still resolves to
#     the captured labelled image; other tags (and so the image) stay.
#   Nothing is ever removed by name alone, and nothing is ever pruned.
#
# Needs: RT (runtime), PY (python3), RUN_ID, RUN_START (epoch), OBJECTS (the
# run's object list file).

die() { echo "rehearsal: $*" >&2; exit 2; }
RUN_LABEL="memora.rehearsal=$RUN_ID"

track() { echo "$*" >> "$OBJECTS"; }   # track KIND ID [TAG]  (container|volume|anon|image|tag)

new_container() {  # new_container VAR RUN-ARGS... -- `run -d`, labelled; VAR := its ID
  local _id
  _id="$("$RT" run -d --label "$RUN_LABEL" "${@:2}")" || die "create failed: run -d ${*:2}"
  [ -n "$_id" ] || die "create returned no ID: run -d ${*:2}"
  track container "$_id"
  printf -v "$1" '%s' "$_id"
}

new_volume() {  # new_volume VAR NAME -- labelled; VAR := its name (a volume's ID)
  local _v
  case "$2" in *"$RUN_ID"*) ;; *) die "volume name $2 is not unique to run $RUN_ID" ;; esac
  _v="$("$RT" volume create --label "$RUN_LABEL" "$2")" || die "create failed: volume $2"
  track volume "$_v"
  printf -v "$1" '%s' "$_v"
}

new_image() {  # new_image VAR TAG CONTEXT -- labelled build; VAR := its image ID
  local _i
  case "$2" in *"$RUN_ID"*) ;; *) die "image tag $2 is not unique to run $RUN_ID" ;; esac
  _i="$("$RT" build -q --label "$RUN_LABEL" -t "$2" "$3" | tail -1)" || die "build failed: $2"
  [ -n "$_i" ] || die "build returned no ID: $2"
  track image "$_i"; track tag "$_i" "$2"
  printf -v "$1" '%s' "$_i"
}

label_of() {  # label_of KIND ID -- the memora.rehearsal label ("" when none or absent)
  case "$1" in
    container) "$RT" inspect "$2" --format '{{index .Config.Labels "memora.rehearsal"}}' 2>/dev/null ;;
    volume) "$RT" volume inspect "$2" --format '{{index .Labels "memora.rehearsal"}}' 2>/dev/null ;;
    image) "$RT" image inspect "$2" --format '{{index .Labels "memora.rehearsal"}}' 2>/dev/null ;;
  esac
}

adopt() {  # adopt VAR KIND NAME -- an object the DEPLOY created under a per-run name: VAR := its ID
  local _id
  case "$3" in *"$RUN_ID"*) ;; *) die "$2 name $3 is not unique to run $RUN_ID" ;; esac
  case "$2" in
    container) _id="$("$RT" inspect "$3" --format '{{.Id}}')" || die "no container $3" ;;
    volume) _id="$("$RT" volume inspect "$3" --format '{{.Name}}')" || die "no volume $3" ;;
    image) _id="$("$RT" image inspect "$3" --format '{{.Id}}')" || die "no image $3" ;;
  esac
  [ "$(label_of "$2" "$_id")" = "$RUN_ID" ] || die "$2 $3 does not carry this run's label"
  grep -qx "$2 $_id" "$OBJECTS" 2>/dev/null || track "$2" "$_id"
  [ "$2" = image ] && { grep -qx "tag $_id $3" "$OBJECTS" 2>/dev/null || track tag "$_id" "$3"; }
  printf -v "$1" '%s' "$_id"
}

adopt_run_tags() {  # the deploy's per-run tags on this run's images (memora-<RUN_ID>:latest|rollback-N)
  local k id t
  while read -r k id _; do
    [ "$k" = image ] || continue
    for t in $("$RT" image inspect "$id" --format '{{range .RepoTags}}{{.}} {{end}}' 2>/dev/null); do
      [[ "$t" =~ ^(localhost/)?memora-${RUN_ID}:(latest|rollback-[0-9]+)$ ]] || continue
      grep -qx "tag $id $t" "$OBJECTS" || track tag "$id" "$t"
    done
  done < <(sort -u "$OBJECTS")
}

anon_of() {  # anon_of VAR CONTAINER_ID -- the anonymous /data volume of OUR container
  local _v
  _v="$("$RT" inspect "$2" --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}')" \
    || die "cannot inspect $2"
  printf '%s' "$_v" | grep -Eqx '[0-9a-f]{64}' || die "container $2 has no anonymous /data volume ('$_v')"
  grep -qx "anon $_v" "$OBJECTS" 2>/dev/null || track anon "$_v"
  printf -v "$1" '%s' "$_v"
}

once() {  # once ARGS... -- a helper container: create (labelled, captured), run attached, remove by ID
  local _id rc
  _id="$("$RT" create --label "$RUN_LABEL" --tmpfs /data "$@")" || die "create failed: helper $*"
  track container "$_id"
  "$RT" start -a "$_id"; rc=$?
  remove_one container "$_id" >/dev/null
  return "$rc"
}

created_in_run() {  # created_in_run VOLUME -- CreatedAt within [RUN_START - 60 s, now]
  "$RT" volume inspect "$1" | "$PY" -c '
import json, re, sys, time
from datetime import datetime
raw = json.load(sys.stdin)[0]["CreatedAt"]
m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)$", raw)
t = datetime.fromisoformat(m.group(1) + m.group(3).replace("Z", "+00:00")).timestamp()
sys.exit(0 if float(sys.argv[1]) - 60 <= t <= time.time() + 60 else 1)
' "$RUN_START"
}

remove_tag() {  # remove_tag IMAGE_ID TAG -- untag, only while TAG still resolves to this run's labelled image
  local id="$1" t="$2" cur
  cur="$("$RT" image inspect "$t" --format '{{.Id}}' 2>/dev/null)" || return 0
  [ "$cur" = "$id" ] || { echo "keep tag $t: it now names another image"; return 0; }
  [ "$(label_of image "$id")" = "$RUN_ID" ] || { echo "keep tag $t: image $id is not labelled $RUN_ID"; return 0; }
  # `rmi TAG` only untags while other tags remain; the image goes with its last tag
  "$RT" rmi "$t" >/dev/null 2>&1 && echo "removed tag $t" || echo "keep tag $t: still in use"
}

remove_one() {  # remove_one KIND ID -- only when it is still this run's; by ID
  local kind="$1" id="$2" t
  case "$kind" in
    container)
      "$RT" container exists "$id" 2>/dev/null || return 0
      [ "$(label_of container "$id")" = "$RUN_ID" ] || { echo "keep container $id: not labelled $RUN_ID"; return 0; }
      "$RT" rm -f "$id" >/dev/null && echo "removed container $id" ;;
    volume)
      "$RT" volume exists "$id" 2>/dev/null || return 0
      [ "$(label_of volume "$id")" = "$RUN_ID" ] || { echo "keep volume $id: not labelled $RUN_ID"; return 0; }
      "$RT" volume rm "$id" >/dev/null && echo "removed volume $id" ;;
    anon)
      "$RT" volume exists "$id" 2>/dev/null || return 0
      printf '%s' "$id" | grep -Eqx '[0-9a-f]{64}' || { echo "keep $id: not an anonymous id"; return 0; }
      [ "$("$RT" volume inspect "$id" --format '{{.Anonymous}}')" = true ] || { echo "keep $id: not flagged anonymous"; return 0; }
      [ -z "$("$RT" ps -a -q --filter "volume=$id")" ] || { echo "keep $id: still used by a container"; return 0; }
      created_in_run "$id" || { echo "keep $id: not created during run $RUN_ID"; return 0; }
      "$RT" volume rm "$id" >/dev/null && echo "removed anonymous volume $id" ;;
    image) return 0 ;;   # an image is never removed by ID: only its recorded tags (below)
  esac
}

teardown() {  # teardown [LIST] -- containers first, then volumes, anonymous volumes, images
  local list="${1:-$OBJECTS}" kind id
  [ -s "$list" ] || return 0
  for kind in container volume anon; do
    while read -r k id _; do
      [ "$k" = "$kind" ] && remove_one "$kind" "$id"
    done < "$list"
  done
  while read -r k id t; do   # images: only the recorded tags, never an image by ID
    [ "$k" = tag ] && remove_tag "$id" "$t"
  done < "$list"
}
