# instances/ — one file per memora store

A memora process binds **one** database for its whole lifetime (`storage.py`
resolves the backend at import time from `MEMORA_STORAGE_URI`). So one store =
one container = one proxy port = one LaunchAgent. Each file here is the complete,
credential-free description of one such deployment.

## Fields

| field | meaning |
|---|---|
| `INSTANCE` | short name; container defaults to `memora-<INSTANCE>` |
| `PORT` | host port the proxy listens on (`http://127.0.0.1:<PORT>/mcp`) |
| `STORAGE_URI` | `d1://<account>/<database>` — omit for local sqlite |
| `VOLUME` | host dir mounted at `/data` — local sqlite only |
| `CONTAINER` | optional; adopt a container that already exists under another name |

**No secrets live here.** The API token and embedding keys are read at run time
from a workspace `.mcp.json` (untracked) and passed with `-e`. That is why these
files are safe to commit and the credential source is not.

## Deploy one

```sh
./scripts/memora-instance.sh build          # once, shared image
./scripts/memora-instance.sh up      ob1    # start the container
./scripts/memora-instance.sh proxy   ob1    # render the plist + print install cmds
./scripts/memora-instance.sh status         # all instances at a glance
```

`proxy` only *renders* the LaunchAgent into `launchd/generated/` and prints the
`launchctl` commands — it never loads a service on your behalf. Run those
yourself, then point the workspace's `.mcp.json` at the URL it prints.

## Why a proxy at all

Apple's `container` reassigns the container's IP on **every start**, not just on
recreate. An MCP client reads its config once at startup, so a changed address
does not produce an error — it produces a permanent silent hang. The proxy is a
stable `127.0.0.1:<PORT>` in front of the moving address, re-resolving on each
connection.

## Current allocation

| instance | store | port | used by |
|---|---|---|---|
| `memora` | `memora-graph` | 8910 | agentic-box |
| `ob1` | `ob1-graph` | 8911 | SAIL/ob1 + tarmacs/terminator |
| `bestation` | `bestation-graph` | 8912 | bestation |
| `re` | `re-graph` | 8913 | re |

All four are deployed, supervised, and repointed — every workspace reaches
memora through its container. The two pilot instances (local sqlite on 8900, a
throwaway D1 on 8901) were retired once the real stores were live; their
containers, proxies and configs are gone.
