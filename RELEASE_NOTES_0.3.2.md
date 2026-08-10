# memora 0.3.2

Consolidated 0.3.x release. **Supersedes 0.3.0 and 0.3.1, whose install path was broken** —
their notes remain in the repo as `RELEASE_NOTES_0.3.0.md` / `_0.3.1.md`.

## Fresh installs work again

`mcp` 2.0.0 removed `mcp.server.fastmcp`, and our dependency was an unbounded `mcp>=1.0.0`, so every
fresh install resolved to the new major and the server died at import with
`ModuleNotFoundError`. Now constrained to `mcp>=1.0.0,<2` (the 1.x line is maintained in parallel).

The failure was invisible from both sides: a dead stdio MCP server looks identical to one exposing no
tools, and existing environments had `mcp` pinned to a working 1.x, so every test suite passed.

Reported and fixed by [@BillyBunn](https://github.com/BillyBunn) in
[#44](https://github.com/agentic-box/memora/pull/44).

## Breaking

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

## Added

- `memory_verify_integrity` — read-only doctor reporting representation counts, coverage and
  offending ids, with concrete remediation.
- `MEMORA_EMBEDDING_STRICT=1` (**recommended**) turns a broken endpoint into a named error instead of
  silent degradation.
- Integrity derived from SQL and invalidated by a DB-owned change epoch, so an external writer cannot
  leave a stale "healthy" verdict behind.

## Fixed

- Batch embedding responses are validated for cardinality, index coverage and uniform dimensions, and
  reconstructed **by index** — a reordered response could previously attach a vector to the wrong
  memory.
- Coverage uses indexed anti-joins in both directions; counting by subtraction let one orphan cancel
  one missing embedding.
- `absorb` no longer leaves untracked partial rows.
- Concurrent `ensure_schema` no longer raises on duplicate columns.
- `install.sh` and the README no longer generate or document the broken configuration.

## memora-graph

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

## Known issues

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

## Upgrading

1. Upgrade, then run `memory_verify_integrity` before anything else.
2. If it reports a repairable mismatch, run an embedding rebuild.
3. Set `MEMORA_EMBEDDING_STRICT=1`.
4. **Verify by looking at a stored vector**, not at your config — it should have your model's
   dimension count with numeric keys covering `0..N-1`. Configuration that looks correct is what hid
   the original problem.
