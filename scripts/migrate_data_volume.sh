#!/bin/sh
# Copy an old /data volume into the named /data volume, staged and verified
# (docs/local-primary-implementation.md §8 L2a). Run INSIDE a throwaway
# container of the memora image, with the old volume at /from (read-only)
# and the named volume at /to, while every container using either is
# STOPPED:
#
#   <runtime> run --rm -v OLD:/from:ro -v NEW:/to IMAGE \
#     sh -c "$(cat scripts/migrate_data_volume.sh)" migrate_data_volume migrate OLD_ID
#
# (sh -c takes the NEXT word as $0, hence the extra "migrate_data_volume".)
# MIGRATE_FROM / MIGRATE_TO override /from and /to, for tests only.
#
# Callers: scripts/memora-instance.sh (cmd_up) and scripts/deploy-memora-all.sh,
# which pass this file's text, so both use one implementation.
#
# The marker /to/.memora-volume-source records WHICH volume was copied and a
# digest of its content (sha256 over "sha256  path" for every file). The copy
# is skipped only when the marker names the same source AND the source's
# digest is unchanged. Otherwise (first migration, a different source, or a
# source that changed since, e.g. after a rollback that wrote to it):
#   1. clear /to/.memora-staging and copy /from into it (cp -a);
#   2. verify: the staging digest must equal the source digest;
#   3. move the live entries of /to aside into /to/.memora-previous-<ts>
#      (kept, never deleted here) and move the staged entries into place;
#   4. write the marker (temp file + rename) and sync.
# A failure before step 3 leaves /to's live content untouched; the staging
# area is cleared on the next run. Nothing is ever copied over live files.
#
# Output (last line): "skip <digest>" or "copied <digest>". Exit 1 on any
# failure.
set -eu

FROM="${MIGRATE_FROM:-/from}"
TO="${MIGRATE_TO:-/to}"
MARKER=.memora-volume-source
STAGING=.memora-staging

manifest() {  # manifest DIR -- "sha256  ./path" per file, control entries excluded
  (cd "$1" && find . \( -path "./$STAGING" -o -path './.memora-previous-*' \) -prune \
      -o -type f ! -path "./$MARKER" ! -path "./$MARKER.tmp" -print \
    | LC_ALL=C sort | while IFS= read -r f; do sha256sum "$f"; done)
}

digest() { manifest "$1" | sha256sum | cut -d' ' -f1; }

cmd="${1:-}"
case "$cmd" in
  digest)
    digest "$FROM" ;;
  migrate)
    src="${2:?source volume id required}"
    [ -d "$FROM" ] && [ -d "$TO" ] || { echo "need $FROM and $TO mounted" >&2; exit 1; }
    src_digest="$(digest "$FROM")"
    if [ -f "$TO/$MARKER" ] \
       && [ "$(sed -n 's/^source=//p' "$TO/$MARKER")" = "$src" ] \
       && [ "$(sed -n 's/^digest=//p' "$TO/$MARKER")" = "$src_digest" ]; then
      echo "skip $src_digest"
      exit 0
    fi
    rm -rf "$TO/$STAGING"
    mkdir "$TO/$STAGING"
    cp -a "$FROM"/. "$TO/$STAGING/"
    sync
    staged_digest="$(digest "$TO/$STAGING")"
    if [ "$staged_digest" != "$src_digest" ]; then
      echo "verify failed: staged $staged_digest != source $src_digest" >&2
      exit 1
    fi
    prev="$TO/.memora-previous-$(date +%s)-$$"
    mkdir "$prev"
    for e in "$TO"/* "$TO"/.[!.]* "$TO"/..?*; do
      [ -e "$e" ] || [ -L "$e" ] || continue
      case "${e##*/}" in "$STAGING"|.memora-previous-*) continue ;; esac
      mv "$e" "$prev/"
    done
    for e in "$TO/$STAGING"/* "$TO/$STAGING"/.[!.]* "$TO/$STAGING"/..?*; do
      [ -e "$e" ] || [ -L "$e" ] || continue
      mv "$e" "$TO/"
    done
    rmdir "$TO/$STAGING"
    rmdir "$prev" 2>/dev/null || true   # nothing was live: keep no empty dir
    printf 'source=%s\ndigest=%s\ncopied_at=%s\n' "$src" "$src_digest" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      > "$TO/$MARKER.tmp"
    mv "$TO/$MARKER.tmp" "$TO/$MARKER"
    sync
    echo "copied $src_digest" ;;
  *)
    echo "usage: migrate_data_volume.sh digest | migrate SOURCE_ID" >&2
    exit 2 ;;
esac
