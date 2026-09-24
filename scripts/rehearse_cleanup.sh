# Sourced by scripts/rehearse_deploy.sh and scripts/rehearse_cleanup_selftest.sh.
# Needs RT (the runtime) and PY (python3). Review 7725 / 7729: remove only
# what carries a run id recorded in the given ledger AND has an expected
# rehearsal name; never by name alone, never prune.
# Ledger lines: "run ID START", "end ID END", "anon ID CONTAINER_ID VOLUME".
CTR_RE='^(memora-rh(-grok-[0-9]+|-migrate-[0-9]+)?|rh-helper-[a-z0-9-]+)$'
VOL_RE='^memora-rh-(data|scratch[0-9]*)$'
cleanup_ledger() {  # cleanup_ledger LEDGER_FILE
  local ledger="$1" runs line id name lbl kind rid cid v
  [ -s "$ledger" ] || return 0
  runs=" $(awk '$1 == "run" {printf "%s ", $2}' "$ledger")"
  # containers: its label is a recorded run id AND its name fits
  while IFS='|' read -r id name lbl; do
    [ -n "$lbl" ] && [[ "$runs" == *" $lbl "* ]] && [[ "$name" =~ $CTR_RE ]] || continue
    "$RT" rm -f "$id" >/dev/null && echo "cleanup: removed container $name (run $lbl)"
  done < <("$RT" ps -a --format '{{.ID}}|{{.Names}}|{{index .Labels "memora.rehearsal"}}')
  # labelled volumes: the same rule
  while IFS='|' read -r name lbl; do
    [ -n "$lbl" ] && [[ "$runs" == *" $lbl "* ]] && [[ "$name" =~ $VOL_RE ]] || continue
    "$RT" volume rm "$name" >/dev/null && echo "cleanup: removed volume $name (run $lbl)"
  done < <("$RT" volume ls --format '{{.Name}}|{{index .Labels "memora.rehearsal"}}')
  # anonymous volumes (the image's VOLUME /data: they cannot be labelled at
  # creation): recorded with their run and container, 64-hex, flagged
  # anonymous, used by no other container, and CREATED within that run.
  while read -r kind rid cid v; do
    [ "$kind" = anon ] && [[ "$runs" == *" $rid "* ]] || continue
    "$RT" volume exists "$v" 2>/dev/null || continue
    printf '%s' "$v" | grep -Eqx '[0-9a-f]{64}' || { echo "cleanup: keeping $v (not an anonymous id)"; continue; }
    [ "$("$RT" volume inspect "$v" --format '{{.Anonymous}}' 2>/dev/null)" = true ] \
      || { echo "cleanup: keeping $v (not flagged anonymous)"; continue; }
    local users; users="$("$RT" ps -a -q --no-trunc --filter "volume=$v")"
    [ -z "$users" ] || [ "$users" = "$cid" ] || { echo "cleanup: keeping $v (used by $users)"; continue; }
    if created_in_run "$ledger" "$rid" "$v"; then
      [ -n "$users" ] && "$RT" rm -f "$cid" >/dev/null
      "$RT" volume rm "$v" >/dev/null && echo "cleanup: removed anonymous volume $v (run $rid)"
    else
      echo "cleanup: keeping $v (not created during run $rid)"
    fi
  done < "$ledger"
}
created_in_run() {  # created_in_run LEDGER RUN_ID VOLUME -- CreatedAt within [start, end or now] (+-60 s)
  "$RT" volume inspect "$3" | "$PY" -c '
import json, re, sys, time
from datetime import datetime
ledger, rid = sys.argv[1], sys.argv[2]
start, end = None, time.time()
for line in open(ledger):
    f = line.split()
    if len(f) >= 3 and f[1] == rid:
        if f[0] == "run": start = float(f[2])
        if f[0] == "end": end = float(f[2])
raw = json.load(sys.stdin)[0]["CreatedAt"]
m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)$", raw)
created = datetime.fromisoformat(m.group(1) + (m.group(3).replace("Z", "+00:00"))).timestamp()
sys.exit(0 if start is not None and start - 60 <= created <= end + 60 else 1)
' "$1" "$2"
}
