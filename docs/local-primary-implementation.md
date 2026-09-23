# Local-primary: implementation plan

This plan implements `plans/nuc8-local-primary-design.md`. It is based on dev
at 3d03123. Line numbers refer to that commit.

The user's decisions (leader msgs 7507, 7520):
- Replication is async for every store.
- The memora-graph viewer becomes read-only.
- nuc8 `memora-all` becomes the only D1 writer.
- **D1 is precious.** Today it is the only complete copy of every store, so
  §0 overrides every other section.

Nothing runs against a live store before its cutover slice. All tests are
offline, using local SQLite and FakeD1 (`tests/conftest.py`).

## 0. D1 data protection (binding on every slice)

- **P1. Verified export before every D1-affecting step.**
  - `scripts/local_primary.py export <db>` runs
    `wrangler d1 export <name> --remote --output` and writes the result to
    `/data/exports/<db>/<ts>.sql` and to `r2://<bucket>/exports/<db>/<ts>.sql`.
  - It checks the export: the file is loaded into scratch SQLite, and its
    per-table row counts and content hashes must equal the same values read
    from D1 right after the export. The content hash is sha256 over rows
    ordered by pk. On a mismatch it retries up to 3 times, then fails.
  - It writes a receipt, `<ts>.receipt.json`, containing the db, the D1
    `database_id`, per-table counts and hashes, the sha256 of the SQL file,
    the R2 key and `verified_at`.
  - A fresh receipt is required for EVERY store before the first replicator
    write reaches D1. A receipt for the affected store is required before
    each cutover, rollback, restore and sequence high-water step. Each of
    these takes `--receipt`, and refuses if the receipt is missing,
    unverified, for another database, or older than 24 h.
- **P2. Statement allow-list.** `replicator._build_statements` is the only
  producer of the statements the replicator sends to D1. It emits exactly
  four shapes:
  - an UPSERT of one row with a full pk (§2.3);
  - `DELETE FROM <t> WHERE <every pk col> = ?`;
  - `SELECT` by full pk (the read-back);
  - the epoch `SELECT`.
  `_check_statement(sql)` re-parses every statement with a fixed regex set
  before sending. It rejects anything else: DROP, TRUNCATE, a DELETE or
  UPDATE without the full pk predicate, `sqlite_sequence`, and any table
  outside the 7 in §1. A rejection halts the store. A mutation test removes
  the check and must fail the build.
- **P3. Deletion guard.** Before sending, the replicator counts the batch's
  net deletes per table. A net delete is a DELETE not followed by a re-INSERT
  of the same key; the §2.3 embedding DELETE+INSERT pair does not count.
  - If deletes exceed 50, or exceed 1% of the table's local row count, the
    replicator does not send. It halts the store with
    `halted_reason='delete_guard: <table> n/N'` and raises an alert.
  - `local_primary.py resume <db> --allow-deletes <attempt_id>` overrides it,
    for that one attempt only.
  - Tables under 100 rows therefore halt on any delete; that is deliberate.
  - The same guard applies to restore replay (§4).
- **P4. Log-only first.** `MEMORA_REPLICATION=log` builds, checks and logs
  every statement (JSONL, `/data/replica-log/<db>/`). It sends nothing to D1
  and acks nothing: a `log_cursor_seq` in `sync_state` avoids logging twice.
  - The first migrated store runs log-only for at least 7 days. For that
    week its D1 copy and the viewer are stale; the store's durability is the
    local file plus the nightly R2 snapshot (§4).
  - The §5.2 compare runs nightly by replaying the logs into a scratch copy
    of the store's latest verified export and comparing that copy with the
    local snapshot. Writes (`MEMORA_REPLICATION=write`) are enabled only after
    7 consecutive clean nights.
- **P5. Order.** Least critical first; `memora` last. Each store waits until
  the previous one has completed a clean week with writes enabled (§8).
- **P6. Old tools inert before L2.** `sync-to-d1.py` (every remote run, not
  only `--replace`), `link-r2-images.py` and `setup-cloudflare.sh` (remote
  migration and Pages deploy) must exit 1 before slice L2 lands. The CI
  guard (F2) holds them there.
- **P7. No automatic D1 deletes by rollback or restore.** Any diff that would
  need a D1 delete is reported for a human decision (§4, §5.3).

§8 lists the D1 statements each slice can issue.

## 1. Schema: outbox, state, triggers (L2)

`_ensure_sync_outbox(conn)` goes in `memora/schema.py`. `ensure_schema` calls
it last, after `_ensure_import_lease_table`.
- On a `D1Connection` it returns at once. That makes it inert on `d1://`
  stores: no sync object is ever created in D1. It is cached like the rest of
  the schema, through `_backend_schema_signature`.
- It only maintains existing state, and it does nothing when a store has no
  `sync_state` table.
- When `trigger_version < SYNC_TRIGGER_VERSION` (starting at 1), it runs one
  `BEGIN IMMEDIATE` that drops every `trg_sync_*` trigger, recreates them
  and bumps the version.
- Replication is enabled only by `install_sync(conn, replica_uri, d1_epoch)`,
  which only the seed script calls (§4).

```sql
CREATE TABLE sync_outbox (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  tbl TEXT NOT NULL,
  op  TEXT NOT NULL CHECK (op IN ('U','D')),   -- advisory; row state at send time decides
  pk  TEXT NOT NULL,                           -- json_array(pk cols)
  created_at REAL NOT NULL DEFAULT (julianday('now'))
);
CREATE TABLE sync_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  replica_uri TEXT NOT NULL,
  last_acked_seq INTEGER NOT NULL,
  log_cursor_seq INTEGER NOT NULL DEFAULT 0,   -- P4
  d1_epoch_expected INTEGER,
  trigger_version INTEGER NOT NULL,
  inflight_id TEXT, inflight_lo INTEGER, inflight_hi INTEGER,
  inflight_epoch_before INTEGER, inflight_at TEXT,  -- H3 marker
  halted_reason TEXT, halted_at TEXT
);
-- template, per (table, action); R = NEW, or OLD for DELETE; op = 'U', or 'D' for DELETE
CREATE TRIGGER trg_sync_<T>_<insert|update|delete> AFTER <A> ON <T> [WHEN <filter>]
BEGIN INSERT INTO sync_outbox(tbl, op, pk) VALUES ('<T>', <op>, json_array(<R>.<pk>)); END;
CREATE TRIGGER trg_sync_<T>_update_pk AFTER UPDATE ON <T> WHEN OLD.<pk> IS NOT NEW.<pk>  -- any pk col
BEGIN INSERT INTO sync_outbox(tbl, op, pk) VALUES ('<T>', 'D', json_array(OLD.<pk>)); END;
```

The seven tables:

| table | pk | filter |
|---|---|---|
| memories | id | — |
| memories_embeddings | memory_id | — |
| memories_crossrefs | memory_id | — |
| tombstones | content_hash, memory_id | — |
| tombstone_components | memory_id | — |
| memories_actions | id | — |
| memories_meta | key | `<R>.key NOT IN ('embedding_change_epoch','embedding_rebuild_lease','embedding_integrity')` |

That makes 28 triggers. The three `memories_meta` exclusions:
- `embedding_change_epoch`: D1 keeps its own, and §2.6 depends on it.
  Excluding it also keeps each write's epoch bump out of the outbox.
- `embedding_rebuild_lease` (`_REBUILD_LEASE_KEY`): it is process-local.
- `embedding_integrity`: its stamp is bound to the local epoch. Rollback
  recertifies D1 instead (§5.3).

Not replicated:
- `memories_fts`: D1 has no FTS5.
- `memories_events`: can be added later with a version bump.
- `memories_embedding_repairs`, `absorb_inflight`, `import_lease`.

**Connection capabilities and read policy (M10, also L2):**
- **`supports_transactions`** is a class attribute: True on
  `_LockedWriterConnection`, and False on `D1Connection`, CloudSQLiteBackend
  connections and `_LockedReaderConnection`.
- **`LocalSQLiteBackend.live_primary`** is True when the store is named in
  `MEMORA_REPLICAS`, so it stays dark until configured. On a live primary:
  - `connect_read_only` never takes the `mode=ro&immutable=1` branch; it
    always uses `mode=ro` with WAL.
  - The replicator's anchor writer (§2.2) keeps `-wal`/`-shm` present, and
    missing sidecars raise the existing `StoreLockedError`.
  - `memora-all` holds `fcntl.flock` on `/data/<db>.db.primary-lock`.
    Scripts that write, or that need a still file (`local_primary.py`,
    `apply_backfill_47.py`), take that lock non-blocking and refuse if it is
    held. So no external writer runs while the service runs; maintenance goes
    through the API or a stopped service.
- **`MEMORA_READONLY_DBS`** (a list of names): `LocalSQLiteBackend.connect()`
  raises `StoreReadOnlyError`. `connect_replicator()` bypasses it; it is the
  replicator's only entry point, and a test asserts no other module calls it
  (H6).

## 2. Replicator (`memora/replicator.py`, L3)

```python
class StoreReplicator:
    def __init__(self, name: str, local: LocalSQLiteBackend, replica: D1Backend, *,
                 mode: Literal["log", "write"], batch_rows: int = 100, poll_s: float = 5.0) -> None
    def start(self) -> None; def stop(self) -> None; def wake(self) -> None
    def status(self) -> dict                   # never calls D1
def start_replicators(registry: dict) -> dict[str, StoreReplicator]
def replicator_for(name: str) -> StoreReplicator | None
def _build_statements(tbl: str, pk: list, row: dict | None) -> list[tuple[str, tuple]]   # P2
def _check_statement(sql: str) -> None                                                   # P2
```

### 2.1 Where it runs

There is one daemon thread per store that is local, named in
`MEMORA_REPLICAS` and has a `sync_state` row. It starts in `server.main`,
next to `_startup_import_sweep` (server.py:3550), only when
`MEMORA_REPLICATION` is `log` or `write`.

### 2.2 Loop

The thread owns one anchor writer from `connect_replicator()` for the life of
the process. Holding a writer open does not hold a `_StoreRWLock` side: only
open and close do. It wakes on `_notify_commit(db_path)`, a per-path
`threading.Event` set by `_LockedWriterConnection.commit()`, or after
`poll_s`. Each cycle:
1. Read `SELECT seq,tbl,op,pk FROM sync_outbox WHERE seq > :acked ORDER BY seq LIMIT :n`.
2. Coalesce on (tbl, pk), keeping the highest seq.
3. Read each key's current local row. A present row becomes an upsert; an
   absent row becomes a delete.
4. Run P2 and P3.
5. In **log** mode, append the statements to the log and advance
   `log_cursor_seq`.
6. In **write** mode:
   1. Write the H3 marker in a local transaction:
      `inflight_id = uuid`, `lo..hi`, `inflight_epoch_before = expected`.
   2. Run the preflight (§2.6).
   3. Send the batch (§2.4).
   4. Ack (§2.4).

Invariant: after an ack through seq `s`, each key in the batch holds its
local row as of some time at or after `s` committed. D1 converges when the
outbox has drained.

### 2.3 Statements

Column lists come from `PRAGMA table_info` on the local store, read once per
cycle.
- **Six tables:** `INSERT INTO <T>(all cols) VALUES (…) ON CONFLICT(<pk>) DO UPDATE SET c=excluded.c, …`.
- **`memories_embeddings`:** `DELETE … WHERE memory_id=?` followed by
  `INSERT (all cols)`, adjacent in the same batch.
  - This avoids D1's `trg_embedding_external_update`
    (`UPDATE OF embedding WHEN NEW.writer_token IS OLD.writer_token`), which
    would null `representation`.
  - `trg_embedding_external_insert` fires only when `writer_token IS NULL`,
    and then the local row is identical anyway.
- **Delete:** `DELETE FROM <T> WHERE <all pk cols>=?`.

Explicit ids advance D1's `sqlite_sequence` for any row that reaches D1. For
the one case that does not reach it, see §4 (sequence high-water).

### 2.4 Batching and ack

What D1 guarantees:
- The REST `POST …/d1/database/{id}/query` accepts
  `{"batch":[{sql,params},…]}`, but documents no atomicity:
  https://developers.cloudflare.com/api/resources/d1/subresources/database/methods/query/.
- Only the Workers binding documents `batch()` as a transaction:
  https://developers.cloudflare.com/d1/worker-api/d1-database/.

Decision:
- **Batch shape:** one REST batch of at most `batch_rows` keys, sent by the
  new `D1Connection.execute_batch(stmts) -> list[dict]`. It is not
  `retry_safe`. If the body is rejected with HTTP 400, it falls back to one
  statement per request.
- **No reliance on atomicity:** every statement is idempotent, and a partial
  batch is resent whole.
- **Ack:** only on HTTP 200 with every result `success:true`, and only when
  the trailing epoch SELECT returned `post`. The ack is one local
  `store_write`:
  - `last_acked_seq=:hi`;
  - `d1_epoch_expected=:post`;
  - clear the `inflight_*` marker;
  - `DELETE FROM sync_outbox WHERE seq <= :acked AND created_at < julianday('now') - 1`.
  Acked rows are kept for 24 h because §5.2 needs them.
- **After the ack:** `cloud_sync.schedule_sync()`, which sends `/broadcast`.
- **Anything else** leaves the marker in place and triggers reconciliation
  (§2.7), with backoff of 1, 2, 4 … 60 s.

### 2.5 Metrics on `/health/db/<name>`

`health._probe_one` adds a `replication` block with:
- `mode`;
- `status`: running, backoff, halted or disabled;
- `head_seq`, `last_acked_seq`, `log_cursor_seq`, `lag_rows`;
- `oldest_unacked_age_s`, `last_ack_at`, `last_error`, `halted_reason`;
- `epoch_unverified_batches`;
- `d1_missing_vectors`, taken from the last compare.

Store readiness does not depend on replication. `_redact` keeps only
`status` and `lag_rows`.

`scripts/memora_watchdog.py` alerts when any of these holds:
- `oldest_unacked_age_s > 300`;
- `status == halted`;
- `d1_missing_vectors > 0`.

Target: p95 lag under 10 s.

### 2.6 Foreign-writer epoch check (H2)

1. **Preflight**, a request of its own before any mutating request:
   `SELECT value FROM memories_meta WHERE key='embedding_change_epoch'`.
   If the value differs from `d1_epoch_expected`, the store halts with
   `foreign_writer: expected E got P`, and the watchdog alerts. Only
   `local_primary.py resume <db> --accept-d1-epoch` clears it, and only after
   a §5.2 barrier compare.
2. **Postcheck**: the batch's last statement is the same SELECT, and it
   gives `post`.

Stated limits:
- A foreign write between the preflight and the batch, or inside the batch,
  is not detected. The writer freeze and token rotation (§6) are the guards.
- The epoch moves only on writes to `memories` and `memories_embeddings`. For
  crossrefs, actions, tombstones and meta, the guards are token rotation and
  the §5.2 compare (M9).

### 2.7 Unknown outcome, outage and restart (H3)

The marker is persisted before each send. It is reconciled on the next cycle
in-process, and at startup whenever a marker is present:
1. Recompute the keys from the outbox for `inflight_lo..hi`. These rows are
   still present because they were not acked.
2. Read those keys back from D1 with pk SELECTs, and compare them with the
   current local rows.
3. If every key matches, ack as in §2.4. `d1_epoch_expected` becomes the D1
   epoch read now, and `epoch_unverified_batches` goes up by 1.
4. Otherwise, resend. The preflight is relaxed to
   `pre >= inflight_epoch_before`, for this attempt only.
5. Clear the marker.

During an outage, local writes are unaffected. The outbox grows at about
50 B per row, and alerts fire at 5 minutes. After an outage, the backlog
drains in seq order.

A halt persists across restarts.

**Degraded mirror (M9).** If a batch fails after an embedding DELETE and
before its INSERT, D1 lacks that vector until the batch is resent. If the
store is halted at that point, the gap persists. This is accepted:
- `compare` reports `d1_missing_vectors` and the watchdog alerts on it;
- the repair is `resume`, which resends through the normal path.

### 2.8 Synchronous commit (flag only)

`MEMORA_REPLICA_SYNC_WAIT_S`, default 0. `_notify_commit` waits, outside
every lock, for `last_acked_seq >= MAX(seq)`, up to that many seconds. It
never fails a write.

## 3. Absorb on a transactional backend (L4)

Absorb uses `_has_transactions(conn) = getattr(conn, "supports_transactions", False)`
(M10). `import_memories` keeps its own test (storage.py:10067), so
CloudSQLite import behaviour does not change.

Where `_has_transactions(conn)` is true:

| line | D1 machinery | transactional behaviour |
|---|---|---|
| 7309-7312 | `_begin_absorb_inflight` | skipped. The nonce is still minted (provenance) |
| 7112, 7129, 7602 | `_touch_absorb_inflight` | skipped |
| 7923 | `_complete_absorb_inflight` | skipped |
| 7935-7990 | `_recover_absorb_owned_ids`, compensating `delete_memory(require_absorb_nonce=)`, orphan touch | `conn.rollback()`; `counts` zeroed as today |
| 7621, 7762 | compensation when a component retired at the boundary or after link | kept: a plain delete inside the transaction |
| 3620/3696 | `_cas_store_crossrefs` loop | kept: the first CAS succeeds under the lock |
| 641, 687 | `list_absorb_inflight`, `reconcile_dead_absorbs` | `[]` / no-op |
| 4282/4288 | test hooks | fire inside the transaction |

### Phase 3 in one `store_write` (`BEGIN IMMEDIATE`)

No network call happens while the lock is held. Four sources were checked:
- **Embeddings.** Phase-3 prep (from line 7405) computes every storage
  vector before any write.
- **Supersede gate.** `_absorb_partition_targets` calls
  `_verify_absorb_supersede_llm` at 7504, 7657 and 7728.
  - Before BEGIN: for every `supersedes` job, run
    `_resolve_absorb_supersedes_target` and `_component_live_leaves` on the
    target. Gate every leaf found, and store the verdicts keyed by
    `(job, leaf)`.
  - Inside the transaction: re-resolve, and pass the verdicts through a new
    `gate_verdicts=` parameter.
  - If a leaf has no verdict: ROLLBACK, gate it outside, retry at most 3
    times. After that, the leaf is treated as rejected (a kept fork, with the
    decision logged).
- **Fork healing (H8).** The `may_collapse` callback (7707) in
  `_heal_supersession_fork`:
  - Under the lock, the new row's id is the store's maximum (AUTOINCREMENT,
    no concurrent insert). So `winner = max(contenders)` is always the new
    row.
  - Therefore the sibling branch (`loser == _new`, which makes an LLM call
    through `_absorb_check_sibling_pair`) is unreachable. In transactional
    mode it raises, which rolls the transaction back.
  - The `winner == _new` branch is reachable for a pre-existing fork in the
    component. It gates through `_absorb_partition_targets`, which reads the
    pre-computed verdicts. A missing verdict follows the same
    rollback-and-retry path.
- **Images (H8).** `_prepare_metadata(…, memory_id=)` uploads to R2
  (storage.py:940/1001/1026, only when `memory_id` is set). It is called
  after the INSERT (5646) and in `update_memory` (8418).
  - In transactional mode, the new `defer_images=True` stores metadata with
    the image sources as given, sets `metadata.images_pending = true`, and
    returns the pending list.
  - After the commit, `_apply_deferred_images(conn, memory_id, pending)`
    uploads and then updates the row in its own short `store_write`.
  - Visible intermediate state: the row carries unprocessed image sources
    until the follow-up commits. If the follow-up fails, the flag stays set,
    an ERROR is logged, and a startup sweep retries.

Other rules:
- Every write in the body uses `commit=False`. L4 audits `delete_memory` and
  `update_memory` for inner commits; any inner commit becomes conditional on
  `not conn.in_transaction`.
- There is a single commit at 7924, then `invalidate_corpus_cache`.

**SQLite settings:**
- `connect()` sets WAL once and `busy_timeout=5000` on every writer.
- `store_write(conn)` is a new context manager in backends.py. It takes a
  per-path process-wide `threading.Lock`, runs BEGIN IMMEDIATE, and commits
  or rolls back.
  - It is used by absorb phase 3, `_import_write_transactional` and the
    replicator's ack.
  - It sets a thread-local flag, `in_store_write`, which the no-network tests
    check.
  - Connections are thread-affine (`_ThreadCheckedConnection`), so a lock
    replaces "one shared connection".
- The api_v1 read path is unchanged. WAL readers see the last committed
  snapshot and never wait for phase 3.

## 4. Export, seed, restore, snapshot (`scripts/local_primary.py`, L5)

- **`export <db>`**: P1.
- **`seed <db> --receipt R --out /data/<db>.db`**
  1. Load the receipt's export into a fresh file.
  2. Run `ensure_schema`.
  3. Rebuild FTS explicitly: `DELETE FROM memories_fts; INSERT INTO memories_fts(rowid, content, metadata, tags) SELECT id, content, metadata, tags FROM memories`.
     L5 verifies this matches `_fts_upsert`'s form. `memories_fts` is a
     standalone fts5 table, created empty.
  4. Sequence high-water, local side:
     `sqlite_sequence.seq = max(local seq, D1 seq, max(id))` for `memories`
     and `memories_actions`. The D1 value is read by SELECT.
  5. Run `install_sync` with `last_acked_seq = 0` and `d1_epoch_expected`
     taken from the receipt.
  6. Run a §5.2 barrier compare, which must show zero diffs.
- **`restore <db> --receipt R`** (the default, H5) is a full re-seed from a
  new verified export. Unacked local writes at the time of a disk loss are
  lost; that was accepted in msg 7507.
- **`restore <db> --from-r2 <key> --receipt R`** is only for D1 corruption or
  operator error. It loads the snapshot, rebuilds FTS, then reconciles every
  table against D1, key by key:
  - The key has an outbox row in the snapshot with seq > the snapshot's
    `last_acked_seq`:
    - If the snapshot has the row, UPSERT it to D1, unless D1's
      `memories.updated_at` is newer (for child tables, the parent memory's).
      In that case keep D1 and report it.
    - If the snapshot lacks the row (a pending delete), report it; never
      delete (P7).
  - The key exists only in D1: pull it into local.
  - The key exists only in the snapshot, or differs, with no unacked row:
    report it, and change neither side.
  - `--dry-run` prints every decision.
  - Apply requires `--approve <report sha256>`, and P3 applies to the
    upserts.
- **Sequence high-water, D1 side** (H7): run before rollback and before
  restore, with a receipt.
  - First choice: `UPDATE sqlite_sequence SET seq=? WHERE name=? AND seq<?`.
    L5 checks whether D1 allows this on a throwaway D1 database (with leader
    approval), never on a live one.
  - Fallback, which needs `--confirm`: INSERT a sentinel `memories` row
    (`content='__memora_seq_sentinel__'`) at the high-water id, then DELETE
    it by that id. This is the only D1 delete outside the replicator, and it
    touches only the row it just inserted.
- **`snapshot <db>`**: `sqlite3.Connection.backup` to a temp file, gzip, then
  `r2://<bucket>/<db>/<date>.db.gz`. It keeps 14, runs nightly from cron,
  and refuses if free space is below 2× the DB size.
- **`resume <db> [--accept-d1-epoch | --allow-deletes <attempt>]`**.
- **`--rehearse`** on seed and restore runs into a temp path.

## 5. Migration plumbing (L6)

### 5.1 Mixed URIs

A cutover is a config change: `MEMORA_DATABASES` maps the store to
`sqlite:///data/<db>.db`, and `MEMORA_REPLICAS` maps it to its `d1://` URI.
The `MEMORA_REPLICAS` value must equal `sync_state.replica_uri`, or the
replicator does not start. Other stores stay on `d1://`.

### 5.2 Compare (H4)

It covers every replicated table, every column, and keys present on only one
side:
- `memories`;
- `memories_embeddings`, including `representation`, `dimension`,
  `encoding_source` and `writer_token`;
- `memories_crossrefs`, `memories_actions`, `tombstones`,
  `tombstone_components`;
- `memories_meta` minus the §1 exclusions.

Local rows come from one consistent `.backup` snapshot `S`.

Two modes:
- **Barrier** (cutover, rollback, `resume`):
  1. Freeze ingress (`MEMORA_READONLY_DBS`, restart).
  2. Drain to `lag_rows = 0`.
  3. Take `S`.
  4. Compare. Zero diffs are required.
- **Nightly**:
  1. Take `S`, and let `H` be `MAX(seq)` in `S`.
  2. Wait for `last_acked_seq >= H`. If that takes more than 30 minutes,
     skip and alert.
  3. Read D1.
  4. Let `K` be the live outbox keys with seq > H. Acked rows are retained
     for 24 h, so every key written after `S` is in `K`.
  5. Compare, excluding `K`.
  6. Retry each diffed key once, after a fresh drain. A diff that persists
     alerts.

  A key not in `K` had no local write after `S`, and it was acked. So D1
  must equal `S` for it, unless a foreign write happened.

In log mode (P4), the "D1" side is the latest export with the logged
statements replayed.

### 5.3 Rollback (H6)

1. Take an export receipt.
2. Freeze ingress (`MEMORA_READONLY_DBS`, restart). The replicator keeps
   draining through `connect_replicator()`.
3. Wait for `lag_rows = 0`.
4. Stop the service.
5. Run a barrier compare. Diffs that need a D1 delete are reported (P7).
6. Run the D1-side sequence high-water step.
7. Recertify D1: `verify_embedding_integrity` against the `d1://` backend.
8. Repoint `MEMORA_DATABASES` to `d1://` and remove the store from
   `MEMORA_REPLICAS`.
9. Start the service.

A test drains under `MEMORA_READONLY_DBS`.

## 6. Writer freeze checklist

Verified D1 write paths (grep of the repo, including `memora-graph/`, at
3d03123; confirmed by review 7518):

| path | writes | kind |
|---|---|---|
| `memora-graph/functions/api/chat.ts` `executeToolCall`: `create_memory` / `update_memory` / `delete_memory` | INSERT memories; UPDATE memories content, tags; DELETE memories_embeddings, then `db.batch` DELETE memories_crossrefs and memories | viewer-edit, caller `public/index.html:3828` |
| `chat.ts` `computeAndStoreEmbedding` | CREATE TABLE IF NOT EXISTS memories_embeddings; upsert embedding | viewer-edit |
| `functions/api/memories/[id].ts` `onRequestPatch` | UPDATE memories metadata, tags | viewer-edit, callers `index.html:1426/2136/2172`, `force-graph.html:317` |
| `memora-graph/scripts/sync-to-d1.py`, `sync.sh` | `wrangler d1 execute --file`: upserts on memories, crossrefs, actions, embeddings, meta; `--replace` first deletes 4 tables | sync tool (writes even without `--replace`) |
| `scripts/link-r2-images.py` `d1_query` (~81-129) | REST `/query` UPDATE memories.metadata unless `--dry-run` | other |
| `scripts/setup-cloudflare.sh` | `wrangler d1 execute --remote --file=migrations/0001_init.sql` (~126; the file does not exist); `wrangler pages deploy` (~198) | other |
| `package.json` `d1:migrate` (`wrangler d1 migrations apply memora-graph`, local by default), `deploy` (Pages) | a migration; a redeploy of the old write handlers | other |
| any memora process with a `d1://` URI (credentials files, `memora-instance.sh`, `apply_backfill_47.py`) | everything `storage.py` writes | memora client |

Not writers: the GET-only functions (`actions`, `databases`, `duplicates`,
`graph`, `memories`, `r2/[[path]]`), `worker/` (no D1 binding),
`graph-ui.yml` (`--local` only) and `scripts/measure_*` (FakeD1).

| # | step | owner | done when | slice |
|---|---|---|---|---|
| F3 | `sync-to-d1.py`, `sync.sh`, `link-r2-images.py`, the remote branch of `setup-cloudflare.sh`, and the `package.json` `d1:migrate`/`deploy` scripts exit 1 with a pointer here (P6) | worker | each exits 1; a test runs each | L1b |
| F2 | CI guard in `graph-ui.yml`. It fails on write SQL inside `.prepare(`/`.batch(`/`.exec(` under `memora-graph/functions/`, on `DB_MEMORA\|DB_OB1\|DB_BESTATION\|DB_RE` used with write SQL, on Python `requests.post` to `/d1/database/…/query`, and on `wrangler d1 execute --remote` / `migrations apply` without `--local`. An allow-list covers only `memora/backends.py` | worker | it fails on 3d03123 and passes after F1 and F3 | L1b (tools), L7 (handlers) |
| F1 | viewer read-only: `chat.ts` keeps search and answers but drops the 3 write tools and `computeAndStoreEmbedding`; `[id].ts` PATCH returns 405; the edit controls in `index.html` and `force-graph.html` are hidden or disabled; `test_tag_writes.mjs` is replaced by 405 and no-tool tests | worker, deploy by leader | the deployed viewer returns 405; no edit controls | L7 |
| F4a | from the Mac, over nuc8's endpoint: authenticate, and create and delete one memory in a `scratch` local store in the registry. D1 is never touched | user | receipt noted | L8 |
| F4 | repoint the Mac `~/.config/memora/credentials.mcp.json` to nuc8: no `d1://`, no `CLOUDFLARE_API_TOKEN` | user | `audit-configs` is clean | L8 |
| F5 | the same for every `.mcp.json` / `credentials*.mcp.json` on ob1, bestation and re (REVERT.md lists 4) | user | audit output | L8 |
| F6 | rotate the old Cloudflare token. One D1-edit token goes to the nuc8 replicator only; the viewer keeps its binding (read-only by F1/F2) | user | a stale client gets 401/403 | L8, after F4a–F5 |
| F7 | `cloud_sync.schedule_sync` stays, called after the ack | worker | — | L3 |

F1 is swappable: if the viewer later needs edits, it becomes "route writes
to nuc8", and nothing else changes.

## 7. Tests (offline, local SQLite + FakeD1)

FakeD1 gains:
- `batch` support;
- injectable failures: fail-before, apply-then-lose-response, half-apply;
- D1's epoch and external-embedding triggers.

Property tests use a seeded random op generator (200 runs), or `hypothesis`
if it is in the dev deps.

**Replication** (L3):
- outbox order equals commit order, and a rolled-back transaction leaves no
  row;
- replay is idempotent;
- partial batch resent;
- 5000-row outage backlog;
- foreign writer halts (preflight), and the halt persists;
- meta exclusions;
- embedding representation kept;
- final per-table equality after every generated sequence;
- **`test_replicator_killed_between_send_and_ack`**: a real subprocess on a
  file-backed FakeD1, with a `MEMORA_TEST_KILL_AFTER_SEND` hook that calls
  `os._exit`. Restart must reconcile by read-back (H3);
- **`test_statement_allow_list`**, plus a mutation that disables
  `_check_statement` (P2);
- **`test_delete_guard_halts`**: 51 deletes, and 2% of a 100-row table (P3);
- **`test_log_mode_sends_nothing`**: FakeD1 records zero requests (P4);
- **`test_log_replay_compare`**;
- **`test_drain_under_readonly_flag`** (H6);
- **`test_connect_replicator_single_caller`**.

**Schema and read policy** (L2):
- D1 store has no sync objects;
- trigger version upgrade;
- a store without `sync_state` gets no triggers;
- **`test_live_primary_never_immutable`** (M10);
- flock refusal.

**Absorb** (L4):
- rollback leaves no rows (memories, crossrefs, tombstones, outbox,
  absorb_inflight) at every hook;
- re-gate on a new leaf;
- D1 absorb suite unchanged;
- reader sees the pre-transaction snapshot;
- **`test_no_network_under_store_write`**: `image_storage.upload_image`, the
  LLM client and the embedding client are patched to raise when
  `in_store_write` is set (H8);
- deferred images applied, and the flag stays set on failure;
- pre-existing fork gated outside;
- the sibling branch raises.

**Seed and restore** (L5):
- **FTS search parity** between a seeded store and a store built with
  `add_memory`;
- **`test_sequence_high_water`**, the reviewer's reproduction: insert and
  delete the highest id locally inside one batch, so D1 never sees it; after
  the high-water step, the next D1 insert gets a higher id;
- the H5 rule table, one test per row, including "a pending delete is
  reported, not applied";
- receipt refusal (P1).

**Launcher** (L2a):
- subprocess tests of `cmd_up` argv: named volume present when any URI is
  local;
- startup refusal on a non-mount path, a read-only path, or a missing volume
  marker.

Each slice's report lists its mutation checks.

## 8. Slice plan

"Dark" means no effect on any store without `sync_state` / `MEMORA_REPLICAS`,
with flags off.

| slice | content | dark by | D1 statements it can issue | live rows touched |
|---|---|---|---|---|
| L1b | F3, plus F2 for the tools (P6) | — | none (removes writers) | 0 |
| L2a | launcher and volume (C1). See the note below | only adds a mount | none | 0 |
| L2 | §1, M10 (`supports_transactions`, `live_primary`, flock), `MEMORA_READONLY_DBS`, `connect_replicator` | no `sync_state` | none | 0 |
| L3 | §2 (log and write modes, P2/P3/P4, H2/H3, metrics, F7) | `MEMORA_REPLICATION` unset | write mode only: P2's 4 shapes | 0 |
| L4 | §3 | local stores only; none serve prod | none new (D1 absorb path unchanged) | 0 |
| L5 | §4 | run by hand | export and read-back SELECTs; R2-restore UPSERTs with `--approve`; the sequence UPDATE or sentinel with `--confirm` | 0 at merge |
| L6 | §5 compare and rollback | run by hand | SELECT only (rollback uses L5's sequence step) | 0 |
| L7 | F1, F2 for handlers; viewer deploy | viewer deploy | none (removes writers) | 0 |
| L8 | F4a–F6, `local_primary.py audit-configs` | ops | none | 0 |
| L9 | first store (`re` or `bestation`, the less critical): export all (P1), seed, repoint, log-only ≥ 7 clean nights, then write mode | per store | L3 write mode | whole store |
| L10 | the other of `re`/`bestation`, after L9 has a clean week in write mode | per store | same | whole store |
| L11 | `ob1`, after a clean week | per store | same | whole store |
| L12 | `memora`, after a clean week (P5); nightly R2 snapshot for all | per store | same | whole store |

**L2a, launcher and volume (C1):**
- `memora-instance.sh` `cmd_up`, in the `MEMORA_DATABASES` branch (line
  ~203): when any registry URI is `sqlite://`, it adds
  `-v memora-<instance>-data:/data` (a named volume) and
  `-e MEMORA_DATA_VOLUME=<name>`.
- `deploy-memora-all.sh` replaces `VOLUME_ID` (the name read by
  `docker inspect`, line 133) with the named volume `memora-all-data`. It
  copies the old volume once, with `docker run --rm -v old:/from -v new:/to`,
  and refuses if `docker volume inspect` shows an anonymous (64-hex) name.
- Server startup refuses to serve a local store unless:
  - `/data` is a mount point (its `st_dev` differs from `/`'s);
  - a probe file can be written, fsynced and removed;
  - `MEMORA_DATA_VOLUME` is set and is not a 64-hex name.
- **Memory gate:** measure RSS with four local stores (live-sized fixtures,
  964+ rows), FTS and the 384 MB corpus cache, against the 768 MB limit.
  The launcher default becomes max(768m, 1.5 × peak RSS).

**Order:**
- L1b first, then L2a and L2.
- L3 to L6 in any order after L2.
- L7 and L8 gate L9.
- Each cutover can be reversed with §5.3 until the next store's slice
  starts.
