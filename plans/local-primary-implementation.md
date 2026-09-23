# Local-primary: implementation plan

Implements `plans/nuc8-local-primary-design.md`. Base: dev @ 3d03123.
The user's decisions (leader msg 7507):
- Replication is async for ALL stores, including `memora`.
- The memora-graph viewer becomes read-only.
- nuc8 `memora-all` becomes the only D1 writer.

Every slice is dark until its store is cut over: nothing below changes a
store that has no `sync_state` row. No step runs against a live store before
cutover (§8). All tests are offline (local SQLite plus FakeD1).

## 1. Schema: outbox, state, triggers

A new function, `_ensure_sync_outbox(conn)`, goes in `memora/schema.py`.
- `ensure_schema` calls it last, after `_ensure_import_lease_table`.
- It returns at once when `isinstance(conn, D1Connection)`. That makes it
  inert on `d1://` stores: no table and no trigger is ever created in D1, and
  the replicator's own D1 writes cannot enqueue anything.
- On a local store it only **maintains** state; it never enables replication.
  - When the store has no `sync_state` table, it does nothing, so stores that
    are not cut over (and every test store) are untouched.
  - When `sync_state.trigger_version < SYNC_TRIGGER_VERSION` (a module
    constant, starting at 1), it runs, in one `BEGIN IMMEDIATE`:
    - `DROP TRIGGER IF EXISTS` for every `trg_sync_*` trigger;
    - it recreates them from the DDL below;
    - it updates `trigger_version`.
    That is the versioning scheme. It is cached like the rest of the schema
    through `_backend_schema_signature` / `_mark_backend_schema_ensured`.
- Replication is enabled ONLY by the seed script (§4), which creates both
  tables, installs the triggers and writes the `sync_state` row.

```sql
CREATE TABLE sync_outbox (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,      -- commit order within the store
  tbl TEXT NOT NULL,                          -- source table
  op  TEXT NOT NULL CHECK (op IN ('U','D')),  -- U = row exists after write, D = deleted
  pk  TEXT NOT NULL,                          -- json_array(pk columns)
  created_at REAL NOT NULL DEFAULT (julianday('now'))
);
CREATE TABLE sync_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  replica_uri TEXT NOT NULL,                  -- d1://... this store replicates to
  last_acked_seq INTEGER NOT NULL,
  d1_epoch_expected INTEGER,                  -- D1 embedding_change_epoch after our last ack
  trigger_version INTEGER NOT NULL,
  halted_reason TEXT,                         -- non-NULL = stopped, needs an operator
  halted_at TEXT
);
```

The trigger template is instantiated once per (table, action). `<T>` is the
table, `<A>` is INSERT, UPDATE or DELETE, `<R>` is `NEW` (or `OLD` for
DELETE), and `<op>` is `'U'` (or `'D'` for DELETE):

```sql
CREATE TRIGGER trg_sync_<T>_<a> AFTER <A> ON <T> [WHEN <filter>]
BEGIN
  INSERT INTO sync_outbox(tbl, op, pk) VALUES ('<T>', <op>, json_array(<R>.<pk cols>));
END;
```

| table | pk cols | filter (all actions) |
|---|---|---|
| memories | id | — |
| memories_embeddings | memory_id | — |
| memories_crossrefs | memory_id | — |
| tombstones | content_hash, memory_id | — |
| tombstone_components | memory_id | — |
| memories_actions | id | — |
| memories_meta | key | `<R>.key NOT IN ('embedding_change_epoch','embedding_rebuild_lease','embedding_integrity')` |

That is 21 triggers (7 tables × 3 actions).

UPDATE triggers also fire an extra `'D'` for `OLD` when a pk column changes:
`WHEN OLD.<pk> IS NOT NEW.<pk>` (for a composite pk, any column differs). That
adds a second UPDATE trigger per table, `trg_sync_<T>_update_pk`, for 28 in
total. The code never changes a pk, but a manual fix
could.

The `memories_meta` exclusions are needed:
- **`embedding_change_epoch`**: D1 keeps its own epoch through its own
  `trg_embedding_epoch_*` triggers. The foreign-writer check (§2.6) depends on
  that value.
- **The rebuild lease** (`embedding_rebuild_lease`, from embeddings.py
  `_REBUILD_LEASE_KEY`): it is process-local.
- **`embedding_integrity`** (`_INTEGRITY_KEY`): its stamp is bound to the
  local epoch, which D1 does not share. Rollback recertifies D1 instead (§5.3).

The exclusion also keeps every write's own epoch bump out of the outbox.

These tables are NOT replicated:
- `memories_fts`: D1 has no FTS5 (`_fts_enabled`).
- `memories_events` (as briefed); adding it later is a version bump.
- `memories_embedding_repairs`, `absorb_inflight`, `import_lease`: local
  bookkeeping.

The outbox stores only the pk. Row contents are read when the row is sent
(§2.3), which is what makes a replay idempotent.

## 2. Replicator (`memora/replicator.py`, new)

### 2.1 Shape

```python
class StoreReplicator:
    def __init__(self, name: str, local: LocalSQLiteBackend, replica: D1Backend, *,
                 batch_rows: int = 100, poll_s: float = 5.0) -> None
    def start(self) -> None          # daemon thread "memora-replicator-<name>"
    def stop(self, drain_timeout_s: float = 0.0) -> None
    def wake(self) -> None
    def status(self) -> dict         # §2.5; in-memory plus one local read, never a D1 call
def start_replicators(registry: dict) -> dict[str, StoreReplicator]
def replicator_for(name: str) -> StoreReplicator | None
```

- There is one thread per store whose backend is local and whose
  `sync_state` row exists. It runs in-process in `memora-all` and is started
  in `server.main` next to the `_startup_import_sweep` thread
  (server.py:3550).
- It is started only when `MEMORA_REPLICATION=1`. That flag keeps it dark.
  Without the flag, the outbox grows and nothing is sent; the lag metric
  shows it.

### 2.2 Wake and ordering

- `backends._LockedWriterConnection.commit()` gains one post-commit call:
  `_notify_commit(db_path)`. That sets a per-path `threading.Event` in a
  module registry, and the replicator waits on that Event.
- A `poll_s` timeout fallback catches commits from other processes (scripts,
  sqlite3 CLI). Each cycle:
  1. On a short-lived reader connection, run:
     `SELECT seq, tbl, op, pk FROM sync_outbox WHERE seq > :acked ORDER BY seq LIMIT :n`.
  2. Coalesce the rows on `(tbl, pk)`, keeping the highest seq.
  3. For each key, read the current local row by pk. If the row exists, send
     an upsert; if not, send a delete. The `op` column is advisory: the row
     state at send time decides.
  4. Close the local connection **before** any HTTP call, so the
     `_StoreRWLock` is never held across the network.
- Invariant: after a batch is acked up to seq `s`, each key in it holds the
  local row as of some time ≥ the commit of `s`. D1 converges fully once the
  outbox is drained. Intermediate D1 states can mix row versions from several
  moments; that is acceptable for a replica. Rollback always drains first
  (§5.3).

### 2.3 Statements (explicit ids, full rows)

Column lists come from `PRAGMA table_info` on the local store, read once per
cycle, so a schema addition flows through. The statements are:

- **memories, memories_crossrefs, tombstones, tombstone_components,
  memories_actions, memories_meta**: an upsert of every column, including
  the pk:
  `INSERT INTO <T>(cols) VALUES (...) ON CONFLICT(<pk>) DO UPDATE SET c=excluded.c, ...`
- **memories_embeddings**: `DELETE ... WHERE memory_id=?` followed by
  `INSERT (all cols)`, never an `ON CONFLICT UPDATE`.
  - Why: D1's `trg_embedding_external_update` fires on
    `UPDATE OF embedding WHEN NEW.writer_token IS OLD.writer_token`, and it
    would null the replicated `representation`.
  - A plain INSERT fires only `trg_embedding_external_insert`
    (`WHEN writer_token IS NULL`). In that case the local row carries the same
    nulls, so the replica stays identical.
- **Delete**: `DELETE FROM <T> WHERE <pk>=?`.
- Explicit ids advance D1's `sqlite_sequence` for `memories` and
  `memories_actions`, so a rollback to D1 continues the id space.

### 2.4 Batching and the ack

This depends on what D1 guarantees:
- The REST `POST .../d1/database/{id}/query` accepts
  `{"batch": [{sql, params}, ...]}`, and a single `sql` with `;`-joined
  statements "executed as a batch"
  (https://developers.cloudflare.com/api/resources/d1/subresources/database/methods/query/).
- The REST docs state NO atomicity or rollback guarantee.
- The Workers binding does document one: "Batched statements are SQL
  transactions ... aborts or rolls back the entire sequence"
  (https://developers.cloudflare.com/d1/worker-api/d1-database/). That is not
  the API we call.

Decision:
- Send one REST `batch` of at most `batch_rows` coalesced keys (roughly 2 to
  4 statements each). Correctness never depends on atomicity: every statement
  is idempotent, and a partly applied batch is simply resent whole.
- A new `D1Connection.execute_batch(stmts: list[tuple[str, tuple]]) -> list[dict]`
  posts that body through the existing keep-alive transport. It is not
  `retry_safe`; the replicator owns retries.
- If the `batch` body is rejected (HTTP 400 on the format), fall back to one
  statement per `/query`.
- The ack happens only when HTTP 200 comes back and every result has
  `success: true`. Then one local `BEGIN IMMEDIATE` runs
  `UPDATE sync_state SET last_acked_seq=:max_seq, d1_epoch_expected=:post` and
  `DELETE FROM sync_outbox WHERE seq <= :max_seq`. That is the pruning; there
  is no separate pruning job.
- After the ack, `cloud_sync.schedule_sync()` is called: `/broadcast` goes
  out after the D1 ack, not after the local commit.
- Any other outcome (a transport error, a 5xx, a partial `success`, a timeout
  or an unknown result) sends no ack. The batch is resent from `:acked` with
  backoff of 1, 2, 4 … 60 s.

### 2.5 Metrics: `/health/db/<name>`

`health._probe_one(name)` adds a `replication` block, from
`replicator_for(name).status()`, to each store that has a `sync_state` row:
- `status`: `running`, `backoff`, `halted` or `disabled`;
- `head_seq` and `last_acked_seq`;
- `lag_rows`;
- `oldest_unacked_age_s`: `now - MIN(created_at)` of the unacked rows;
- `last_ack_at` and `last_error`;
- `halted_reason`.

Store readiness (200/503) is **not** tied to replication: local serving is
healthy while D1 lags. `_redact` keeps only `status` and `lag_rows` for
unauthorised callers.

Alerting goes through `scripts/memora_watchdog.py`, which gains a check that
alerts on either of these:
- `oldest_unacked_age_s > 300` (the design's 5-minute alert);
- `status == halted`.

Target: p95 lag under 10 s.

### 2.6 Foreign-writer epoch check

Every batch is wrapped like this:
- first statement: `SELECT value FROM memories_meta WHERE key='embedding_change_epoch'`
  (this reads `pre`);
- the batch itself;
- last statement: the same SELECT (this reads `post`).

Rules:
- If `pre != d1_epoch_expected`, something else wrote D1. The replicator
  writes `halted_reason='foreign_writer: expected E got P'` and `halted_at`,
  stops sending, and logs at ERROR; the watchdog then alerts.
  - The batch's writes may already have been applied, because atomicity is
    undocumented. That is harmless: it is our data, and it is idempotent.
  - Nothing more is sent until the operator runs
    `scripts/local_primary.py resume <db> --accept-d1-epoch`, after a shadow
    compare (§5.2).
- When the previous attempt ended with an **unknown** outcome, our own
  partial writes may have moved the epoch. On that retry only, the check is
  relaxed to `pre >= expected`, and `epoch_unverified_batches` is counted in
  status.
- Residual risk, stated plainly: two kinds of foreign write go undetected by
  the epoch check:
  - one interleaved *inside* a batch;
  - one coinciding with a failed-unknown batch.
  The nightly shadow compare (§5.2) and token rotation (§6) cover these.

### 2.7 Outage and restart

- **During an outage**, local writes are unaffected (async). The outbox
  grows, and it stores pk only, about 50 B a row. The replicator backs off;
  lag and age rise and alert at 5 minutes.
- **On recovery**, it drains in seq order, `batch_rows` at a time.
- **On restart**, it resumes from `sync_state.last_acked_seq`. A crash
  between the D1 commit and the local ack replays the same batch, which is
  idempotent.
- On a halt, `halted_reason` persists across restarts.

### 2.8 Optional flag: synchronous commit (not designed around)

The user chose async. This flag stays only because it is trivial:
- `MEMORA_REPLICA_SYNC_WAIT_S=<n>` makes `_notify_commit` wait, after the
  commit and outside every lock, until `last_acked_seq >= MAX(seq)` or `n`
  seconds pass. The default is 0, which means off.
- It never fails the write; a timeout only logs.

## 3. Absorb on a transactional backend

A capability test goes in storage.py:
`_has_transactions(conn) -> bool: return not isinstance(conn, D1Connection)`.
It is the same test `import_memories` already uses (storage.py:10067, which
chooses `_import_write_transactional` or `_import_write_d1`). The import
path therefore needs no change.

Branch points in `_absorb_memory_impl` (line numbers @3d03123). Where the
store has transactions:

| line | D1 machinery | transactional behaviour |
|---|---|---|
| 7309-7312 | `absorb_nonce` minted; `_begin_absorb_inflight` | nonce still minted (metadata provenance); `_begin_absorb_inflight` skipped |
| 7112, 7129, 7602 | `_touch_absorb_inflight` heartbeats | skipped |
| 7923 | `_complete_absorb_inflight` | skipped |
| 7935-7990 | except path: `_recover_absorb_owned_ids`, compensating `delete_memory(require_absorb_nonce=)` loop, orphan `_touch_absorb_inflight` | replaced by `conn.rollback()`; `owned_ids` reported as rolled back, `counts` zeroed as today |
| 7621, 7762 | compensate the new row when its component retired at the boundary / after link | kept as-is. Inside the transaction it is a plain delete that rolls back with everything else. No concurrent retirement can land mid-transaction, so these become unreachable in practice but stay correct |
| 7745 | `_heal_supersession_fork` | kept. With phase 3 serialized, no concurrent fork can form, so it finds nothing to heal |
| 3620/3696 | `_cas_store_crossrefs` CAS loop in `_upsert_crossref_edge` | kept. The first CAS always succeeds under the write lock; that is one loop iteration, and no branch is needed |
| 641, 687 | `list_absorb_inflight`, `reconcile_dead_absorbs` | return `[]` / a no-op result on a transactional conn |
| 4282/4288 | `_after_absorb_resolve`, `_before_absorb_supersede_links` test hooks | unchanged; they fire inside the transaction |

`absorb_operation_key` stays, because it is idempotency across calls, not
compensation.

### Phase 3 in one `BEGIN IMMEDIATE`

- Phase-3 prep (from line 7405) already computes every storage vector before
  any write. Embedding calls are therefore outside by construction.
- Phase 3 also calls LLMs at the write boundary:
  `_absorb_partition_targets` (7504, 7657, 7728) runs the supersede gate
  `_verify_absorb_supersede_llm` on leaves re-resolved there. Transactional
  mode splits it:
  1. **Outside** (before BEGIN), `_resolve_absorb_supersedes_target` runs for
     every `supersedes` job, and the gate runs on each resolved leaf. The
     verdicts are kept in a dict keyed by `(job, leaf_id)`.
  2. **Inside** `BEGIN IMMEDIATE`, re-resolution runs again. It reads only
     locally and makes no LLM call.
     - If every resulting leaf has a verdict, the phase-3 body runs unchanged,
       with the verdicts supplied by a new `gate_verdicts=` parameter on
       `_absorb_partition_targets`.
     - If some leaf has no verdict (another writer committed between steps 1
       and 2), the transaction runs `ROLLBACK`, those leaves are gated
       outside, and the whole step repeats, at most 3 times.
     - After that, any leaf still without a verdict is treated as `rejected`:
       the existing kept-leaf path, with no supersede, a fork kept and a
       decision logged.
- Every write in the body already takes `commit=False` (`add_memory`,
  `add_link`). `delete_memory` and `update_memory` get audited in L4 for
  inner commits; any inner commit becomes conditional on
  `not conn.in_transaction`.
- Line 7924 `conn.commit()` is the single commit, followed by
  `invalidate_corpus_cache` as today.

### SQLite settings and the read path

- `LocalSQLiteBackend.connect()` sets `PRAGMA journal_mode=WAL` once per file
  and `PRAGMA busy_timeout=5000` on every writer connection.
- "Single writer": writer connections are thread-affine
  (`_ThreadCheckedConnection`), so one shared connection cannot serve
  request threads. Instead, a process-wide per-path `threading.Lock` is
  added: `store_write(conn)`, a context manager in backends.py. It takes the
  lock, runs `BEGIN IMMEDIATE`, and commits (or rolls back) on exit. Absorb
  phase 3, `_import_write_transactional` and the replicator's ack use it.
  Other writes are single short statements; for those, and for writers
  outside the process (scripts, the sqlite3 CLI), `busy_timeout` is the
  serializer.
- The replicator's ack transaction (via `store_write`) takes the same lock.
  It holds it for milliseconds; there is no network I/O inside.
- The api_v1 read path is unchanged. `connect_read_only` takes the shared
  side of `_StoreRWLock` only at open and close, and WAL readers never block
  on the phase-3 transaction: they see the last committed snapshot.
- Neither the replicator nor absorb holds a `_StoreRWLock` side across an LLM
  call or an HTTP call.
  - The absorb connection is opened before phase 1, as today. Opening and
    closing a connection is when the lock is taken, so a writer that stays
    open does not block reads.
  - The only cost is a writer open (the exclusive side) waiting for open
    readers to close. That is existing behaviour.

## 4. Seed and restore scripts (`scripts/local_primary.py`, one CLI)

- **`seed <db> --from-d1 <d1-uri> --out /data/<db>.db`**
  1. Export D1 with `wrangler d1 export <d1-name> --remote --output <file>.sql`
     (schema and data).
  2. Load it into a fresh file.
  3. Run `ensure_schema`, which adds FTS and rebuilds it from `memories`.
  4. Create `sync_outbox` and `sync_state`, install the triggers, and set
     `last_acked_seq = 0` with an empty outbox. The head is 0, because
     nothing local is unreplicated yet.
  5. Set `d1_epoch_expected` to D1's epoch **as read at export time**.
     If D1 moves before cutover, the first batch halts with foreign_writer.
     That is intended: the freeze (§6) must come before the seed.
  6. Verify with the per-id hash compare (§5.2): zero diffs required.
- **`restore <db> --from {r2:<key>|d1}`**
  1. Stop the replicator (`MEMORA_REPLICATION=0`, restart).
  2. Fetch the R2 snapshot, or re-seed from D1.
  3. Run `PRAGMA integrity_check`, then `ensure_schema` (FTS rebuild).
  4. When restoring from R2, D1 may be *ahead* of the snapshot (rows
     replicated after the snapshot). Run the shadow compare and pull the
     D1-only ids back (`--pull-d1-ahead`) before the replicator starts.
  5. Set `last_acked_seq` to the head and `d1_epoch_expected` to D1's
     current epoch.
  - `--rehearse` runs the whole procedure into a temp path and reports
    counts and diffs, with no swap.
- **`snapshot <db> --r2-bucket <b>`** runs `sqlite3 .backup` (the online
  backup API: `sqlite3.Connection.backup`) to a temp file, gzips it and
  uploads to `r2://<b>/<db>/<YYYY-MM-DD>.db.gz`, keeping 14. It runs nightly
  from cron on nuc8.
- **`resume <db> --accept-d1-epoch`** clears `halted_*` and sets
  `d1_epoch_expected` to D1's current value.
- **Volume alert**: the watchdog checks the `/data` filesystem and alerts at
  80% used. `snapshot` refuses to run below 2× the DB size free.

## 5. Migration plumbing

1. **Mixed URIs.** `MEMORA_DATABASES` already accepts mixed backends
   (`database_registry()`, `backend_for`). Cutover for one store is a config
   change:
   - `"bestation": "sqlite:///data/bestation.db"`;
   - plus `MEMORA_REPLICAS='{"bestation": "d1://…"}'`, read by
     `start_replicators`. The value must equal `sync_state.replica_uri`, or
     the replicator refuses to start.
   Stores that are not cut over stay `d1://`.
2. **Shadow compare**: `local_primary.py compare <db>`, nightly.
   - It computes a per-id hash for `memories`: `content`, `metadata`, `tags`,
     `created_at`, `updated_at`, the embedding blob, crossrefs, and whether
     the id is tombstoned (`content_tombstone_hash`).
   - It runs this on both sides, in chunks by id range: only mismatching
     ranges are re-read in full.
   - It compares only ids with `id <= max id in acked rows`, so in-flight
     writes do not show as diffs.
   - It exits non-zero on any diff; the watchdog alerts.
3. **Rollback**: `local_primary.py rollback <db>`.
   1. Freeze local writes: set the store read-only through
      `MEMORA_READONLY_DBS`. A new registry flag makes `connect()` raise.
   2. Drain until `lag_rows == 0`.
   3. Run `compare`, which must return zero diffs.
   4. Recertify embedding integrity on D1: run `verify_embedding_integrity`
      against the `d1://` backend, which restamps `embedding_integrity` for
      D1's epoch.
   5. Repoint `MEMORA_DATABASES[db]` to `d1://`, remove the entry from
      `MEMORA_REPLICAS`, and restart.
   6. Restore D1 write scope to the tokens that need it.

## 6. Writer freeze checklist

| # | step | owner | done when |
|---|---|---|---|
| F1 | memora-graph viewer read-only. Disable the D1-writing Pages functions: `functions/api/chat.ts` (INSERT/UPDATE/DELETE on memories, memories_embeddings, memories_crossrefs) and `functions/api/memories/[id].ts` (PATCH metadata/tags). Before merging, grep the repo (`env.DB.prepare\|\.run()\|\.batch(` on `INSERT\|UPDATE\|DELETE\|REPLACE`) for writers missed by the design audit; return 405 from the write handlers, hide the edit UI | leader (memora-graph repo) | grep shows no write SQL; deploy returns 405 |
| F2 | Pages D1 binding switched to a read-only use: the Pages binding cannot be scoped, so F1's grep plus a CI grep guard enforce it | leader | CI guard merged |
| F3 | retire `memora-graph/scripts/sync-to-d1.py` / `sync.sh` (`wrangler --replace`) | leader | removed or exit-1 with a pointer here |
| F4 | Mac `~/.config/memora/credentials.mcp.json` → nuc8's HTTP endpoint; no `d1://`, no `CLOUDFLARE_API_TOKEN` | user (Mac) | file audited |
| F5 | every `.mcp.json` / `credentials*.mcp.json` on every host (ob1, bestation, re; REVERT.md lists 4) → nuc8 endpoint | user, per host | audit output attached |
| F6 | rotate the Cloudflare API token the old configs use. Mint ONE token with D1 edit scope for the nuc8 replicator only; the viewer keeps its binding (read-only by F1/F2) | user (CF dashboard) | old token revoked; a stale client fails with 401/403 |
| F7 | `cloud_sync.schedule_sync` stays: broadcast only, now after the ack | worker (L3) | — |

F1 is swappable. If the viewer later needs edits, F1 becomes "route writes
to a nuc8 endpoint", and nothing else in this plan changes. F1 to F6 must be
done before the first `seed` (§4).

## 7. Test strategy (offline: local SQLite plus FakeD1)

FakeD1 is the existing test double for `D1Connection`. It gains:
- `batch` support;
- failure injection: fail before apply, apply-then-lose-response, and
  apply half the batch;
- its own copies of the epoch and external-embedding triggers.

The property-style tests use `hypothesis` if it is in the dev deps; if not,
a seeded random op generator run 200 times. Each generates a random sequence
of add, update, delete, link, tombstone and meta writes.

Tests:
- `test_outbox_order_matches_commit_order`: seq is monotonic per commit, and
  a rolled-back transaction leaves no outbox rows.
- `test_replay_idempotent`: replaying any acked prefix twice leaves FakeD1
  identical.
- `test_crash_between_d1_commit_and_ack`: kill after FakeD1 applies and
  before the local ack; after restart the stores converge, and the epoch
  check passes on the relaxed retry.
- `test_partial_batch_resent_whole`.
- `test_outage_backlog_drains_in_order`: N=5000 rows during an outage, then
  recovery; lag metrics fall to 0.
- `test_foreign_writer_halts`: FakeD1 is written directly between batches;
  the replicator halts, `halted_reason` persists across restart, and `resume`
  clears it.
- `test_meta_exclusions_not_enqueued`.
- `test_embedding_replica_keeps_representation`: guards the §2.3
  DELETE+INSERT choice.
- `test_d1_store_has_no_sync_objects`.
- `test_trigger_version_upgrade`.
- `test_no_sync_state_no_triggers`.
- `test_final_state_equal`: per-id hash equality after every generated
  sequence.

Absorb tests:
- `test_absorb_txn_rollback_leaves_no_rows`: inject failure at each hook
  point; no memories, crossrefs, tombstones or outbox rows remain, and there
  is no `absorb_inflight` row.
- `test_absorb_txn_no_llm_inside`: the gate and embed callables assert
  `not conn.in_transaction`.
- `test_absorb_regate_on_new_leaf`: another writer adds a leaf between the
  pre-gate and BEGIN; the transaction rolls back, the leaf is gated, and the
  step retries.
- `test_absorb_d1_path_unchanged`: the existing D1 absorb suite runs
  unchanged.
- `test_reader_sees_pre_txn_snapshot`: during phase 3.

Every slice's report lists the mutation checks for its guards.

## 8. Slice plan

In the table, "Dark" means nothing changes for any store without
`sync_state`, and the flag defaults to off. The rows column is live rows
touched at merge time.

| slice | content | dark by | rows |
|---|---|---|---|
| L2 | §1: `_ensure_sync_outbox`, trigger DDL and versioning, and the seed-only installer function `install_sync(conn, replica_uri, d1_epoch)`. Tests: outbox order, exclusions, D1-inert, version upgrade | no `sync_state` anywhere | 0 |
| L3 | §2: `replicator.py`, `D1Connection.execute_batch`, commit notify, epoch check, halt/resume, health block, watchdog check, broadcast-after-ack, FakeD1 batch and failure injection. Property tests | `MEMORA_REPLICATION` unset | 0 |
| L4 | §3: `_has_transactions` branches, pre-gate/in-transaction re-resolve, WAL/busy_timeout, `store_write`, inner-commit audit. Absorb transaction tests | only reachable on local stores; none serve prod | 0 |
| L5 | §4: `local_primary.py` seed/restore/snapshot/resume, rehearsed against a FakeD1-backed export fixture | a script, run by hand | 0 |
| L6 | §5: compare, rollback, `MEMORA_REPLICAS`, `MEMORA_READONLY_DBS` | config unset | 0 |
| L7 | §6 F1–F3 in memora-graph (viewer read-only) | viewer deploy | 0 (removes writers) |
| L8 | §6 F4–F6 plus the audit script `local_primary.py audit-configs` | ops | 0 |
| L9 | cutover of `re`, then `bestation`: rehearse restore, then seed, then repoint, then 48 h of shadow compares at zero diffs | per store | whole store (bestation first if smaller) |
| L10 | cutover of `ob1` | per store | whole store |
| L11 | cutover of `memora`; nightly R2 snapshot enabled for all | per store | whole store |

Order: L2 first. L3 to L6 then merge in any order; L3 and L5 need L2's
`install_sync`. L7 and L8 gate L9. Every cutover is reversible by §5.3
until the next store's cutover begins.
