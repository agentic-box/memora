# memora proxy LaunchAgent

Stable `127.0.0.1:8900` in front of the Apple `container` named `memora-pilot`.
The proxy is a user LaunchAgent: starts at login, restarts on crash. It does
**not** start or restart the container — a dead container must show up as
fail-fast connection errors in `/tmp/memora-proxy-pilot.log`.

This file is the install instructions. The plist is **not** loaded by the
commit that adds it; the operator copies it into `~/Library/LaunchAgents`.

## One-time install (operator)

Stop the hand-started proxy first if it is still bound to 8900:

```bash
# identify, then stop — do not kill the memora-pilot container
lsof -nP -iTCP:8900 -sTCP:LISTEN
```

```bash
UID=$(id -u)
SRC=/tmp/memora-pilot-wt/launchd/com.memora.proxy.memora-pilot.plist
DST="$HOME/Library/LaunchAgents/com.memora.proxy.memora-pilot.plist"
cp "$SRC" "$DST"
# If the worktree path or python binary differs, edit ProgramArguments in $DST.
launchctl bootout "gui/$UID" "$DST" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$DST"
launchctl enable "gui/$UID/com.memora.proxy.memora-pilot"
```

Unload:

```bash
UID=$(id -u)
DST="$HOME/Library/LaunchAgents/com.memora.proxy.memora-pilot.plist"
launchctl bootout "gui/$UID" "$DST"
```

A second database is a second plist: copy, change `Label`,
`MEMORA_PROXY_CONTAINER`, `MEMORA_PROXY_LISTEN_PORT`, and the log paths.

## Verify after load

Proxy is listening and launchd owns it:

```bash
lsof -nP -iTCP:8900 -sTCP:LISTEN
launchctl print "gui/$(id -u)/com.memora.proxy.memora-pilot" | egrep 'state =|pid =|path ='
```

Proof: `state = running` and a `pid =` line, plus `Python` / `memora_proxy.py`
on `127.0.0.1:8900`.

### (a) container stop/start — address unchanged

```bash
# before
lsof -nP -iTCP:8900 -sTCP:LISTEN
curl -sS -m 3 -o /dev/null -w 'before:%{http_code}\n' http://127.0.0.1:8900/mcp || true
container stop memora-pilot
# connections must fail fast (<3s), listen address MUST still be 127.0.0.1:8900
python3 -c 'import socket,time; t=time.time();
import sys
try:
    s=socket.create_connection(("127.0.0.1",8900),5); s.close(); print("connected", round(time.time()-t,3))
except Exception as e:
    print(type(e).__name__, e, "after", round(time.time()-t,3))'
lsof -nP -iTCP:8900 -sTCP:LISTEN
container start memora-pilot
# wait until `container list` shows an IP, then:
curl -sS -m 5 -o /dev/null -w 'after:%{http_code}\n' http://127.0.0.1:8900/mcp || true
```

Proof: the listen line stays `127.0.0.1:8900` across stop/start; the same
proxy PID (or a KeepAlive replacement) is still bound; after start, curl
gets a real HTTP status rather than a hang.

### (b) proxy killed — launchd brings it back

```bash
UID=$(id -u)
LABEL=gui/$UID/com.memora.proxy.memora-pilot
OLD=$(launchctl print "$LABEL" | awk '/pid =/{print $3}')
echo "old_pid=$OLD"
kill -9 "$OLD"
sleep 3
launchctl print "$LABEL" | egrep 'state =|pid ='
lsof -nP -iTCP:8900 -sTCP:LISTEN
```

Proof: `state = running`, `pid =` is **different** from `old_pid`, and 8900
is listening again. If `pid` is missing and state is not running, KeepAlive
did not fire — check `/tmp/memora-proxy-pilot.stderr.log`.

### (c) login before the container runtime

Covered by RunAtLoad: the proxy binds 8900 even when `container list` fails.
A connection then fail-fasts (resolve timeout 2s + no retry hang). Once the
container appears, the next connection re-resolves and succeeds. Confirm in
the log:

```bash
tail -50 /tmp/memora-proxy-pilot.log
```

Look for `fail-fast no-upstream` while the container is down, then `connect`
lines with a `192.168.64.` address after it is up.

## What this service does not do

It does not start `memora-pilot`. One job per service: the proxy is the
stable address. Auto-starting the container would hide a crashed/unhealthy
container behind a green listen socket.

## Install (stable paths)

The script must NOT run from a worktree or /tmp — those do not survive a reboot,
and a missing script means launchd crash-loops while the proxy's absence shows up
as a silent client hang. Canonical locations:

    ~/.local/libexec/memora/memora_proxy.py     # the daemon
    ~/.local/var/log/memora-proxy-*.log         # its logs

Install/refresh the script after any change on this branch:

    install -d ~/.local/libexec/memora ~/.local/var/log
    install -m 755 scripts/memora_proxy.py ~/.local/libexec/memora/memora_proxy.py

The plist in this directory already points at those paths.

## Where everything lives

| Artefact | Path | In git? |
|---|---|---|
| Image recipe | `Dockerfile` | yes (this branch) |
| Container run config | `scripts/pilot-containers.sh` | yes |
| Proxy source | `scripts/memora_proxy.py` | yes |
| LaunchAgent plists | `launchd/*.plist` | yes |
| Proxy as run by launchd | `~/.local/libexec/memora/memora_proxy.py` | no — installed copy |
| Loaded agents | `~/Library/LaunchAgents/com.memora.proxy.*` | no — installed copy |
| Credentials | a workspace `.mcp.json`, read at run time | **no, by design** |

Credentials are never baked into the image and never committed: `pilot-containers.sh`
reads them from `$CRED_SOURCE` (default `~/repos/agentic-box/.mcp.json`) and passes
them with `-e` at `container run`.

## Rebuilding from scratch

    ./scripts/pilot-containers.sh build
    ./scripts/pilot-containers.sh up          # both instances
    ./scripts/pilot-containers.sh status      # containers + both proxies

Proxies are supervised separately by launchd and do not need restarting when a
container is recreated — they re-resolve the container IP per connection, which
is the whole point (Apple's `container` reassigns an IP on every start).
