# memora 0.3.0

**Read this before upgrading. This release forces a one-time rebuild of every stored embedding.**

## Why this release exists

Memora could be configured — following its own README and its own installer — into a state where
**no embedding was ever computed and nothing said so**. Every semantic search silently became a
keyword search, and the store filled with keyword bags while reporting healthy.

The trigger: `install.sh` generated `OPENAI_EMBEDDING_MODEL="openai/text-embedding-3-small"` and the
README recommended OpenRouter as an `OPENAI_BASE_URL`. **OpenRouter serves no embeddings endpoint**
— its catalogue lists 400 models and zero with an embedding task. Every embed call returned 404,
memora logged one warning to a server's stderr, fell back to TF-IDF, and carried on.

In the store where this was found, 756 memories had been keyword vectors for months.

## Breaking

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

## Added

- `memory_verify_integrity` — a read-only doctor reporting representation counts, coverage, and
  bounded lists of offending ids, with concrete remediation.
- `MEMORA_EMBEDDING_STRICT=1` (**recommended**) turns a broken endpoint into a hard, named error
  instead of silent degradation. This is the flag whose absence let the failure above run for months.
- Integrity is derived from SQL and invalidated by a database-owned change epoch maintained by
  triggers, so an external writer — a sync script, a Worker, another process — cannot leave a stale
  "healthy" verdict behind.
- Per-row `representation`, `dimension`, `encoding_source`, `writer_token`. Rows written by an
  unrecognised writer are marked unknown rather than assumed valid.

## Fixed

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

## memora-graph (web UI)

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

## Known issues

- **Epoch time-of-check window.** A single query can use data that changed mid-call; the next call
  detects it. Transient and self-healing, not persistent staleness.
- **`memories_embedding_repairs` is unbounded** and has no foreign key to memory lifetime. Explicit
  id reuse (reachable via `sync-to-d1 --replace`) can mark an unrelated row as recurring.
- **D1 ownership recovery after a lost response.** If an INSERT commits remotely but the response is
  lost, ownership recovery lacks bounded retry and a unique operation record.
- **External writers must populate the new columns.** A writer that emits a thresholded/sparse
  encoding of a dense vector is reported as an encoding fault naming the rows, and auto-rebuild is
  skipped deliberately rather than looping on something a rebuild cannot fix.

## Upgrading

1. Upgrade, then run `memory_verify_integrity` before anything else.
2. If it reports a repairable mismatch, run an explicit embedding rebuild.
3. Set `MEMORA_EMBEDDING_STRICT=1`.
4. **Verify by looking at a stored vector**, not at your config: it should have the dimension count
   your model produces, with numeric keys covering `0..N-1`. Configuration that looks correct is what
   hid this problem in the first place.
