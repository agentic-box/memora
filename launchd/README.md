# Supervised memora proxies

Each memora container gets a LaunchAgent running `scripts/memora_proxy.py`,
which holds a stable `127.0.0.1:<PORT>` in front of the container. Apple's
`container` runtime reassigns a container's IP on **every start**, and an MCP
client reads its config once at startup — so without the proxy a restart does
not produce an error, it produces a permanent silent hang.

## Install one

Do not hand-write these files. `memora-instance.sh proxy <name>` renders the
plist for that instance into `launchd/generated/` (gitignored — the paths are
machine-specific) and prints the exact `launchctl` commands. It deliberately
does **not** load the service for you; loading a supervised background job is
the operator's decision.

```sh
./scripts/memora-instance.sh proxy ob1     # render + print the install commands
```

Then run what it prints. It ends with an `lsof` check so you see the listener
appear, and gives you the `.mcp.json` line for the workspace.

## What the generated plist sets, and why

| key | why it matters |
|---|---|
| `PATH` | launchd gives a job a minimal PATH that does **not** include the `container` CLI, which the proxy shells out to for the container's current IP. Without this the proxy starts and then cannot resolve anything. |
| `KeepAlive` + `ThrottleInterval 5` | the proxy is respawned if it dies, with a floor on restart rate. |
| `RunAtLoad` | it comes back after a reboot. |
| `MEMORA_PROXY_CONTAINER` | which container to resolve — the one field that differs per instance besides the port. |
| `ProcessType Background` | scheduling hint; this is infrastructure, not interactive work. |

## Checking and removing

```sh
./scripts/memora-instance.sh status                 # containers + proxy health
launchctl print gui/$(id -u)/com.memora.proxy.memora-ob1 | grep state
tail -f ~/.local/var/log/memora-proxy-ob1.log       # per-connection resolve/connect log

launchctl bootout gui/$(id -u)/com.memora.proxy.memora-ob1   # stop supervising
rm ~/Library/LaunchAgents/com.memora.proxy.memora-ob1.plist
```

Stopping a container without unloading its proxy leaves a listener with nothing
behind it, which makes clients **hang** rather than fail. Unload the proxy too,
so callers get a clean connection-refused they can act on.
