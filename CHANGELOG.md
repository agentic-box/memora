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
