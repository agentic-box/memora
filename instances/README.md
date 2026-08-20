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
| `test-sqlite` | local sqlite in `~/repos/memora-test/data` | 8900 | pilot |
| `test` | `memora-test-graph` (throwaway D1) | 8901 | pilot / memora-test |
| `memora` | `memora-graph` | 8910 | agentic-box |
| `ob1` | `ob1-graph` | 8911 | SAIL/ob1 + tarmacs/terminator |
| `bestation` | `bestation-graph` | 8912 | bestation |
| `re` | `re-graph` | 8913 | re |

All six are deployed and supervised. Only `agentic-box` has actually been
repointed at its container; `SAIL/ob1`, `tarmacs/terminator`, `bestation` and
`re` still talk to memora directly, so their containers are running but idle.

## Credentials are per instance, not shared

`CRED_SOURCE` defaults to `~/.config/memora/credentials.mcp.json` but each
instance can name its own, and the four real stores do. This is not tidiness:
`bestation` and `re` do NOT set the AWS/R2 backup variables that `agentic-box`
and `ob1` set. Feeding them one shared credential file would have switched cloud
backup ON for two stores that never had it — silently, since nothing would have
failed. Each workspace's `.mcp.json` env was copied to
`~/.config/memora/<name>.credentials.mcp.json` (mode 0600) before its container
was built, and the injected key set is verified against that file per container
(18 keys for `memora`/`ob1`, 15 for `bestation`/`re`).
