#!/usr/bin/env bash
# DEPRECATED shim. The two pilot instances are now ordinary entries in
# instances/ (test-sqlite, test) and are driven by memora-instance.sh, which
# works for any number of stores instead of the two that were hardcoded here.
#
#   ./pilot-containers.sh up sqlite   ->  ./memora-instance.sh up test-sqlite
#   ./pilot-containers.sh up d1       ->  ./memora-instance.sh up test
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
map() { case "$1" in sqlite) echo test-sqlite ;; d1) echo test ;; *) echo "$1" ;; esac; }
echo "note: pilot-containers.sh is deprecated -- use memora-instance.sh" >&2
case "${1:-}" in
  build)  exec "$HERE/memora-instance.sh" build ;;
  status) exec "$HERE/memora-instance.sh" status ;;
  up)     case "${2:-both}" in
            both) "$HERE/memora-instance.sh" up test-sqlite; exec "$HERE/memora-instance.sh" up test ;;
            *)    exec "$HERE/memora-instance.sh" up "$(map "$2")" ;;
          esac ;;
  down)   "$HERE/memora-instance.sh" down test-sqlite; exec "$HERE/memora-instance.sh" down test ;;
  logs)   exec "$HERE/memora-instance.sh" logs "$(map "${2:-}")" ;;
  *) echo "usage: $0 {build|up [sqlite|d1|both]|down|logs <sqlite|d1>|status}" >&2; exit 1 ;;
esac
