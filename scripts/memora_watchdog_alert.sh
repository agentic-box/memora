#!/usr/bin/env bash
# Alert hook for the memora watchdog. Invoked as: <this> <container> <message>
#
# The watchdog fires this on a FAILED restart, on a start that never became
# healthy, and on a SUCCESSFUL restart -- six workspaces share one process, so
# a restart nobody is told about is the silent-outage failure this phase exists
# to remove.
#
# Must exit 0 on delivery. A non-zero exit is logged by the watchdog as an
# undelivered alert; it never kills the watchdog.
set -uo pipefail

container="${1:-unknown}"
message="${2:-}"
stamp="$(date '+%Y-%m-%d %H:%M:%S')"
line="[$stamp] memora watchdog: $container -- $message"

# 1. Durable record, independent of launchd's log rotation.
log="${MEMORA_WATCHDOG_ALERT_LOG:-$HOME/.local/var/log/memora-watchdog-alerts.log}"
mkdir -p "$(dirname "$log")" 2>/dev/null || true
printf '%s\n' "$line" >> "$log" || exit 1

# 2. Desktop notification, best effort -- its absence must not fail delivery,
#    because the durable record above is what actually matters.
if command -v osascript >/dev/null 2>&1; then
  # Text via argv, NOT interpolated into the script source: a quote in a CLI
  # error message would otherwise break the notification.
  osascript - "$message" "memora: $container" >/dev/null 2>&1 <<'OSA' || true
on run argv
  display notification (item 1 of argv) with title (item 2 of argv)
end run
OSA
fi

exit 0
