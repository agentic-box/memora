# Changelog

Release notes for memora, newest first.

CONSOLIDATED 2026-08-22 from four version-stamped files
(RELEASE_NOTES_0.3.0.md .. RELEASE_NOTES_0.3.3.md). Those grew one per release
and could not be linked to from anywhere stable -- a link to
RELEASE_NOTES_0.3.2.md is stale the day 0.3.3 ships. This file has a fixed
name, so README, docs and issues can point at it and never rot.

The content was CONCATENATED rather than discarded: git tags exist for every
version, but the GitHub releases page only carries 0.3.2 and 0.3.3, so the
0.3.0 and 0.3.1 notes lived nowhere else. Add new releases at the top.

## Unreleased

### Local-primary L9a round 2 (review 7701)
- `shadow-night` needs a drained, stable applier: it reads the `shadow` block of `/health/db/<db>` (new `--health-token-file`, `--memora-url`) and waits up to `--stable-wait-s` (300 s) for `applier_alive`, with `queue_depth`, `unfinished`, `pending_keys` and `inflight` all 0. After the D1 compare, the applier `instance` and `generation` must be unchanged and the block still drained. Otherwise the night is DEFERRED: exit 6, `shadow_state` untouched, nothing marked dirty, nothing counted.
- The `shadow` health block adds `inflight`, `unfinished`, `generation` and `instance`.
- Check (b) allows a log key with no outbox row only for a `memories` parent the replicator added to a child upsert (`replicator.is_added_parent`): every record of it must be an upsert with the same attempt and seq as an upsert of that memory's `memories_embeddings`/`memories_crossrefs` row. Any other extra key is still a defect. The case also relies on L6's `iter_log` dedup fix, which landed first, and a test covers it.
- Copy-back: the local equalisation rechecks the generation after its writes, holding the applier lock that a shadowed mutation's start needs, and commits under it. If the generation moved, the transaction is rolled back and the keys stay pending.
- Typo in `docs/local-primary-credentials.md` ("Then Then").

### Local-primary L3/L9a: the delete guard records would-halt events in log mode
- In log mode the P3 delete guard no longer halts the replicator (log mode sends nothing to D1; leader 7699, plan §9 (n)). A batch over the limit (> 50 net deletes, or > 1% of the table, so any delete from a table under 100 rows) adds a `sync_would_halt` row (table, deletes, row count, threshold, attempt id; once per attempt), increments `sync_state.would_halt_count`, sets `last_would_halt`, logs a warning, and is logged as usual. Write mode still halts before sending.
- The replication health block (`/health/db/<name>`, `replication`) gets `would_halt_count` and `last_would_halt`.
- `shadow-night` reports the would-halt events recorded since the previous night (`would_halt`; `shadow_state.would_halt_reported_id`). They do not fail the night. A halted log (any other halt cause) still does.

### Local-primary L9a (piece b): nightly shadow check, health block
- `local_primary.py shadow-night <db> --shadow S --seed-export SQL --account A --database-id D [--read-token-file F]` (`memora/shadow_night.py`, §2.9), no barrier and no D1 write:
  - (a) compares the seven replicated tables of the shadow with D1, read through the READ token (the replicator's `memories_meta` exclusions apart). A diffed key is re-read once after a pause, so a write in flight during the read does not count.
  - (b) checks that the replicator log's key set equals the outbox key set up to `log_cursor_seq`, and that the log is not halted. It replays the log into a scratch copy of the seed export and compares it with a `.backup` of the shadow; keys touched after the cursor are left out.
  - A clean night increments `clean_nights` once per UTC day; `ready_for_cutover` at 7. A diff resets the count and marks the shadow dirty; a halted log resets the count without marking the shadow dirty. Exit 5 when the night is not clean.
- `/health/db/<name>` gets a `shadow` block for a shadowed store: `enabled`, `dirty`, `dirty_reason`, `queue_depth`, `pending_keys`, `applier_alive`, `clean_nights`, `last_clean_night`, and `refused` when the applier could not start.

### Local-primary L9a (piece a): shadow-local hook, applier, dirty rule
- Per `docs/local-primary-implementation.md` §2.9 and §0 P4. No new D1 statement: the shadow only forwards the app's existing writes, unchanged, and reads D1 through `D1SelectOnlyConnection` with `MEMORA_D1_READ_TOKEN`. Dark unless `MEMORA_SHADOW_LOCAL` names a store.
- `memora/shadow.py`: for a store named in `MEMORA_SHADOW_LOCAL` (`{"<db>": "/data/shadow/<db>.db"}`), `D1Backend.connect()` returns a `ShadowingD1Connection`. The L2 gated and journaled `_execute_api` stays the parent and runs unchanged; the app gets D1's result and exception objects as they were. After a mutating statement D1 answered successfully, still inside the write gate (enter, D1 request, enqueue, leave), a new `_after_mutation` hook enqueues it when its target is one of the seven replicated tables. Other targets are ignored, DDL marks the shadow dirty at once, and an unknown statement marks it dirty. Any exception from a mutating statement marks the shadow dirty and is re-raised as the same object; a refusal before sending (write gate, intent journal) reached nothing and is not dirty. `execute_batch` raises.
- `ShadowApplier` (one thread, one FIFO queue, off the request path): replays each item on the shadow in a `store_write`, collects the touched keys from the outbox triggers, and adds D1's `last_row_id` and the statement's own primary key. At a quiescent point (no shadowed mutation in flight, every item replayed; a generation counter rechecked after the reads) it copies the pending keys back by primary key -- `served_by_primary` required, and the written values where the key's last statement was its own write, up to 5 attempts 200 ms apart, never inside a `store_write` -- then makes each local row equal to D1's with the replicator's statement shapes and verifies it column for column. See the plan note ("As built") for why copy-back is not per item.
- The dirty table, every row: a failed or unknown-outcome mutating request, a failed element after earlier ones, a failed enqueue or a stopped applier, a replay error (rolled back), a copy-back read that fails, stays replica-served or shows stale written values, a local row differing after copy-back, an unknown statement or an unresolvable target, DDL, the applier thread dying, and a start that finds `shadow_state.clean_shutdown = 0`. The first reason is kept, and `clean_nights` restarts at 0. `clean_shutdown` is set only by a graceful stop that drained the queue and settled every pending key.
- Server startup: a malformed `MEMORA_SHADOW_LOCAL` is fatal (exit 2). Each applier starts only if the store is on `d1://`, `MEMORA_D1_READ_TOKEN` is set and D1's `read_replication.mode` is `disabled`; otherwise it is refused and every write marks the shadow dirty. Appliers are drained at exit. The log-mode replicator (L3) reads the shadow's outbox as before.
- `local_primary.py shadow-init --shadow /data/shadow/<db>.db`: makes a store seeded by `local_primary.py seed` a shadow file (a clean `shadow_state`, `clean_shutdown = 1`). It refuses a file without the sync outbox.

### Local-primary L6 (piece b): rollback runbook, restamp
- Per `docs/local-primary-implementation.md` §5.3, §8 L6, §9 w.
- **`local_primary.py rollback <db> --phase drain|verify|finish --store P`** (`memora/rollback.py`):
  - `drain` (live): freeze, then wait for the replicator to ack the whole outbox.
  - `verify` (memora-all stopped, checked at every boundary): verified export; barrier compare (any diff halts; D1-only keys are reported, never deleted); the D1 sequence high-water; a read-only D1 integrity audit that must equal the local store's; recheck (only the sequence counters may move). It then prints the repoint.
  - `finish` (repointed), still under the freeze:
    - `/health/db` 200 ok with the store's journal;
    - `/admin/data-volume` reports the store's live backend identity (new `identity` field), which must be the verified `d1://<account>/<database>`;
    - a recheck of verify's final receipt must show no drift.
    Any failure keeps the freeze; then the freeze is lifted.
  - Each phase run has a generation id: a new drain or verify clears the later phases, a failure clears its own phase, and finish needs the verify that followed the current drain.
- **`restamp <db> --receipt R`** (write path 4, never automatic): under a freeze and a rechecked receipt, a read-only D1 audit, then exactly one allow-listed `embedding_integrity` UPSERT carrying `verify_embedding_integrity(stamp=True)`'s stamp, read back.
- §9 (w): conflict groups list their inbound crossrefs, and the R2 restore apply and `--dry-run` warn of `dangling_references`.
- A test drains a store named in `MEMORA_READONLY_DBS` through the replicator's exempt writer.


### Local-primary L6 (piece a): the §5.2 compare
- Per `docs/local-primary-implementation.md` §5.2, §2.7, §2.9 (b), §9 m. New `memora/compare.py` and `local_primary.py compare <db> --mode barrier|nightly|log --store P`. D1 is read with the read token only; nothing is written to D1.
- One `.backup` snapshot per run. Every replicated table and column is compared: keys on one side only, changed rows, embedding provenance mismatches, `d1_missing_vectors`, and meta minus the excluded keys.
- The modes follow §5.2:
  - barrier: freeze or stopped service, re-checked at every boundary, drained first;
  - nightly: `S`/`H`/`K`, skip past the ack timeout, one retry that retakes all three, hot keys across two nights;
  - log: the log's key set against the outbox, then the log replayed into the seed export and compared with `S`.
- Recording is verified by the store, not trusted. A run registers first (`begin`, the store's clock). `record` takes the report file path and checks: its hash, the store name and D1 URI, the registered run (at most 12 h old), and the snapshot's hash. It advances `compare_consumed_seq` to the report's H only for a clean barrier or nightly report, never past `last_acked_seq`. The routes are `POST /admin/compare/<db>/begin|record|abort`, or the same calls directly with `--service-stopped`.
- The outbox prune never removes a row newer than a running compare's start.
- `/health/db` now shows `d1_missing_vectors`, `last_compare_at`, `last_compare_clean`, `last_compare_mode` and `compare_consumed_seq`.
- The weekly barrier's `--brief-freeze` lifts only a freeze it placed. The cron entries are documented in §5.2.
- Fix found by the log compare: `replicator.iter_log` deduplicated on `(seq, index)` and dropped a child statement that shared its seq with the FK parent the batch added. It now deduplicates on `(seq, table, pk, index)`.


### Local-primary L8: config audit, endpoint check, repoint tool, credential inventory
- Per `docs/local-primary-implementation.md` §6 F4a–F6 and §6.1. This slice issues no D1 statement: it reads configuration files and talks only to memora-all's HTTP endpoint.
- `scripts/audit_configs.py` (logic in `memora/config_audit.py`, standard library only): finds every memora client configuration that can reach D1 directly -- a `d1://` URI, `MEMORA_STORAGE_URI` or a `MEMORA_DATABASES` entry on `d1://`, or `CLOUDFLARE_API_TOKEN` / `CF_API_TOKEN` with a value or a reference -- in `*.mcp.json`, `.claude.json`, `*.env`, `instances/*.env`, launchd plists, shell rc files, `~/.codex/*` and `~/.config/memora/*` under `$HOME` or the given roots (caches, `node_modules`, `.git` and most of `~/Library` skipped). Findings name the file, line, kind, a masked value (first four characters and the length; token values are never printed) and whether the file is memora-all's own (`instances/all.env`; on nuc8, `~/.config/memora/credentials.mcp.json` and `all.*`; `--memora-all PATH`), which is reported but does not fail. `--host H` (repeatable) audits other hosts by piping the module to `ssh -o BatchMode=yes H python3 -`. Exit 1 while any other direct-D1 client remains, or when a file cannot be read or a host cannot be audited; 0 when clean. Repoint backups (`*.bak-repoint-*`) are found too, since they hold the old token. Every audit also inspects the environment of EVERY persisted container of each runtime on the host, running or stopped (docker/podman `ps -a`, Apple's `container list --all`; `MEMORA_AUDIT_RUNTIMES` overrides the list), masked the same way, as kind `runtime_env` with the container's name and state; a stopped container holding the old token blocks too (it can be restarted) until recreated or removed; a runtime that cannot list or inspect its containers makes the host not clean; the `memora-all` container on nuc8 is memora-all's own. `--containers-only` re-checks just the containers. Nothing a runtime or ssh prints is copied into a report (command name and exit status only), and every report starts with its coverage (this user's files and runtimes; another user's rootless runtime is invisible). An audit that queried no container runtime -- `MEMORA_AUDIT_RUNTIMES` set and empty, or leaving out a runtime installed on the host -- is not clean ("no container runtime audited").
- `scripts/local_primary.py check-endpoint --memora-url --health-token-file --admin-token-file [--store scratch] [--no-write]` (`memora/endpoint_check.py`), for F4a. It checks, and stops at the first failure: liveness; that the admin token is enforced (no token and the health token are refused); that `/admin/data-volume` lists the store as `kind: "sqlite"` and not refused (a `d1`/`s3` store, an unknown store, or a memora-all without kinds is refused before anything is written); authenticated `/health/db/<store>`; then over MCP `/mcp/<store>`: `memory_stats` bound to the store, and `memory_create` → `memory_get` → `memory_delete` → `memory_get` (not found) of one tagged throwaway memory. Token files must be 0600 and owned by the user.
- `/admin/data-volume` also reports each store's `kind` (`sqlite`, `d1`, `s3`).
- `scripts/repoint_mcp_config.py FILE --url http://nuc8:8920/mcp/<store>`: in a JSON MCP config (`credentials*.mcp.json`, a workspace or Codex `.mcp.json`, `~/.claude.json`), rewrites only the routing of each server entry that reaches D1 directly: `command`/`args` become `"type": "http", "url": …`, and `CLOUDFLARE_API_TOKEN`/`CF_API_TOKEN`, a `d1://` `MEMORA_STORAGE_URI` and the `d1://` entries of `MEMORA_DATABASES` leave `env`. Every other env key stays (`memora-instance.sh`'s `cred_args` reads them); `--drop-env` removes the env. Output never contains a value: keys and routing only, each value as `<redacted:LENGTH>`, a URL as `scheme://host[:port]` plus the number of path segments (no path segment, userinfo, query or fragment). `--drop-env` is for client configs only, never for the `memora-instance.sh` credential source. Dry run by default; `--apply` writes a 0600 backup `FILE.bak-repoint-<UTC timestamp>`, then replaces the file atomically with its own mode. With `--check-health-token-file` and `--check-admin-token-file` it runs check-endpoint against the server through the scratch store (`--check-store`) first, and refuses to apply if that fails.
- New `docs/local-primary-credentials.md`: which process holds which token in each phase (`MEMORA_D1_EDIT_TOKEN`, `MEMORA_D1_REPLICATOR_TOKEN`, `MEMORA_D1_READ_TOKEN`, operator, Pages, health and admin tokens); the Cloudflare minting steps (account-scoped D1 Edit / D1 Read, no Pages permission, IP filtering, verification); the rotation order (mint → scratch check → repoint → recreate every `memora-instance.sh` container → audit files and running containers on every host → check-endpoint from every client host → move memora-all to (a) → revoke the old token only when all of those hold → delete the backups); and the 14 days (a) is kept after the last cutover.


### Local-primary L5 (piece c): restore, conflicts/approve, reconcile, resume
- Per `docs/local-primary-implementation.md` §4, §1 "Reconciliation", §2.6, §0 P3/P7.
- **`restore <db> --receipt R --out P`** (default): a full re-seed. The target's primary lock is held, and the old store and its sidecars are moved into `<name>.pre-restore-<ts>/`, never deleted. It then seeds (with a recheck); the old store is put back if the seed fails.
- **`restore <db> --from-r2 KEY --receipt R`** writes `conflicts-<ts>.json`: every difference between the snapshot and D1's verified export over the §5.2 tables. Differences are grouped per memory id and per meta key, with both versions of every row and D1's preimage hash.
- **`... --conflicts F --approve A --out P --credential-file C [--dry-run] [--allow-deletes ID]`**:
  - The approve file quotes F's sha256 and chooses `d1|snapshot` for every group.
  - `snapshot` groups send per-key UPSERT/DELETE statements (replicator-built, P2-checked, P3-guarded with a one-attempt override). The group's D1 preimage is revalidated first; a changed group is aborted and the others continue. Each group is read back, and a failed send or read-back HALTS.
  - `d1` groups never write D1. The local store is then rebuilt from a fresh verified export of D1, after checking that every group in it holds the chosen rows.
  - The apply needs memora-all stopped (`--service-stopped`, refused before any send otherwise). It holds the store's primary lock from before the first D1 send through the rebuild. Groups are compared by enumerating their current D1 rows (every child table by memory id), so a row added after prepare aborts the group. `--dry-run` is the whole no-write plan, delete-guard result included; `--rehearse` does not apply to `--from-r2`.
- **`reconcile <db> [--accept ID --receipt R --operator O --decision applied|not-applied --evidence-sha256 X]`**: shows the open intents, or POSTs L2's accept body after checking the receipt's D1 identity and that the evidence is unchanged.
- **`resume <db> --store P [--accept-d1-epoch N | --allow-deletes A]`**: clears a replicator halt while holding the store's primary lock (memora-all stopped). An accepted epoch must equal D1's current one.


### Local-primary L5 (piece b): seed, sequence high-water, snapshot, volume alert
- Per `docs/local-primary-implementation.md` §4 and §9 k/v.
- **`seed <db> --receipt R --out P`**: builds a new local store from a verified export, under the freeze already in place. It first rechecks R under that freeze and seeds from what the recheck returns (a fresh export if D1 changed). The replica URI is derived from the verified D1 identity; a `--replica-uri` that differs is refused. Steps:
  1. `ensure_schema`, then an explicit FTS rebuild in `_fts_upsert`'s form (`COALESCE` for NULL metadata/tags). Keyword and hybrid search on the seeded store match its source.
  2. `sqlite_sequence` = max(local seq, D1 seq, max(id)) for `memories` and `memories_actions`.
  3. `install_sync` with `last_acked_seq` 0 and the receipt's epoch.
  4. Verification against the receipt.
  - The target's primary lock is held throughout, and the file is hard-linked into place only after it verifies. An existing store or sidecar is never touched. `--rehearse` seeds into a temp directory.
- **`sequence-highwater`**: after a passing recheck, raises D1's `sqlite_sequence` to the local high-water with `UPDATE sqlite_sequence SET seq = ? WHERE name = ? AND seq < ?`, only where D1 is behind. The UPDATE is sent through an allow-listed operator writer (`--credential-file`, mode 0600) and read back. A rejected or unapplied UPDATE, or a missing D1 row, HALTS (exit 3).
- **`snapshot`**: backup through the read-only connection, `integrity_check`, gzip, R2 upload with read-back. Keeps the newest 14 of its own keys, and refuses below 2× the store size of free space. **`volume-check`**: exit 4 on low space.
- `freeze`/`thaw`; a failed step's output names `local_primary.py thaw <db>` (the freeze is kept on purpose).
- `LocalSQLiteBackend` creates a live primary's parent directory before taking its primary lock (§9 k).


### Local-primary L7: read-only viewer, blocking D1 write guard
- Per `docs/local-primary-implementation.md` §6 F1/F2 and §9 (f)/(g). This slice issues no D1 statement; it removes the viewer's D1 writes. The Pages deploy is a separate user step.
- `memora-graph/functions/api/memories/[id].ts`: `PATCH`, `PUT`, `POST` and `DELETE` answer `405` (`{"error": "read_only"}`, `Allow: GET, HEAD`) before any D1 call. `GET` is unchanged.
- `memora-graph/functions/api/chat.ts`: search and answers only. The three write tools (`create_memory`, `update_memory`, `delete_memory`), `executeToolCall`, `computeAndStoreEmbedding` and the tag-policy load are removed. The model is offered no tools; a tool call it emits anyway is not executed, and the answer says no memory was changed. No `action` event is sent.
- New `GET /api/capabilities` (`{"read_only": true}` on Pages). memora's own graph server (`memora/graph/server.py`) answers `{"read_only": false}` and keeps editing through memora. The shared `memora/graph/index.html` starts read-only and stays so unless a server says `read_only: false` (a failed or unknown answer stays read-only): it shows a "Read-only viewer" badge, hides the edit/save/cancel buttons, makes the favorite stars and issue status/category controls inert, and every write function refuses first. `force-graph.html`: the favorite write is removed (the star still shows state) and a read-only note is shown.
- `memora-graph/scripts/test_readonly.mjs` replaces `test_tag_writes.mjs`: 405 on every write method with no D1 statement, no chat tools, no write SQL and no executed tool call, capabilities, and the same over HTTP in CI. The tag-policy conformance cases are kept.
- `scripts/d1_write_guard.py` (§9 (f)): H1 also matches an interpolated table (`UPDATE ${…} SET`); new H2 flags a string literal or template fragment headed by an uppercase write verb (write SQL built by concatenation); T2/T4 read logical lines (backslash continuations joined); new T6 flags a wrangler D1 `execute`/`migrations apply` whose arguments come from a shell variable without a literal `--local`; T5 now needs an executed guard run with `--scope all` chained directly before the deploy (`&&`, or `|| { …; exit 1; };`), so the guard's name in a comment, an `echo` or a string no longer satisfies it. `--scope all` is clean on the tree.
- `graph-ui.yml`: the handlers step is blocking (no `continue-on-error`), a blocking `--scope all` step is added, and the read-only tests replace the tag-write tests.
- `memora-graph/README.md` (§9 (g)): `npm run setup` is labelled partial (it stops after creating the D1 database), and a "Read-only viewer" section documents the above.


### Local-primary L2a: named /data volume, startup mount check, admin token, memory gate
- Per `docs/local-primary-implementation.md` §8 L2a and §9 (a)/(b). This slice issues no D1 statement.
- `scripts/memora-instance.sh up`: a store that keeps state under `/data` (any registry entry or `STORAGE_URI` that is not `s3://`: a local SQLite path, or a `d1://` primary, whose L2 write gate keeps its freeze file and intent journal there) now gets the NAMED volume `memora-<instance>-data` at `/data` (created with `volume create` if missing) and `-e MEMORA_DATA_VOLUME=<name>`. Before, only the single-store `VOLUME` branch mounted anything, so every registry instance wrote `/data` into the image's anonymous `VOLUME /data`, which the next `up` (stop, rm, run) replaced with an empty one. A host-directory `VOLUME` stays a bind mount, with its path as the marker. `MEMORA_DATA_VOLUME` and `MEMORA_ADMIN_TOKEN` from a credential file are ignored (instance-owned). If the existing container mounts a different /data (the anonymous volume of an instance started before L2a), `up` copies it into the named volume first: tokens, registry and volume are checked before anything is stopped; an existing container whose listing or `inspect` fails, whose inspect output does not parse, or which shows no single `/data` mount is refused before it is stopped (the raw inspect output is kept and its path printed); the copy runs while the container is stopped (refused if it still runs) through `scripts/migrate_data_volume.sh`; a failed copy aborts before `run` and leaves the old container stopped and unchanged; the old container is renamed `<name>-pre-data-volume-<ts>` for rollback (removed only if the runtime cannot rename; its volume is kept).
- `scripts/deploy-memora-all.sh`: mounts the named volume `memora-all-data` instead of reusing memora-all's current `/data` volume by id (normally anonymous). It creates and checks the volume before stopping memora-all; while memora-all is stopped, and only if no running container still uses the old volume, it runs `scripts/migrate_data_volume.sh` (sent from the local checkout). It refuses a 64-hex volume name. The rollback container keeps the old volume.
- `scripts/migrate_data_volume.sh` (shared by both launchers, run inside a throwaway container of the image): copies `/from` into `/to/.memora-staging`, verifies a sha256 digest of every file against the source, moves the live content of `/to` aside into `.memora-previous-<ts>` (never deleted, never overlaid) and moves the staged files in; the marker `.memora-volume-source` records the source volume id and the source digest. The copy is skipped only when both match, so a rollback that wrote to the old volume, a different source, or a failed partial copy (staging is cleared first) leads to a fresh copy.
- Every runtime query that gates a destructive step (`list --all`, `inspect`, `list` in `memora-instance.sh up`; `docker ps --filter volume=` in the deploy) has its exit status checked on its own: a failed query refuses instead of reading as "no container" or "not in use". The migration program builds its digest from separately checked steps (plain sh has no pipefail), so an unreadable file stops it.
- Token files (`memora-instance.sh` `<instance>.health-token` and `<instance>.admin-token`, `deploy-memora-all.sh` `all.health-token` and `all.admin-token`): an existing file must be a regular file, not a symlink, owned by the user and mode 0600; otherwise the launcher refuses before touching the container and says to remove it and re-mint. It is never chmod-ed into shape. The server reads the token from the environment only.
- Server startup (`memora/data_volume.py`), before L2's primary fence and the prewarm: a store that keeps state in the data directory (`MEMORA_DATA_DIR`, default `/data`: a local SQLite path under it, and every `d1://` store) is refused unless `MEMORA_DATA_VOLUME` is set and not a 64-hex anonymous id, the directory is a mount point (`st_dev` differs from `/`'s), and a probe file can be created, written, fsynced and removed in it and in its `intent/`. The refusal is per store: the process keeps serving the other stores; the refused one raises `DataVolumeRefused` on every connection (nothing is created), gets no primary fence, gate or journal, and `/health/db` names the reason.
- `/admin/*` auth (§9 (a), `memora/admin_auth.py`, installed through L2's `memora.admin.set_admin_auth`): `MEMORA_ADMIN_TOKEN` only, as a Bearer token; not the health token and no loopback exemption. Unset: 403 `admin_disabled`. Shorter than 32 characters, non-ASCII, or equal to `MEMORA_HEALTH_TOKEN`: the server refuses to start. New `GET /admin/data-volume` (read-only: the startup decision per store). A test calls every registered `/admin/*` route and method without the token. The launchers mint the token (`~/.config/memora/<instance>.admin-token`, 48 alphanumerics, 0600); operators can also call the routes through `docker exec` with the container's own token.
- §9 (b): `tests/test_connect_pragmas.py` traces the raw connection inside `LocalSQLiteBackend.connect()` and requires exactly L4's `writer_setup_pragmas()` (`busy_timeout` on every writer, `journal_mode=WAL` on a live primary) plus the touch read; L4's own test checks the tuple, this one catches a statement run outside it.
- Memory gate (§8 L2a): new `scripts/measure_memory_gate.py` (offline, synthetic stores; one process imports `memora.server` and runs semantic, hybrid and FTS searches over 4 local stores with the 384 MB corpus cache). It was measured on server2 inside the memora image (podman, python 3.12.14). Peak RSS: 525.7 MB (4 × 1000 rows, 1024-dim), 581.0 MB (4 × 1500) and 629.7 MB (4 × 1500, 1536-dim). The container default is therefore max(768M, 1.5 × 629.7 MB) rounded up to 64 MiB = **960M** (`memora-instance.sh` `DEFAULT_MEMORY`, was 512M; `deploy-memora-all.sh` `--memory 960m`, was 768m). With `MEMORA_CORPUS_CACHE_BUDGET_MB=256` the peak is about 426 MB. Full table in the plan, §8 L2a.

### Local-primary L5 (piece a): verified export, receipt, recheck
- Per `docs/local-primary-implementation.md` §0 P1, §4, §9 p. New operator tool `scripts/local_primary.py` (logic in `memora/local_primary.py`), run by hand; it reads D1 only with the read token.
- **`export <db>`**:
  - places the §1 freeze (`POST /admin/freeze/<db>`) if it is not already in place, and re-checks `/health/db/<db>` at every step boundary. It continues only while the store is `frozen` with 0 in flight and no open intent.
  - No command lifts the freeze except the explicit `thaw <db>`. `recheck` requires the freeze to be in place already (`freeze <db>`); it never places or lifts one.
  - `sqlite_sequence` is hashed, and the dump replaces SQLite's auto-created sequence rows with D1's counters.
  - `--native-export` tries `wrangler d1 export --remote` first, in an environment built from scratch that holds only the read token; if that is refused, it falls back to a paged SELECT.
  - Brackets the export with D1's epoch, loads the file into scratch SQLite and compares per-table counts and content hashes with D1 (3 attempts).
  - Uploads the file to R2 and reads it back, then writes a version-1 receipt. Earlier exports and receipts are never overwritten.
- **`recheck <db> --receipt R`**: under the freeze, compares D1's epoch, table set and full per-table hashes with the receipt. If anything changed, it takes a fresh export. Receipts are refused when they are for another store or another D1 database (account id, database id, URI), older than 24 h, not matched against R2, or when their SQL file changed.
- Credential files (`--admin-token-file`, `--health-token-file`, `--read-token-file`, `--credential-file`) follow the L2a rule: checked with `lstat`, not a symlink, a regular file owned by the current user, mode exactly 0600. They are never chmod-ed. `--health-token-file` is required with the freeze and must differ from the admin token.
- `--service-stopped` swaps the freeze for a `docker inspect` check that memora-all is stopped.
- `_absorb_link` refuses a nested `absorb_link` savepoint on the same connection (§9 u).

### Local-primary L4: absorb in one local transaction
- Per `docs/local-primary-implementation.md` §3. D1 SQL is unchanged, and the d1:// absorb path keeps its inflight lease, owned-id recovery and compensating deletes.
- **`store_write(conn)`** (backends) runs one `BEGIN IMMEDIATE` under a per-store process-wide lock:
  - inner `commit()` calls are deferred to its end, and an inner `rollback()` aborts the whole transaction (`StoreWriteAborted`);
  - `with conn:` inside it neither commits nor rolls back;
  - callbacks registered with `after_store_write` run after the commit, outside the lock;
  - nested calls join the outer transaction;
  - `in_store_write()` marks the no-network rule.
  Absorb phase 3, the local import and the replicator's local transactions use it.
- **Transactional absorb:** on a writable local connection (`supports_transactions`), phase 3 is one `store_write`. No inflight row, heartbeat or compensation is used, and a failure rolls everything back and propagates (nothing is ever half-written).
  - Supersede checks run before BEGIN, for the target's fresh resolution and every live leaf of its component (pre-existing forks).
  - Inside the transaction the gate runs offline: a leaf that still needs a check rolls the transaction back, is gated outside, and the transaction is retried, at most 3 times. After that the leaf is kept, not superseded (profile counters `tx_regates`, `tx_ungated_leaves`).
  - The fork heal's sibling branch is unreachable under the lock and raises.
- **Images and R2:** inside `store_write`, `add_memory` stores image sources as given with `images_pending`. They are uploaded after the commit, and the row is swapped with a compare-and-set; a failure keeps the flag, and `sweep_pending_images` (server startup) retries. R2 deletions happen after the commit only. `add_memory` refuses to compute an embedding under the lock.
- **Writers:** `connect()` sets `PRAGMA busy_timeout = 5000` on every writer, and `journal_mode = WAL` on live primaries only.
- `_WriteGate.enter(exempt=True)` is refused unless called from `memora.replicator`.
- Deferred images re-read the row under the lock and swap only when its `images` field is unchanged. The swap applies onto the current metadata, so concurrent content, tag and metadata edits are kept, and FTS is re-indexed from the current row. On a transactional store, absorb's `created_unlinked` links run in a `SAVEPOINT`, so no half edge survives. A caught inner `rollback()` poisons the transaction, which then refuses to commit. Thawing a store sweeps its pending images.


### Local-primary L3: the replicator (dark)
- Per `docs/local-primary-implementation.md` §2 and §0 P2-P4. `memora/replicator.py`: one thread per replicated store reads `sync_outbox` in seq order, coalesces it per key, and builds statements from each key's CURRENT local row. A present row becomes an UPSERT of every column (`memories_embeddings`: a DELETE+INSERT pair, so D1's update trigger cannot null `representation`); an absent row becomes a DELETE by the full primary key. `memories_meta` exclusions are enforced by the triggers.
- **P2 allow-list:** `_check_statement` accepts exactly those shapes, plus per-key read-back SELECTs and the epoch SELECT; anything else halts the store (`statement_rejected`). The replicator's D1 writer (`ReplicaD1Connection`) has only `execute_batch` and checks every statement before sending.
- **P3 deletion guard:** a batch whose net deletes on a table exceed 50 rows, or 1% of the table's rows (so any delete from a table under 100 rows), halts before anything is sent. `replicator.resume(conn, allow_deletes=<attempt>)` allows that one attempt.
- **Log mode** (`MEMORA_REPLICATION=log`): JSONL under `$MEMORA_DATA_DIR/replica-log/<db>/`, fsynced (and the directory, for a new file) before `log_cursor_seq` advances. Nothing is sent or acked. `iter_log` deduplicates re-appended ranges and drops a torn final line.
- **Write mode** (`MEMORA_REPLICATION=write`, token `MEMORA_D1_REPLICATOR_TOKEN`; reads use `MEMORA_D1_READ_TOKEN`; never `CLOUDFLARE_API_TOKEN`):
  - A durable in-flight marker is written before anything is sent.
  - The epoch preflight runs in its own request. A mismatch halts the store (`foreign_writer`), and only `resume(conn, accept_d1_epoch=…)` clears it.
  - One REST batch is sent, with the epoch postcheck as its last statement; if D1 rejects the batch body (HTTP 400), statements go one per request.
  - The ack happens only when every result succeeds. It is one local transaction that sets `last_acked_seq` and `d1_epoch_expected`, clears the marker, and prunes outbox rows at or below `min(acked, compare_consumed_seq)` that are older than 24 h. `/broadcast` follows the ack.
  - After an unknown outcome, reconciliation reads the range's keys back: if all match, the batch is acked (`epoch_unverified_batches` += 1); otherwise it is resent whole, with the preflight relaxed to at least the marker's epoch.
  - Halts persist across restarts.
- **D1 foreign keys:** a batch sends parent (`memories`) upserts first, then every other table, then parent deletes. Every child upsert carries its parent's current row. An ack requires `success: true` on every result; a missing field counts as an unknown outcome.
- **The freeze sees the replicator:** a send, from marker to ack, is an in-flight entry of the store's write gate (exempt from refusal, so it can drain a frozen store). The gate reads the in-flight marker from a copy that is set together with the durable marker commit and cleared only after the ack commits.
- The live D1 module first asks Cloudflare, with the read token, for the database's name. It fails before any setup unless that name contains "throwaway" and equals `MEMORA_D1_TEST_DATABASE_NAME`.
- **Scope:** replication is dark unless `MEMORA_REPLICATION` is `log` or `write` and `MEMORA_REPLICAS` names a local store whose `sync_state.replica_uri` matches. Stores in `MEMORA_SHADOW_LOCAL` are forced to log mode, and write mode refuses a shadow store.
- **Metrics and wake-up:** `/health/db/<db>` (authorised) gains a `replication` block: mode, status, head, acked and log cursors, `lag_rows`, `oldest_unacked_age_s`, `last_ack_at`, `last_error`, `halted_reason`, `epoch_unverified_batches` and `d1_missing_vectors` (null until the compare, L6). A commit wakes the replicator (`commit_event`). The in-flight marker counts as an open intent of the store's write gate.
- `sync_state` gains `allow_deletes_attempt`, `epoch_unverified_batches`, `last_ack_at` and `last_error`; existing tables are upgraded by `ensure_schema`.
- `tests/test_l3b_live_d1.py` holds live checks against a throwaway D1 database, and is skipped unless `MEMORA_D1_TEST_*` is set.


### Local-primary L2: write gate, D1 intent journal, sync schema
- Per `docs/local-primary-implementation.md` §1 and §2.9. This slice sends no new statement to D1; its only D1 reads are the reconciliation evidence SELECTs, through `D1SelectOnlyConnection` with `MEMORA_D1_READ_TOKEN`.
- **Write gate (live on every store):** `_WriteGate` admits every mutating statement, on local writers (per transaction, through `_GatedCursor`, so `cursor()` is not a bypass) and on D1 connections (per request). `freeze()` closes admission, waits for in-flight writes, and reports `frozen`; on timeout it reopens and raises `FreezeTimeout` with the in-flight list. A freeze file `$MEMORA_DATA_DIR/freeze/<db>` or `MEMORA_READONLY_DBS` starts a store frozen. Reads always continue. `connect_replicator()` returns a gate-exempt writer; nothing calls it yet.
- **D1 write-ahead intent journal (live on every `d1://` store):** before each mutating D1 request, an intent is appended to `$MEMORA_DATA_DIR/intent/<db>.jsonl` and fsynced; if that fails the request is not sent (`IntentJournalError`). A known outcome appends a resolution (fsynced lazily); an unknown outcome (timeout, reset, process death) leaves the intent open, and a frozen store with open intents reports `frozen-unsafe`. Also: one writer per journal (a flock on `<db>.lock`), an inode check before every append, repair to the last newline after any write error (a failed repair refuses every mutation), strict startup replay (a malformed middle record refuses the store), and compaction under the journal mutex with the fd reopened. HTTP 4xx and `success:false` are definite failures (`D1DefiniteError`, a `RuntimeError`); 5xx and transport errors are unknown outcomes. Nothing resolves an intent automatically: `POST /admin/reconcile/<db>/<id>` with a verified export receipt does.
- **Admin endpoints** (`memora/admin.py`): `POST`/`DELETE /admin/freeze/<db>`, `GET /admin/intents/<db>` (open intents plus read-back evidence, at least 60 s after sending, primary-served), `POST /admin/reconcile/<db>/<id>`. Every request is refused (403) until the admin auth layer installs `memora.admin.set_admin_auth` (slice L2a). `/health/db/<db>` and `/health/db` (authorised) report `freeze` (state, in-flight count, open intents) and the journal path.
- **Sync schema** (`memora/schema.py`): `install_sync()` creates `sync_outbox`, `sync_state` and the 28 `trg_sync_*` triggers on a LOCAL store, and `install_shadow_state()` creates the shadow file's state row; only the seed script (L5) will call them. `ensure_schema` only upgrades trigger versions, never creates sync objects, and does nothing on D1.
- `classify_statement` (`memora/sql_classify.py`) classifies the main statement (after comments and `WITH` prefixes, with `CREATE TRIGGER` bodies kept whole) and whitelists read-only PRAGMAs. It is shared by the gate, the journal and the SELECT-only reader.
- **Journal unavailable, reads stay up:** when the intent journal is held by another process, the data dir is not writable, or the journal is corrupt or broken, `D1Backend.connect()` returns a read-only connection. Reads work, every mutation raises `StoreReadOnlyError`, and schema setup is skipped. A write whose journal breaks after its intent was appended (a failed compaction, a failed repair elsewhere) is not sent.
- **One lock per store, however it is named:** the primary lock is derived from the canonical path of the database, so symlink aliases of the file or of a parent directory share it. A registry that names one store twice (colliding canonical paths, or one D1 database under two names) refuses to start.
- **Live primaries fenced before any open:** `server.main` takes every live primary's lock before the prewarm. A store locked by another process is refused (reported on health), and a refused default store aborts startup. Every writer open of a live primary takes the lock first.
- `POST /admin/reconcile` requires `operator`, `intent_id`, `decision` and `evidence_sha256` (matching `GET /admin/intents`), in addition to the receipt. Replay validates the fields of every record.
- `supports_transactions` on connections; `LocalSQLiteBackend.live_primary` (store named in `MEMORA_REPLICAS`, which is unset today) never reads immutable. A live primary's `<db>.primary-lock` is held by memora-all, and `scripts/apply_backfill_47.py` refuses while it is held.

### Local-primary L1b: retired D1 writers made inert, D1 write guard
- Per `docs/local-primary-implementation.md` §0 P6 and §6 F2/F3, memora is the only code allowed to write Cloudflare D1. This slice issues no D1 statement.
- `memora-graph/scripts/sync-to-d1.py`: every remote run (`--remote`, with or without `--replace`) exits 1 before importing memora, reading a store or starting wrangler; argument abbreviations are rejected. Local-D1 development runs are unchanged, and the wrangler command is always `--local`.
- `memora-graph/scripts/sync.sh`: `--remote` exits 1 before it reads `.mcp.json`, runs python or calls the worker broadcast. The remote-only broadcast step is removed.
- `memora-graph/scripts/link-r2-images.py`: retired. It wrote `memories.metadata` through the D1 REST API; it now exits 1 for every invocation, `--dry-run` included, without importing boto3 or requests.
- `memora-graph/scripts/setup-cloudflare.sh`: the remote D1 migration step exits 1 with a pointer. The Pages deploy step runs the guard first and exits 1 on any finding.
- `memora-graph/package.json`: `deploy` runs the guard first (`--scope all`); `d1:migrate` exits 1 (`d1:migrate-local` is unchanged); `sync-remote` exits 1 through `sync.sh`.
- New `scripts/d1_write_guard.py`, with scopes `tools` (retired scripts, remote wrangler D1 execute or migrations, `requests` calls to the D1 REST API, Pages deploys not preceded by the guard) and `handlers` (write SQL in the memora-graph Pages functions and worker). The only allow-listed file is `memora/backends.py`. `graph-ui.yml` runs the tools scope as a blocking step. It runs the handlers scope as a reporting step (`continue-on-error`) that fails by design until slice L7 makes the viewer read-only; L7 makes it blocking. The tools scope also runs on every push through the test suite. Scripted Pages deploys run `--scope all`, so they refuse until L7. A direct `wrangler pages deploy` is forbidden by operator rule.

### Issue #47 backfill apply
- `scripts/apply_backfill_47.py --preview <approved.json> [--db NAME] [--expect-count N] [--dry-run] [--allow-skips] [--report out.json]` applies ONLY the rows of an approved preview with `approved: true` and `status: "proposed"`.
- Before any store connection it validates the file: a non-empty approval block (`approved_by`, `at`, `rule`), unique memory ids, the approved+proposed count (`--expect-count`), full fingerprints on every considered row (`content_sha256` over the full content, `metadata_sha256` over the full canonical stored metadata), and each `proposal.retag_typed` recomputed from the stored tags and target.
- Per row, the re-read, the checks, the write and its read-back verification are one protected section (a `BEGIN IMMEDIATE` transaction on local SQLite, rolled back on any verification problem; on D1 the store's import lease, fenced, plus a guard on the UPDATE statement itself -- `update_memory(expected_row=...)`, including no supersedes edge to the memory in any crossrefs row, where any valid JSON is tolerated and only object elements are inspected -- so a concurrent change is never overwritten). The full content hash is checked first; the row must then be exactly the preview's state (written) or exactly the expected post-write state (already applied, or repaired when its FTS entry, embedding provenance and vector, or newest action row lag -- biased toward a harmless repair); anything else is stale. KNOWN LIMITATION (heuristic derived-state check, accepted): a same-second action plus a near-identical stale vector can pass as current; a later link action or provider variance can over-trigger repair, which rewrites updated_at and an action row. There is no durable progress marker. The write is one normal `update_memory` setting `metadata.project`: the memory's own typed tags are re-prefixed and its embedding and FTS entry refreshed. A row whose update would change anything beyond `metadata.project`, the typed-tag prefix and an absent `hierarchy.path` is refused (`canonicalization-diff`); after each write the full row is read back and diffed. Section is never changed.
- Outcomes: `applied`, `repaired`, `already-applied` (dry run: `would-apply`, `would-repair`); `skipped-missing`, `skipped-pending`, `skipped-retired`, `skipped-stale`; `refused`, `canonicalization-diff`, `raced`, `failed`, `verify-error`, `uncertain` (an unexpected error during the write: unknown whether it landed), `not-attempted`. Any error in a row's lease fence, BEGIN, assessment or write is caught per row; the first `failed`, `raced`, `verify-error` or `uncertain` row stops the run, and the `--report` JSON is written on every classified path (also when the artifact or the store is refused, the store cannot be opened or the import lease cannot be taken). Its destination is checked (a probe file) before any store connection; if the final write still fails, the full report is printed to stdout after a `REPORT WRITE FAILED` line and the exit is 1. An interruption (KeyboardInterrupt or any other BaseException) marks the current row `uncertain` (during its write) or `failed`, the rest `not-attempted`, writes the report best-effort and re-raises. The import lease is held from before its acquire, so a failed acquire is still released; a D1 row left part-written is completed by a re-run (`repaired`). Exit 0 only when every considered row is applied, repaired or already applied (`--allow-skips` also accepts `skipped-*`).
- `scripts/preview_backfill_47.py` now records the full stored metadata, `content_sha256`, `metadata_sha256` and `updated_at` per row, and `--carry-approval OLD.json` carries an older approval onto a new preview row only when the memory id, group, target, `retag_typed`, evidence, content preview and the stored section, subsection, tags, type and `metadata.project` are all identical, and its `updated_at` is not after the old file's `summary.generated_at` (now recorded by every preview; without it, the end of `approval.at`'s day, with a warning). This is a POLICY carry, not a proof that the row is unchanged: the user's approval is scoped to the id, target, typed-tag retag and section; content beyond the 120-char preview and metadata not shown in the approved file are unverified, and the `updated_at` bound is best-effort (a NULL `updated_at` is not proof of no modification, because some paths update fields without setting it). The new file's approval block records this rule verbatim (`carried_rule`). Every other row stays unapproved, with a carried/dropped table naming the reason.
- `update_memory` gains three internal parameters: `expected_row` (the conditional guard above; `ConcurrentUpdateError` when it matches no row, before any index write), `force_reindex` and `commit`.

## 0.4.6

A plain JSON API (Phase 0 of the clmux memora daemon), the absorb type
boundary, and typed tags no longer taken as project evidence.

**The API is OFF unless configured:** its routes are registered ONLY when
`MEMORA_API_TOKENS_FILE` is set. memora-all does not set it in this release,
so there the API is not registered at all (every `/api/v1/...` path is 404).
The contract is frozen as `contracts/memora-api/v1` 1.0.0, tagged
`memora-api-v1.0.0`. No configuration change is needed for this release.

### Plain JSON API `/api/v1/<store>/{health,search,absorb}` (Phase 0 of the clmux memora daemon)
- New HTTP routes next to MCP on the streamable-http/sse transports, outside the MCP session machinery: `GET health` (200 ok, or a 503 health document: down `probe_error`/`store_missing`/`store_locked`/`integrity_fault`, degraded `unproven`/`stale`), `POST search` (hybrid-v1 with `follow="active"`, raw `cosine`, `fused` order, 1-based `rank`, `preview`, `project` and `tags_any` filters, `unscored` coverage count) and `POST absorb` (validated, then 501 `writes_unsupported`: no store is transactional yet). The versioned contract lives in `contracts/memora-api/v1` (1.0.0: schemas, fixtures, manifest; `scripts/memora_api_contract.py` validate / live), and `scripts/memora_api_smoke.py` is a live gate.
- **Off unless configured:** the routes are registered only with `MEMORA_API_TOKENS_FILE` (sha256 token -> stores, re-read on change at most once a second, strict file and parent-directory checks); every request needs a token for the store. Request order: 401, 404, 403, 404, 413 (64 KiB body cap, counted at the ASGI receive boundary), 400, 429 (`MEMORA_API_MAX_INFLIGHT`, default 8), result. One error envelope for every non-2xx except health's 503.
- **Reads never write, set up a schema or create files:** the API search is strictly read-only (no embedding rebuild on a model mismatch: 503 `store_degraded`; no vector repair: `unscored`), and the API and the readiness probe (`/health/db` too) connect without schema setup. A local SQLite store opens read-only under a new per-store in-process reader-writer lock that every local writer connection's open and close takes (so no in-process writer can open or close during a read); a missing store, schema or usable WAL sidecars answers 503 instead of being created. Writers in another process are out of scope. The read-only corpus snapshot is single-flight and shares the corpus cache's budget.
- **Thread contract:** a local SQLite connection keeps stock sqlite3's `check_same_thread` behaviour exactly -- every public Connection and Cursor method, cursors returned by `execute`/`executemany`/`executescript`, blobs and `iterdump`, verified against native sqlite3 -- although the native connection underneath is opened with `check_same_thread=False` so a close by the garbage collector can happen under the store lock. GC closes never block; one that cannot take the lock at once is deferred and drained inside the next writer section.
- `memora-server` now pins uvicorn to `http="h11"`, `loop="asyncio"`, `h11_max_incomplete_event_size=16384` (the stated body bound -- 64 KiB plus one transport read -- depends on it: the largest ASGI body message was measured at 262144 bytes in the image runtime itself, CPython 3.12.14 and uvicorn 0.53.0, with `docker run --rm -i memora:latest python - < scripts/measure_asgi_body_messages.py` (17 messages for a 4 MiB chunked body); additional data points, all 262144: uvicorn 0.53.0 and 0.42.0 on CPython 3.12.8, uvicorn 0.42.0 on CPython 3.13.1). A local SQLite store's directory is now created on its first writing connection rather than when the backend object is built.

### Typed tags are not project evidence
- A typed tag (`<project>/issues`, `/todos`, `/sections`, `/documents`, `/knowledge`) says what a memory IS, not which project it belongs to: the old default put `memora/issues` and `memora/todos` on every issue and todo whatever its project, so many clmux issues carry `memora/issues` as their only memora tag. Project inference from tags now ignores typed tags; a memory's project comes from an explicit `project`, `metadata.project`, or a non-typed configured project tag (`clmux/tui`).
- A memory's own typed tags (a system kind matching its `metadata.type`, bare or under any project prefix, the old default included) stay exempt from the tag allowlist and are exported as `system_tags`. They are **re-prefixed to the memory's project once one is resolved**: an update that sets `metadata.project` or adds a non-typed project tag, or an import that declares a project, turns `memora/issues` into `clmux/issues`. With no resolved project they are left as they are. On import, the only legacy form accepted without a project is the old default `memora/<kind>`; any other prefix in `system_tags` is refused as a forged system tag.
- `scripts/preview_backfill_47.py` is a READ-ONLY preview of the #47 backfill with an explicit approval list (every row `"approved": false`); it creates nothing and may refuse: a local WAL store with sidecars (a writer process such as the server) is refused, with a non-zero exit. It supports local SQLite and D1 stores only: any other store URI (`s3://` or unknown) is refused from the configured URI text, before any backend is built or memora is imported, because a cloud backend creates its cache directory and syncs its cache; with a registry present, `MEMORA_STORAGE_URI` is pinned to the selected store for the run so memora's import-time backend is never a cloud one. Contradictions (stored section vs the project the old typed-tag rule gave) get a proposed target only from agreeing non-typed evidence (metadata.project, non-typed tags, section); keyword-only rows only where the section came from content keywords; everything else is `needs-human`. A proposal sets `metadata.project` and re-prefixes the typed tags, with a `<project>/<subsection>` marker tag listed as an alternative.

### Absorb: a supersession never crosses memory types
- Absorb could let a plain narrative fact supersede an open todo when the verifier judged the fact to "reiterate and confirm" it, so the task vanished from `follow="active"` lists (memora issue memory 1126: #1122 superseded open todo #1118). The per-leaf supersede gate now checks the **type boundary first**, before the similarity floor and without an LLM call: a leaf whose `metadata.type` (`todo`, `issue`, `section`, `document_root`, `document_fragment`, or none for a plain memory) differs from the new fact's (the absorb call's `metadata.type`) is downgraded to a related link. The decision's `supersede_check` and `leaf_checks` report `gate: "type"`, `type_mismatch: true`, `old_type` and `new_type`.
- The same rule applies to the concurrent-sibling (fork heal) pair check: siblings of different types never collapse.
- For a same-type pair the supersede verifier now sees both sides' type.
- A leaf check is reused at the write boundary only while the leaf's fingerprint is unchanged, and the fingerprint now covers its type and `metadata.project` as well as text and tags: a metadata-only patch between classification and the write (e.g. to `type: "todo"`) forces a re-gate. The fingerprint also covers the leaf's stored vector (every metadata change re-embeds a memory), and a reused verdict is still refused if the fresh similarity is below the supersede floor.

## 0.4.5

Project identity is explicit (issue #47), and the import hardening that fixing
it forced. **Configuration change:** set `MEMORA_PROJECTS` (below); without it
no project is inferred from tags, so only memories given an explicit project
get sections and project-prefixed tags. The MCP tool count is now 44 (new
admin tool `memory_import_sweep`, `full` profile only).

### Project identity is explicit, never guessed (issue #47)
- **Removed** keyword-based project detection (`_detect_project`, `_PROJECT_INDICATORS`, `_TAG_PROJECT_MAP`, `_KNOWN_PROJECT_PREFIXES`). Generic words such as "embedding", "workspace", "daemon" or "sidebar" no longer put a memory into `memora` or `clmux`; that misfiled memories, prefixed their generic tags with the wrong project (`clmux/architecture` on pi facts, #1109-#1114) and fed a wrong supersession (#1082).
- A memory's project now comes, in order, from: an explicit **`project`** argument (new, optional, on `memory_create`, `memory_create_issue`, `memory_create_todo`, `memory_absorb`, the CLI `absorb --project`, and the storage functions); else the memory's `metadata.project`; else exactly one tag naming a project configured for the store. Otherwise it has no project. An explicit project is also recorded as `metadata.project`.
- **`MEMORA_PROJECTS`** (new, optional): the projects a store holds, as a JSON list (every store) or `{store: [projects]}` (`"default"` for a single-store deployment). Unset: no project is inferred from tags, and explicit projects are accepted as any valid name (`[a-z0-9_-]{1,64}`). Set: a project outside the store's list is rejected (`invalid_input`), whether it arrives as the `project` argument or as `metadata.project` (on create, import and update). The whole value, including stores this server does not open, is validated at startup, and the server refuses to start on a malformed one. Deployments that relied on `memora/...` and `clmux/...` tags implying a project should set it, e.g. `MEMORA_PROJECTS='["memora","clmux"]'`: under it, memora-tagged content still resolves to memora, so the memora store keeps its conventions.
- Section/subsection assignment and generic-tag prefixing (`architecture` -> `<project>/architecture`) now act only on that resolved project; the memora/clmux section conventions are unchanged when the project is given, and now work for any project.
- LLM-suggested absorb tags are kept when the configured tag allowlist permits them (`MEMORA_ALLOW_ANY_TAG` permits any project-prefixed tag), instead of only `memora/` and `clmux/`; with an explicit project, a suggestion naming a different configured project is dropped. The classify prompt no longer uses `memora/research` and `clmux/architecture` as examples.
- **Typed tags follow the project, with no memora default:** `memory_create_issue`, `memory_create_todo`, `memory_create_section` and `memory_store_document` (new `project` argument on the last two) tag `<project>/issues`, `/todos`, `/sections`, `/documents`, or bare `issues`, `todos`, `sections`, `documents` without a project (previously always `memora/...`). `memory_create`'s type-based tag suggestions (`<project>/todos`, `/issues`, `/knowledge`) use the memory's own project, never another's. These typed tags are memora's own and are **exempt from the tag allowlist**, so the tools work under the default policy; user-supplied tags are still enforced (a caller cannot hand-apply `pi/issues` under a policy that does not allow it). A system tag is bound to the memory's `metadata.type` (`issues` only on an issue, and so on) and to its own project, and only internal paths can apply one: a `memory_create_batch` entry carrying `system_tags` is rejected (`invalid_batch`). An update may keep a memory's own typed tags; adding one it never had is enforced like any user tag.
- Generic tags are prefixed with the project (`plan` -> `<project>/plan`) only when the tag policy permits the prefixed form; otherwise they stay bare, so an explicit project never turns an allowed tag into a rejected one.
- Existing memories are not modified, and **`backfill_tags` does not remediate them**: it keeps an existing section and treats legacy `clmux/...`/`memora/...` tags as identity. `scripts/report_project_detection.py` is a read-only **remediation preview**: it lists memories whose stored section or project-prefixed tags came from the removed keyword heuristics and would change under a remediation (a separate item).
- A document's tag follows its resolved project: explicit, else `metadata.project`, else a configured project tag. The graph viewer's issue and todo filters, and the digest's todo and issue buckets, accept any `<project>/issues`, `<project>/todos` or bare tag, the legacy `memora/...` ones included, as well as `metadata.type`.

### Import and export (hardening forced by #47)

**A D1 replace is NOT atomic.** D1 has no transactions: every statement
commits on its own. If a replace stops part-way, the store may hold part of
the old contents, part of the new, or neither; the result says exactly what
happened (below). **Recovery: keep the export file and re-run the same
`memory_import(..., strategy="replace")` from it** -- clearing is idempotent,
and the re-run replaces whatever the stopped one left. (If the stopped import's
process died, its lease still blocks the re-run with `import_in_progress` until
it expires, at most 30 minutes; `memory_import` also has a 60 s cooldown.) Local SQLite imports
are one transaction and are all-or-nothing.

- **Export:** records gain a `system_tags` field (the typed tags memora applied); import re-applies them through the same type-bound validation, so an export restores under the default tag policy.
- **Prepare before delete:** every entry is validated and embedded before anything is written. A replace with any failing entry aborts with nothing deleted (`replaced: false`); it used to clear the store first and then reject entries one by one.
- **Local SQLite:** the replace's DELETEs and every INSERT are one transaction; any write error rolls back to the unchanged store (`replaced: false`).
- **D1 staged clear:** a replace clears crossrefs, embeddings, FTS, then memories last, each stage retried. A stage that still fails stops with `replaced: "partial"` and `clear_stage`; every memory is still present until the last stage.
- **D1 rows:** written in order, each retried a bounded number of times. Every INSERT carries a per-import, per-row marker (`metadata.import_attempt`, with the row's own time), stripped once the row is complete by a compare-and-set followed by a read-back. Only a row carrying that marker is ever adopted after a lost response or removed after a failed write, so a pre-existing memory with the same text is never touched. A row found removed before completion is inserted again, and **a row is never counted until it is verified complete**. A failed row's cleanup deletes the memory row before its vector and then checks it is gone.
- **Truthful partial results:** the import stops at the first row that still fails and reports `failed` and `written_ids` (exactly the rows it added), `replaced: "partial"` for a replace, and a `message`. Also, when they apply: `orphan_ids` (a failed row the cleanup could not remove; present without a vector, hidden from reads, removed by the sweep), `left_marked` (a row left marked when the lease was lost; for the sweep), `unconfirmed_ids` (a row completed just before the lease was lost: a normal memory, reported apart), and `post_write` (below). A replace is never reported as done with errors.
- **One import per store (D1):** every strategy (replace, merge, append) first takes the store's single import lease (new table `import_lease`), before the sweep, the merge read and preparation. A second import on the same store fails fast with `error: "import_in_progress"` and writes nothing; it does not wait. The lease is valid 30 minutes and renewed at least every 30 s; renewal only extends an unexpired lease this import still owns, so an expired lease is never revived. Ownership is proven by a fresh read before each clear stage, each row's INSERT, its completion and its counting; an import that loses its lease stops at once and writes nothing further.
- **Post-write steps under the lease:** restoring a replace's embedding-integrity baseline and rebuilding cross-references (with the rebuild's lazy embedding backfill) run only after a clean row phase and only while the lease is proven, fenced before every write. `post_write` is `"done"`, `"skipped"` (row phase incomplete) or `"incomplete"` (lease lost during the rebuild); otherwise `post_write_note` says to run `memory_rebuild_crossrefs`. The lease is released after these steps.
- **Stale-marker sweep:** rows left marked by a crash or a failed cleanup are finished by any later run. The sweep never touches rows of an import holding a live lease; otherwise it completes each marked row at least 10 minutes old that has its vector (strips the marker) and removes each one that has none. It runs at the start of every import, at server startup (every configured store, on a background thread), and on demand with the new admin tool **`memory_import_sweep`** (`older_than_minutes`, default 10).
- **Pending rows are hidden from every read:** until finished, a marked row is not a memory. List, keyword and semantic search, hydration, `get`, export, the graph viewer's `/api/memories` (and, through list/get, `/api/graph` and `/api/memories/{id}`), tags, tag validation, the hierarchy, related-metadata batches, duplicate pairs, link/boost/update, `backfill_tags`, merge-import dedupe and the R2 image migration all skip it; `memory_stats` counts it separately as `import_pending`. Neither the corpus repair pass, the embedding integrity audit nor a rebuild embeds it.
- `import_attempt` is a reserved metadata key: a create or update carrying it is rejected, and an import strips it.

## 0.4.4

Fast reads. Prompted by live timings from the Mac against memora-all:
`memory_semantic_search` 10-14 s even warm, `memory_get` 2 s, `memory_list`
1.3 s, with clmux meant to become memora's only client.

### Read paths
Fake-D1 bench (`scripts/measure_read_roundtrips.py`: 964 rows, the real MCP
tool functions, one statement == one D1 request). "Warm" is a repeat call,
"cold" the first call after a write. Seconds are **modeled** at 0.2 s per
request, not measured live.

| tool | warm requests | warm seconds (modeled) |
|---|---|---|
| `memory_semantic_search` | 16 -> 3 | 3.3 -> 0.6 |
| `memory_hybrid_search` | 17 -> 4 | 3.5 -> 0.8 |
| `memory_get` (current) | 8 -> 1 | 1.6 -> 0.2 |
| `memory_get` (stale id -> leaf) | 20 -> 8 | 4.1 -> 1.8 |
| `memory_get` `follow=full_history` | 37 -> 9 | 7.5 -> 1.8 |
| `memory_list` | 4 -> 2 | 0.8 -> 0.4 |
| `memory_related` (empty stored list, cold) | 21 -> 1 | |

The first search after a write still costs ~25 requests (integrity audit on
the new epoch + a cold corpus reload); not addressed in this release.

- Semantic and hybrid search score against the epoch-validated in-process corpus snapshot (metadata, tag and date filters applied before top-k, same tie-breaks) and hydrate only the ranked results. The snapshot's repair pass replaces the inline embedding backfill.
- One `memories_meta` read feeds both the integrity check and the corpus-cache freshness check.
- `follow=active` / `latest`: superseded, retired and malformed-link status for a whole page in one statement; `latest` walks only superseded items, through one shared bounded view.
- `memory_get`: one statement (row, crossrefs, retirement); walks a chain only when one exists.
- `memory_related` recomputes score against the snapshot.
- Results are identical to the previous read paths: `tests/test_fast_reads.py` compares every fast path against the old one on SQLite and fake D1. Any SQL failure, a neighbourhood past the view bounds, or crossref data the old walks read quirkily falls back to the old reads (one WARNING per process for SQL failures).

### D1 transport
- One persistent HTTPS connection per worker thread (keep-alive across tool calls; reconnect after 25 s idle). A request is retried at most once, only for a `SELECT`, only on a reused socket, and only when no response byte arrived; writes are never re-sent. The old urllib path is kept whenever an HTTP(S) proxy variable is set. Not reflected in the bench above (it saves a TCP + TLS handshake per request live).

### Query-embedding cache
- Search query embeddings are cached in-process (LRU 256), keyed by backend, model, endpoint and query; empty results and failures are never cached.

### Bounded corpus cache
- The corpus snapshot is now also cached for databases that are only searched (before, only after an absorb). About 93 KB per row with 1024-dim vectors (measured; vectors dominate).
- Least-recently-used eviction of whole snapshots under a byte budget across all stores: **`MEMORA_CORPUS_CACHE_BUDGET_MB`** (new, optional, default 384; valid values are finite, > 0 and <= 1 TiB, anything else uses the default). A snapshot larger than the budget is served uncached. A store's entries cached under a previous embedding model are evicted on its next load. Evictions are logged at INFO; an evicted store just reloads cold.

### Read profiles
- `memory_semantic_search`, `memory_hybrid_search`, `memory_get`, `memory_list`, `memory_list_compact` and `memory_related` return a `profile` field (per-phase seconds and D1 request counts), also logged at INFO (visible with `MEMORA_LOG_LEVEL=INFO`).

### Behaviour changes
- `memory_related`: a memory whose stored crossref list is **empty** now gets that empty list back; it is recomputed only with `refresh=True` or `memory_rebuild_crossrefs`, the same staleness rule every non-empty list already had. Previously an empty list was recomputed (a full-store scan) on every call, which also let a list stored empty while the store had no neighbours heal itself on the next read; it now stays empty until refreshed. A memory whose crossrefs were never computed (no stored row) is still computed on read.
- **Malformed tags JSON** (e.g. from a bad import) is read as untagged everywhere instead of raising: every filter mode (tags_any, tags_all, tags_none, dates, none) and hybrid search treat the row as untagged, and search, get and list return it with `"tags": []` plus `"tags_invalid": true` (the marker appears only on such rows). The embedding rebuild (including semantic search's auto-rebuild) and the corpus snapshot load also read it as untagged. A warning is logged once per memory. Previously any search that scanned such a row, and any get/list that returned it, failed with a JSON error.

## 0.4.3

Absorb: far fewer D1 round trips, and supersessions that must be verified
against the exact memory they hide. Prompted by live `memory_absorb` calls
failing the caller's 300 s timeout on update-heavy batches while the server
kept committing, and by #1082 (a parked design idea) being superseded by
#1109 (unrelated work that only shared "clmux agent delivery").

### Absorb: D1 round trips
- Measured offline with `scripts/measure_absorb_roundtrips.py` (a 9-fact update-heavy absorb against a 964-row store through the FakeD1 double; modeled 0.2 s per D1 request, 0.1 s per embedding, 2 s per LLM call): **550 D1 requests / ~120 s -> 204 / ~49 s**, with identical decisions (the script asserts this against a pre-change run). These are modeled seconds, not a live measurement.
- Phase 1 is batched across facts: one tombstone-hash lookup, one embedding batch, one hydration of every fact's candidates, one bounded retirement lookup (was per fact, and per candidate).
- Supersession graph reads use a bounded neighborhood view: one `memories LEFT JOIN memories_crossrefs` query per BFS level plus two retirement queries, instead of per-node crossref and existence reads (each walk re-read nodes several times). Loaded fresh per call, never reused across a graph write; falls back to per-row reads past 1000 nodes.
- `add_link` checks existence with `SELECT 1` instead of fetching both full memories.
- Phase-3 storage embeddings go out as one batch on the dense backend.
- Writes, their order, and corpus-cache invalidation are unchanged. What batching writes would take is in `plans/absorb-write-batching-notes.md` (not in the repo; plans/ is git-ignored).

### Absorb: per-phase profile
- Every call returns `result["profile"]`: exclusive wall time and DB request count per phase (`corpus_load`, `phase1_prep`, `embeddings`, `classification`, `supersede_plan`, `supersede_verify`, `phase3_insert`, `phase3_link`, `supersede_resolve`, `supersede_link`, `fork_heal`, `final_checks`, `inflight`, ...) plus counters (LLM calls, embedding requests, late/re-gated/sibling checks). Also logged at INFO.
- `D1Connection.request_count` counts HTTPS POSTs.

### Absorb: supersede gate
- A classifier UPDATE is only a proposal. Absorb supersedes a memory only after gating **every leaf it would actually supersede** (the classifier's candidate is resolved to the current live leaves of its supersession chain first): the fact's similarity to that leaf must be at least 0.55, and a second, narrow LLM check (`_verify_absorb_supersede_llm`) shown both texts in full, their tags and the caller's context must answer an explicit yes to same project, same entity and full replacement. No LLM, an error or an unparseable answer never supersedes.
- Leaves that fail stay live: an **intentional fork**, reported in the decision (`intentional_fork`, `not_superseded`, `leaf_checks`). If no leaf passes, the new memory is linked RELATED to the closest leaf instead (or left unlinked if the check calls them unrelated).
- The write boundary re-resolves and re-reads every leaf: a check is reused only if the leaf's fingerprint (text + tags) is unchanged since it was made, so an `update_memory` edit in between is re-gated; a leaf that appeared in between is gated then.
- Fork heal no longer lets a concurrent absorb's new memory supersede this call's new memory on the strength of both having passed against the same old leaf: that exact pair must pass the gate, or both stay live.
- The classifier sees 800 characters per candidate (was 300) and a strict UPDATE definition.
- Every supersede and every downgraded UPDATE is logged at INFO with target, score, gate, both reasons, old text and new text.
- Calibration (`scripts/measure_supersede_gate.py`, 19 labelled pairs in `tests/fixtures/supersede_gate_pairs.json`, live bge-m3 + `openai/gpt-4o-mini`): precision 7/7, recall 7/7, 0 false supersedes; true updates scored 0.79-0.89, the #1082/#1109 analogue 0.47. A project-tag-prefix rule was tried and **removed**: it blocked a genuine update tagged `clmux/` vs `memora/`, and the verifier (which sees the tags) rejected every cross-project pair on its own. 19 pairs is a small set.
- Prompt-injection framing: stored and caller text goes into nonce-delimited data blocks with marker runs defanged, and the prompt says the blocks contain no instructions. **Limit:** this stops stored text escaping its block, not semantic injection inside it; the tests prove the framing only. What bounds the damage is structural (explicit yes on all three fields, fail-closed parsing, the score floor, the audit log).

### Operations
- New opt-in `MEMORA_LOG_LEVEL` (e.g. `INFO`): attaches a stderr handler to the `memora` loggers. Nothing configured logging before, so every memora INFO line (including the absorb profile and supersede audit above) was silently dropped. Unset keeps the old behaviour. The memora-all deploy sets it to `INFO`; note that the supersede audit lines put up to 500 characters of memory text into the container log.
- No other new env vars or config. `MEMORA_LLM_MODEL` is unchanged (`openai/gpt-4o-mini`). Each UPDATE now costs one extra LLM call per leaf it would supersede.

## 0.4.2

Absorb classification fix for the v0.4.1 gpt-4o-mini switch — a same-day
follow-up.

### Absorb
- The classify response parser now recovers `memory_id` from a real `int`, or a string that is (optional whitespace +) exactly one of `482`, `#482`, `[#482]` — whole-string only, nothing else in the value. `openai/gpt-4o-mini` was found — live, against the real model — to consistently echo the prompt's own `[#482]` match-display notation back as the value rather than the bare number the prompt asks for, which the old parser rejected outright: every classification on the v0.4.1 deploy came back `LLM classify empty; preserving as related` despite OpenRouter returning 200 on every call. The accepted forms are deliberately narrow: stripping every non-digit character out of an arbitrary string is unsafe, since e.g. `"#482 and #483"` would strip to `482483` and `"1. [#482]"` to `1482` — both digit-run concatenations that can coincide with a genuine candidate id in the same fact's match set and silently misroute the classification onto the wrong memory. Ambiguous text is dropped, not guessed at.
- `json.loads` now retries against the outermost `{...}` span if the first parse fails, recovering a JSON object a model prefixed with reasoning or commentary text despite being told not to.
- The classify prompt is more explicit that `memory_id` must be the bare number, not the bracketed form — a second line of defense, not a substitute for the parser fix, since this model didn't comply with the prior wording either way.
- The raw LLM response is now logged at debug level whenever the model answers but nothing survives validation, so this class of bug is diagnosable from logs without a live repro.

## 0.4.1

Absorb latency and embedding-rebuild throughput, plus a proxy container-resolve
hardening pass.

### Absorb
- `memory_absorb`'s per-fact LLM classification calls are dispatched concurrently (bounded, `MEMORA_ABSORB_CONCURRENCY`, default 4) instead of one at a time; embeds and searches stay sequential (cheap, and the DB connection isn't safe to touch from worker threads). Measured 3.5x from concurrency alone, up to ~18.5x combined with a faster model, on a 7-fact absorb.
- A single classify call failing in the concurrent phase degrades to a pending create with reason `classify failed: <type>` instead of aborting the whole batch; the sequential path (always exactly one call in flight — this is also what `scripts/measure_absorb_classifier.py`'s live measurement mode exercises) is unchanged and still propagates a raise immediately.
- `absorb_inflight` tracking now begins before phase 1, not just phase 3's writes, and is heartbeated after each concurrent classify call — a multi-fact batch no longer goes silent for minutes before the first memory is created.
- The classify prompt identifies each candidate match by its bracketed id only; a prior `"{i+1}. [#{id}]"` numbering let some models return the list position instead of the id, which failed validation and silently produced an empty classification. The response validator also accepts a bare `"id"` key defensively.

### Embeddings
- `memory_rebuild_embeddings` processes rows in chunks (`MEMORA_REBUILD_CHUNK_SIZE`, default 32) — one batched embedding call and one commit per chunk. The dominant cost was the rebuild-lease heartbeat's D1 round trip, which fired twice per row; chunking collapses that to twice per chunk. Measured 332.3s -> 103.7s (3.2x) rebuilding a 237-row D1 store.

### Deployment tooling
- `scripts/memora_proxy.py`'s container-IP resolver is now single-flight (concurrent callers on an expired cache collapse into one `container list` subprocess instead of one each) with stale-while-revalidate serving inside a bounded grace window, and closes three follow-on races in that change (a follower observing an unrelated mutation instead of the flight it joined, a stale connect failure deleting a newer successful resolve, a failed-thread-start leaking a flight permanently). Not deployed by this release — the running proxy is a separate, manual step.

## 0.4.0

Multi-database release. One memora process now serves every workspace from its
own store, with per-database health, images and identity.

### Multi-database routing
- `MEMORA_DATABASES` registers named stores (`{name: uri}`); a workspace reaches its own at `/mcp/<name>`. Unset keeps single-store behaviour.
- Names are one URL path segment, matched on component boundaries; an unknown store gets a generic 404 that does not disclose the registry.
- A malformed registry, a duplicate name, an empty URI or an unusable backend is fatal at startup rather than silently serving the wrong store.
- `memory_identity` reports which database the session is bound to (#997).

### Health and readiness
- `GET /health` is liveness with no database I/O — the only signal a supervisor may restart on. `GET /health/db` is per-database readiness, `GET /health/db/{name}` a single store.
- Probes run off the event loop, bounded and concurrent; a timed-out probe is truly abandoned rather than left to land later.
- Readiness refreshes on its own schedule (`MEMORA_HEALTH_REFRESH_INTERVAL`), so a proxy deployment with no loopback caller no longer reports `unknown` while every database is fine.
- Detailed bodies are token-gated (`MEMORA_HEALTH_TOKEN`); an unauthenticated caller sees aggregate status only, because FastMCP custom routes are unauthenticated even when MCP auth is configured.
- A staleness budget bounds how old a cached result may be before it stops counting as ready.
- A watchdog supervises the shared container (#987).

### Session and transport hardening (#999)
- A hard ceiling on live sessions, counted atomically, with admission refunded when a request is rejected — a rejected request no longer costs a session.
- Routing mirrors the SDK's own acceptance rules and the transport's security settings, closing a POST bypass that reached deployments unguarded.
- Terminated transports are purged rather than ignored; idle sessions are reaped.

### Images
- Object keys are namespaced per database, so two stores cannot collide in one bucket (#965 phase 3).
- Images are keyed by `(name, uri)` rather than name alone — the same name at a new URI is a new image, not a stale hit.

### Absorb
- The corpus is cached across calls, keyed on a monotonic database epoch, and loaded once per call instead of once per fact. Absorb of many facts no longer re-reads the store for each one.

### Tool profiles (#981)
- `MEMORA_TOOL_PROFILE` exposes `full` (43 tools), `leader` (19) or `agent` (12). An unknown value refuses to start rather than guessing.
- Gating is attested through the public handler path and fails closed if the profile cannot be verified.

### Deployment
- Every runtime call routes through `CONTAINER_BIN`, not just `run` (#996).
- Routing is instance-owned; a credential file can no longer supply it.

### Docs
- Four version-stamped release-notes files consolidated into this CHANGELOG, which has a stable name README and issues can link to without rotting (#1000).
- README rewritten around the container path, which is what a running memora actually is.

## 0.3.3

Search accuracy and hardening release.

### Search
- Full-text search now queries the FTS index correctly, improving keyword relevance; substring matching remains only as an explicit fallback.
- `limit` is honored on `memory_hybrid_search` and `memory_semantic_search` (`top_k` still accepted).
- Searches and lists with lineage filtering (`follow=active/latest`) fill the requested result count even when top-ranked candidates are superseded, scanning beyond the previous 5,000-row window with a loud error at the safety bound instead of silent truncation.

### Absorb & lineage
- Absorb updates supersede the current version of a memory, resolving through the supersession chain to the leaf.
- New classifier measurement harness: labeled fixture pairs, per-class precision/recall and confusion matrix, dry-run safe, with a `--min-macro-f1` gate for regression testing.
- All LLM calls are bounded by an explicit timeout (`MEMORA_LLM_TIMEOUT`, default 60s). Measurement mode fails loud; runtime absorb degrades gracefully.

### Tag policy
- The Cloudflare graph app validates tag writes (memory edit and chat) against a versioned policy stored per database, failing closed when the policy is unavailable.
- Wildcards support slash namespaces (`memora/*`) alongside dot namespaces; tags are capped at 100 characters, counted identically (Unicode code points) in Python and TypeScript and guarded by a shared conformance fixture.

### Graph UI
- The WebGL canvas tracks its container through drawer transitions via ResizeObserver, fixing a sizing race under load.

### CI
- New `clean-install` workflow: builds the wheel, installs into an empty environment, and runs the suite — on push, tags, and a daily schedule.
- New `graph-ui` workflow: browser tests for the graph UI (drawers, top bar, render-idle power behavior, database selector), tag-policy write tests, and lineage logic tests against a seeded local D1.

### Docs
- Install instructions lead with PyPI (`pip install memora-mcp`); absorb, supersession lineage, and digest documented in Features; `MEMORA_TAG_FILE` format corrected (JSON array).

---

## 0.3.2

Consolidated 0.3.x release. Notes for the earlier 0.3.x tags remain in the repo as
the 0.3.1 and 0.3.0 sections below.

### Fresh installs work again

`mcp` 2.0.0 removed `mcp.server.fastmcp`, and our dependency was an unbounded `mcp>=1.0.0`, so every
fresh install resolved to the new major and the server died at import with
`ModuleNotFoundError`. Now constrained to `mcp>=1.0.0,<2` (the 1.x line is maintained in parallel).

The failure was invisible from both sides: a dead stdio MCP server looks identical to one exposing no
tools, and existing environments had `mcp` pinned to a working 1.x, so every test suite passed.

Reported and fixed by [@BillyBunn](https://github.com/BillyBunn) in
[#44](https://github.com/agentic-box/memora/pull/44).

### Breaking

**Embeddings.** memora could be configured — following its own installer and README — into a state
where no embedding was ever computed and nothing said so. `install.sh` generated
`openai/text-embedding-3-small` while the README recommended OpenRouter as the base URL, and
OpenRouter serves no embeddings endpoint. Every call 404'd, one warning went to a log nobody reads,
and stores silently filled with TF-IDF keyword bags. In the store where this was found, 756 memories
had been keyword vectors for months.

- **Every store needs one embedding rebuild.** The model fingerprint now records backend, model,
  endpoint host and representation, so the old bare `"openai"` stamp no longer matches.
- **Dense backends no longer fall back to TF-IDF.** A provider failure raises instead of persisting a
  wrong vector. Set `tfidf` explicitly if you want it.
- **`MEMORA_EMBEDDING_API_KEY` / `MEMORA_EMBEDDING_BASE_URL` are an atomic pair** — set both or
  neither. A partial pair is rejected rather than borrowing the missing half from `OPENAI_*`, which
  could previously send one provider's secret to another provider's host.

**Issues are no longer inferred.** `memory_create_issue` and `memory_create_todo` are now the only
ways a memory becomes typed; `memory_absorb` and plain creates stay untyped knowledge. A keyword
classifier had mislabelled 130 knowledge memories as open issues. Existing typed memories are
untouched.

### Added

- `memory_verify_integrity` — read-only doctor reporting representation counts, coverage and
  offending ids, with concrete remediation.
- `MEMORA_EMBEDDING_STRICT=1` (**recommended**) turns a broken endpoint into a named error instead of
  silent degradation.
- Integrity derived from SQL and invalidated by a DB-owned change epoch, so an external writer cannot
  leave a stale "healthy" verdict behind.

### Fixed

- Batch embedding responses are validated for cardinality, index coverage and uniform dimensions, and
  reconstructed **by index** — a reordered response could previously attach a vector to the wrong
  memory.
- Coverage uses indexed anti-joins in both directions; counting by subtraction let one orphan cancel
  one missing embedding.
- `absorb` no longer leaves untracked partial rows.
- Concurrent `ensure_schema` no longer raises on duplicate columns.
- `install.sh` and the README no longer generate or document the broken configuration.

### memora-graph

- **Supersession is visible.** Superseded memories render dimmed amber with a `SUPERSEDED` badge and
  directed lineage arrows; a toggle collapses to current-state only. Only `supersedes` edges count as
  lineage — `references`/`contradicts`/`implements` were previously drawn as supersession. Half-written
  crossrefs are detected rather than shown as current, and the page reports
  `LINEAGE UNAVAILABLE` instead of a confident zero when it cannot tell.
- **The 3D view no longer burns the CPU.** Its render loop repainted 60×/sec forever, whether or not
  anything changed — measured at ~227% CPU on an idle page. The loop now stops once the layout
  settles and wakes on interaction: **~227% → 0.4%**. Kill switch:
  `localStorage.setItem("memora-graph.noIdle","1")`.
- Resizable timeline and detail drawers with independently persisted widths; per-tab panel widths in
  the default view.
- The database selector lists every configured database in both views (it was hardcoded in one).
- The top bar no longer hides behind open drawers.

### Known issues

- **Epoch time-of-check window** — a query can use data that changed mid-call; the next call detects
  it. Transient, self-healing.
- **`memories_embedding_repairs` is unbounded** with no foreign key to memory lifetime; explicit id
  reuse can mark an unrelated row as recurring.
- **D1 ownership recovery after a lost response** lacks bounded retry and a unique operation record.
- **External writers must populate the new columns.** A writer emitting a thresholded/sparse encoding
  of a dense vector is reported as an encoding fault by id, and auto-rebuild is skipped deliberately.
- **The default `index.html` view lacks force-graph's database-switch protection** — after a switch or
  live refresh an already-rendered list can briefly show another store's authority state. Memory ids
  overlap across databases, so this is worth knowing.

### Upgrading

1. Upgrade, then run `memory_verify_integrity` before anything else.
2. If it reports a repairable mismatch, run an embedding rebuild.
3. Set `MEMORA_EMBEDDING_STRICT=1`.
4. **Verify by looking at a stored vector**, not at your config — it should have your model's
   dimension count with numeric keys covering `0..N-1`. Configuration that looks correct is what hid
   the original problem.

---

## 0.3.1

A patch release. No schema change, no embedding rebuild, no action required on upgrade.

Two things motivated it: the graph UI was heating the machine badly, and 0.3.0 shipped with an
internally inconsistent version number.

### The power problem

**force-graph's 3D view repainted 60 times a second forever, whether or not anything changed.**
Measured on a reporter's Mac with the page completely untouched: **~227% CPU** — over two full cores —
with the browser's GPU helper process alone pinned at 140%. The 2D view cost ~37% under the same
conditions. A static picture of a settled graph was doing continuous work.

**Idle now costs essentially nothing: ~227% → 0.4%.** Once the layout settles the render loop stops,
and the GPU helper process drops out of the process list entirely. It wakes instantly on zoom, drag,
click or scroll.

This was measured end to end in the reporter's own browser (LibreWolf) against a real 771-memory
store, before and after — not inferred from a synthetic benchmark.

#### Why this needed care

An earlier attempt at exactly this fix was reverted for two bugs: zoom would stick, and after a while
motion would stop altogether. The idea was never wrong — the mechanism was. `wake()` called the
library's `resumeAnimation()` **unconditionally** and was wired to `pointermove`, so adding a
timer-driven pause meant pause and resume interleaving dozens of times a second, desyncing the
library's own frame bookkeeping.

The fix makes `renderPaused` the single source of truth and calls the library's pause/resume **only on
a real state transition**. High-frequency callers just re-arm a timer. Discrete input
(`pointerdown`/`wheel`/`touchstart`) goes through a separate path that resyncs unconditionally — safe
precisely because those events are rare, and it is the escape hatch if the flag ever drifts. Pausing
additionally requires the physics engine to have settled, so a layout is never frozen mid-settle.

Both historical bugs are covered by regression tests that reproduce them on purpose. Renders are
counted via the WebGL draw counter rather than a `requestAnimationFrame` probe — a raw rAF counter
keeps ticking while the library is paused and would pass vacuously.

**Kill switch:** `localStorage.setItem("memora-graph.noIdle", "1")` disables idling entirely and
restores the previous always-render behaviour, no redeploy needed. Remove the key to re-enable.

### Also in the graph UI

- **Resizable drawers in force-graph.** The timeline and the memory-detail drawers now have
  independent drag handles. Each remembers its own width, clamped between 280px and 90% of the
  viewport, re-clamped on window resize so a width saved on a large display cannot swallow a laptop
  screen. Double-click a handle to reset that drawer.
- **Per-tab panel widths in the default graph view.** The side panel hosts several tabs; a timeline
  list reads fine narrow while memory content wants to be wide, so each tab now keeps its own width.
  The old cap of 800px is gone.
- **The 2D/3D choice persists.** Previously every reload silently returned you to the expensive
  renderer even if you had chosen 2D.
- **Cheaper frames while active:** the WebGL pixel ratio is capped at 1.5 (an uncapped Retina display
  was drawing ~4x the pixels it needed — measured 1.78x less fill per frame), sphere geometry is
  lighter, and the renderer asks the OS for the energy-efficient GPU.

### Fixed

- **`agent.yaml` and `pyproject.toml` disagreed on the version.** 0.3.0 bumped the package but not the
  manifest, so the published tag claimed two different version numbers. A test for exactly this
  already existed and was not run before tagging. Both sources now move together.

### Known issues

Unchanged from 0.3.0 and still open — see the 0.3.0 notes for detail: the epoch time-of-check window;
`memories_embedding_repairs` being unbounded with no foreign key to memory lifetime; D1 ownership
recovery after a lost response lacking bounded retry; and the default `index.html` view not carrying
force-graph's database-switch protection.

### Upgrading

Nothing to do. If you were avoiding the 3D graph because of heat, it is worth another look.

---

## 0.3.0

**Read this before upgrading. This release forces a one-time rebuild of every stored embedding.**

### Why this release exists

Memora could be configured — following its own README and its own installer — into a state where
**no embedding was ever computed and nothing said so**. Every semantic search silently became a
keyword search, and the store filled with keyword bags while reporting healthy.

The trigger: `install.sh` generated `OPENAI_EMBEDDING_MODEL="openai/text-embedding-3-small"` and the
README recommended OpenRouter as an `OPENAI_BASE_URL`. **OpenRouter serves no embeddings endpoint**
— its catalogue lists 400 models and zero with an embedding task. Every embed call returned 404,
memora logged one warning to a server's stderr, fell back to TF-IDF, and carried on.

In the store where this was found, 756 memories had been keyword vectors for months.

### Breaking

- **Every existing store will require one embedding rebuild.** The stored model fingerprint now
  records backend, model id, endpoint host and representation, so the old bare `"openai"` stamp no
  longer matches. First use after upgrade reports a mismatch and rebuilds.
- **Dense backends no longer fall back to TF-IDF.** With `openai` or `sentence-transformers`
  configured, a provider failure now raises instead of silently persisting a keyword vector.
  *A wrong embedding is worse than a missing one.* Configure `tfidf` explicitly if you want it.
- **Embeddings and the LLM are configured separately.** `MEMORA_EMBEDDING_API_KEY` and
  `MEMORA_EMBEDDING_BASE_URL` are an **atomic pair** — set both or neither. A partial pair is
  rejected rather than borrowing the missing half from `OPENAI_*`, which previously could send one
  provider's secret to another provider's host.

### Added

- `memory_verify_integrity` — a read-only doctor reporting representation counts, coverage, and
  bounded lists of offending ids, with concrete remediation.
- `MEMORA_EMBEDDING_STRICT=1` (**recommended**) turns a broken endpoint into a hard, named error
  instead of silent degradation. This is the flag whose absence let the failure above run for months.
- Integrity is derived from SQL and invalidated by a database-owned change epoch maintained by
  triggers, so an external writer — a sync script, a Worker, another process — cannot leave a stale
  "healthy" verdict behind.
- Per-row `representation`, `dimension`, `encoding_source`, `writer_token`. Rows written by an
  unrecognised writer are marked unknown rather than assumed valid.

### Fixed

- Credential pairs can no longer cross providers (embedding key sent to the LLM host, or vice versa).
- Batch embedding responses are validated for cardinality, index coverage and uniform dimensions, and
  are reconstructed **by index** — a partial or reordered response could previously attach a vector to
  the wrong memory.
- `absorb` no longer leaves untracked partial rows; ownership is recorded at INSERT and compensation
  verifies an operation nonce **before** any destructive work.
- Coverage uses indexed anti-joins in both directions. Counting by subtraction previously let one
  orphan embedding cancel one missing embedding, hiding a memory that was invisible to search.
- Concurrent `ensure_schema` from several processes no longer raises on duplicate columns.
- `install.sh` and the README no longer generate or document the broken configuration.

### memora-graph (web UI)

The graph viewer previously drew superseded memories exactly like current ones — a memory the store
*knew* had been replaced looked like live truth.

- **Supersession is now visible.** Superseded memories render dimmed amber with a `SUPERSEDED` badge,
  and lineage draws as directed arrows newer → older. A toggle collapses to current-state only.
- **`references` / `contradicts` / `implements` are no longer drawn as lineage.** Every non-`related_to`
  edge was previously marked directed and painted as supersession, asserting relationships that did
  not exist. Lineage now keys strictly on `edge_type === "supersedes"`.
- **Half-written lineage is detected.** A supersession where only one side of the crossref survived —
  the case the nightly rebuild exists to repair — is now found rather than silently shown as current.
- **The page no longer reports confident zeros when it cannot tell.** When crossrefs are unavailable
  it shows `LINKS UNAVAILABLE · DUPS UNAVAILABLE · LINEAGE UNAVAILABLE (cannot confirm current)` and
  *disables* current-only mode instead of filtering nothing and implying success.
- **In force-graph**, switching databases can no longer paint one graph's lineage onto another
  graph's identical ids, and an open detail panel refreshes rather than showing stale authority.
  (The default `index.html` view does not yet carry this protection — after a database switch or a
  live refresh, an already-rendered list can briefly show another store's authority state until it
  is reopened. Memory ids overlap across databases, so this is worth knowing.)

### Known issues

- **Epoch time-of-check window.** A single query can use data that changed mid-call; the next call
  detects it. Transient and self-healing, not persistent staleness.
- **`memories_embedding_repairs` is unbounded** and has no foreign key to memory lifetime. Explicit
  id reuse (reachable via `sync-to-d1 --replace`) can mark an unrelated row as recurring.
- **D1 ownership recovery after a lost response.** If an INSERT commits remotely but the response is
  lost, ownership recovery lacks bounded retry and a unique operation record.
- **External writers must populate the new columns.** A writer that emits a thresholded/sparse
  encoding of a dense vector is reported as an encoding fault naming the rows, and auto-rebuild is
  skipped deliberately rather than looping on something a rebuild cannot fix.

### Upgrading

1. Upgrade, then run `memory_verify_integrity` before anything else.
2. If it reports a repairable mismatch, run an explicit embedding rebuild.
3. Set `MEMORA_EMBEDDING_STRICT=1`.
4. **Verify by looking at a stored vector**, not at your config: it should have the dimension count
   your model produces, with numeric keys covering `0..N-1`. Configuration that looks correct is what
   hid this problem in the first place.
