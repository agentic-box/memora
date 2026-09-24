# Cutover runbook: one store to local primary (REL1)

This runbook moves ONE store of memora-all (nuc8) from `d1://` to a seeded
local SQLite file on memora-all's `/data` volume. The file is replicated to
the same D1 database in write mode. `scripts/cutover_store.sh <db>` runs it
from the Mac, in the checkout whose `instances/all.env` the deploy reads.
The first store is `re`. memora, ob1 and bestation stay on `d1://`.

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
- **One-time, on nuc8, before the first v0.5.0 deploy**, write the
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
  - In this pilot, `d1-read.token` is both the read token and the
    replicator token (the user's decision). To split them later, add a
    file and set `DEPLOY_D1_REPLICATOR_TOKEN_FILE=<name>` for the deploy.
- **A verified export of the store** exists on nuc8 under
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
| b | Copy the newest receipt and its `.sql` into `/data/exports/<db>/`, then `recheck` under the freeze. A fresh export (still frozen) is taken when D1 moved, or when the receipt is older than 24 h. A fresh export is copied out to nuc8 (`~/memora-lp/exports/<db>/`) and to this Mac (`~/memora-lp/exports/<db>/`, or `CUTOVER_OFFHOST_DIR`) | the tool returns a receipt; still frozen |
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

- `MEMORA_REPLICATION_INTERVAL_S` is `--interval` (default 60, the `re`
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
scripts/cutover_store.sh re                                   # the plan; nothing is done
scripts/cutover_store.sh re --execute                         # a, b, c; prints d's edits and stops
scripts/cutover_store.sh re --execute --from d --apply-env    # d, e, f, g; stops before the thaw
scripts/cutover_store.sh re --execute --from h --thaw         # h
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
  token is on a command line of the Mac or of nuc8.

### What is not exercised before the live run

Write mode against real D1 runs for the first time at step e of the first
live cutover.
- The server2 rehearsal (`docs/deploy-rehearsal.md`) runs the v0.5.0 deploy
  with a seeded local store as a replicated store in **log** mode: the
  passthrough, the replication block and the token mount.
- There is no fake D1 HTTP endpoint the replicator can be pointed at: its
  D1 base URL is fixed to `api.cloudflare.com`. A write-mode rehearsal
  would therefore send a throwaway token to Cloudflare, so none is run.
- Step f's checks (mode `write`, not halted, lag 0) and step g's compare
  are the gate before the thaw.

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
