# API2 design: `/api/v1` writes on the local primary

Scope: clmux `docs/PLAN_MEMORA_DAEMON.md` §3.1 (absorb route), §3.2 (exactly-once;
"only a row in `api_idempotency` with status `done` and a stored `response_json`
counts as completion"), §13 Q12 Option B. The contract
`contracts/memora-api/v1` is **FROZEN at 1.0.1**: response shapes stay those of
its fixtures; no contract edit. Writes land only on a local SQLite primary
(Phase L); a `d1://`, cloud or read-only store keeps `writes: "unsupported"`
and the absorb route 501s as today. Out of scope: anything in clmux, the search
route, D1 changes, and Phase L's lock watchdog/supervisor (bound 4).

## (1) Capability — `memora/api_v1.py`, `memora/storage.py`

`writes_capability(store)` returns `"transactional"` iff the store resolves to a
`backends.LocalSQLiteBackend` — the only backend whose writer connection sets
`supports_transactions=True`, i.e. the same condition L4's
`storage._has_transactions(conn)` requires. New side-effect-free helper
`storage.store_is_transactional(store)`: map the API store name via
`configured_stores()`, resolve `backend_for(registry_name)` or `STORAGE_BACKEND`,
catch `DatabaseRegistryError`/`DataVolumeRefused`/`StoreLockedError` → `False`.
No connection is opened, so `/health` stays read-only. `_health_body` already
calls `writes_capability`; the 501 branch in `_api_absorb` is unchanged.

## (2) Executor — `memora/api_absorb.py` (new), `memora/schema.py`, `memora/storage.py`

Schema: `_ensure_api_idempotency_table(conn)` in `ensure_schema`, non-D1 only:
`api_idempotency(key PK, request_sha256, status, owner, fence, response_json,
lease_until_ms, created_at_ms, updated_at_ms)`, status in
(`in_progress`,`done`,`failed_clean`). **Not** added to `SYNC_TABLES`: the
replicator's trigger allow-list is per table, and a replay is per store while D1
serves no writes (Option B), so the table stays local.

`api_v1.absorb_executor = api_absorb.execute` at import; the route keeps calling
it on a worker thread. On the store's writer connection (`storage.connect()`,
under the route's `_bound_store`):

1. **Claim**, one short `BEGIN IMMEDIATE` (a `store_write`): read the row.
   - absent → insert `in_progress` (owner `uuid4`, `fence=0`, lease now+LEASE_MS);
   - `done` → same `request_sha256` returns the stored body, different → 409
     `key_conflict`;
   - `in_progress` → different sha → 409 `key_conflict`; same sha with a live
     lease → 409 `in_progress`; expired, or `failed_clean` → takeover
     `fence=fence+1`.
   The frozen `absorb_in_progress` fixture is **409** (leader confirmed), so the
   design returns 409, not the "202" wording in the brief; the contract is
   authoritative.
2. **Absorb** outside the claim's lock: `storage.absorb_memory(conn, facts, ...,
   project=..., source=..., context=..., metadata=..., tags=..., phase3_done=cb)`.
   New optional `phase3_done` seam threaded through `_absorb_memory_impl` into
   `_absorb_phase3_transactional`, which calls `cb(conn)` inside the same
   `store_write`, after every effect. `cb` runs the fenced `UPDATE
   api_idempotency SET status='done', response_json=? WHERE key=? AND owner=?
   AND fence=?`; 0 rows raises `StoreWriteAborted` → the whole transaction rolls
   back (the fence rule). Phase 1/2 (LLM, embeddings, R2) stay outside the lock,
   exactly as L4 already enforces. `response_json` is the contract body
   (`status/created/superseded/skipped/linked/memory_ids/took_ms`) built from
   `counts`, created ids and the elapsed time.
3. Reply 200 with the stored body; a replay reads it back and never re-runs the
   LLM. `done` remains the only completion boundary. On any absorb failure the
   executor first re-reads the claim, because a commit can be ambiguous and an
   exception can occur after the done commit:
   - row now `done` → the transaction committed (an ack was lost); reply with the
     stored body, exactly once;
   - row still `in_progress` with this `owner` and `fence` → the phase-3
     transaction rolled back (the done update is inside it), so the executor may
     best-effort `failed_clean`, but only with the conditional
     `UPDATE ... SET status='failed_clean' WHERE key=? AND owner=? AND fence=? AND
     status='in_progress'`; it must never overwrite `done` and matches no row once
     a commit happened. An unreadable or ambiguous claim is left untouched for a
     replay or lease takeover, which always reads `done` first.

## (3) Tags — `memora/__init__.py`

Add `"landing"`, `"clmux"`, `"memora"` to `DEFAULT_TAGS`. Sourcing in
`_load_tag_whitelist`: `MEMORA_ALLOW_ANY_TAG=1` → any; else `MEMORA_TAG_FILE`
(or the packaged `config/allowed_tags.json` if present); else `MEMORA_TAGS`;
else `DEFAULT_TAGS`. The deploy keeps its `MEMORA_ALLOW_ANY_TAG=1` (leader
decision, msg 8283), so the container accepts any tag today and the new
`DEFAULT_TAGS` entries cover a container that does not set it. **Out of scope,
recorded P2 follow-up:** enforcing the plan §6.3/§13 Q4 restricted-tag policy
means dropping that env, which needs an audit of the tags the four stores
already use so existing writes are not rejected.

## (4) Deploy smoke — `scripts/deploy-memora-all.sh`, `scripts/rehearse_deploy.sh`

When `api-smoke.token` is present, the final Python, after the 200/403 store
checks, also `GET /api/v1/<allowed store>/health` with the token and asserts
`writes == "transactional"`; the rehearsal's deploy-4 grep gains that line.

## (5) Changelog — `CHANGELOG.md`

Unreleased entry in the v0.5.6 style. No version bump here; the release-prep
commit owns it (as for v0.5.6).

## (6) Tests

`tests/test_api_v1.py`: capability (`transactional` local, `unsupported` D1 /
cloud / refused), the real executor through the route, replay returns the stored
body without calling the LLM, same-key/different-sha 409, live-lease 409,
expired takeover, wedged resume rolls back, and an injected post-commit /
ack-loss failure whose replay returns the stored body without re-running the
LLM (exactly once). `tests/test_api_absorb.py` (new): claim/done/fence,
crash-at-each-statement via the L4 hooks plus new claim/done hooks, the
conditional `failed_clean` (never overwrites `done`), the replay response
compared to the `absorb_done`, `absorb_in_progress` and `absorb_key_conflict`
fixtures. `tests/test_schema.py`
(or the schema-pending test) for the new table. `tests/test_deploy_memora_all.py`
and `tests/test_api_contract.py` stay in the targeted set. The final report maps
each review finding to the section above.
