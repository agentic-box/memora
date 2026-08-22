# memora watchdog — runbook

Supervises the **shared** memora container. Under the consolidated deployment one
container serves every workspace, so a restart affects all of them — and a
watchdog that restarts wrongly is worse than none.

## The rule

**Only repeated `/health` (liveness) failures cause a restart.**
`/health/db` and `/health/db/{name}` report *database* health and must never
restart the process: one slow remote store would otherwise take memory away from
every workspace. The URL is **validated** — anything that is not exactly
`/health` on a loopback host is refused at startup, so the rule cannot be
misconfigured away.

## Thresholds

| setting | default | meaning |
|---|---|---|
| `MEMORA_WATCHDOG_INTERVAL` | 10s | seconds between probes |
| `MEMORA_WATCHDOG_THRESHOLD` | 3 | consecutive failures before acting (~30s) |
| `MEMORA_WATCHDOG_TIMEOUT` | 20s | per-probe timeout. Raised from 5s after host compilation load tripped it 18x/hour and restarted a healthy server (2026-08-22); a restart cannot fix host contention. |
| `MEMORA_WATCHDOG_GRACE` | 15s | after TERM before KILL |
| `MEMORA_WATCHDOG_STARTUP` | 60s | must become healthy after start, or the restart FAILED |
| `MEMORA_WATCHDOG_BACKOFF` | 30s | first backoff, doubling |
| `MEMORA_WATCHDOG_BACKOFF_MAX` | 600s | backoff cap |
| `MEMORA_WATCHDOG_HEALTHY_RUN` | 3 | consecutive healthy probes before backoff clears |

A restart counts only when the container starts **and** `/health` becomes healthy
within the startup deadline. Command success alone is not evidence.

## Install

```sh
U=$(id -u); H=$HOME
mkdir -p "$H/.local/libexec/memora" "$H/.local/var/log" "$H/.local/var/run"
cp scripts/memora_watchdog.py scripts/memora_watchdog_alert.sh "$H/.local/libexec/memora/"
chmod +x "$H/.local/libexec/memora/memora_watchdog_alert.sh"
sed -e "s|REPLACE_HOME|$H|g" \
    -e "s|REPLACE_WATCHDOG_BIN|$H/.local/libexec/memora/memora_watchdog.py|" \
    launchd/com.memora.watchdog.memora-all.plist > "$H/Library/LaunchAgents/com.memora.watchdog.memora-all.plist"
plutil -lint "$H/Library/LaunchAgents/com.memora.watchdog.memora-all.plist"
launchctl bootstrap gui/$U "$H/Library/LaunchAgents/com.memora.watchdog.memora-all.plist"
launchctl enable gui/$U/com.memora.watchdog.memora-all
```

## Status

```sh
launchctl print gui/$(id -u)/com.memora.watchdog.memora-all | grep -E 'state|pid|last exit'
tail -f ~/.local/var/log/memora-watchdog-memora-all.log
```

A healthy system logs nothing but its startup line. Restart attempts,
failures, exit codes and stderr are all logged.

## Verify it does NOT restart a healthy container

The first thing to check after installing — an untested supervisor can kill the
service it protects.

```sh
# Watch for 60s with the container healthy. Expect NO restart lines.
sleep 60; grep -c restarting ~/.local/var/log/memora-watchdog-memora-all.log   # 0
container list | grep memora-all      # same STARTED timestamp as before
```

## Rollback

```sh
launchctl bootout gui/$(id -u)/com.memora.watchdog.memora-all
rm ~/Library/LaunchAgents/com.memora.watchdog.memora-all.plist
```

The container keeps running; only supervision is removed.

## Only one watchdog

An exclusive `flock` is held for the process lifetime, keyed to the container. A
second instance exits immediately with `another watchdog already holds …`.
Two watchdogs stopping and starting one container concurrently would take the
whole fleet down.

## Alerting

A hook is **installed and configured by default** — log-only supervision of a
process six workspaces share is a silent outage.
`scripts/memora_watchdog_alert.sh` appends to
`~/.local/var/log/memora-watchdog-alerts.log` and raises a desktop notification
(best effort; its absence does not fail delivery).

It fires on a **successful restart**, a **failed restart**, and a container that
starts but never becomes healthy. A hook exiting non-zero is logged by the
watchdog as undelivered and never kills it.

```sh
tail -f ~/.local/var/log/memora-watchdog-alerts.log
```

Replace the script to route elsewhere; it is invoked as
`<cmd> <container> <message>` and must exit 0 on delivery.
