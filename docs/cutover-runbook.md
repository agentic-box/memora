# Cutover runbook: one store to local primary (REL1)

This runbook moves ONE store of memora-all (deploy-host) from `d1://` to a seeded
local SQLite file on memora-all's `/data` volume. The file is replicated to
the same D1 database in write mode. `scripts/cutover_store.sh <db>` runs it
from the Mac, in the checkout whose `instances/all.env` the deploy reads.
The first store is `gamma`. memora, alpha and beta stay on `d1://`.

The script runs only the steps below and checks each boundary. With no
flags it prints the plan and does nothing: it makes no ssh call and no edit
(`--dry-run` is the default). Every failure message ends with the rollback
pointer (below).

## Before you start

- **memora-all runs v0.5.0**, deployed by `scripts/deploy-memora-all.sh`.
  - That release mounts `~/.config/memora-lp` read-only at
    `/run/secrets/memora`.
  - It sets `CLOUDFLARE_API_TOKEN_FILE`, `MEMORA_D1_READ_TOKEN_FILE` and
    `MEMORA_D1_REPLICATOR_TOKEN_FILE`, and no token value. The server
    reads these with the rule in `memora/secret_files.py`:
    - an absolute path;
    - not a symlink, and a regular file;
    - no group or other permission bits;
    - readable by the server;
    - not empty.

    The owner is not checked, because a container maps the host uid to
    another uid. `FOO` and `FOO_FILE` both set stops the server.
- **One-time, on deploy-host, before the first v0.5.0 deploy**, write the
  Cloudflare token that `credentials.mcp.json` holds today into its file.
  The value is never printed:

  ```
  ( umask 077; python3 -c 'import json,os; e=json.load(open(os.path.expanduser("~/.config/memora/credentials.mcp.json")))["mcpServers"]["memora"]["env"]; print(e.get("CLOUDFLARE_API_TOKEN") or e["CF_API_TOKEN"])' > ~/.config/memora-lp/cloudflare-api.token )
  ```

  - The deploy refuses a missing, empty, symlinked, foreign-owned or
    non-0600 token file before it fetches, builds or stops anything.
  - The new image then reads every file through the read-only mount
    before the old container is stopped.
  - Keep only token files in `~/.config/memora-lp`: the container sees the
    whole directory. The deploy lists the directory's entries.
  - The replicator writes to D1, so by default it takes the write-capable
    `cloudflare-api.token`; `d1-read.token` is the read-only token and must
    be a different file (the pilot's shared-file arrangement ended with
    Item 99: a read-only token as the replicator's made every store's
    replicator back off with D1 code 7500). To use another file, set
    `DEPLOY_D1_REPLICATOR_TOKEN_FILE=<name>` in `instances/deploy.env` (or
    the environment). After the start the deploy checks each
    replicated store's `/health/db/<store>` `write_probe`: `ok` passes; `refused`
    (D1's definite code 7500) fails the deploy with the D1 error; `unproven` (a
    timeout, 5xx or other D1 answer) prints "write capability unproven" as a
    warning and is not a success. That D1 refuses the zero-row probe for a
    read-only token is **assumed** from Cloudflare's documentation and the
    2026-10-17 production 7500, **not measured**. `scripts/d1_permission_probe.py`
    (in the image) can measure it on a throwaway `memora-probe-*` D1 database;
    it is available and has not been run.
- **A verified export of the store** exists on deploy-host under
  `~/memora-lp/exports/<db>/` (`<stamp>.sql` and `<stamp>.receipt.json`),
  with copies off the host.
- **Tool path.** The operator tool is in the image, at
  `/app/scripts/local_primary.py`. The script runs it with `docker exec
  memora-all python /app/scripts/local_primary.py …`. The named volume is
  root-owned on the host, so seeding `/data/<db>.db` must run inside the
  container.

## The steps

| step | what | boundary check |
|---|---|---|
| a | `freeze <db>` through `POST /admin/freeze/<db>`. The freeze is persisted on `/data` | `/health/db/<db>`: frozen, 0 in flight, no open intent |
| b | Copy the newest receipt and its `.sql` into `/data/exports/<db>/`, then `recheck` under the freeze. A fresh export (still frozen) is taken when D1 moved, or when the receipt is older than 24 h. A fresh export is copied out to deploy-host (`~/memora-lp/exports/<db>/`) and to this Mac (`~/memora-lp/exports/<db>/`, or `CUTOVER_OFFHOST_DIR`) | the tool returns a receipt; still frozen |
| c | `seed <db> --receipt <b> --out /data/<db>.db`. It rechecks again, loads the export, raises the sequences, installs sync with `d1://<account>/<database-id>`, verifies against the receipt and links the file into place. Then `fk-audit` | seed exit 0; fk audit clean; still frozen |
| d | Print the `all.env` edits; `--apply-env` makes them. A timestamped 0600 backup, `all.env.bak-cutover-<db>-<ts>`, is taken first | the file reads back with all three values |
| e | `scripts/deploy-memora-all.sh` (production defaults). The store comes up frozen, from the persisted freeze | the deploy's own checks, every store included |
| f | health | the store is served from `/data/<db>.db`; memora-all's env has `MEMORA_REPLICATION=write` and `MEMORA_REPLICAS[<db>]`; there is a replication block; mode `write`; the configured `interval_s`; not halted; `last_acked_seq` reaches `head_seq` (`lag_rows` 0) within 2 × interval + 30 s; still frozen |
| g | `compare <db> --mode barrier --store /data/<db>.db --drain-timeout max(600, 2 × interval + 30)`, recorded in the store | exit 0; the recorded last compare is a clean `barrier` |
| h | `thaw <db>`, only with `--thaw` | before: a clean barrier compare recorded, and nothing written after it (`compare_consumed_seq == head_seq`). After: freeze `open` |

The `all.env` edits in step d are:

```
MEMORA_DATABASES='{…, "<db>": "/data/<db>.db"}'
MEMORA_REPLICAS='{"<db>": "d1://<account>/<database-id>"}'   (merged with any store already there)
MEMORA_REPLICATION=write
MEMORA_REPLICATION_INTERVAL_S=60                             (--interval)
```

- `MEMORA_REPLICATION_INTERVAL_S` is `--interval` (default 60, the `gamma`
  pilot's once a minute, leader 7763). It applies to every replicated
  store of memora-all.
- **The replicator's timing** (leader 7762), from `all.env` through the
  deploy; the server refuses an invalid value for the store, and the
  reason shows in `/health/db/<db>` as `replication: {status: refused,
  error}`. It never falls back to a default:
  - `MEMORA_REPLICATION_INTERVAL_S` (≥ 0, default 0): the minimum time
    between the starts of consecutive sends. Commits made in between
    accumulate into the next batch. 0 sends on commit, as before.
  - `MEMORA_REPLICATION_POLL_S` (> 0, default 5): the fallback wake.
  - `MEMORA_REPLICATION_BATCH_ROWS` (1–1000, default 100): the rows per
    send. A backlog larger than one batch drains one batch per interval.
  - `/health/db/<db>`'s replication block shows the effective
    `interval_s`, `poll_s` and `batch_rows`.
  - A freeze is not delayed by the interval: no send is in flight while
    the replicator waits. A drain that waits for the acks (the barrier
    compare, the L6 rollback's drain, `last_acked_seq ≥ head`) waits up to
    one interval longer per batch. So step f allows 2 × interval + 30 s
    for the ack to reach head, and step g passes the compare
    `--drain-timeout max(600, 2 × interval + 30)`.
- The account and database id come from the store's current `d1://`
  entry. The replicator requires `MEMORA_REPLICAS[<db>]` to equal
  `sync_state.replica_uri`, which the seed derives from the same two
  values.
- The registry entry is a plain path. `sqlite:////data/<db>.db` is NOT a
  URI memora parses: `parse_backend_uri` would take it as a literal file
  name. `file:///data/<db>.db` also works.
- `MEMORA_REPLICAS` is a JSON map. A bare store name does not work.

**Between step e and the thaw, the store serves reads, not writes.** It
comes up frozen from the persisted freeze. Since X2 a frozen store skips
the schema pass when a read-only check shows it would change nothing, so
searches and `memory_stats` work; writes are refused until step h. If
that check finds a pending upgrade, the store refuses even reads, and says
so, until it is thawed.
- The deploy's store check runs `memory_stats` on a frozen store too.
- For a store in `MEMORA_REPLICAS`, frozen or not, the check also requires
  (review 7787, leader 7789) a replication block; the configured mode and
  `replica_uri`; a status neither `refused` nor `halted`; and the sync
  schema read from the store at the current trigger version.
  - A refused or halted replicator fails the deploy at once.
  - A block that has not appeared, or a status other than `running`
    (such as `backoff` after a D1 error), is waited for (90 s), then
    fails, naming the last error.
  - Each failure names the rollback.

### Runs

```
scripts/cutover_store.sh gamma                                   # the plan; nothing is done
scripts/cutover_store.sh gamma --execute                         # a, b, c; prints d's edits and stops
scripts/cutover_store.sh gamma --execute --from d --apply-env    # d, e, f, g; stops before the thaw
scripts/cutover_store.sh gamma --execute --from h --thaw         # h
```

- `--from` takes `a`, `d`, `e`, `f`, `g` or `h`. Steps a to c are one
  unit: a is idempotent, and the seed rechecks its receipt under the
  freeze itself.
- A seed refuses when `/data/<db>.db` already exists. For a rerun after a
  failed c, first move that file aside (inside the container).
- From `e` on, the script requires the `all.env` edits to be in place.
- **Tokens.** The admin and health tokens reach the tool as 0600 files in
  `/dev/shm/memora-cutover`, a tmpfs inside the container. The container's
  own shell writes them from its environment (`printf` is a shell
  builtin, so there is no process argv), and they are removed on exit.
  The D1 read token is the container's `MEMORA_D1_READ_TOKEN_FILE`. No
  token is on a command line of the Mac or of deploy-host.

### What is not exercised before the live run

Write mode against real D1 runs for the first time at step e of the first
live cutover.
- The build-host rehearsal (`docs/deploy-rehearsal.md`) runs the v0.5.0 deploy
  with a seeded local store as a replicated store in **log** mode: the
  passthrough, the replication block and the token mount.
- There is no fake D1 HTTP endpoint the replicator can be pointed at: its
  D1 base URL is fixed to `api.cloudflare.com`. A write-mode rehearsal
  would therefore send a throwaway token to Cloudflare, so none is run.
- Step f's checks (mode `write`, not halted, lag 0) and step g's compare
  are the gate before the thaw.

## Recovery: memora-all restarts itself (Item 108)

**The probe is the authority.** Whether memora-all is alive is decided by `GET /health` answering (the
database-free liveness route), never by the runtime's state: a container can be "running" and dead
(m1003, the Apple container runtime), and a container that was `docker kill`ed counts as a manual
stop, so `--restart unless-stopped` leaves it down (2026-10-17: every container on the deploy host was
killed and memora-all stayed down until someone restarted it by hand). Two layers:

1. **A HEALTHCHECK on the container** (set by `scripts/deploy-memora-all.sh`): `GET /health` on the
   container's own port 8000, every 30 s, timeout 10 s, 3 retries, start period 120 s. `docker ps`
   shows `healthy`/`unhealthy`. A cold start (embedding warmup, the replicators starting) is verified
   healthy within about 60 s, and a failed check inside the 120 s start period does not count, so a
   start never flaps. Unhealthy takes about 90 s of a dead `/health`, deliberately slower than the
   watchdog: this status is for people and the runtime, the watchdog acts.
2. **A watchdog service on the deploy host** (`memora-watchdog-memora-all.service`, a systemd *user*
   service, installed by the deploy from the tagged checkout; `scripts/memora_watchdog.py` is the
   same reviewed supervisor the macOS launchd job uses). A service rather than a timer or cron
   because the supervisor is already a loop with an exclusive lock, a failure threshold, backoff and
   a post-start check, and systemd restarts the watchdog itself (`Restart=always`; user lingering is
   on). It probes `/health` every 10 s with a 20 s timeout.
   - **Dead or not running** (killed, exited, hung, 503): after 3 failed probes in a row it starts a
     stopped container, or stops (TERM, then KILL after 15 s) and starts a running-but-dead one, then
     requires `/health` to answer within 90 s. Detected and back within about 30 s of a kill, and
     about 100 s of a hung process (3 probes of 20 s each plus the intervals). It issues only
     `inspect`, `stop`, `kill`, `start`, `unpause`: never `rm`, `run` or `create`, and nothing on a
     volume. A container that does not exist is reported, not created.
   - **A degraded store never restarts it**: only `/health`, never `/health/db`.
   - **Backoff and cap:** 30 s doubling to 600 s between attempts, and at most 3 attempts (a failed one
     counts, a successful one counts) in a rolling hour. The next one is not made: it alerts "GIVING
     UP" once and takes no action until `/health` answers again. A given-up container is left down on
     purpose; look at it, fix it, `docker start memora-all` (the service resumes by itself) or restart
     the service to reset the count.
   - **Alerts:** one clmux message to the leader on each restart attempt (success or failure) and on
     giving up, from `scripts/memora_watchdog_alert_clmux.sh` (no token can travel in it; long
     token-like strings are redacted). The recipient is NOT in the repo: put
     `MEMORA_WATCHDOG_ALERT_TO=<node>/<agent>` in `~/.config/memora/watchdog.env` on the deploy host (the
     installer warns when it is missing; alerts are then only logged). **The sender is unverified:** clmux
     has no identity path for a non-interactive service yet, so the message arrives as "unknown sender,
     claimed from memora-watchdog" and its text starts with "[UNVERIFIED sender, no clmux service
     identity yet]"; anyone on the host could forge it. If clmux is unreachable it is logged and the
     watchdog carries on.
   - **Logs:** `~/.local/var/log/memora-watchdog-memora-all.log` (every decision),
     `~/.local/var/log/memora-watchdog-alerts.log` (every alert); `systemctl --user status
     memora-watchdog-memora-all`.
3. **An intentional stop must hold the watchdog**, or it will be started again within about 30 s.
   The deploy does this itself (held before it stops the container, released after the new one is
   verified; a deploy that fails leaves the hold, which expires on its own after 45 minutes). For
   anything else (a rollback, the stopped-required phases of `local_primary.py`, a manual
   `docker stop memora-all`):

   ```sh
   python3 ~/.local/libexec/memora/memora_watchdog.py hold --container memora-all --minutes 45 --reason "rollback"
   # ... stop, work, start ...
   python3 ~/.local/libexec/memora/memora_watchdog.py release --container memora-all
   ```

   While held the watchdog neither probes nor acts. A stale hold (past its expiry, or unreadable) is
   ignored and removed, so a forgotten hold never blocks recovery for more than its length.
   `scripts/switch-embedding-host.sh` holds and releases by itself.

Rehearsed on the deploy host with a scratch container (not memora-all) and the real installer: a
`docker kill` was back in 26-28 s; a process answering 503 in 38 s; a hung process in 97 s; a held kill
stayed down for the whole hold and was restarted 26 s after the release; a stale hold was ignored; a
crash loop got 3 attempts, then GIVING UP, then no further action. Every restart and the give-up
reached the leader's inbox.

## Rollback

**Before the thaw (steps a–g; the store is still frozen).**
- The freeze stops writes before they reach the gate, so nothing was
  written to the local store after the seed. A clean or skipped compare
  confirms D1 is unchanged. (The replicator sends only rows it drains from
  the outbox, and a frozen store adds none.)
- If `all.env` was not edited (a failure in a–d): lift the freeze with
  `local_primary.py thaw <db>` (in the container, as the script runs it),
  or leave it frozen while you investigate.
- If `all.env` was edited (a failure in e–g):
  1. Restore the backup: `cp -p instances/all.env.bak-cutover-<db>-<ts> instances/all.env`.
  2. Run `scripts/deploy-memora-all.sh`: the store is served from `d1://`
     again, still frozen.
  3. Check `/health/db/<db>`: it has no replication block.
  4. Thaw.
- The seeded `/data/<db>.db` stays; move it aside before a new attempt.

**After the thaw: scheduled compares.** `scripts/nightly_compare.sh` (NC1,
from cron on the deploy host) compares every replicated store every night,
and under a brief freeze every Sunday. Check its log
(`~/memora-lp/compare-logs/`) after the first nights.

**After the thaw (the store takes writes and replicates them).** Use the L6
runbook: `docs/local-primary-implementation.md` §5.3,
`local_primary.py rollback <db> --phase drain|verify|finish --store /data/<db>.db`.
- **Where the stopped-required phases run: X3's venue, `scripts/lp_container.sh`.**
  `rollback --phase verify` (like `restore`, `resume` and
  `sequence-highwater`) needs memora-all stopped and the store file on the
  root-owned volume. `lp_container.sh` runs the operator tool in a one-off
  container of memora-all's current image, with `memora-all-data` at
  `/data` and `LP_TOKEN_DIR` read-only at `/run/secrets/memora`.
  `--lock-barrier` makes the data volume's service lock
  (`/data/.service.lock`, which memora-all holds for its lifetime; the
  deploy pins `MEMORA_DATA_DIR=/data` so both sides lock the same file) the
  proof that memora-all is stopped, held for the whole run. Usage and exit
  codes: the script's header and `docs/local-primary-implementation.md`
  §5.3 (X3).
  - Rehearse the post-thaw rollback with it before the first store is
    thawed (leader 7760: no store is thawed until X3 is in place).
