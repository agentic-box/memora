# Memora 0.3.3

Search accuracy and hardening release.

## Search
- Full-text search now queries the FTS index correctly, improving keyword relevance; substring matching remains only as an explicit fallback.
- `limit` is honored on `memory_hybrid_search` and `memory_semantic_search` (`top_k` still accepted).
- Searches and lists with lineage filtering (`follow=active/latest`) fill the requested result count even when top-ranked candidates are superseded, scanning beyond the previous 5,000-row window with a loud error at the safety bound instead of silent truncation.

## Absorb & lineage
- Absorb updates supersede the current version of a memory, resolving through the supersession chain to the leaf.
- New classifier measurement harness: labeled fixture pairs, per-class precision/recall and confusion matrix, dry-run safe, with a `--min-macro-f1` gate for regression testing.
- All LLM calls are bounded by an explicit timeout (`MEMORA_LLM_TIMEOUT`, default 60s). Measurement mode fails loud; runtime absorb degrades gracefully.

## Tag policy
- The Cloudflare graph app validates tag writes (memory edit and chat) against a versioned policy stored per database, failing closed when the policy is unavailable.
- Wildcards support slash namespaces (`memora/*`) alongside dot namespaces; tags are capped at 100 characters, counted identically (Unicode code points) in Python and TypeScript and guarded by a shared conformance fixture.

## Graph UI
- The WebGL canvas tracks its container through drawer transitions via ResizeObserver, fixing a sizing race under load.

## CI
- New `clean-install` workflow: builds the wheel, installs into an empty environment, and runs the suite — on push, tags, and a daily schedule.
- New `graph-ui` workflow: browser tests for the graph UI (drawers, top bar, render-idle power behavior, database selector), tag-policy write tests, and lineage logic tests against a seeded local D1.

## Docs
- Install instructions lead with PyPI (`pip install memora-mcp`); absorb, supersession lineage, and digest documented in Features; `MEMORA_TAG_FILE` format corrected (JSON array).
