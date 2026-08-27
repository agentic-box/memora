# instances/ — one file per memora *container*

Each file here is the credential-free description of one container + proxy
port + LaunchAgent. A file may describe a **single store** (`STORAGE_URI` or
`VOLUME`) or a **registry** (`MEMORA_DATABASES`) that serves every workspace
from that one container by URL path (`/mcp/<name>`). Adding a store is a new
entry in an existing registry, or a new file — not a fork of the deploy script.

`storage.py` still resolves a backend at import when `MEMORA_DATABASES` is
unset (one process, one store). With the registry set, one process serves
every named store.

## Fields

| field | meaning |
|---|---|
| `INSTANCE` | short name; container defaults to `memora-<INSTANCE>` |
| `PORT` | host port the **proxy** listens on (`http://127.0.0.1:<PORT>/mcp` , or `/mcp/<name>` for a registry) |
| `STORAGE_URI` | `d1://<account>/<database>` — single-store |
| `VOLUME` | host dir mounted at `/data` — single-store local sqlite |
| `MEMORA_DATABASES` | JSON `{name: uri}` registry; the container serves `/mcp/<name>` |
| `MEMORA_DEFAULT_DB` | which registry name a bare `/mcp` uses |
| `CONTAINER` | optional; adopt a container that already exists under another name |
| `IMAGE` | optional; pin this instance to its own image tag |
| `CRED_SOURCE` | optional; per-instance credential file (see below) |
| `TOOL_PROFILE` | optional; `full` / `leader` / `agent` (script default `leader`) |
| `MEMORY` / `CPUS` | optional; per-instance VM size (script defaults `512M` / `2`) |
| `MEMORA_HEALTH_TIMEOUT` | optional; per-store probe bound in seconds (script injects `15` if unset). `all.env` sets `30`. |
| `MEMORA_HEALTH_REFRESH_INTERVAL` | optional; background readiness refresh interval (script injects `15` if unset) |
| `MEMORA_VECTOR_SCAN_PAGE_SIZE` | optional; embedding page size (script injects `100` if unset — not the server default of `1000`) |

`load()` requires **at least one** of `STORAGE_URI`, `VOLUME`, or
`MEMORA_DATABASES`. If more than one is set, `cmd_up` uses `MEMORA_DATABASES`,
then `STORAGE_URI`, then `VOLUME`. Routing is instance-owned: a credential
file may not supply `MEMORA_DATABASES` / `MEMORA_DEFAULT_DB` /
`MEMORA_STORAGE_URI` / `MEMORA_DB_PATH` (the script skips those from
`$CRED_SOURCE` so a stale copy cannot silently retarget the container).

**No secrets live here.** API tokens and embedding keys are read at run time
from `$CRED_SOURCE` and passed with `-e`. If the instance file does not set
`CRED_SOURCE`, the script uses `~/.config/memora/credentials.mcp.json` **when
that file exists**, otherwise `~/repos/agentic-box/.mcp.json`. Once a
workspace points at the container, its `.mcp.json` is a bare `{type, url}`
HTTP entry — the env block has nowhere to live.

## Deploy one

Default runtime is Apple's `container` CLI. `scripts/memora-instance.sh`
invokes it as `$MEMORA_CONTAINER_BIN` (default `container`) for every
container operation the script performs (`build`, `up`, `status`, `logs`,
`down`). The generated proxy process hardcodes `container list`.

```sh
mkdir -p ~/.local/libexec/memora ~/.local/var/log     # the proxy + its logs live here
cp scripts/memora_proxy.py ~/.local/libexec/memora/   # the generated plist points at $MEMORA_PROXY_BIN
```

Without the copy, `proxy` renders a plist whose executable does not exist and
whose log paths are uncreatable. Set `MEMORA_PROXY_BIN` in the environment if
you keep the proxy somewhere else.

```sh
./scripts/memora-instance.sh build   ob1   # build that instance's image tag
./scripts/memora-instance.sh up      ob1   # start the container
./scripts/memora-instance.sh proxy   ob1   # render the plist + print install cmds
./scripts/memora-instance.sh status        # all instances at a glance
```

`build` takes an instance name and tags that instance's image
(`IMAGE` in its file, default `memora-pilot` for an unnamed build); `up`
runs the instance's `IMAGE`. Build and up the same named instance so they
agree.

`proxy` only *renders* the LaunchAgent into `launchd/generated/` and prints the
`launchctl` commands — it never loads a service on your behalf. Run those
yourself. The printed workspace URL is always `http://127.0.0.1:<PORT>/mcp`
(the registry default). For a non-default store, append `/<name>` — a bare
`/mcp` on a registry silently binds `MEMORA_DEFAULT_DB`, which is a
wrong-store write with no error.

## Why a proxy at all

`up` does not publish a host port. Apple's `container` also reassigns the
container's IP on **every start**, not just on recreate. An MCP client reads
its config once at startup, so a changed address does not produce an error —
it produces a permanent silent hang. The proxy is a stable `127.0.0.1:<PORT>`
in front of the moving address, re-resolving on each connection.

## Instance files in this directory

Per-store files (one D1 database each):

| file | `PORT` | used by (from the file's own comment) |
|---|---|---|
| `memora.env` | 8910 | agentic-box |
| `ob1.env` | 8911 | SAIL/ob1 + tarmacs/terminator |
| `bestation.env` | 8912 | bestation |
| `re.env` | 8913 | re |

Registry file (one container, every store by path):

| file | `PORT` | names in `MEMORA_DATABASES` |
|---|---|---|
| `all.env` | 8920 | `memora`, `ob1`, `bestation`, `re` (`MEMORA_DEFAULT_DB=memora`) |

Which of those is running on a given host is an operational fact, not
something these files can assert. `./scripts/memora-instance.sh status`
lists what is up.

`example.env` is a template. Copy it; do not point a workspace at it.
