# Runbook: add a new, empty store to memora-all, and copy memories into it (Item 95)

This adds a further store (`<new-store>`) to memora-all: a local
SQLite primary on `/data/<db>.db`, replicated in write mode to a NEW
Cloudflare D1 database, served at `/mcp/<db>`. It then copies the memories
tagged for that store out of another store (the originals stay).

It reuses the proven cutover (`docs/cutover-runbook.md`, `scripts/cutover_store.sh`,
unchanged) and adds what a **brand-new, empty** D1 database needs before the
cutover can run. Everything privileged runs inside the memora-all container
through the operator tool, with credentials as file paths only: no token value
is ever on a command line, printed, or handled by the leader.

**Requires an image that contains this item** (`bootstrap-d1`, `pre-freeze`,
`clear-freeze` in `scripts/local_primary.py`, and `scripts/copy_memories.py`).
`add_store.sh` refuses when the running memora-all predates it. Deploy a build
with it first (a normal deploy; no configuration change).

## Who does what

| | step |
|---|---|
| **user** | `wrangler d1 create <name>` (note the database id and name; the account is the existing one) |
| **user** | have the D1 **edit** token as a 0600 file the container can read (e.g. in `~/.config/memora-lp`, the directory mounted read-only at `/run/secrets/memora`) and confirm the D1 **read** token can read the new database (`add_store.sh`'s bootstrap step verifies this and refuses otherwise) |
| **leader** | `scripts/add_store.sh …` (steps 1-5), `scripts/cutover_store.sh …` (unchanged), the copy |
| **user** | **after the thaw and a clean barrier compare**: add the store to the named tokens in `api-tokens.json` (below), then the clmux bindings |

The account id, database id and database name appear only in git-ignored
files and on the command line; never in a commit or a test.

## Why this shape (decisions of 2026-10-16)

- **The replicator never sends DDL.** A new D1 database needs the app's schema
  before the first replicated write. `bootstrap-d1` gives it exactly that.
- **Scoped exception to "a verified export before every D1-affecting step"**
  (`docs/local-primary-implementation.md` §0 P1; leader-approved): a database
  with no schema cannot have a receipt. The exception is narrow: the exact
  database pinned by account + id + name from the user's `wrangler d1 create`
  output, proven read-only to be that database, readable, holding **zero user
  objects**, and not used by any store; then **only** what `schema.ensure_schema`
  sends over D1 (CREATE/ALTER … IF NOT EXISTS and the one `memories_meta`
  `embedding_change_epoch = '0'` row). A statement guard on the connection
  refuses everything else before it is sent (tests record the real initializer's
  statements and refuse a DROP, DELETE, UPDATE, INSERT into a business table, a
  parameterised statement, a second statement behind an allowed one, and a trigger
  with another body). After the schema exists the normal freeze, export, receipt,
  recheck, seed and barrier compare apply unchanged.
- **The interim served stage is pre-frozen** (leader-approved): the export and seed need
  memora-all to serve the store (the freeze is memora-all's), so the store is served
  from D1 for a while, but with the persisted freeze (`/data/freeze/<db>`) written
  **before** the first deploy: nothing can write it through memora-all, and nothing
  else holds a D1 write credential. Its capability is `writes: unsupported` by design
  (D1); after the cutover it is `transactional`.

## The steps (`scripts/add_store.sh`; dry run by default)

```
scripts/add_store.sh <new-store> --d1-account <account> --d1-id <id> --d1-name <d1-name> --credential-file F          # the plan
scripts/add_store.sh <new-store> … --execute                           # 1 bootstrap, 2 pre-freeze (stops)
scripts/add_store.sh <new-store> … --execute --from env --apply-env    # 3 env, 4 deploy, 5 export (then prints the cutover commands)
scripts/cutover_store.sh <new-store> --execute                         # a, b, c (the freeze is already in place)
scripts/cutover_store.sh <new-store> --execute --from d --apply-env    # d..g: env edits, the redeploy (the ONE deploy of the finished store), health, barrier compare
scripts/cutover_store.sh <new-store> --execute --from h --thaw         # h: only after a clean barrier compare
scripts/add_store.sh <new-store> --d1-account <account> --d1-id <id> --d1-name <d1-name> --execute --verify-first-write --expect-rows N   # AFTER a first write (the copy): the first REAL replicated write, below
```

**Every resumable boundary proves its preconditions first.** `--from prefreeze|env|deploy|export` run
`local_primary.py bootstrap-status` (read-only) before anything else: a verified bootstrap state for **exactly** the
given account, database id and name (a second invocation with another id is refused), and the persisted freeze file;
`--from env` also needs the operator files to be new or to hold exactly this entry, `--from deploy|export` need
them to already hold the interim entry (`add_store_config.py verify-applied`), and the export step needs the running
route to be exactly the pinned `d1://` entry, frozen, with nothing in flight. Otherwise the script refuses **before** any
edit, deploy or export, and does not initialise or freeze anything to make the check pass.

1. **bootstrap** — `local_primary.py bootstrap-d1`. Checks: inputs well-formed; the id is in neither
   `MEMORA_DATABASES` nor `MEMORA_REPLICAS` and the store is in neither; Cloudflare returns exactly the
   pinned `{uuid, name}`; the read token can read it; zero user objects (the LAST gate, immediately
   before the DDL, with identity re-checked); then `ensure_schema` through the guarded connection (a
   `D1Connection` subclass, so the D1 branches run: no FTS, no `api_idempotency`, no sync objects);
   afterwards `schema_pending == []`, the normalised definitions of every object equal what the app's D1 path
   builds on a scratch database (a right-named, wrongly defined object fails), `memories_meta` is exactly the
   epoch row `'0'`, every business table is empty, and the identity is checked once more. Durable state
   `/data/bootstrap/<db>.json` (0600): `ddl_started`, `ddl_done`, `verified`.
   **Resume rules:** a complete matching database is verified without any DDL; a crash before the first
   statement (still zero objects) is retried cleanly; **a partial or different schema is never replayed
   automatically**: it refuses and names the file to inspect (any safe-retry path needs its own decision).
2. **pre-freeze** — writes `/data/freeze/<db>` (needs a `verified` bootstrap state). Idempotent.
3. **env** — `scripts/add_store_config.py`: adds `MEMORA_DATABASES[<db>] = d1://<account>/<id>` to
   `instances/all.env` and `MEMORA_PROJECTS[<db>] = ["<db>"]` to `instances/deploy.env`, nothing else. The
   guards re-run on the current files at the moment of writing, under ONE shared operator lock for the pair
   (`instances/.operator-config.lock`, honoured by this tool; `cutover_store.sh` and a person with an editor do not
   take it), and each file is replaced only if its bytes are still exactly those the edit was computed from (a
   concurrent change is refused and preserved); a crash between the two files is resumed by running the step again
   (the same entry is accepted, a different value is refused); backups are dated, 0600, in
   `DEPLOY_BACKUP_DIR` (outside the repo; a directory inside it is refused) and are evidence only; the files
   are read back and every other line and mapping must be byte-identical. `MEMORA_REPLICATION=write` and
   `MEMORA_REPLICATION_INTERVAL_S` must already be set (the other stores use them).
4. **deploy** — `scripts/deploy-memora-all.sh`. The store comes up frozen. Stage check: HTTP 200 ok, frozen
   with 0 in flight, served from `d1://`, **no** replication block.
5. **export** — the first verified export (the cutover's step b rechecks the newest receipt and requires one
   under `~/memora-lp/exports/<db>/` on deploy-host), taken under the persisted freeze, copied to deploy-host
   and to this Mac.

**The interim store is not advertised.** No clmux binding, MCP entry or token scope for the store may exist
until the cutover's barrier compare is clean and the store is thawed as a local primary.

## The first real replicated write (leader-only, after the thaw)

The barrier compare before the thaw runs on an empty store, so it says nothing about the replication path. The first
real write is proven end to end before any client is pointed at the store. It needs a write to have happened (the copy
below is the natural one; an empty store is refused as proof) and **no live D1 write by anyone but the replicator**.

```
# after: cutover --from h --thaw, and the copy (apply) with N rows
scripts/add_store.sh <new-store> --d1-account <account> --d1-id <id> --d1-name <d1-name> --execute --verify-first-write --expect-rows N
```

1. `local_primary.py verify-replication` (read-only; credentials are file paths in the container: the health token
   file the script places on tmpfs, and the D1 READ token the container already has or `--read-token-file`):
   the store is a local transactional primary (`writes: transactional`), served from `/data/<db>.db`, thawed, the
   replicator is in **write** mode at the configured interval (from `MEMORA_REPLICATION_INTERVAL_S`, 60 s), running
   and not halted; then the outbox must drain: `head_seq > 0` and `last_acked_seq == head_seq` with `lag_rows 0` within
   **2 x interval + 30 s (150 s)**, polled every 3 s; then **every local row** is read back from D1 through the READ
   token with identical columns (`id, content, metadata, tags, created_at, updated_at`) and the D1 row count equals the
   local count (at least `--expect-rows`). Exit 0 = proven; 5 = not proven, with the checks that failed in the JSON.
2. A barrier compare, now non-vacuous, under a brief freeze: `freeze`, `compare --mode barrier --drain-timeout
   max(600, 2 x interval + 30)` (exit 0 = 0 diffs), `thaw`. On any non-zero exit the script stops and leaves the store
   **frozen** (no client has it) and names the rollback (stage S6, the L6 runbook).
3. Final report: the script prints `first-write verification PASSED …` (local transactional primary, write mode every
   60 s, ack reached head, rows read back identical, barrier compare clean, thawed). Report that line (with the row
   count) to the leader; only then the `api-tokens.json` scope and the clmux binding.

Timeout and failure handling: the wait is bounded (150 s); a halted or backing-off replicator, a D1 error, a delete
guard or a non-draining outbox fail the check and leave everything as it is. **Do not delete a copied memory from the
new store to "clean up": deletes on a table under 100 rows halt the replicator by design (P3).** If no copy is wanted,
one throwaway write can serve; it stays in the store.

## Rollback by stage

| stage | state | rollback |
|---|---|---|
| S0 | nothing run | nothing to do |
| S1 | bootstrapped | nothing is served and the store is in no registry. The database keeps its schema (the user may `wrangler d1 delete` it; no script ever does). State file kept as evidence |
| S2 | pre-frozen and/or env edited, not deployed | `add_store.sh <db> … --execute --rollback-env` removes ONLY the `<db>` keys from the CURRENT files (never restores a whole stale backup), then `--clear-freeze` |
| S3 | interim deployed (frozen, from D1) | `--rollback-env`, **redeploy**, verify `/mcp/<db>` and `/health/db/<db>` are gone, **then** `--clear-freeze` (it refuses while the running service still routes the store: the freeze stays until the route is verified gone). D1 was never written beyond the schema: verify with the read token that the business tables hold 0 rows |
| S4 | cutover a–d | `docs/cutover-runbook.md` "Rollback" (`--rollback-env` refuses once `MEMORA_REPLICAS` names the store) |
| S5 | cutover e–g (local, frozen) | the same, then the L6 runbook if the replica was written: `local_primary.py rollback <db> --phase drain\|verify\|finish` |
| S6 | thawed | the L6 runbook; a copy is undone by deleting the manifest's destination ids from the **destination** only |

## The token scope (user step, after the thaw)

`/api/v1` tokens live in `api-tokens.json` as `sha256(token) → [stores]`; the server re-reads the file when it
changes. Add the new store to the named tokens only:

- keep the existing clmux token: edit its digest's list (0600 file, owned by you), e.g. on deploy-host
  `python3 - <<'PY'` … load the JSON, `table[<digest>].append("<new-store>")`, write it back atomically with the same mode …`PY`;
  other tokens' lists are not touched; or
- mint a new token for the stores it should reach: `scripts/mint_api_token.sh --stores <store-1>,<store-2>,…,<new-store> --out <new file>`
  (the old token keeps working until revoked; clients must be given the new one).

Every other workspace that will use the store needs a token that reaches `<new-store>` and nothing it does not need.

## The copy (`scripts/copy_memories.py`)

Run **inside the memora-all container** (the only execution location: the lock, state and manifest are on its
`/data` volume), after the thaw. The container's health token is placed as a 0600 tmpfs file the way
`cutover_store.sh` does.

```
docker exec memora-all python /app/scripts/copy_memories.py --source <source-store> --dest <new-store> --tag <tag> \
    --health-token-file /dev/shm/…/health.token --plan /data/copy/plan.json              # dry run: writes only the plan
docker exec memora-all python /app/scripts/copy_memories.py --source <source-store> --dest <new-store> \
    --plan /data/copy/plan.json --apply --health-token-file …
```

- **Source: read only** (MCP read tools: `memory_list`, `memory_get` with images, `follow=all` for the exact row).
  **Destination: written only through its running server** (`memory_create`, `memory_update`,
  `memory_link`: the tools of the production `leader` profile; `memory_import` is not in it), so the gate, outbox
  triggers and replication stay in force. A file is never opened directly.
- **`created_at` becomes the copy time** (leader decision 2026-10-16: no API sets it, and for a copy of this size
  an admin import route is not worth the new server code). The source's `created_at` is kept in
  `metadata.original_created_at`; the dry-run report says so (`created_at_becomes_copy_time`). If a larger copy ever
  needs exact timestamps, an admin-token import route is the possible later item.
- **Dry run** reads both stores and writes only the plan (0600): the selected rows in full (content, metadata
  with images, tags, `created_at`, links) and a digest. `--apply` copies from the saved plan and refuses if a
  fresh read of the source no longer matches the digest or the plan file was edited.
- **Provenance**: every copy carries `metadata.memora_copy = {store, id}` and `metadata.project`. A source row that
  already has `memora_copy` is refused. Markers are looked up over **all** destination rows (every lineage
  state), so a superseded copy is never re-copied. After the creates there must be exactly one destination row per
  source id; a duplicate marker stops the run.
- **Retries and unknown outcomes (fail-closed)**: one run at a time per destination (an `flock` held for the
  whole operation) plus a durable state file `/data/copy/<src>-<dest>.json` (phases `new → creating → mapped →
  remapped → linked → done`, bound to the plan digest). Rows are created **one at a time**, in ascending source id.
  Before EVERY `memory_create` a write-ahead record `attempts[<source id>] = {"state": "sent"}` is made durable
  (fsync). It becomes `done` only when the response carries an integer id **and** a fresh listing of the whole
  destination (every lineage state) shows exactly that one marker. A timeout, a dropped connection, a crash before
  or after the send, a response without an id, an error, an acknowledged create whose marker is not visible, or a
  different id leave it `sent` and stop the run. On every rerun, before anything is created: a `sent` row with
  exactly one marker is reconciled (zero creates for it); a `sent` row with **no** marker makes the tool refuse to
  create **anything** (zero creates), however much time has passed: a request that timed out may still be queued on
  the server and land later, and neither elapsed time nor stable reads can prove it will not. Duplicates (more than
  one marker for a source id) stop the run. A short poll for the marker right after an unknown outcome
  (`--unknown-wait`, default 60 s) only reconciles a quick completion.
  **Guarantee, stated precisely:** under the documented model (the tool runs only inside the one memora-all
  container, one run at a time per destination under its `flock`, with its durable state intact) there is **no
  automatic duplicate retry**: an unknown outcome is never turned into permission to create again. It is not a
  claim of universal exactly-once: manual state edits, a deleted state file, a second container or unrelated
  writers to the destination are outside it. Partial progress and a manual step after an unknown outcome are the
  intended cost. The state file is written with an atomic 0600 replacement and an fsync of the file and of its
  directory before each request; a corrupt or unreadable state file stops the run (it is never read as "no attempts").
  **Manual reconciliation (deliberately outside the tool)** to re-create an ambiguous row that has no marker:
  1. make sure no copy invocation is running (nothing but this container runs the tool);
  2. restart memora-all (`docker restart memora-all`, or a deploy) **and verify the old server process is gone**:
     `docker inspect -f '{{.State.StartedAt}} {{.State.Pid}}' memora-all` shows a new start time and pid, and the
     container's uptime is seconds; issuing the command is not proof. A request queued in the old process died with it;
  3. with fresh reads from the NEW process, confirm in `/mcp/<dest>` (`memory_list`, `follow=all`) that no memory
     carries `metadata.memora_copy.id` equal to that source id (if one does, do nothing: the next `--apply`
     reconciles it);
  4. delete that source id's attempt **and record why**, under the same destination lock, in one step:

```
docker exec -i memora-all python - <<'PY'
import fcntl, json, os, time
SRC_ID, DEST, SRC = "<source id>", "<dest>", "<source store>"
NOTE = "<who, when, what you verified in steps 2 and 3>"
lock = os.open(f"/data/copy/{DEST}.lock", os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)          # fails if a copy is running
path = f"/data/copy/{SRC}-{DEST}.json"
st = json.load(open(path))
assert st["attempts"][SRC_ID]["state"] == "sent", st["attempts"].get(SRC_ID)
del st["attempts"][SRC_ID]
st.setdefault("manual_reconciliations", []).append(
    {"source_id": SRC_ID, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "note": NOTE})
tmp = path + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
os.write(fd, (json.dumps(st, indent=2, sort_keys=True) + "\n").encode()); os.fsync(fd); os.close(fd)
os.replace(tmp, path)
dfd = os.open("/data/copy", os.O_RDONLY); os.fsync(dfd); os.close(dfd)
PY
```

  5. rerun `--apply`. Every row that already has a marker (reconciled, or created by hand) must still match the
     saved plan (content, tags, metadata keys, project, `original_created_at`) before any remap or link; a marker
     alone does not certify a row.
  An error response (for example a tag the destination's allowlist rejects) is treated the same way: the run stops,
  the tool never drops a tag to make the copy pass; fix the cause, then reconcile as above.
- **Metadata references**: `merged_from` is remapped to the destination id after the ids exist (a reference to
  a memory that was not copied is dropped and listed in the manifest); other reference-shaped keys
  (`*_id`, `parent`, …) with integer values are reported by the dry run and refused unless `--allow-unknown-refs`.
  No integer is ever remapped by value.
- **Links** (done last): every edge between two copied memories is preserved with its type and direction:
  stored pairs (`references/referenced_by`, `implements/implemented_by`, `extends/extended_by`,
  `supersedes/superseded_by`, and the symmetric `related_to` and `contradicts`) are one `memory_link`;
  a forward-only edge is created without a reciprocal. `related_to` is copied too: there is no reliable
  way to tell a user's relation from a derived one. A reciprocal-typed edge with no forward edge cannot be created
  by `memory_link`: the dry run lists it as unrepresentable and `--apply` refuses unless `--allow-unrepresentable`
  (then it is listed in the manifest). Edges to memories outside the selection are dropped and counted. The score of a
  created edge is 1.0 (`memory_link` has no score): edges whose source score differed are counted. The
  destination's `memory_create` may also compute automatic crossrefs: extra edges it created between copied rows are listed
  as `recomputed_extra_edges` and never removed.
- **Verification**: content equal, `metadata.original_created_at` equal to the source's `created_at`, tags a superset, metadata keys equal, the marker and
  project present, and every source edge present in the destination; the source is re-read afterwards and its
  digest and size compared. A mismatch is reported as *the source changed during the run* (exit 4), never as a
  write by this tool; the tool never calls a write tool on the source.
- Outputs: `/data/copy/<src>-<dest>.json` (state), `….manifest.json` (source id → destination id, links
  created/present/unrepresentable/dropped, dropped references).

## Rehearsal

`scripts/rehearse_add_store.sh` (build-host, podman; local stores only). It cannot reach D1, so it does **not**
rehearse bootstrap against a real new database, the interim `d1://` stage, the export/receipt/seed, write-mode
replication or the barrier compare against D1; those are covered by fake-D1 tests and, live, by the cutover's own
gates. It does prove, with the real image and server: the tools exist in the image, `pre-freeze` makes a store
come up frozen in a real deploy, `clear-freeze` refuses while routed, a fifth store is routed at `/mcp/<db>` and
survives a redeploy, and the copy (dry run, refused apply into a frozen store, apply, rerun, redeploy) behaves as
above with the source byte-identical.
