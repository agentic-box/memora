# Local-primary: implementation plan

This plan implements `plans/nuc8-local-primary-design.md`. It is based on dev
at 3d03123. Line numbers refer to that commit.

The user's decisions (leader msgs 7507, 7520):
- Replication is async for every store.
- The memora-graph viewer becomes read-only.
- nuc8 `memora-all` becomes the only D1 writer.
- **D1 is precious.** Today it is the only complete copy of every store, so
  §0 overrides every other section.
- **D1 stays primary during the shadow period** (msg 7526; §2.9).

Nothing runs against a live store before its cutover slice. All tests are
offline, using local SQLite and FakeD1 (`tests/conftest.py`).

## 0. D1 data protection (binding on every slice)

- **P1. Verified export before every D1-affecting step.**
  - `scripts/local_primary.py export <db>` runs
    `wrangler d1 export <name> --remote --output` and writes the result to
    `/data/exports/<db>/<ts>.sql` and to `r2://<bucket>/exports/<db>/<ts>.sql`.
  - The export runs under the §1 freeze from start to receipt (§4).
  - It checks the export: the file is loaded into scratch SQLite, and its
    per-table row counts and content hashes must equal the same values read
    from D1 right after the export. The content hash is sha256 over rows
    ordered by pk. On a mismatch it retries up to 3 times, then fails.
  - It records D1's `embedding_change_epoch` just before and just after the
    export. The two must be equal, or it retries.
  - It uploads the file to R2, then reads the object back and hashes it. The
    receipt is accepted only when the read-back sha256 equals the local
    file's.
  - It writes a receipt, `<ts>.receipt.json`, containing:
    - the db and the D1 `database_id`;
    - the D1 epoch;
    - per-table row counts and content hashes;
    - the sha256 of the SQL file;
    - the R2 key and the sha256 of the R2 object as read back;
    - `verified_at`.
  - **Freeze-recheck (`local_primary.py recheck <db> --receipt R`)** runs
    immediately before seed and before any repoint (cutover, rollback,
    restore). It:
    1. freezes the source store (the §1 barrier: `frozen`, 0 in flight);
    2. re-reads D1's epoch, per-table `COUNT(*)` and the FULL per-table
       content hashes, computed the same way as in the receipt;
    3. compares all of them with the receipt. The epoch covers only
       `memories` and `memories_embeddings`, and counts miss same-count
       edits, so the hashes are the deciding check.
    If anything differs, it takes a fresh export and receipt while still
    frozen, and the step continues with that receipt. The freeze stays in
    place until the step completes.
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
- **P4. Shadow-local first; D1 stays primary** (P0-1, user decision
  7526). Each store first runs in shadow-local mode (§2.9) for at least 7
  consecutive clean nights, while D1 remains its primary. During that time:
  - the app writes D1 exactly as today;
  - a single feature-flagged wrapper (`MEMORA_SHADOW_LOCAL`) mirrors each
    successful D1 write into a local shadow file, best effort;
  - the replicator runs with `MEMORA_REPLICATION=log` against the shadow's
    outbox. It builds, checks and logs every statement (JSONL,
    `/data/replica-log/<db>/`, made durable as in §2.2 step 5), sends nothing
    and acks nothing;
  - reads stay on D1.
  Consequences:
  - D1 is never stale, and a bug in the local path cannot touch D1.
  - The cost is the temporary dual-write wrapper, which is removed after the
    last cutover (L13).
  Cutover never promotes the shadow file. It is: freeze, a FULL re-seed from
  a fresh verified export, recheck, repoint, and write mode (§8 L9).
- **P5. Order.** Least critical first; `memora` last. Each store waits until
  the previous one has completed a clean week with writes enabled (§8).
- **P6. Old tools inert before L2.** `sync-to-d1.py` (every remote run, not
  only `--replace`), `link-r2-images.py`, and the remote migration in
  `setup-cloudflare.sh` and `package.json` `d1:migrate` must exit 1 before
  slice L2 lands.
  - **Scripted** Pages deploys (`setup-cloudflare.sh`'s
    `wrangler pages deploy`, and `package.json` `deploy`) run the F2 guard
    first and refuse on any finding. Only these scripted paths are tested.
  - **Direct** `wrangler pages deploy` bypasses the guard. It is forbidden
    by operator rule until F1 lands.
  - The Pages deploy credential (a `wrangler login` OAuth session, or a
    token with Pages permission) is held only by the user, on no agent
    host. Checked on the Mac on 2026-09-23: there is no wrangler config at
    `~/.wrangler`, `~/Library/Preferences/.wrangler` or `~/.config/.wrangler`.
    nuc8, ob1, bestation and re are checked by `audit-configs` (L8).
  - The §6.1 tokens (a), (b) and (c) are minted with D1 permissions only,
    and no Pages permission.
  - The CI guard (F2) holds the scripted paths.
- **P7. No automatic D1 writes by rollback or restore.** Rollback writes
  nothing to D1 except the operator-run sequence step (§4). Restore writes
  only the keys an operator selected one by one (§4). Every other difference
  is reported for a human decision.

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
  log_cursor_seq INTEGER NOT NULL DEFAULT 0,   -- log mode (P4)
  compare_consumed_seq INTEGER NOT NULL DEFAULT 0,  -- P1-5: highest seq a clean compare covered
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
- **Freeze: a quiescence barrier (P0-1, P0-2).**
  - **Admission gate.** `_WriteGate` in backends.py holds one gate per
    registry store name, process-wide: a lock, a condition, `state`
    (`open`, `draining`, `frozen` or `frozen-unsafe`), an in-flight set and
    the open intents of the write-ahead journal (below). Each in-flight
    entry records the statement class, the thread and the age.
    ```python
    def write_gate(name: str) -> _WriteGate
    class _WriteGate:
        def enter(self, desc: str) -> _GateToken   # raises StoreReadOnlyError unless state == "open"
        def leave(self, token: _GateToken) -> None
        def freeze(self, timeout_s: float = 30.0) -> None   # close admission atomically, wait for in-flight == 0
        def thaw(self) -> None
    ```
    - `freeze` closes admission under the lock (`state = draining`), then
      waits for the in-flight set to empty, and then sets `frozen`.
    - On timeout, it reopens the gate, which aborts the freeze, and raises
      `FreezeTimeout` listing the in-flight entries. The step that asked for
      the freeze does not proceed.
  - **`d1://` (P0-1).** The connection `backend_for(name)` hands out
    overrides `_execute_api`. For every statement `classify_statement` does
    not class as `read`, it calls `enter` before sending. It calls `leave`
    after the response has been parsed, or after the exception has been
    determined, including a timeout with an unknown outcome. So a request
    admitted before the freeze always finishes before the freeze completes.
    A request arriving after the freeze started is refused. Reads never
    touch the gate.
    On a shadowed store, `ShadowingD1Connection` (§2.9) subclasses this gated
    connection. The order is: enter, D1 request, shadow enqueue, leave. So a
    completed freeze also means every admitted write has been enqueued.
    Draining the shadow queue is a separate step (§8 L9).
  - **Local (P0-2).** `_LockedWriterConnection.execute`, `executemany` and
    `executescript` classify every statement. The first mutating statement
    of a transaction calls `enter`, and the token is held until `commit`,
    `rollback` or `close`. `store_write` enters at `BEGIN IMMEDIATE`.
    `cursor()` returns `_GatedCursor`, whose `execute`, `executemany` and
    `executescript` go through the same classification and the same
    transaction token. The raw sqlite3 cursor is therefore not a bypass
    (round-8 P2c).
    - A connection opened before the freeze is refused on its next mutation
      unless it already holds a token.
    - A transaction admitted before the freeze may finish, and the freeze
      waits for it to commit or roll back.
    - The earlier `PRAGMA query_only` mechanism is dropped; the gate is the
      only mechanism.
    - The backend's own setup PRAGMAs in `connect()` (`journal_mode=WAL` once
      per file, and `busy_timeout`) run on the raw sqlite3 connection before
      it is handed out, so they are not gated.
  - **Exempt by construction.** `connect_replicator()` returns
    `_ReplicatorConnection`, a separate class that never calls the gate. The
    shadow applier's backend is built from `MEMORA_SHADOW_LOCAL`, not the
    registry, so it has no gate. Both must keep running to drain.
  - **Placing and holding a freeze.** The gate is in-process, so a freeze
    is valid only while memora-all is the sole D1 writer (F1–F6 require
    that).
    - Placed through `POST /admin/freeze/<db>` (admin-token auth, §9 (a)). It calls
      `freeze()` and then writes `/data/freeze/<db>`, the persisted intent.
      It returns 200 `{"state":"frozen","in_flight":0}`, or 409 with the
      in-flight list if it timed out.
    - Lifted through `DELETE /admin/freeze/<db>`, which removes the file and
      calls `thaw()`.
    - At startup, a store with a freeze file, or named in
      `MEMORA_READONLY_DBS`, starts `frozen`.
    - `/health/db/<name>` reports `freeze: {state, in_flight}`.
    - `local_primary.py` export, seed, recheck and repoint call the endpoint
      and re-read `/health/db/<name>` at every step boundary. They refuse
      unless it reports `frozen` with 0 in flight; `frozen-unsafe` is a
      refusal. The barrier is held
      through recheck and repoint.
    - When memora-all is stopped (the rollback steps after the stop), the
      scripts instead require `docker inspect … State.Running=false`.
  - Reads continue while frozen.
  - **Write-ahead intent journal (round-9 P0).** Every mutating D1 request
    is journaled before it is sent, so no crash or disk failure can leave a
    server-side write unrecorded.
    - **Before send**, still inside the gate: append an intent record to
      `/data/intent/<db>.jsonl`, then `flush` and `fsync`. The record holds
      `{"type":"intent", "id", "sql", "params_sha256", "target", "keys",
      "post_state", "sent_at"}`:
      - `id` is monotonic per store; the next id is recovered from the file
        at startup;
      - `keys` and `post_state` hold the touched keys and intended values
        (INSERT columns, `SET` values) when the classifier can derive them.

      If the append or the fsync fails, the request is NOT sent. The app
      gets the error (`IntentJournalError`), the gate token is released, and
      the gate state does not change.
    - **One journal mutex per store, and one writer (round-10 P1).** A
      single mutex covers all of these:
      - intent append plus fsync;
      - resolution append;
      - updating the in-memory open set;
      - the whole compaction: snapshot, write, fsync, rename and directory
        fsync.

      **Lock and fd lifecycle (round-11 P0).** A flock belongs to an inode,
      not a path, so it is never taken on the journal file, which compaction
      replaces.
      - memora-all holds `fcntl.flock(LOCK_EX)` on a stable, never-renamed
        lockfile, `/data/intent/<db>.lock`, for its lifetime. It asserts
        that it is the only writer.
      - Before every pre-send intent append, it runs `fstat` on the active
        fd and `stat` on `/data/intent/<db>.jsonl`, and compares
        `(st_dev, st_ino)`. On a mismatch the send is refused with
        `IntentJournalError` and the gate goes `frozen-unsafe`. It never
        writes to a mismatched fd.
      - Operator actions (`reconcile --accept`) do not write the file. They
        go through the admin endpoint (`POST /admin/reconcile/<db>/<id>`,
        which needs a receipt), so the process is the only journal writer.
      - When memora-all is stopped, `local_primary.py` takes the same
        lockfile flock before touching the journal.
    - **Repair after any write error (round-10 P0).** After ANY failed append
      or fsync (intent or resolution), under the mutex and before any further
      D1 send for that store:
      1. truncate the file to the last byte that ends a verified newline
         (re-read from disk);
      2. fsync the file and the directory;
      3. only then reopen admission.

      Until repair completes, the gate refuses mutations. If the repair
      itself fails, the gate stays `frozen-unsafe` and refusing, and health
      names the file, until an operator repairs it (by stopping the
      container, or fixing the mount). A torn tail can therefore never have
      a valid record appended after it.
    - **Startup replay (round-10 P0):**
      - A malformed record that is **not** the final line means that store
        refuses to start (health names the file and offset). Later bytes are
        never discarded.
      - A torn **final** line is dropped, with a log line. This is safe: an
        intent is sent only after the fsync of its complete line has
        returned, which cannot have happened for an incomplete final line. A
        torn final resolution just leaves its intent open (safe).
    - **After a known response** (success, or a definite HTTP/SQL failure):
      append `{"type":"resolved", "id", "outcome"}`. This record is fsynced
      lazily: it rides on the next intent's fsync, or a 1 s timer. If it is
      lost, or its append fails (for example on a full disk), the intent
      simply stays open. That is an extra unsafe record, which is safe.
    - **An unknown outcome** (client timeout, reset after send, or process
      death between send and response) writes no resolution. So the earlier
      "record after the exception" step is gone. There is nothing to write
      after the fact, and a death before the catch still leaves the intent
      open.
    - **Open intents** (an intent with no resolution) are held in memory and
      rebuilt from the journal at startup. While any exist:
      - the gate reports `frozen-unsafe` instead of `frozen`;
      - every export, seed, recheck and repoint script refuses;
      - health and the admin endpoint list the ids.
      The replicator's H3 `inflight_*` marker counts as an open intent too.
    - **The journal is a file**, not `sync_state`/`shadow_state`, because a
      store still on `d1://` before its shadow week has neither table.
      Compaction runs under the journal mutex when the file exceeds 8 MB. No
      intent can be appended between the snapshot and the rename; a mutation
      that arrives meanwhile waits on the mutex. Before the mutex is
      released it does all of this: write the replacement, fsync it, rename
      it over the path, fsync the directory, close the old fd, and reopen
      the active fd from the path (round-11 P0). It writes a new file holding
      only the open intents and the id counter, fsync it, `rename` it
      atomically over the old one, and fsync the directory. Only resolved
      pairs are dropped.
    - **Cost:** one fsync per D1 mutation (milliseconds on NVMe, well under
      the ~100 ms D1 round trip). Accepted. Reads are never journaled.
    - **Reconciliation (round-10 P2): every open intent needs an operator.**
      No automatic resolution exists today, because no intent carries a
      unique proof of its effect:
      - INSERTs omit the AUTOINCREMENT id (storage.py:5640, 5669, 5788), and
        identical rows can already exist, so a matching row is not proof;
      - a no-op UPDATE, or a DELETE of an absent key, cannot prove "not
        applied", and trigger and meta effects are unchecked.

      So memora-all only assists. At least 60 s after `sent_at` (2 × the 30 s
      client timeout, `_D1_TIMEOUT_SECONDS`, backends.py:1540, which also
      bounds Cloudflare's per-request limit), it reads back the keys and
      values it can derive, through `D1SelectOnlyConnection` with the read
      token and `served_by_primary`. It shows that evidence on health and in
      `local_primary.py reconcile <db> --show`.
      - Resolution is always
        `local_primary.py reconcile <db> --accept <id> --receipt R`, sent
        through the admin endpoint, which appends
        `{"type":"resolved", "id", "outcome":"operator-accepted"}`.
        Aborting the migration step is the alternative.
      - A cutover after any accept still takes the full fresh export and
        recheck it always takes.
      - **Operational cost:** any D1 write with an unknown outcome (a client
        timeout, or a reset) needs a human before the next export, seed,
        recheck or repoint of that store. That is rare and accepted.
      - Automatic resolution needs an ownership nonce: §9 items (d) and (e).
    - A fresh re-seed never resolves intents; the seed refuses while any are
      open.
    - **Journal unavailable in this process (leader 7583, review 7584 P1-2).**
      When the journal is held by another process, the data dir is not
      writable, or the journal is corrupt or broken, `D1Backend.connect()`
      returns a READ-ONLY connection: SELECTs work, and every mutation
      (every execute variant, PRAGMA setters, DDL, `execute_batch`) raises
      `StoreReadOnlyError`. `schema.connect` skips schema setup on it. A
      connection whose journal breaks later refuses mutations through the
      gate. The writer process is unaffected.
    - **Not sent after a late journal failure (review 7584 P1-3).**
      `append_intent` raises when its own compaction broke the journal, and
      `_execute_api` re-checks journal health immediately before the send.
      Either way the intent is abandoned (dropped from the open set, with a
      `not-sent` resolution when the journal can still take one); it was
      never sent.

  - `connect_replicator()` is the replicator's only entry point, and a test
    asserts that no other module calls it (H6).
  - **One identity per store (review 7588 P1).** The primary lock file is
    derived from the canonical path (`realpath` of the database, parents
    included) with the suffix appended afterwards, so symlink aliases share
    one lock. At startup, a registry that names one store twice (colliding
    canonical paths, or one D1 database under two names) refuses to start.
  - **Fencing live primaries (review 7584 P1-1).** `server.main` takes every
    live primary's lock before any prewarm or writer open
    (`fence_live_primaries`). A store whose lock another process holds is
    refused in this process (every open raises; health reports `refused`);
    a refused default store aborts startup. Every writer open of a live
    primary (`_open_writer`) takes the lock first, in any process, so a
    script cannot write it while memora-all runs.

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

There is one daemon thread per store that has a `sync_state` row and is
either:
- local and named in `MEMORA_REPLICAS` (after cutover, `log` or `write`
  mode); or
- named in `MEMORA_SHADOW_LOCAL` (shadow week). Its outbox is the shadow
  file's, and the mode is forced to `log`: write mode refuses to start on a
  shadow store.

It starts in `server.main`, next to `_startup_import_sweep`
(server.py:3550), only when `MEMORA_REPLICATION` is `log` or `write`.

The replicator's D1 connection is schema-free: `D1Backend` is opened without
`ensure_schema`, like `connect_without_schema`. The replicator therefore
never sends DDL to D1. A test asserts that FakeD1 receives no
`CREATE`/`DROP`/`ALTER`/`INSERT OR IGNORE INTO memories_meta` from it.

### 2.2 Loop

The thread owns one anchor writer from `connect_replicator()` for the life of
the process. Holding a writer open does not hold a `_StoreRWLock` side: only
open and close do. It wakes on `_notify_commit(db_path)`, a per-path
`threading.Event` set by `_LockedWriterConnection.commit()`, or after
`poll_s`. Each cycle:
1. Read `SELECT seq,tbl,op,pk FROM sync_outbox WHERE seq > :cursor ORDER BY seq LIMIT :n`.
   `:cursor` is `last_acked_seq` in write mode and `log_cursor_seq` in log
   mode.
2. Coalesce on (tbl, pk), keeping the highest seq.
3. Read each key's current local row. A present row becomes an upsert; an
   absent row becomes a delete.
4. Run P2 and P3.
5. In **log** mode:
   1. Append one JSONL record per statement. Each record carries
      `attempt_id`, `seq`, `tbl`, `pk`, `sql` and `params`.
   2. `flush`, then `os.fsync` the file, plus the directory when the file is
      new.
   3. Only after the fsync returns, run a `store_write` that sets
      `log_cursor_seq=:hi`.
   A crash between steps 2 and 3 makes the next cycle append the same range
   again. Readers deduplicate by `seq`, and apply is idempotent. A torn
   final line is dropped when the log is read.
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
- **Foreign keys (L3 review 7599 P1-2).** D1 enforces foreign keys
  (https://developers.cloudflare.com/d1/sql-api/foreign-keys/), and
  `memories_embeddings` and `memories_crossrefs` reference `memories(id)`.
  A batch therefore sends parent upserts first, then every other table, then
  parent deletes. A child upsert always travels with its parent's current
  row, even when the parent's own outbox rows are outside the range. A
  child whose parent is gone locally is sent as a delete.

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
  - `DELETE FROM sync_outbox WHERE seq <= min(:acked, compare_consumed_seq) AND created_at < julianday('now') - 1`.
  Outbox rows are kept until a clean compare has consumed them, and for at
  least 24 h. In log mode nothing is acked, so nothing is pruned. The compare
  can therefore also derive the expected key set from the outbox (§5.2).
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

### 2.9 Shadow-local mode (`memora/shadow.py`, L3b)

`MEMORA_SHADOW_LOCAL='{"<db>": "/data/shadow/<db>.db"}'` holds one entry
per store in shadow. The registry keeps the store on `d1://`, so every read
and every primary write still go to D1.

**The only hook** (P1-2, P2-4). `D1Backend.connect()` returns a
`ShadowingD1Connection(D1Connection)` for a shadowed store.
- It overrides exactly one method: `_execute_api(sql, params)`
  (backends.py:1377). `execute` (1459), `executemany` (1478) and
  `executescript` (1493) each call it once per statement or element, and
  they are NOT overridden.
  - The result and exception shapes are therefore the parent's own code, by
    construction. Nothing is re-implemented: not the
    `meta.get("changes", 0)` aggregation, not `lastrowid` carry-over, not the
    `rowcount=meta.get("changes", len(rows))` fallback in `execute`, and not
    the `sql_script.split(";")` splitter in `executescript`.
  - Every storage.py write to D1 passes through `_execute_api`, so no call
    site changes.
- The override calls `super()._execute_api(sql, params)`.
  - On success, it returns the response dict untouched, and enqueues
    `(sql, params, meta)` when the statement is to be mirrored (below).
    `executemany` and `executescript` therefore enqueue each element as it
    completes.
  - On any exception it marks the shadow dirty and re-raises the same
    exception object. That covers a failed element of a composite call and
    a timeout with an unknown outcome.
  - The new `execute_batch` (§2.4) is overridden to raise on a shadowed
    connection; the app never calls it.
- **Which statements are mirrored (P1-1).** A new shared parser,
  `classify_statement(sql) -> (kind, target)` in backends.py, works as
  follows:
  1. Strip leading whitespace, `-- …` line comments and `/* … */` block
     comments.
  2. Tokenize, respecting quoted strings, identifiers and parentheses. More
     than one top-level statement means `unknown`.
  3. If the statement starts with `WITH [RECURSIVE]`, skip each
     `name[(cols)] AS [NOT] [MATERIALIZED] ( … )` (balanced parentheses,
     comma-separated) to reach the main statement. CTE bodies are SELECT-only
     in SQLite.
  4. Classify the main statement:
     - `SELECT`, `VALUES`, `EXPLAIN` → `read`: never mirrored, never dirty;
     - `PRAGMA` (P1-3): `read` only for the read-only forms in a
       whitelist: `table_info(t)`, `table_xinfo(t)`, `index_list(t)`,
       `index_info(i)`, `foreign_key_list(t)`, `database_list`,
       `integrity_check`, `quick_check`, and the no-argument, no-`=` forms of
       `journal_mode` and `user_version`. memora itself uses only
       `table_info` and `database_list`; the rest are listed for the
       tools. Every other PRAGMA (`optimize`, `wal_checkpoint`,
       `incremental_vacuum`, `shrink_memory`, any `=` or setter form) is
       `ddl`-like: gated as a mutation under freeze, never mirrored, and
       dirty in the shadow;
     - `INSERT` / `REPLACE` (target after `INTO`), `UPDATE` (the next name,
       after an optional `OR <conflict>`), `DELETE FROM` → `mutation` with
       that target;
     - `CREATE`, `ALTER`, `DROP` → `ddl`;
     - anything else → `unknown`.
  Rules applied to the result:
  - `mutation` with a §1 target is mirrored; any other target is ignored.
  - `ddl` that succeeds on D1 marks the shadow dirty at once (P2-3). Schema
    drift with no later row write therefore cannot leave the shadow stale
    but clean. The parent's `executescript` splitter is unchanged; each
    split statement is classified on its own.
  - `unknown` marks dirty: fail closed, but only for genuinely unrecognised
    statements.
  - The existing `_is_read_statement` (which is `startswith("SELECT")`,
    used only for `retry_safe`) stays unchanged. It is not used for
    classification.
  - The same parser backs the §1 freeze wrapper and
    `D1SelectOnlyConnection`.

**`ShadowApplier(name, shadow: LocalSQLiteBackend, reader: D1SelectOnlyConnection)`**
is one thread with a FIFO queue, off the request path. For each item, in
one local `store_write`:
1. Run the same SQL and params on the shadow. The local outbox triggers fire
   and record the touched keys.
2. Add D1's `last_row_id` as a key for an INSERT into `memories` or
   `memories_actions`.
3. Copy back: read each key from D1 by pk, and make the local row identical
   by upserting or deleting it locally.
4. Verify per key: re-read the local row for every touched key. It must equal
   the D1 row just read, column for column. A key absent on D1 must be
   absent locally. A mismatch means dirty (row 6). D1's `rows_written` is
   not compared: it counts index and trigger writes.

**As built (L9a): copy-back at a quiescent point.** Steps 1–2 run per item,
in order. Steps 3–4 run for the accumulated touched keys only when no
mutation of the store is in flight through a shadowed connection and every
enqueued item has been replayed, so D1 holds exactly the state after the
last replayed statement. A generation counter bumped at the start of every
shadowed mutation is read before and after the copy-back reads; if it moved,
the reads are discarded and taken again at the next quiet point. The reads
are never made inside a `store_write`. Reason: the applier runs behind D1,
so a per-item copy-back reads D1 state that LATER writes produced. That gave
a false written-value mismatch (row 5), and it made a later replay conflict
with the future row copy-back had installed (row 4). The app's own embedding
DELETE+INSERT triggers the latter. The written-value check applies where the
last statement on a key was that key's own parameterised write. Rows 1–10
are unchanged.

**Read consistency (P1-4).** D1's Sessions API (bookmarks) "is only
available via the D1 Worker Binding and not yet available via the REST
API", and read replication is opt-in
(https://developers.cloudflare.com/d1/best-practices/read-replication/).
The existing `cf-d1-session-token` handling in `D1Connection` therefore
gives no guarantee over REST. The plan does not rely on it:
- **Required:** read replication stays disabled on every store's D1
  database. L3b checks `read_replication.mode` through the REST API, and
  the applier refuses to start if it is not `disabled`.
- **Per response:** the reader asserts `meta.served_by_primary == true` on
  every copy-back response. A replica-served answer is retried, up to 5
  times, 200 ms apart.
- **Written values:** for statement shapes the parser recognises (the
  INSERT column list, and `UPDATE … SET col = ?`), the copy-back row must
  show the written parameter values in those columns. Expression columns
  such as `datetime('now')` are exempt. A mismatch is retried with the same
  bounds.

**`D1SelectOnlyConnection` (P0-1).** This is a separate class. It is not
`D1Connection` and not the P2 checker.
- Its `execute` accepts only statements that `classify_statement` classes
  as `read` and whose main statement is `SELECT`, with or without a
  `WITH … SELECT` prefix. It rejects everything else: `EXPLAIN`, `PRAGMA`,
  `VALUES`, every mutation (including the per-key UPSERT and DELETE that P2
  allows), DDL, `unknown`, `RETURNING`, and more than one statement.
- It has no `executemany`, `executescript`, `commit` or `execute_batch`.
- It is opened schema-free.
- Credential: Cloudflare offers a "D1 Read" API-token permission ("Grants
  read access to D1",
  https://developers.cloudflare.com/fundamentals/api/reference/permissions/).
  It is account-scoped, not per-database. The shadow reader and the
  compare, export and recheck tools use a separate D1 Read token
  (`MEMORA_D1_READ_TOKEN`); this is **required**.
  - L3b verifies, on a throwaway database, that the D1 Read token can run a
    SELECT through `/query` and is refused an INSERT.
  - If D1 Read cannot run `/query` SELECTs, the plan records that the
    SELECT-only guard is the only boundary.

**Dirty rule (every failure point).** Any of the following sets
`shadow_state.dirty = 1` with a reason. The D1 result and the D1 path are
never touched, and nothing retries into D1. A dirty shadow is re-seeded from
a fresh export, and the 7-night clock restarts.

| # | failure point | rule |
|---|---|---|
| 1 | a single mutating D1 request raises, or times out with an unknown outcome | dirty |
| 2 | `executemany` / `executescript`: an element raises after earlier elements succeeded (those were already enqueued) | dirty |
| 3 | enqueue fails, or the applier is not running when an item arrives | dirty |
| 4 | local replay raises | dirty; roll back this item's local transaction |
| 5 | a copy-back read fails, stays replica-served, or shows a written-value mismatch after the retries | dirty |
| 6 | after copy-back, a touched key's local row ≠ D1's row, or a key deleted on D1 is still present locally | dirty |
| 7 | `classify_statement` returns `unknown`, or a `mutation` target cannot be resolved | dirty |
| 7b | a `ddl` statement succeeds through the shadowed connection | dirty, at once (P2-3) |
| 8 | the applier thread dies (its run loop catches `BaseException`) | dirty; health shows `applier_alive=false` |
| 9 | process exit or restart with a non-empty queue: the queue is in memory | dirty via the marker below |
| 10 | a D1 write not made through the wrapper (a foreign writer) | cannot be seen per write; the nightly shadow-vs-D1 compare finds it, and the writer freeze (§6) prevents it |

The marker for row 9 is `shadow_state.clean_shutdown`:
- It is set to 0 when the applier starts.
- It is set to 1 only after a graceful stop has drained the queue.
- A start that finds 0 marks the shadow dirty.

```sql
CREATE TABLE shadow_state (   -- in the shadow file only
  id INTEGER PRIMARY KEY CHECK (id = 1),
  dirty INTEGER NOT NULL DEFAULT 0, dirty_reason TEXT, dirty_at TEXT,
  clean_shutdown INTEGER NOT NULL DEFAULT 0,
  clean_nights INTEGER NOT NULL DEFAULT 0, last_clean_night TEXT
);
```

**Nightly check (shadow mode).** No barrier is needed: D1 is the source.
- **(a) Shadow vs D1.** A full-table compare of all §5.2 tables, after the
  queue drains. A diffed key is re-read once; a persistent diff is a
  shadow-apply bug.
- **(b) Builder.** The log key set must equal the outbox key set up to
  `log_cursor_seq`. Then the log is replayed into a scratch copy of the
  shadow's seed export and compared with a `.backup` of the shadow at the
  same seq. A diff is a statement-builder bug.
- A clean night means (a) and (b) show zero diffs and `dirty = 0`. Either
  failure resets `clean_nights`.
- **As built (L9a):** `local_primary.py shadow-night <db> --shadow S
  --seed-export SQL --account A --database-id D` (`memora/shadow_night.py`)
  runs both checks. (a) re-reads each diffed key once after a pause. (b)
  also fails while the replicator's log is halted: L3's delete guard (P3)
  halts log mode as well, and a table under 100 rows halts on any delete.
  So a shadow night stays not-clean until `resume` is run. The shadow itself
  is not marked dirty for that. A diff in (a) or (b) marks the shadow dirty.
  A clean night counts once per UTC day, and the command reports
  `ready_for_cutover` at 7. Exit 5 when the night is not clean.
- `/health/db/<name>` carries the `shadow` block (`enabled`, `dirty`,
  `dirty_reason`, `queue_depth`, `pending_keys`, `applier_alive`,
  `clean_nights`, and `refused` when the applier could not start).

**Metrics.** `/health/db/<name>` gets a `shadow` block with `queue_depth`,
`applier_alive`, `dirty`, `dirty_reason` and `clean_nights`.

**Removal.** L13 deletes `ShadowingD1Connection`, `ShadowApplier`,
`D1SelectOnlyConnection`'s shadow use and `MEMORA_SHADOW_LOCAL`.

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
  - **Frozen (P1-2).** Export places the §1 freeze on the store before
    starting, and keeps it through schema capture, data capture, the
    post-export hashes and the receipt write. This applies to native and
    fallback exports alike. The native `wrangler d1 export` is preferred;
    the paged-SELECT fallback is allowed only under this freeze, because
    the epoch bracket covers only `memories` and `memories_embeddings`.
    Under the freeze, memora-all's writes fail loudly instead of being
    captured half-way. The viewer and every other writer are already gone
    by L9 (L7, L8).
  - Credentials:
  - `wrangler d1 export --remote` runs as a subprocess with an environment
    built from scratch: `PATH`, `HOME`, `CLOUDFLARE_ACCOUNT_ID`, and
    `CLOUDFLARE_API_TOKEN` set to the value of `MEMORA_D1_READ_TOKEN`.
    Nothing else is inherited, and `CLOUDFLARE_API_TOKEN` never appears in
    the app's own environment for this purpose.
  - L3b tests on a throwaway database whether a D1 Read token may export.
    If export is denied, `export` fails closed and uses the fallback: a
    paged `SELECT` of every table through `D1SelectOnlyConnection` with the
    read token (already verified), written as SQL `INSERT`s plus the schema
    from `sqlite_master`. The read token remains the only credential used
    for export.
  - `local_primary.py` makes every D1 call through `memora/backends.py`
    classes (`D1SelectOnlyConnection` for reads, and `D1Connection` with the
    operator credential for the write paths). So the F2 CI guard's
    allow-list stays `memora/backends.py` only.
- **Freeze lifetime, receipt binding, sequences (L5 review 7621).**
  - No command lifts the freeze by itself. `export` places it if needed and
    leaves it in place. `recheck` and every step that relies on it (seed,
    sequence high-water, restore) require the freeze to be in place already,
    and refuse otherwise. Only the explicit `thaw` command lifts it.
    `freeze` places it by hand.
  - Tokens (L2a cross-finding 7633):
    - Every credential file is checked with `lstat`, is not a symlink, is a
      regular file owned by the operator, and has mode exactly 0600.
    - The freeze client takes its own `--health-token-file`, which never
      defaults to the admin token: memora-all refuses equal tokens.
  - A receipt is bound to the D1 database, not only the store name:
    `account_id`, `database_id` and `d1_uri` must equal the command's.
  - `sqlite_sequence` is in the hashed set, ordered by name. The paged dump
    replaces each auto-created sequence row with D1's counter
    (`DELETE` then `INSERT`), so the next id after a load is D1's
    counter + 1.
- **`seed <db> --receipt R --out /data/<db>.db`**
  1. Load the receipt's export into a fresh file.
  2. Run `ensure_schema`.
  3. Rebuild FTS explicitly: `DELETE FROM memories_fts; INSERT INTO memories_fts(rowid, content, metadata, tags) SELECT id, content, COALESCE(metadata, ''), COALESCE(tags, '') FROM memories`.
     `memories_fts` is a standalone fts5 table, created empty. L5 verified
     that the rebuild matches `_fts_upsert`'s form, which writes `''` for a
     NULL `metadata` or `tags`; hence the `COALESCE`. A search-parity test
     compares keyword and hybrid results on the seeded store and on its
     source.
  4. Sequence high-water, local side:
     `sqlite_sequence.seq = max(local seq, D1 seq, max(id))` for `memories`
     and `memories_actions`. The D1 value is read by SELECT.
  5. Run `install_sync` with `last_acked_seq = 0` and `d1_epoch_expected`
     taken from the receipt.
  6. Verify that the seeded file's per-table counts and content hashes equal
     the receipt's. This is deterministic, with no live read: D1 may have
     moved on since the export, and `recheck` covers that.
     `sqlite_sequence` is raised on purpose by step 4, so it is checked as
     "no counter below the export's".
  - As built in L5:
    - The seed runs under the freeze left by export/recheck. It is required
      at the start and re-checked around the D1 read and before placing the
      file.
    - The seed runs `recheck` itself under that freeze and seeds from the
      receipt the recheck returns: the same one, or a fresh export if D1
      changed (review 7642 P1-1). There is no shortcut based on proof of an
      earlier recheck.
    - `sync_state.replica_uri` is derived from the verified D1 identity,
      `d1://<account_id>/<database_id>`. A supplied `--replica-uri` must equal
      it (7642 P1-2).
    - A retry removes a crashed seed's `.seed-partial` together with its
      `-wal`, `-shm` and `-journal` files (7642 P2).
    - It holds the target's primary lock and creates the parent directory
      first (§9 k).
    - It builds `<out>.seed-partial` and hard-links it into place only after
      it verifies. It never overwrites a file or its sidecars.
    - `--rehearse` seeds into a temp directory.
- **`restore <db> --receipt R`** (the default, H5) is a full re-seed from a
  new verified export. Unacked local writes at the time of a disk loss are
  lost; that was accepted in msg 7507.
- **`restore <db> --from-r2 <key> --receipt R`** is only for D1 corruption or
  operator error. There is no automatic resolution rule: `updated_at` is not
  a freshness signal, because storage.py:5655, 8092, 9577 and 10348 change
  metadata, tags or importance without touching it.
  1. Load the snapshot and rebuild FTS.
  2. Compare every §5.2 table with D1. **Every** difference is a CONFLICT,
     including keys present on only one side. Differences are grouped per
     memory id (the `memories` row plus its embeddings, crossrefs,
     tombstones, tombstone_components and actions), plus one group per
     `memories_meta` key.
  3. Write `conflicts-<ts>.json`, containing both versions of every row in
     each group.
  4. The operator writes `--approve <file>`, choosing `d1` or `snapshot` for
     each group. The file must name every group, and it carries the
     conflicts file's sha256.
     - `d1` changes only local, and never writes D1.
     - `snapshot` sends per-key UPSERTs to D1, or a per-key DELETE when the
       snapshot lacks the key. These are built by `_build_statements` and
       pass `_check_statement` and P3.
  5. Apply runs under the freeze. Before writing each `snapshot` group, it
     re-reads the group's D1 rows (the memories row and all child rows, or
     the meta key) and hashes them. If the hash differs from the D1
     preimage hash recorded in the conflicts file, that group is aborted
     and reported; the other groups continue.
  6. Nothing is written to D1 for a group without a selection.
  7. `--dry-run` prints the exact statements.
  - As built in L5 (piece c):
    - **Prepare** (`restore <db> --from-r2 KEY --receipt R`), under the freeze
      already in place:
      - rechecks R;
      - fetches the snapshot, which must gunzip and pass `integrity_check`;
      - compares it with D1's verified export over the §5.2 tables, minus
        the excluded meta keys;
      - writes `conflicts-<ts>.json`. It carries both versions of every row
        in each group, D1's preimage sha256 per group, the snapshot key and
        sha256, the receipt and its sha256, and the D1 identity.
      - An action with no memory is its own group.
    - **Apply** (`... --conflicts F --approve A --out P --credential-file C
      --service-stopped`), after review 7674:
      - It needs memora-all STOPPED. With the live freeze barrier it refuses
        before any send.
      - It holds the target's primary lock from before the first D1 send
        through the fresh export and the rebuild.
      - A group's preimage and read-back ENUMERATE the group's current rows
        on D1: the memories row and every child table by memory id, the meta
        key, or the memory-less action. So a row added since prepare aborts
        the group, and an extra row after the writes HALTS.
      - The fresh export is checked the same way before the rebuild: each
        group holds exactly the chosen rows.
      - `--dry-run` is the complete no-write plan: the statements per group,
        the delete-guard result and the rebuild target. `--rehearse` is
        refused for `--from-r2`.
      - The approve file must quote F's sha256 and choose `d1|snapshot` for
        every group and nothing else. It then rechecks R.
      - Each `snapshot` group gets per-key statements from the replicator's
        `_build_statements`: UPSERTs parents first, DELETEs children first.
        Rows that are already equal get none. Every statement passes
        `_check_statement` and is sent through `OperatorD1Writer` with
        `allow_restore`, which accepts only those P2 shapes.
      - P3: DELETEs per table are counted against the receipt's row counts
        with the replicator's thresholds. Over the limit the apply refuses
        before any write, naming an attempt id derived from the conflicts
        and approve hashes. `--allow-deletes <id>` allows that one attempt.
      - Before each group, D1's rows for it are re-read and hashed. A group
        whose hash is not the recorded preimage is aborted and reported; the
        others continue. After the writes, the group must read back as the
        snapshot, or the apply HALTS; a failed send also HALTS.
      - When every group is resolved, D1 holds the chosen state everywhere.
        The local store is then rebuilt by the default restore from a fresh
        verified export, so a `d1` choice changes only the local store.
        If a group was aborted, the local store is not rebuilt.
    - **Default restore** (`restore <db> --receipt R --out P`):
      - Holds the target's primary lock (memora-all stopped).
      - Moves the old store and its sidecars into
        `<name>.pre-restore-<ts>/`; they are never deleted.
      - Seeds (which rechecks R).
      - If the seed fails, the old store is put back.
- **Sequence high-water, D1 side** (H7): run before rollback and before
  restore, with a receipt and a passing recheck.
  - It is one statement per table:
    `UPDATE sqlite_sequence SET seq=? WHERE name=? AND seq<?`.
  - L5 first checks whether D1 accepts this, on a throwaway D1 database
    created with leader approval, never on a live one.
  - If D1 rejects it, the step HALTS and reports; the rollback or restore
    does not proceed. There is no fallback.
  - As built in L5 (`sequence-highwater`):
    - It runs `recheck` under the freeze already in place, reads the local
      store read-only, and sends the UPDATE only for a table where D1 is
      behind.
    - The send goes through `OperatorD1Writer`: a `D1Connection` from a 0600
      `--credential-file`, with an allow-list of exactly this statement.
    - It reads the result back. The step HALTS (exit 3) when:
      - D1 rejects the UPDATE;
      - D1 accepts it but the counter did not move;
      - D1 has no `sqlite_sequence` row for the table (no INSERT is
        allowed).
    - Whether D1 accepts the UPDATE at all is checked by an env-gated test on
      the throwaway database.
- **`snapshot <db>`**: `sqlite3.Connection.backup` to a temp file, gzip, then
  `r2://<bucket>/<db>/<date>.db.gz`. It keeps 14, runs nightly from cron,
  and refuses if free space is below 2× the DB size.
  - As built in L5:
    - The source is the store's read-only connection (a live primary is read
      through its WAL sidecars).
    - The copy must pass `integrity_check`, and the R2 object is read back
      and hashed.
    - The key is `<db>/<YYYY-MM-DD>T<HHMMSS>Z.db.gz`, so two runs in one day
      do not overwrite each other.
    - Retention deletes only keys of that exact form.
- **`volume-check --store <path>...`**: alerts (exit 4) when a store's
  volume has less free space than 2× the store, or less than
  `--min-free-pct` (default 10%).
- **`resume <db> [--accept-d1-epoch | --allow-deletes <attempt>]`**.
  - As built in L5:
    - `resume <db> --store P` holds the store's primary lock, so memora-all
      must be stopped. It calls the replicator's `resume()`.
    - An accepted epoch must equal D1's epoch read now with the read token.
      The §5.2 barrier compare that must precede it is L6's.
    - A delete-guard halt is cleared only for the named attempt.
- **`reconcile <db> [--accept ID ...]`** (§1): shows `GET /admin/intents/<db>`.
  - `--accept` first checks the receipt: it must be usable, for this D1
    database, and at a path memora-all can read.
  - It also checks that the intent is open and that its evidence sha256 is
    still the one shown.
  - It then POSTs exactly L2's accept body, `{receipt, operator,
    intent_id, decision, evidence_sha256}`; the server re-checks all of it.
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
  1. Freeze ingress (the §1 barrier, no restart).
  2. Drain to `lag_rows = 0`.
  3. Take `S`.
  4. Compare. Zero diffs are required.
  5. Lift the freeze.
  - A **weekly** barrier compare runs Sunday 04:00 under a brief freeze and
    covers every key. It sets `compare_consumed_seq` to the drained head.
- **Nightly**:
  1. Take `S`, and let `H` be `MAX(seq)` in `S`.
  2. Wait for `last_acked_seq >= H`. If that takes more than 30 minutes,
     skip and alert.
  3. Read D1.
  4. Let `K` be the live outbox keys with seq > H. Acked rows are retained
     for 24 h, so every key written after `S` is in `K`.
  5. Compare, excluding `K`.
  6. On any diff, retry the whole run once with a freshly taken `S`, `H` and
     `K`. A diff that persists alerts.
  7. When the run is clean, set `compare_consumed_seq = H`.
  8. Report keys that were in `K` on two consecutive nightly runs. The weekly
     barrier compare covers them.

  A key not in `K` had no local write after `S`, and it was acked. So D1
  must equal `S` for it, unless a foreign write happened.

**As built in L6 (piece a)**: `memora/compare.py`, run by
`local_primary.py compare <db> --mode barrier|nightly|log --store P`.
- **Snapshot.** Local rows always come from one `.backup` snapshot `S`
  (`backup_store`: the read-only connection, then `integrity_check`). `H` is
  the highest outbox seq ever assigned: `sqlite_sequence`, which survives
  pruning. D1 is read only through the SELECT-only reader.
- **The compare** covers every §5.2 table and every column: columns present
  on one side only, keys present on one side only, and changed rows with the
  changed columns listed.
  - An embedding row whose changed columns are all provenance
    (`representation`, `dimension`, `encoding_source`, `writer_token`) is
    also listed as a `provenance_mismatch`.
  - `d1_missing_vectors` (§2.7) counts local vectors that D1 lacks: the row
    is absent on D1, or its `embedding` is NULL there.
  - The excluded meta keys are dropped on both sides.
- **Barrier.**
  - The freeze, or with `--service-stopped` the stopped container, is
    required, then re-checked after the drain, after the snapshot and after
    reading D1.
  - The compare waits for `last_acked_seq >= head`, and refuses if the drain
    does not finish in time.
  - `consumed_seq = H` when the compare is clean.
- **Nightly.** The run follows §5.2 above: `S`, then wait for the acks to
  pass `H` (skip and exit 6 past `--wait-timeout`), then read D1, then read
  `K` from the live outbox, then compare excluding `K`.
  - A diff triggers one retry that retakes `S`, `H` and `K`.
  - Keys in `K` on two consecutive nights are reported as `hot_keys`. The
    state is `<out-dir>/<db>/nightly-state.json`, and a rerun on the same
    night compares with the night before.
- **Log** (the shadow period, §2.9 (b)):
  - The compare waits for `log_cursor_seq >= head` and snapshots `S`.
  - Up to the cursor, every outbox key must appear in the log. An extra log
    key must be a `memories` FK parent that the replicator added.
  - The log is replayed in order, with foreign keys on, into the store's
    seed export (`--receipt`; its age is not limited, it is the seed's) and
    compared with `S`. D1 is not read, and nothing is consumed.
  - L6 found a real builder-path bug this way. `iter_log` deduplicated on
    `(seq, index)`, which dropped a child statement sharing its seq with the
    FK parent the batch added. It now deduplicates on
    `(seq, table, pk, index)`.
- **Report and record** (after review 7695). The store verifies the
  report; it does not take the caller's word.
  - **Begin.** A run first registers itself on the store with
    `begin_compare`. The store records the start by its own clock in
    `sync_state.compare_runs`.
  - **The report** carries the run id, the store name, the D1 identity
    (`d1_uri`), H, and the snapshot's path, sha256 and time. It is written
    to `<out-dir>/<db>/compare-<mode>-<ts>.json`.
  - **Record.** `record_compare` takes only the report FILE path, which must
    be readable by the store's process, like a reconcile receipt, and
    optionally its sha256. It verifies:
    - the file's actual hash;
    - the store name, and that `d1_uri` equals `sync_state.replica_uri`;
    - that the run was registered here and started at most 12 h ago
      (P2b: a longer run cannot record, well inside the 24 h retention);
    - that the snapshot file exists and matches its hash.
  - **Consume.** `compare_consumed_seq` is derived from the report's H,
    only for a clean barrier or nightly report. It never goes past
    `last_acked_seq` and never backwards. Unclean and log reports record
    only the health fields. Any failed check records nothing
    (`CompareNotRecorded`, a 409 on the route).
  - **Routes.** `POST /admin/compare/<db>/begin|record|abort` (admin token)
    is operator attestation of a verified report: the admin token is the
    operator, and the server checks what it can check itself. With
    `--service-stopped` the same functions run directly under the store's
    primary lock. A failed or skipped run is aborted. The snapshots are
    removed once a run completes, and kept as evidence when it fails.
  - **Pruning.** The outbox prune also never removes a row newer than the
    oldest registered compare start; runs older than 24 h are ignored as
    stale. With these bounds the rule is defence in depth. A prunable row
    is over 24 h old, so a run that started before it would be stale; a
    test with a longer stale bound shows the rule working.
  - **Health.** The replicator's health block shows `d1_missing_vectors`
    (no longer null), `last_compare_at`, `last_compare_mode`,
    `last_compare_clean` and `compare_consumed_seq`.
- **Exit codes**: 0 clean, 5 diffs, 6 skipped, 2 refused.
- **Cron entries** (documented, not installed by the code; tokens are 0600
  files):
  ```
  # nightly, 03:15
  15 3 * * *  scripts/local_primary.py compare <db> --mode nightly --store /data/<db>.db \
                --account <acct> --database-id <id> --read-token-file ~/.config/memora/d1-read.token \
                --admin-token-file ~/.config/memora/all.admin-token --health-token-file ~/.config/memora/all.health-token
  # weekly barrier, Sunday 04:00, under a brief freeze it places and lifts itself
  0 4 * * 0   scripts/local_primary.py compare <db> --mode barrier --brief-freeze --store /data/<db>.db \
                --account <acct> --database-id <id> --read-token-file ... --admin-token-file ... --health-token-file ...
  ```
  - `--brief-freeze` places the freeze only if none is in place and lifts
    only the freeze it placed. An operator's freeze stays.

The shadow period uses the §2.9 nightly check instead. There, the log's key
set for seqs up to `log_cursor_seq` must first equal the outbox's key set
over the same range; the outbox is retained, so this catches lost log
lines.

### 5.3 Rollback (H6)

1. Freeze ingress (the §1 barrier). The replicator keeps draining
   through `connect_replicator()`.
2. Wait for `lag_rows = 0`, then stop the service.
3. Take an export and receipt (P1) of the post-drain D1.
4. Run a barrier compare against the local file. Any diff is reported and
   stops the rollback (P7).
5. Run the D1-side sequence high-water step (§4). If it halts, the rollback
   stops.
6. Validate D1 read-only: `verify_embedding_integrity(conn, stamp=False)`
   against the `d1://` backend. It writes nothing.
   - D1's `embedding_integrity` stamp is now stale (an older epoch).
     Rollback does not restamp it. The restamp is a separate, explicitly
     operator-run admin step (write path 4, §8): `local_primary.py
     restamp <db> --receipt R`. It runs after the rollback compare and after
     the repoint, requires its own fresh export and receipt, and calls
     `verify_embedding_integrity(conn, stamp=True)`. That writes one
     `memories_meta` row, `embedding_integrity`. It is never automatic.
7. Run `recheck` against step 3's receipt.
8. Repoint `MEMORA_DATABASES` to `d1://` and remove the store from
   `MEMORA_REPLICAS`.
9. Start the service, then lift the freeze.

A test drains under the freeze.

**As built in L6 (piece b)**: `memora/rollback.py`, run by hand as
`local_primary.py rollback <db> --phase drain|verify|finish --store P`. The
operator does the deployment actions (stop, repoint, start) between the
phases; every step boundary is checked. The state is
`<out-dir>/<db>/rollback-state.json`, bound to the store and its D1
identity.
- **`drain`** (memora-all live), steps 1-2:
  - Places the freeze, or keeps the operator's.
  - Waits until `last_acked_seq >= head`, re-checking the freeze on every
    poll. The replicator drains through its gate-exempt writer; a test
    drains a store named in `MEMORA_READONLY_DBS`.
  - Past `--drain-timeout` it refuses and leaves the freeze in place.
  - Then the operator stops memora-all.
- **`verify`** (memora-all stopped: `docker inspect` State.Running=false,
  re-checked at every boundary), steps 3-7:
  - It requires the drain phase and a drained store file.
  - Step 3: a verified export and receipt.
  - Step 4: a barrier compare, recorded under the primary lock. Any diff
    HALTS. Nothing on D1 is changed or deleted (P7): keys present only on
    D1 are listed as deletions a human must decide.
  - Step 5: the §4 sequence high-water, through L5's allow-listed operator
    writer. A HALT stops.
  - Step 6: `verify_embedding_integrity(stamp=False)` against D1, through a
    D1Connection holding the read token and marked read-only, so any
    mutation is refused before it is sent. The audit must equal the same
    audit of the local store's `.backup`; the reps, missing, orphan and
    unknown ids, and the counts are compared.
  - Step 7: `recheck`. A fresh export is accepted only when `sqlite_sequence`
    alone moved, and only after step 5 sent an UPDATE.
  - It then prints the repoint: `MEMORA_DATABASES[<db>] =
    d1://<account>/<database>`, and the store removed from `MEMORA_REPLICAS`
    in memora-all's own configuration. (L8's `repoint_mcp_config.py` is for
    client configurations, not this.)
- **`finish`** (repointed and started), step 9 (after review 7712):
  - The freeze must be in place with nothing in flight.
  - `/health/db` must answer 200 ok, show the store's intent journal and
    show no replication block.
  - `/admin/data-volume` (admin token) must report the store's live backend
    identity as exactly the verified `d1://<account>/<database>`, not
    refused. Each store now carries `identity`: `d1_uri`, `account_id`,
    `database_id`, or a local store's canonical path.
  - `recheck` runs against the verify phase's final receipt (full per-table
    hashes, read token). Any drift HALTS and names the changed tables.
  - Any failure keeps the freeze. Only then is the freeze lifted.
- **Phase generations** (7712 P1-3):
  - Each phase run gets a generation id and is marked running while it
    runs.
  - Starting drain or verify clears every later phase, and a failure clears
    the phase it was running.
  - verify records the drain generation it followed, and finish requires
    that pairing. A stale or crashed verify can therefore never let finish
    through.
- **`restamp <db> --receipt R --store P --credential-file C`** (write
  path 4, never automatic):
  - It requires the rollback's verify phase and a freeze.
  - It rechecks a fresh receipt.
  - It audits D1 read-only, then sends exactly ONE statement through the
    operator writer: `INSERT INTO memories_meta (key, value) VALUES (?, ?)
    ON CONFLICT(key) DO UPDATE SET value = excluded.value` with the key
    `embedding_integrity`. The allow-list refuses any other key or
    statement.
  - The value is `embeddings.integrity_stamp`, the same stamp
    `verify_embedding_integrity(stamp=True)` writes. It is read back.

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
| F3 | `sync-to-d1.py`, `sync.sh`, `link-r2-images.py`, `setup-cloudflare.sh`'s remote migration and `package.json` `d1:migrate` exit 1 with a pointer here. `package.json` `deploy` and `setup-cloudflare.sh`'s Pages deploy first run the F2 guard, and refuse on any finding (P6) | worker | each exits 1, or refuses on 3d03123; a test runs each | L1b |
| F2 | CI guard in `graph-ui.yml`. It fails on write SQL inside `.prepare(`/`.batch(`/`.exec(` under `memora-graph/functions/`, on `DB_MEMORA\|DB_OB1\|DB_BESTATION\|DB_RE` used with write SQL, on Python `requests.post` to `/d1/database/…/query`, and on `wrangler d1 execute --remote` / `migrations apply` without `--local`. An allow-list covers only `memora/backends.py` | worker | it fails on 3d03123 and passes after F1 and F3. **L7:** `--scope all` is clean; the handlers step and an `--scope all` step block in `graph-ui.yml` | L1b (tools), L7 (handlers) |
| F1 | viewer read-only: `chat.ts` keeps search and answers but drops the 3 write tools and `computeAndStoreEmbedding`; `[id].ts` PATCH returns 405; the edit controls in `index.html` and `force-graph.html` are hidden or disabled; `test_tag_writes.mjs` is replaced by 405 and no-tool tests | worker, deploy by leader | the deployed viewer returns 405; no edit controls. **L7 (code):** write methods on `[id].ts` answer 405 before any D1 call; `chat.ts` offers no tools and never executes one; `GET /api/capabilities` answers `read_only: true` and the shared `index.html` hides or disables its edit controls unless a server says `read_only: false` (only memora's own graph server does, which keeps editing through memora); `force-graph.html`'s favorite write is removed; `scripts/test_readonly.mjs` replaces `test_tag_writes.mjs`. The deploy is the leader's step | L7 |
| F4a | from the Mac, over nuc8's endpoint: authenticate, and create and delete one memory in a `scratch` local store in the registry. D1 is never touched | user | receipt noted. **L8 tool:** `scripts/local_primary.py check-endpoint` (liveness; admin token enforced; `/admin/data-volume` shows the store is `kind: sqlite`, else it refuses before writing; authenticated `/health/db/<store>`; MCP stats, create, get, delete, gone) | L8 |
| F4 | repoint the Mac `~/.config/memora/credentials.mcp.json` to nuc8: no `d1://`, no `CLOUDFLARE_API_TOKEN` | user | `audit-configs` is clean. **L8 tools:** `scripts/repoint_mcp_config.py` (dry run by default; 0600 backup; optional check-endpoint gate) and `scripts/audit_configs.py`; procedure in `docs/local-primary-credentials.md` | L8 |
| F5 | the same for every `.mcp.json` / `credentials*.mcp.json` on ob1, bestation and re (REVERT.md lists 4) | user | audit output (`scripts/audit_configs.py --host ob1 --host bestation --host re`, exit 0) | L8 |
| F6 | mint the three credentials in §6.1, move memora-all to (a), then revoke the OLD token (the one in the Mac MCP and on other hosts). The viewer keeps its Pages binding (read-only by F1/F2). (a) is revoked only after the last cutover and its rollback window | user | a stale client gets 401/403; memora-all is healthy on (a). Minting, rotation order and retention: `docs/local-primary-credentials.md` | L8, after F4a–F5 |
| F7 | `cloud_sync.schedule_sync` stays, called after the ack | worker | — | L3 |

F1 is swappable: if the viewer later needs edits, it becomes "route writes
to nuc8", and nothing else changes.

### 6.1 Credential inventory (P0-1)

Every D1 token permission is account-scoped: "D1 Read" or "D1 Edit", with
no per-database scope. Separation is therefore by holder and purpose, not by
database.

| credential | permission | held by | how it is delivered |
|---|---|---|---|
| (a) `MEMORA_D1_EDIT_TOKEN` | D1 Edit | memora-all, for every store still served from `d1://` (all stores until their cutover, including the shadow week) | as `CLOUDFLARE_API_TOKEN` in memora-all's env: the name `D1Backend` already reads (backends.py:1767), so there is no code change |
| (b) `MEMORA_D1_REPLICATOR_TOKEN` | D1 Edit | memora-all's replicator, in write mode only | `MEMORA_D1_REPLICATOR_TOKEN`, read only by `replicator.py` |
| (c) `MEMORA_D1_READ_TOKEN` | D1 Read | the shadow applier's reader, and `local_primary.py` export, recheck and compare | `MEMORA_D1_READ_TOKEN`; handed to wrangler only as the subprocess's `CLOUDFLARE_API_TOKEN` (§4) |
| operator | D1 Edit: (b) used interactively | a person running `local_primary.py` restore apply, the sequence step or `restamp` | read from a 0600 file given by `--credential-file`, never from any service env. Each use also needs a receipt |

Which process holds which token, by phase:

| phase | memora-all | scripts on nuc8 | other hosts |
|---|---|---|---|
| before F6 (L1b–L8) | the OLD token | none | the OLD token (Mac MCP etc.) |
| after F6, before any shadow | (a) | (c) for exports | nothing: repointed to nuc8 (F4, F5) |
| store X in shadow week | (a) plus (c) (shadow reader) | (c) | nothing |
| store X cut over, others not | (a) for the uncut stores; (b) for X's replicator; (c) while any store is in shadow | (c); operator (b) for restore, sequence or restamp | nothing |
| after L12, rollback window open (14 days after L12's clean write week) | (a) still held, so rollback remains possible; (b); (c) for nightly compares | (c); operator | nothing |
| window closed | (b); (c) | (c); operator | nothing. (a) is revoked; a later rollback needs a new edit token |



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
- **`test_log_crash_between_append_and_cursor`**: a real subprocess killed
  after the fsync and before the cursor commit. Restart re-appends, and the
  reader deduplicates (P1-5);
- **`test_log_key_set_matches_outbox`**;
- **`test_outbox_kept_until_compare_consumed`**;
- **`test_nightly_retry_retakes_snapshot`** and
  **`test_repeat_exclusion_reported`** (P2-6);
- **`test_replicator_sends_no_ddl`**;
- **`test_drain_under_readonly_flag`** (H6);
- **`test_connect_replicator_single_caller`**.

**Shadow-local** (L3b):
- **app-visible identity**: for `execute`, `executemany` and
  `executescript`, the result or exception equals the unwrapped
  connection's (same `rowcount`, `lastrowid`, exception type and message).
  Cases: absent `meta.changes` (the `len(rows)` fallback in `execute`, and 0
  in the composites); an empty parameter list; an empty script; partial
  failure; and identical `rowcount` on the CAS paths (`_cas_store_crossrefs`,
  `UPDATE memories SET metadata = ? WHERE id = ? AND metadata = ?`) and the
  lease paths (`_ImportLease`, `_rebuild_lease`);
- **`test_only_execute_api_overridden`**: asserts `execute`, `executemany`
  and `executescript` are the parent's functions;
- **`test_indexed_and_trigger_writes_not_dirty`**: a write to an indexed
  table (`tombstones`), and a memories write that fires the epoch triggers,
  both stay clean;
- **`test_copyback_mismatch_marks_dirty`**;
- **`test_trigger_ddl_not_mirrored`** and **`test_ddl_marks_dirty`**: a
  `CREATE TRIGGER … UPDATE memories_meta …` through `execute` is not
  mirrored and marks dirty at once (P2-3);
- **`test_classify_retirement_query`**: the exact
  `WITH ids(id) AS (…) SELECT … UNION …` query from storage.py:4921-4950 is
  `read` and never dirties the shadow (P1-1);
- classifier cases: leading `--` and `/* */` comments; `WITH … INSERT`
  (mirrored, target resolved); `WITH … SELECT` (read);
  `INSERT OR IGNORE`; `UPDATE OR REPLACE`; `REPLACE INTO`; string literals
  containing `;` and keywords; two statements (unknown, so dirty);
- **`test_freeze_d1_rejects_writes_keeps_reads`**;
- **`test_freeze_waits_for_admitted_d1_write`** (P0-1): a FakeD1 write is
  paused after admission, and the freeze is placed. The freeze does not
  report `frozen`, and export does not start, until the write completes. A
  write arriving after the freeze started is refused;
- **`test_freeze_timeout_aborts`**: a stuck in-flight write makes the freeze
  time out, reopen the gate, and list the in-flight entry. The export is not
  run;
- **`test_freeze_prior_open_writer`** (P0-2): a local writer opened before the
  freeze is refused on its next mutation. An open transaction is waited for
  until it commits;
- **`test_replicator_and_shadow_exempt_from_gate`**;
- **`test_scripts_refuse_without_frozen_zero_inflight`**;
- **`test_intent_kill_after_send_before_response`** (round-9 P0): a
  subprocess is killed with `os._exit` after the request is sent and before
  the response is handled. FakeD1 commits the write. At restart the intent is
  open and the gate is `frozen-unsafe`, so export refuses. After 60 s (a test
  clock), the evidence is shown, and after `reconcile --accept` the state
  becomes `frozen`;
- **`test_intent_fsync_failure_blocks_send`**: `os.fsync` is patched to
  raise. FakeD1 receives nothing, the app gets `IntentJournalError`, and the
  gate state is unchanged;
- **`test_resolution_append_failure_is_extra_unsafe`**: a full disk during
  the resolution append leaves an open intent, the store is `frozen-unsafe`,
  and it is resolvable by `reconcile --accept`;
- **`test_partial_append_then_send_survives_restart`** (round-10 P0): a
  partial intent write, then an fsync failure (so no send), then repair,
  then a successful D1 write, then a kill and restart. That write's intent is
  present and open, and no record was merged into a torn line;
- **`test_repair_failure_keeps_gate_refusing`**;
- **`test_malformed_middle_record_refuses_start`**, and
  **`test_torn_final_line_dropped_and_logged`**;
- **`test_compaction_serialized_with_append`** (round-10 P1): compaction is
  paused before its rename while another thread attempts a mutation. The
  mutation waits, then appears in the replacement journal;
- **`test_journal_single_writer_flock`**: a second opener, and
  `local_primary.py` while memora-all runs, are refused through the lockfile;
  `--accept` goes through the endpoint;
- **`test_compaction_then_write_survives_restart`** (round-11 P0):
  compaction, then immediately a live D1 write, then a kill and restart. That
  write's intent is in the journal at the path, and it is open;
- **`test_lock_after_compaction_refused`**: right after compaction, a second
  process tries to lock the new journal path's lockfile and is refused;
- **`test_stale_fd_refuses_send`**: an injected stale fd (an inode mismatch)
  refuses the send; FakeD1 receives nothing, and the gate is
  `frozen-unsafe`;
- **`test_every_open_intent_needs_accept`** (round-10 P2): an INSERT whose
  row is present, a no-op UPDATE and a DELETE of an absent key all stay open
  until `reconcile --accept`;
- **`test_intent_journal_compaction_keeps_open`**;
- **`test_intent_survives_restart_and_reseed`**: the seed refuses while an
  intent is open;
- **`test_startup_refuses_d1_primary_without_mount`** (L2a);
- **`test_cursor_is_gated`** (P2c): a mutation through
  `conn.cursor().execute(...)` on a frozen store is refused, and an admitted
  transaction's cursor shares its token;
- **`test_pragma_whitelist`** (P1-3): `optimize` and `wal_checkpoint` are
  refused under freeze and mark the shadow dirty; `table_info` and
  `database_list` are allowed under freeze and are reads;
- **`test_export_frozen_rejects_concurrent_child_write`**: a same-count
  crossref write during the paged fallback fails with `StoreReadOnlyError`
  and is not captured; **`test_export_frozen_rejects_schema_change`**
  (P1-2);
- **`test_executemany_partial_then_fail_marks_dirty`** and
  **`test_executescript_partial_then_fail_marks_dirty`**: completed elements
  are enqueued, dirty is set, and the original exception propagates;
- **`test_unknown_outcome_timeout_marks_dirty`**;
- **`test_select_only_reader_rejects_p2_legal`**: a per-key UPSERT, a
  per-key DELETE, PRAGMA, DDL, a multi-statement body and `RETURNING` are all
  rejected; the missing `executemany`/`executescript` raise
  `AttributeError`. Mutation: loosen the checker to the P2 set, and the
  test must fail;
- **`test_stale_replica_read`**: FakeD1 answers the first two copy-back reads
  with `served_by_primary=false` and old values, then the fresh row.
  Converges within bounds. Always stale means dirty;
- **`test_applier_refuses_with_read_replication`**;
- one test per dirty-rule row 1–9, each asserting dirty is set and FakeD1
  shows no extra write;
- **`test_restart_with_nonempty_queue_marks_dirty`**: a subprocess is killed
  with items queued;
- copy-back equality, including `datetime('now')` columns and ids;
- a property run: random writes through the app path, and the shadow equals
  FakeD1 after the drain;
- the nightly check finds an injected apply bug and an injected builder bug.

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
- **`test_sequence_step_halts_when_rejected`**: FakeD1 rejects
  `UPDATE sqlite_sequence`; the step halts and nothing else is sent
  (P1-3);
- R2 restore (P0-2): every difference becomes a conflict group; D1 receives
  nothing without a selection; a group missing from `--approve` refuses the
  whole apply; `d1` selections never write D1; a wrong conflicts sha refuses;
  P3 applies; a changed D1 preimage aborts only that group (P1-3);
- receipt (P1-4): refused when the R2 read-back hash differs; refused when
  the epoch changes during the export; `recheck` detects a count or epoch
  change and forces a fresh export; **`test_recheck_same_count_crossref_edit`**:
  a same-count edit to crossrefs, actions, tombstones or meta is caught by
  the hash (P1-3);
- **`test_restamp_requires_receipt_and_operator`** (P2-5);
- **`test_rollback_validate_writes_nothing`**: FakeD1 records only SELECTs
  during step 6 (P2-7);
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

| slice | content | dark by | D1 reads | D1 writes | live rows touched |
|---|---|---|---|---|---|
| L1b | F3, plus F2 for the tools (P6) | — | none | none (removes writers) | 0 |
| L2a | launcher and volume (C1); see below | only adds a mount | none | none | 0 |
| L2 | §1, M10, freeze gate with the write-ahead intent journal and reconciliation, `classify_statement`, `D1SelectOnlyConnection`, `_GatedCursor`, `connect_replicator` | sync pieces: no `sync_state`. The gate and journal are **live** for every `d1://` primary (a fsync per mutation; freeze and reconcile are used only by operators); deploy after L2a. There is **no bypass flag**; the operator controls are stopping the container, or repairing the mount or journal | reconciliation: pk and value SELECTs through `D1SelectOnlyConnection` with the read token | none new. The gate only admits or refuses the app's existing writes. `ensure_schema`'s existing D1 DDL is unchanged; `_ensure_sync_outbox` returns early on D1 | 0 |
| L3 | §2: log and write modes, P2/P3, H2/H3, metrics, F7 | `MEMORA_REPLICATION` unset | write mode: epoch SELECT (preflight and postcheck), pk SELECT read-back. Log mode: none | write mode only: per-key UPSERT on 6 tables; embeddings DELETE+INSERT by pk; per-key DELETE (P2, P3). Log mode: none | 0 |
| L3b | §2.9 shadow-local | `MEMORA_SHADOW_LOCAL` unset | pk SELECT copy-back through `D1SelectOnlyConnection` with the D1 Read token; `read_replication` config GET; compare full-table SELECTs | **none new**: the wrapper only forwards the app's existing writes, unchanged | 0 |
| L4 | §3 | local stores only | none new | none new (D1 absorb path unchanged) | 0 |
| L5 | §4 export, seed, recheck, restore, snapshot, sequence step | run by hand | `wrangler d1 export --remote`; per-table `COUNT(*)` and full-table SELECTs (hashes); epoch SELECT; `SELECT … FROM sqlite_sequence`; conflict reads | R2 restore: only per-key UPSERT/DELETE selected in `--approve` (P3). Sequence step: `UPDATE sqlite_sequence SET seq=? WHERE name=? AND seq<?` (≤ 2 statements, operator-run, receipt and recheck). Nothing else | 0 at merge |
| L6 | §5 compare, rollback, `restamp` | run by hand | full-table SELECTs of the 7 tables; epoch; `verify_embedding_integrity(stamp=False)` reads | rollback: only L5's sequence UPDATE. `restamp` (a separate operator step): one `memories_meta` `embedding_integrity` write | 0 |
| L7 | F1, F2 for handlers; viewer deploy | viewer deploy | viewer GET handlers (unchanged) | none (removes writers) | 0 |
| L8 | F4a–F6 including the §6.1 credentials, `audit-configs` | ops | none | none | 0 |
| L9 | first store (`re` or `bestation`, the less critical), see below | per store | L3b, L5, L3 write-mode reads | the app's existing writes during shadow; after cutover, L3 write mode | whole store |
| L10 | the other of `re`/`bestation`: same as L9, after L9 has had a clean week in write mode | per store | same | same | whole store |
| L11 | `ob1`, same, after a clean week | per store | same | same | whole store |
| L12 | `memora`, same, last (P5) | per store | same | same | whole store |
| L13 | remove the shadow wrapper (§2.9) | — | — | — | 0 |

**L9 sequence, per store:**
1. The snapshot cron (§4) is installed and rehearsed on shadow files before
   the first cutover. From each cutover on, it covers that local primary.
2. Export every store (P1). Each export is frozen per store, one store at
   a time.
3. For store X, under one continuous freeze:
   1. export and receipt;
   2. seed the shadow at `/data/shadow/<db>.db` from that same export;
   3. set `MEMORA_SHADOW_LOCAL` and `MEMORA_REPLICATION=log`, and restart
      memora-all (the freeze file survives the restart, and the gate starts
      `frozen`);
   4. confirm `applier_alive` and `dirty = 0` on `/health/db/<name>`;
   5. lift the freeze.
   No D1 write can fall between the export and the start of mirroring.
5. Wait for at least 7 consecutive clean nights (§2.9).
6. Cutover:
   1. freeze the `d1://` store;
   2. drain the shadow queue;
   3. take a fresh export and receipt;
   4. FULL re-seed to `/data/<db>.db` (the shadow file is archived, not
      promoted);
   5. run `recheck`;
   6. repoint (`MEMORA_DATABASES` to `sqlite://`, add `MEMORA_REPLICAS`,
      remove the store from `MEMORA_SHADOW_LOCAL`);
   7. set `MEMORA_REPLICATION=write` (the preflight epoch comes from the
      receipt);
   8. lift the freeze.
7. Run a clean week in write mode.

After cutover, the accepted RPO is the seconds of async loss (msg 7507).
The next store's shadow may start once the previous store has cut over.

**D1 write inventory** (the whole document was re-checked for automatic D1
writes). Every D1 write the plan introduces is one of:
1. replicator write mode: per-key, outbox-driven, P2/P3-guarded;
2. R2 restore: only operator-selected per-key rows, P2/P3-guarded;
3. the sequence UPDATE: operator-run, with receipt and recheck, halts on
   rejection;
4. the post-rollback `restamp`: operator-run, with its own receipt, after
   the rollback compare and the repoint. It writes one `memories_meta`
   row. Never automatic.

Checked and found to write nothing to D1:
- export, recheck, seed, default restore, compare and snapshot;
- rollback: validation uses `stamp=False`;
- `resume`;
- H3 reconciliation: SELECTs, then a resend through (1);
- the shadow applier: `D1SelectOnlyConnection`, SELECT only, with the D1
  Read token;
- the ShadowingD1Connection wrapper: it forwards the app's existing writes
  unchanged and adds none;
- health and watchdog;
- F4a: a scratch local store;
- `cloud_sync` `/broadcast`: not D1;
- the replicator's D1 connection: schema-free, no DDL.

Pre-existing D1 writes the plan leaves as they are:
- memora-all's own writes and `ensure_schema` DDL on stores still served
  from `d1://` (including the shadow period, and after a rollback);

**L2a, launcher and volume (C1):**
- `memora-instance.sh` `cmd_up`, in the `MEMORA_DATABASES` branch (line
  ~203): when any registry URI is `sqlite://`, it adds
  `-v memora-<instance>-data:/data` (a named volume) and
  `-e MEMORA_DATA_VOLUME=<name>`.
- `deploy-memora-all.sh` replaces `VOLUME_ID` (the name read by
  `docker inspect`, line 133) with the named volume `memora-all-data`. It
  copies the old volume once, with `docker run --rm -v old:/from -v new:/to`,
  and refuses if `docker volume inspect` shows an anonymous (64-hex) name.
- Server startup refuses to serve any store that uses `/data` unless the
  checks below pass. From L2 onward that is every local store AND every
  `d1://` primary store, because their gates keep the intent journal in
  `/data/intent/` (round 9):
  - `/data` is a mount point (its `st_dev` differs from `/`'s);
  - a probe file can be written, fsynced and removed, in `/data` and in
    `/data/intent/`;
  - `MEMORA_DATA_VOLUME` is set and is not a 64-hex name.
- For this reason the gate and the journal are live, not dark, for every
  `d1://` primary from L2's deploy. L2 therefore deploys only after L2a's
  named volume is in place.
- **Memory gate:** measure RSS with four local stores (live-sized fixtures,
  964+ rows), FTS and the 384 MB corpus cache, against the 768 MB limit.
  The launcher default becomes max(768m, 1.5 × peak RSS).
  **Measured in L2a** (`scripts/measure_memory_gate.py`, offline synthetic
  stores): on server2 (Fedora, x86_64), inside the memora image built from
  this tree with podman (python 3.12.14, the container's interpreter). One
  process imports `memora.server`, then runs 3 passes over the 4 stores:
  `semantic_search` (loads and caches the corpus snapshot), `hybrid_search`
  (FTS5 plus vector) and `list_memories` with a query (FTS5). Peak RSS is
  `ru_maxrss`. Rows are written through `add_memory` with 600-character
  content, 3 tags and a synthetic dense vector; no embedding API is called.
  Baseline after import: 69.6 MB.

  | stores × rows | dim | cache budget | snapshots cached (estimated MB) | peak RSS | 1.5 × peak |
  |---|---|---|---|---|---|
  | 4 × 1000 | 1024 | 384 MB | 4 (358) | 525.7 MB | 789 MB |
  | 4 × 1500 | 1024 | 384 MB | 2 (269) | 581.0 MB | 872 MB |
  | 4 × 1500 | 1536 | 384 MB | 1 (201) | 629.7 MB | 945 MB |
  | 4 × 1000 | 1024 | 256 MB | 2 (179) | 422.5 MB | 634 MB |
  | 4 × 1500 | 1024 | 256 MB | 1 (134) | 425.5 MB | 639 MB |
  | 4 × 1500, under `--memory=768m` | 1024 | 384 MB | 2 (269) | 580.9 MB, not OOM-killed | — |

  - The corpus-cache estimate undercounts: with all 4 snapshots cached
    (358 MB estimated), RSS grew 456 MB over the baseline.
  - The launcher default is therefore **960M** (945 MB rounded up to
    64 MiB), in `memora-instance.sh` and `deploy-memora-all.sh`
    (`--memory 960m`).
  - The alternative that keeps 768m is `MEMORA_CORPUS_CACHE_BUDGET_MB=256`
    (peak ≈ 426 MB). The cost: only 1–2 of 4 snapshots stay cached, so the
    others are reloaded on use. That is cheap for a local store but costs
    D1 round trips for a store still on `d1://`.

**Order:**
- L1b first, then L2a and L2.
- L3, L3b and L4 to L6 in any order after L2.
- L7 and L8 gate L9.
- Each cutover can be reversed with §5.3 until the next store's slice
  starts.

## 9. Open items (owned by a slice)

| item | owner | resolution due |
|---|---|---|
| (a) `/admin/freeze` auth: an admin-only token (not the health token), or binding the admin routes to loopback only, reached through `docker exec` | L2a | **resolved in L2a**: `MEMORA_ADMIN_TOKEN` only (`memora/admin_auth.py`, installed with `admin.set_admin_auth`), no loopback exemption, 403 when unset, startup refusal when it is short or equals the health token. A test calls every `/admin/*` route without it. Operators call the routes through `docker exec` with the container's own env token |
| (b) the setup PRAGMAs on the raw connection in `connect()`: document that `journal_mode=WAL` and `busy_timeout` are the only ones; both are idempotent and cannot change row data on a frozen primary. A test asserts the set. **L2 status:** `connect()` issues no PRAGMA yet (its only setup statement is one read); the gate is armed after setup (`test_setup_statements_are_not_gated`). L4 adds the two PRAGMAs there and extends the test to assert the exact set | L2 (arming order), L4 (the PRAGMA set) | with L2 / L4. L2a adds `tests/test_connect_pragmas.py`: a trace of the raw connection must show exactly `writer_setup_pragmas()` plus the touch read |
| (c) the `.cursor()` bypass on `_LockedWriterConnection`: **designed now** (§1, `_GatedCursor`) and tested by `test_cursor_is_gated` | L2 | with L2 |
| (d) automatic trigger and meta effect checks in reconciliation (epoch bump, `memories_meta` rows) | L3 | optional; until then, every intent needs the operator |
| (e) an ownership nonce so reconciliation can prove an effect: for example a `memories_actions` row keyed by the intent id, written in the same request, or a metadata field carrying the intent id. With it, INSERT (and, with (d), UPDATE and DELETE) reconciliation can become automatic | L3 | optional; until then, every intent needs `reconcile --accept` |
| (f) `scripts/d1_write_guard.py` limits (L1b review 7574): H1 misses dynamically built write SQL (``db.prepare(`UPDATE ${table} SET …`)``, `"UPDATE " + table + …`); T2/T4 are line-based and miss `--remote` on a continuation line or from a variable; T5 accepts the guard's filename anywhere earlier on a deploy line, even in a comment or `echo`. An AST or data-flow guard, or targeted tests for those forms, must land before the handlers scope becomes blocking | L7 | **resolved in L7** with targeted rules and tests (`tests/test_l7_guard_forms.py`): H1 also matches an interpolated table (`UPDATE ${…} SET`); H2 flags a literal or template fragment headed by an uppercase write verb (concatenation); T2/T4 read logical lines (backslash continuations joined); T6 flags a wrangler D1 command whose arguments come from a shell variable without a literal `--local`; T5 requires an executed guard run with `--scope all` chained directly before the deploy (`&&`, or `\|\| { …; exit 1; };`), so a comment, `echo` or string no longer counts. Not covered (documented): SQL assembled from non-literal pieces that never contain an uppercase verb literal, e.g. a verb read from config |
| (g) `memora-graph/README.md` still calls `npm run setup` a "full automated setup", although it now exits at the remote-migration step: label it retired or partial | L7 | **resolved in L7**: labelled partial (it stops after creating the D1 database), and a "Read-only viewer" section documents the 405s, the tool-free chat, `/api/capabilities` and the tests |
| (h) replay validates every field it relies on (id, sql, outcome, next_id), inside the malformed-record path (L2 review 7584 P2) | L2-followup | **done in L2 round 2** |
| (i) `POST /admin/reconcile` requires, besides a verified export receipt for the database: `operator`, `intent_id` (equal to the path's), `decision` (applied / not-applied) and `evidence_sha256` equal to the evidence `GET /admin/intents` last showed (L2 review 7584 P2) | L2-followup | **done in L2 round 2** |
| (j) the journal-health re-check in `_execute_api` is not atomic with `_send`: a repair failure on another thread can land between the check and the send (L2 review 7588 P2). This is safe: the intent is already durable on disk before the check, so a request that goes out anyway is recorded as an open intent (frozen-unsafe until an operator accepts it), exactly like an unknown outcome. L3 may make the check and send one critical section if the replicator needs it. **L3:** it does not: the replicator never uses the application journal (its D1 writer is `ReplicaD1Connection`, guarded by its own durable H3 marker) | L3 | closed in L3: not needed |
| (k) `acquire_primary_lock` runs before `_ensure_parent_dir`, so a live primary whose parent directory does not exist yet raises FileNotFoundError on first start instead of creating it (L2 review 7592 P2). The seed creates the parent | L5 | **done in L5 piece b**: `_open_writer` creates the parent directory before `fence()`, and the seed creates it before taking the target's primary lock |
| (l) watchdog alerts for replication (§2.5: `oldest_unacked_age_s > 300`, `status == halted`, `d1_missing_vectors > 0`). L3 exposes the metrics on `/health/db/<db>` (authorised); `scripts/memora_watchdog.py` is liveness-only by design, so the alert is a separate check | L9 | before the first cutover |
| (m) `d1_missing_vectors` is reported as `null` until the §5.2 compare exists; the synchronous-commit flag (§2.8) is not implemented | L6 / optional | **done in L6 piece a**: the compare computes it and `record_compare` stores it; the health block reports the last value. The §2.8 synchronous-commit flag stays unimplemented (optional) |
| (n) per-table delete-guard configuration, if the log-only week shows `memories_meta` or `tombstone_components` churn tripping the under-100-rows rule (L3 review 7599 P2: the strict rule is accepted for the shadow week) | L9 | after the log-only week |
| (o) `_WriteGate.enter(exempt=True)` relied on trusted in-process callers (L3 review 7603 P2) | L4 | **done in L4**: exempt entries are refused unless the caller module is `memora.replicator` (`test_exempt_gate_entries_are_for_the_replicator_only`) |
| (p) a second `freeze()` on an already-frozen store returns without waiting for a newly entered exempt replicator token, so the scripts must re-check `/health/db/<db>` for `in_flight = 0` at every step boundary (already required by §1) (L3 review 7603 P2) | L5 | **done in L5 piece a** (kept through b/c): `FreezeClient.check` re-reads `/health/db/<db>` at every step boundary and continues only on `frozen`, `in_flight = 0` and no open intent |
| (q) L4 decisions (§3): WAL is set only on live primaries, so every other local store keeps its journal mode, while `busy_timeout` is set on every writer. `list_absorb_inflight` keeps reading the table instead of returning `[]` on a transactional store: no new rows appear there, but rows left by an earlier version are still reported, not hidden. Inside `store_write`, inner commits are deferred and an inner rollback aborts the transaction (`StoreWriteAborted`), so no helper can split phase 3. `add_memory` refuses to compute an embedding under the lock, and R2 deletions run after the commit | L4 | done |
| (r) a caught `rollback()` inside `store_write` could still let the outer commit happen (L4 review 7610 P2) | L5 | **done in L4 round 2**: `StoreWriteAborted` poisons the transaction, so `store_write` rolls back and raises instead of committing |
| (s) `sweep_pending_images` cannot apply while a store is persisted-frozen (L4 review 7610 P2) | L5 | **done in L4 round 2**: `DELETE /admin/freeze/<db>` (`thaw_store`) runs the sweep for a local store |
| (t) a memory whose `images` field keeps changing between the upload and the swap stays `images_pending` until a later startup or thaw sweep: conservative by design, since the swap never overwrites a newer `images` (L4 review 7614 P2) | L9 | add a metric for rows left `images_pending` |
| (u) `_absorb_link`'s fixed savepoint name assumes `add_link` never opens a same-named nested savepoint (L4 review 7614 P2) | L5 | **done in L5 piece a**: `_absorb_link` refuses a nested `absorb_link` savepoint on the same connection, and `add_link`'s docstring records the constraint |
| (v) a failed step leaves the freeze in place on purpose, so its output must say how to lift it (L5 review 7630 P2) | L5 | **done in L5 piece b**: the failure JSON of export, recheck, seed and sequence-highwater carries `recovery: local_primary.py thaw <db> ...` |
| (w) the conflicts file and the approve review should show inbound `memories_crossrefs.related` dependencies between groups: conflicting choices can leave a logical stale reference (L5 review 7684 P2) | L6 / L9 | **done in L6 piece b**: each memory group lists `inbound_refs` (the memories whose crossrefs point at it, per side, and whether they are conflict groups); the apply report and `--dry-run` list `dangling_references` for the chosen sides (a warning) |
