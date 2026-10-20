#!/usr/bin/env bash
# Install the memora watchdog for one container as a systemd USER service (Item 108).
#
#   install_memora_watchdog.sh <container> <port> [runtime=docker]
#
# Run on the deploy host by scripts/deploy-memora-all.sh (from the tagged checkout), after the new
# container is verified; safe to run again (it re-copies the files and restarts the service).
# The unit is named memora-watchdog-<container>.service, so a rehearsal's scratch container never
# touches the production unit, its scripts (each container has its own copy under
# ~/.local/libexec/memora/watchdog-<container>/) or its alert log. Why a systemd user service and not a timer or cron: the watchdog is
# the already-reviewed daemon loop (scripts/memora_watchdog.py: exclusive lock, threshold, backoff,
# post-start verification), and systemd restarts the watchdog itself (Restart=always); a timer
# would be a second, weaker supervisor of the same decision. User lingering must be on.
set -euo pipefail

C="${1:?usage: install_memora_watchdog.sh <container> <port> [runtime]}"
PORT="${2:?usage: install_memora_watchdog.sh <container> <port> [runtime]}"
RT="${3:-docker}"
printf '%s' "$C" | grep -Eqx '[A-Za-z0-9][A-Za-z0-9_.-]*' || { echo "bad container name '$C'" >&2; exit 1; }
printf '%s' "$PORT" | grep -Eqx '[0-9]{1,5}' || { echo "bad port '$PORT'" >&2; exit 1; }
case "$RT" in docker|podman) ;; *) echo "runtime must be docker or podman, not '$RT'" >&2; exit 1 ;; esac
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$HOME/.local/libexec/memora/watchdog-$C"; UNITS="$HOME/.config/systemd/user"; VAR="$HOME/.local/var"
mkdir -p "$LIB" "$UNITS" "$VAR/log" "$HOME/.local/state/memora-watchdog"
# Atomic replacement: write a temp file in the same directory, then rename it over the target, so a service that
# runs the alert hook (or restarts) mid-install sees the old complete file or the new complete one, never a
# half-copied executable. Each container has its OWN directory, so no other container's files are touched.
for f in memora_watchdog.py memora_watchdog_alert_clmux.sh; do
  tmp="$(mktemp "$LIB/.$f.XXXXXX")"
  cp "$HERE/$f" "$tmp" && chmod 755 "$tmp" && mv -f "$tmp" "$LIB/$f" || { rm -f "$tmp"; echo "cannot install $f" >&2; exit 1; }
done
UNIT="memora-watchdog-$C.service"
UNIT_TMP="$(mktemp "$UNITS/.$UNIT.XXXXXX")"
cat > "$UNIT_TMP" <<UNIT_EOF
[Unit]
Description=memora watchdog for $C (restarts it when /health stops answering)

[Service]
Type=simple
ExecStart=/usr/bin/python3 $LIB/memora_watchdog.py
Environment=MEMORA_WATCHDOG_CONTAINER=$C
Environment=MEMORA_WATCHDOG_URL=http://127.0.0.1:$PORT/health
Environment=MEMORA_WATCHDOG_RUNTIME=$RT
Environment=MEMORA_WATCHDOG_LOCK=%t/memora-watchdog-$C.lock
Environment=MEMORA_WATCHDOG_INTERVAL=10
Environment=MEMORA_WATCHDOG_THRESHOLD=3
Environment=MEMORA_WATCHDOG_TIMEOUT=20
Environment=MEMORA_WATCHDOG_GRACE=15
Environment=MEMORA_WATCHDOG_STARTUP=90
Environment=MEMORA_WATCHDOG_BACKOFF=30
Environment=MEMORA_WATCHDOG_BACKOFF_MAX=600
Environment=MEMORA_WATCHDOG_MAX_RESTARTS_PER_HOUR=3
EnvironmentFile=-$HOME/.config/memora/watchdog.env
Environment=MEMORA_WATCHDOG_ALERT=$LIB/memora_watchdog_alert_clmux.sh
Environment=MEMORA_WATCHDOG_ALERT_LOG=$VAR/log/memora-watchdog-alerts-$C.log
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
Restart=always
RestartSec=10
StandardOutput=append:$VAR/log/memora-watchdog-$C.log
StandardError=append:$VAR/log/memora-watchdog-$C.log

[Install]
WantedBy=default.target
UNIT_EOF
mv -f "$UNIT_TMP" "$UNITS/$UNIT"
systemctl --user daemon-reload
systemctl --user enable "$UNIT" >/dev/null 2>&1
systemctl --user restart "$UNIT"
sleep 2
systemctl --user is-active --quiet "$UNIT" || { echo "the watchdog service $UNIT is not active" >&2; systemctl --user status "$UNIT" --no-pager 2>&1 | tail -8 >&2; exit 1; }
[ -f "$HOME/.config/memora/watchdog.env" ] && grep -q '^MEMORA_WATCHDOG_ALERT_TO=.' "$HOME/.config/memora/watchdog.env" \
  || echo "WARNING: no alert recipient: put MEMORA_WATCHDOG_ALERT_TO=<node>/<agent> in $HOME/.config/memora/watchdog.env (alerts are only logged until then)" >&2
echo "watchdog installed: $UNIT active (probe /health on :$PORT every 10 s, 3 failures, at most 3 restarts an hour; log $VAR/log/memora-watchdog-$C.log)"
if ! loginctl show-user "$(id -un)" -p Linger 2>/dev/null | grep -q 'Linger=yes'; then
  echo "WARNING: user lingering is off, so the watchdog stops when $(id -un) logs out: loginctl enable-linger $(id -un)" >&2
fi
