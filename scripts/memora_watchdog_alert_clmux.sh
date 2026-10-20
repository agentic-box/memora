#!/usr/bin/env bash
# Alert hook for the memora watchdog on the Linux deploy host (Item 108).
# Invoked as: <this> <container> <message>
#
# Fires on every restart attempt (success or failure) and when the watchdog GIVES UP. It
#   1. appends the line to a durable log (always), then
#   2. sends ONE clmux message to MEMORA_WATCHDOG_ALERT_TO (the leader). There is NO default recipient: a
#      name that identifies the operator's fleet does not belong in this public file. The installer's unit reads
#      it from ~/.config/memora/watchdog.env (one line: MEMORA_WATCHDOG_ALERT_TO=<node>/<agent>); without it the
#      alert is logged and this script exits 1.
# The text is the container name and the watchdog's own message; any long token-like run of
# characters is redacted, so a credential can never travel in an alert.
#
# Exit 0 only when the clmux message was delivered. When clmux is unreachable it exits 1 after
# logging: the watchdog records the alert as undelivered and carries on, it never dies of it.
set -uo pipefail

container="${1:-unknown}"
message="${2:-}"
host="$(hostname -s 2>/dev/null || echo host)"
message="$(printf '%s' "$message" | sed -E 's/[A-Za-z0-9_+\/=-]{32,}/[redacted]/g')"
# UNVERIFIED: clmux has no identity path for a non-interactive service yet (an unmarked CLI send is the
# unauthenticated "operator" path; a claimed sender arrives as "unknown sender"), so anyone on this host can
# forge the same text. The marker says so until clmux can attribute the sender.
text="[UNVERIFIED sender, no clmux service identity yet] memora watchdog on $host: $container -- $message"
stamp="$(date '+%Y-%m-%d %H:%M:%S')"

log="${MEMORA_WATCHDOG_ALERT_LOG:-$HOME/.local/var/log/memora-watchdog-alerts.log}"
mkdir -p "$(dirname "$log")" 2>/dev/null || true
printf '[%s] %s\n' "$stamp" "$text" >> "$log" || exit 1

# A systemd service has no clmux pane, and clmux silently drops a send with an EMPTY sender (found in the
# deploy-host rehearsal: exit 0, nothing delivered). A claimed sender name is delivered, marked "unknown sender"
# by clmux. The claim is one STABLE name; the message text itself names the host and the container.
export CLMUX_AGENT_NAME="${CLMUX_AGENT_NAME:-memora-watchdog}"
to="${MEMORA_WATCHDOG_ALERT_TO:-}"
if [ -z "$to" ]; then
  printf '[%s] no alert recipient (MEMORA_WATCHDOG_ALERT_TO is empty); alert logged only\n' "$stamp" >> "$log"
  exit 1
fi
clmux="${MEMORA_WATCHDOG_CLMUX:-$(command -v clmux 2>/dev/null || echo "$HOME/.local/bin/clmux")}"
if [ ! -x "$clmux" ]; then
  printf '[%s] clmux not found (%s); alert logged only\n' "$stamp" "$clmux" >> "$log"
  exit 1
fi
run=("$clmux" registry send "$to" "$text")
if command -v timeout >/dev/null 2>&1; then run=(timeout 20 "${run[@]}"); fi
if "${run[@]}" >/dev/null 2>>"$log"; then
  exit 0
fi
printf '[%s] clmux send to %s FAILED; alert logged only\n' "$stamp" "$to" >> "$log"
exit 1
