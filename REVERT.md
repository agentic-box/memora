# Reverting a workspace to the direct memora server

Each workspace's original `.mcp.json` — the stdio `command`/`args`/`env` form,
including its credentials — was copied to `~/.config/memora/` before the
workspace was repointed at a container. Restoring is one copy plus an agent
restart.

| workspace | restore from |
|---|---|
| `~/repos/agentic-box` | `~/.config/memora/credentials.mcp.json` |
| `~/repos/SAIL/ob1` | `~/.config/memora/ob1.credentials.mcp.json` |
| `~/repos/tarmacs/terminator` | `~/.config/memora/ob1.credentials.mcp.json` |
| `~/repos/bestation` | `~/.config/memora/bestation.credentials.mcp.json` |
| `~/repos/re` | `~/.config/memora/re.credentials.mcp.json` |

```sh
cp ~/.config/memora/ob1.credentials.mcp.json ~/repos/SAIL/ob1/.mcp.json
```

**One catch.** Those stashed files contain ONLY the `memora` server. Every
workspace except `agentic-box` also defines a `clmux` server in the same file,
which the repointing preserved. A blind copy would drop it. Restore just the
memora entry instead:

```sh
python3 - <<'PY'
import json
live = "/Users/spok/repos/SAIL/ob1/.mcp.json"
stash = "/Users/spok/.config/memora/ob1.credentials.mcp.json"
d = json.load(open(live))
d["mcpServers"]["memora"] = json.load(open(stash))["mcpServers"]["memora"]
json.dump(d, open(live, "w"), indent=2); open(live, "a").write("\n")
PY
```

Then restart that workspace's agents — MCP clients read config only at startup.

After restoring, the container and its proxy can be left running (harmless) or
stopped:

```sh
./scripts/memora-instance.sh down ob1
launchctl bootout gui/$(id -u)/com.memora.proxy.memora-ob1
```
