# The tidy pass (Item 251): design

> **Status: design only.** Nothing here is implemented. Names marked *(new)* do not exist yet;
> everything else (files, functions, line numbers at v0.5.7.9) is cited from the repo.
>
> **Scope, plainly.** `memora-dream` (docs/dream.md) checks that *references* exist. It does not make
> the store better organised. `memora-tidy` is a separate, periodic pass that proposes **fewer
> overlapping memories, retired outdated ones, consistent tags and links, and one current summary per
> project**. It proposes; a leader applies. It never deletes.

## Decisions already made (not reopened)

1. **Judge.** Sonnet for merges and digests (wording matters), Haiku for grouping and tags. Both run
   through `ClaudeCodeJudge` (`memora/dream.py:903`, `claude -p` on the subscription, same ToS rule:
   only the `claude` binary, no token reuse, no proxy).
2. **First project:** `clmux`.
3. **Cadence:** weekly, **dry run**, applied by hand by the leader at first.
4. **Digest lives in memora only** (a `kind: digest` memory). Any Obsidian export is out of scope.

## 1. Shape

A new module `memora/tidy.py` *(new)*, entry point `memora-tidy = "memora.tidy:main"` *(new, next to
`memora-dream` in `pyproject.toml:42`)*. It **imports and reuses** dream's machinery rather than
copying it: `Mem`, `load_memories`, `superseded_ids`, `open_readonly_file`, `Limits.from_dict`,
`Budget`, `run_lock`, `lower_priority`, `start_watchdog`, `plan_digest`/`load_plan`, `write_report`,
`_Writer`, the guarded-statement helpers (`_state_guard`, `_guarded_edge`, `_retired`) and
`extract_claims`. Anything tidy needs that is private to dream is promoted to a public name in the
same change, not re-implemented.

```sh
memora-tidy --project clmux --out tidy-report.json          # dry run: reads, judges, writes the report (0600)
memora-tidy --db copy.sqlite --project clmux --out r.json   # same against a read-only copy
memora-tidy --apply --plan tidy-report.json                 # applies exactly that report; no LLM
```

Config: `~/.config/memora/tidy.json` *(new, example in `scripts/tidy/tidy.json.example`)*. Models are
set per role: `MEMORA_TIDY_MODEL_GROUP=haiku`, `MEMORA_TIDY_MODEL_WRITE=sonnet` (no default model, as
in dream); `MEMORA_DREAM_JUDGE=claude-code` is required, and the CLAUDE_BIN variable is shared.
`ClaudeCodeJudge` already takes `model=` in its constructor, so two instances cover both roles.

## 2. Grouping (deterministic; no LLM decides membership)

Per project (`metadata.project`, as `dup_scan` does at `dream.py:1176`), over **live** memories only
(`superseded_ids` and tombstones excluded; same retirement test as `_retired`, `dream.py:1338`).

Build an undirected **evidence graph** with weighted edges, then take connected components under a
size cap:

| edge source | weight | note |
|---|---|---|
| embedding cosine ≥ 0.80 | 1 | computed **fresh** from `memories_embeddings` (`schema.py:324`), top-5 neighbours per memory, same project |
| shared reference (item number, repo file, SHA, tag), via `extract_claims` (`dream.py:392`) | 1 per distinct shared reference, capped at 2 | exact-match, so strong |
| typed edge: `supersedes`, `extends`, `references`, `contradicts` | 2 | a deliberate act |
| shared project-prefixed tag beyond the project tag itself | 0.5 | weak |
| `related_to` | **0** | see below |

Two memories are joined when the summed weight is ≥ 2. Components are split to **2–12 memories**
(a larger one is cut by dropping its weakest edges, then re-split; a singleton is skipped). One
cluster is one judge call.

**`related_to` is not evidence (m453).** Crossrefs mix three things under `edge_type="related_to"`:
the implicit default from `compute_crossrefs` (a real cosine), absorb's `link_memories` (a hardcoded
`score=1.0`), and dream's own `related_to` links for near-duplicates. `find_duplicate_pairs`
(`storage.py:2398`) already skips `score >= 0.9999` for exactly this reason. Tidy goes further: it
treats **no** `related_to` entry as evidence, because a stored cosine is a snapshot from when the
neighbour set was computed and the 1.0 entries are not similarities at all. The cosine is recomputed
from the embeddings.

**Reuse of the existing helpers.**
- `find_duplicate_candidates` (`storage.py:2513`) is **not** used for grouping: it reads stored
  crossrefs (the thing above), is project-blind, and returns pairs, not clusters. Its pair output is
  still useful as a *measure* (section 9: duplicate pairs before and after).
- `find_supersession_candidates` (`storage.py:2529`) is not used for the same reason, but its
  newer/older rule (created_at, then id) is the tie-break tidy uses.
- `memory_digest` (`server.py:2483`) is **not** the digest generator: it is a search-and-aggregate
  read tool ("not a narrative generator"). Tidy's digest memory is what `memory_digest` (and plain
  search) will *find*; `memory_digest` is reused only for the retrieval check in section 6.
- `dup_scan` (`dream.py:1176`, 4-shingle Jaccard ≥ 0.9, same numbers) is reused as one cheap input:
  a pair it finds enters the cluster as a weight-2 edge.

## 3. Per-cluster proposals

For each cluster, **one** judge call returns a strict JSON answer (same fail-closed contract as
dream's `parse_verdicts`, `dream.py:811`: one deviation rejects the whole cluster, nothing is
proposed for it):

```json
{"proposals": [
  {"op": "merge",  "sources": [12, 40, 41], "text": "...", "facts": [{"fact": "...", "from": [12]}], "reason": "..."},
  {"op": "retire", "id": 7, "by": 40, "reason": "..."},
  {"op": "link",   "a": 40, "b": 55, "edge_type": "extends", "reason": "..."},
  {"op": "retag",  "id": 12, "add": ["clmux/spawn"], "remove": ["misc"], "reason": "..."},
  {"op": "keep",   "id": 9, "reason": "..."},
  {"op": "contradiction", "ids": [3, 40], "reason": "..."}
]}
```

Every memory in the cluster appears in exactly one of: a `merge.sources`, a `retire.id`, a `link` or
`retag` (which do not hide anything), a `keep`, or a `contradiction`. A missing or repeated id is a
rejection.

- **merge (N → 1).** A *new* memory that supersedes all N sources (a `supersedes` edge per source,
  as `--apply` already does for corrections). Every fact in the new text carries the ids it came
  from (`facts[].from`, each an element of `sources`). **No new claims.** The mechanical check
  (`merge_problem`, *new*, in the spirit of `correction_problem`, `dream.py:1087`):
  1. every distinct number, SHA, version tag, `Item N`, path and backticked identifier found by
     `extract_claims` + the number regex in any source occurs in the merged text, **or** the plan
     lists it under `dropped` with the source id and a reason, and that reason is one of an
     allow-list (`duplicate_of`, `superseded_value`, `quoted_history`); anything else rejects;
  2. no reference, number or identifier appears in the merged text that is in **no** source;
  3. every `facts[].from` id is a source, every source contributes at least one fact;
  4. the merged text is ≥ 60% of the longest source and keeps ≥ 60% of the distinct words of the
     union (the same size guard as corrections), and carries no prompt-envelope text;
  5. metadata of the sources is unioned (`project`, `section`, `hierarchy` must agree, else
     reject), tags are the union of the sources' tags, and the new memory records
     `metadata.tidy = {run, merged_from: [ids]}`.
  The check is mechanical, so it cannot be argued with; a failed merge is dropped with the reason in
  `proposals_rejected` and the cluster falls back to `link` proposals only.
- **retire.** Supersede memory `id` with the newer, already-existing memory `by` (cited). Allowed
  only when `by` is live, newer, in the same cluster, and shares at least one extracted reference or
  typed edge with `id` (so the model cannot retire one thing "in favour of" an unrelated note).
- **link.** A typed edge between two live memories. `related_to` is never proposed: it is the
  overloaded edge of m453. Allowed types: `extends`, `references`, `contradicts`.
- **retag.** Add or remove tags. Must stay inside a tag vocabulary built from the project's existing
  tags (new tags only as `<project>/<existing section>`); the Haiku grouping call proposes, the code
  validates.
- **keep.** Explicit no-op, recorded so acceptance rates are measurable.
- **contradiction.** Two memories that assert incompatible things. **Flagged only**: the report lists
  it for a leader. The model never resolves it, and nothing is written for it.

Roles: Haiku makes the `link`/`retag`/`keep`/`contradiction` call per cluster (grouping and tags);
if that call proposes a merge or a retire, the cluster is sent to Sonnet **once more**, which
writes the merged text. So a cluster costs 1 Haiku call, plus 1 Sonnet call when text is needed.

## 4. Using `references_checked`

Dream's metadata (`dream.py:1071`) is read, never trusted as truth:

- A memory whose `references_checked.summary` is `missing`, `stale` or `partial` becomes a **review
  candidate**: it is listed under `review[]` in the report with the missing references, and is
  shown to the cluster judge as context ("a reference of this note was not found").
- It is **never retired on that alone.** Absence is not an error (docs/dream.md, Item 244). A retire
  still needs the section 3 rule: a newer live memory in the cluster sharing a reference or typed edge.
- A memory with `all_found` is not "verified"; it only means references resolve.

## 5. Safety (the rules, enforced in code, each with a test)

1. **Never delete.** There is no delete path in `memora/tidy.py`. Hiding a memory is only a
   `supersedes` edge (as in `--apply` for dream). History stays reachable: `follow` reads and
   `memory_get` on the old id still work, and D1 never sees a `D` outbox row from tidy.
2. **Never merged or retired automatically:** `item-record` (the existing `Mem.protected`,
   `dream.py:258`), memories with `kind: decision` or a `decision` tag (the same set dream's
   `priority` uses at `dream.py:1016`), `kind: digest` and `kind: procedure`. They may be **linked**
   and **retagged only by adding**; they may sit in a cluster as context. Protected status is read
   **at apply time**, never taken from the plan (as dream does, `dream.py:1527`); a protected memory
   in a merge/retire step makes that step fail closed.
3. **Caps per run** (`limits` in config, tested): `max_clusters` 12, `max_llm_calls` 24,
   `max_merges` 10, `max_retires` 10, `max_links` 20, `max_retags` 20, `max_writes` 20 (charged before
   dispatch as in dream: a merge costs its worst case, new memory + one edge per source; the rest is
   `deferred`), `max_seconds` 1500, `nice` 10. A merge of more than 8 sources is split.
4. **The plan is sealed.** `plan_digest` over the whole report; `--apply` refuses a changed digest or
   a plan older than 48 h, and at each write re-reads the memory and skips unless its `state_sha` is
   still what was judged (the guarded statements of `dream.py:1358-1394`). One run at a time (file
   lock in `state_dir`).
5. **Apply is a leader act.** Same procedure as m1383, in this order:
   1. rehearse `--apply` on a **writable copy** with the container's embedding env (a 0600 file,
      never printed); delete the copy's `-shm`/`-wal` sidecars between runs; diff `sync_outbox`
      before/after: only `U`/`I`-style rows, **no `D` rows**;
   2. stop the watchdog service first, then the memora container;
   3. run `memora-tidy --apply` in a one-off container (same image, data volume, the plan dir
      mounted read-only, `MEMORA_LLM_ENABLED=false`);
   4. start the container, then the watchdog;
   5. check D1: `last_acked_seq == max(sync_outbox.seq)`, no halt, no error.
6. **Contradictions** are listed in the report under `contradictions[]` and never written.
7. **A protected-memory edit fails closed**: any write the gate or the `protected` check refuses
   aborts that step, not just logs it; the step is listed under `skipped` with the reason.
8. **No writes from the model's text beyond the merged memory body and tag lists**; everything else
   (ids, edge types, metadata) is code.

## 6. The project digest

One `kind: digest` memory per project (`metadata = {kind: "digest", project: "clmux",
digest_of: "tidy", run, covers: [ids]}`, tag `<project>/digest`), written by Sonnet from the project's
**live** memories (and open todos/issues). It supersedes the previous digest of that project (a
`supersedes` edge), so there is exactly one live digest per project. Sections, fixed:

```
## Current decisions        - <statement> [m123]
## Open follow-ups          - <statement> [m456]
## Constraints / pitfalls   - <statement> [m789]
```

Rules, enforced like merges: **every line ends in at least one memory id** that exists, is live and
in the project; no line without an id; no number, path, SHA or identifier that is in none of the
cited memories (the same mechanical check); ≤ 40 lines. The digest cites; it never replaces: all
sources stay live. Protected memories are cited, not rewritten. A digest is generated **after** the
cluster step in the same plan, from the post-plan set of live memories, so it cites the merged
memories, not the retired ones (apply order: clusters, then digest).

**How a session finds it.** By the same ordinary tools:
`memory_hybrid_search(query="<project> digest", metadata_filters={"kind": "digest", "project": "<project>"}, follow="active")`
(the `follow="active"` argument already hides the superseded digest), or `memory_list` with the same
filter. The memora skill (`.claude/skills/memora/SKILL.md`) should be told to read the digest first
when it starts work on a project; that is a one-line change in the apply phase, not in this item.
`memory_digest(topic=...)` (`server.py:2483`) should also surface it, since it aggregates active hybrid-search hits; check this in phase 2.

## 7. Where it runs, and cost

Same user-approved exception as dream on the always-on server: `nice`/`ionice -c 3`, the run lock,
the caps above, **read-only** mirrors (tidy needs none: it reads the store copy, not git), no builds,
no tests. It reads a **copy** of the store (`--db copy.sqlite`, `open_readonly_file`), never the live
file, and has no network access except the `claude` binary's. Writes happen only in `--apply`, run by
the leader on a stopped container (section 5).

Subscription usage per weekly run, first project (`clmux`), estimated: at most 12 clusters × (1 Haiku
+ about 0.5 Sonnet) + 1 Sonnet digest ≈ **12 Haiku + 7 Sonnet = about 19 calls**, hard-capped at 24
(`max_llm_calls`). Each is a fresh print-mode run, a few thousand input tokens (a cluster is ≤ 12
memories). Measure the real figure in the first dry run and put it in the report
(`judge.calls`, `judge.by_model`).

## 8. Report schema (sketch)

```json
{"version": 1, "run": "…", "at": "…", "project": "clmux", "digest": "<sha256 of the rest>",
 "judge": {"status": "ok", "calls": {"haiku": 12, "sonnet": 7}},
 "limits": {"…": 0}, "stopped_by": null,
 "counts": {"live": 0, "clusters": 0, "duplicate_pairs": 0, "contradictions": 0},
 "clusters": [{"id": "c1", "members": [12,40,41], "evidence": [{"a":12,"b":40,"why":"shared:Item 244"}],
               "proposals": [], "proposals_rejected": [{"op":"merge","reason":"dropped_fact:…"}]}],
 "digest_proposal": {"supersedes": 801, "text": "…", "lines": [{"text":"…","ids":[1,2]}]},
 "review": [{"id": 5, "references_checked": "missing", "missing": ["…"]}],
 "contradictions": [{"ids": [3,40], "reason": "…"}],
 "plan_steps": [{"step": 1, "op": "merge", "expect": {"12": "<state_sha>", "40": "<state_sha>"}}]}
```

`plan_steps` is the only thing `--apply` reads. `clusters[]` and the rest are for the human.

## 9. Success measures

All taken in the dry-run report and again after the apply:

- **Counts:** live memories in the project, duplicate pairs (`find_duplicate_pairs`,
  `storage.py:2398`, with the m453 filter), contradictions flagged.
- **Acceptance rate:** of the proposals in the report, the share the leader applies unchanged (a
  `decisions.json` the leader writes when pruning the plan before `--apply`). Target to proceed to a
  second project: ≥ 80% on merges and retires, and **zero** merges rejected afterwards for a
  dropped or invented fact.
- **Retrieval:** a fixed query set (about 20 real queries for the project, recorded once in
  `scripts/tidy/queries.json`) is run before and after through the existing read-effectiveness
  ledger (Item 118, off by default; the memora effectiveness pilot): top-5 hit contains an expected
  memory (or its successor, via `follow="active"`), and the digest is the first hit for
  "what is current for <project>". A drop in recall blocks the next phase.

## 10. Phasing

1. **Design** (this document).
2. **Dry-run-only implementation:** `memora/tidy.py`, the config, the unit file with no `--apply`
   (like dream's timer: the unit must never contain `--apply`), tests with a fake judge. No write path.
3. **One real report** on clmux, read by the user and the leader; the success measures are baselined.
4. **Apply step:** `--apply`, rehearsed on a copy, then live with the m1383 procedure.
5. Only after two clean weekly applies: a second project.

## 11. Open questions, with a recommendation

| # | Question | Recommendation |
|---|---|---|
| 1 | Should merges keep the sources' `created_at` ordering, or take "now"? | "Now" (a new memory), with `merged_from`; the sources stay reachable. |
| 2 | Should retired memories also be tagged (`tidy/retired`)? | No: tag edits on retired memories add writes for nothing; the `supersedes` edge is the record. |
| 3 | Do item-record memories ever get merged? | No, never (decided above). Only linked. A later opt-in needs its own item. |
| 4 | Sonnet for digest only, or also for merges? | Both (decided); revisit if the first report shows Haiku merges pass the mechanical check equally often. |
| 5 | One lock per project or one global? | One global (dream's `run_lock`); the weekly schedule never overlaps. |
| 6 | Cosine threshold 0.80 and cluster size 12: right? | Start there, make them config; tune from the first report's `evidence` data. |
| 7 | Does the digest need a staleness mark when cited memories later change? | Yes, cheaply: the next run regenerates it; between runs `covers` + `updated_at` lets a reader see drift. Not in phase 2. |
| 8 | Where does the digest stay for projects with no `metadata.project`? | Skipped: tidy refuses a null project. |
| 9 | Is a rejected merge retried next week? | Yes, but a cluster whose merge was rejected twice is listed under `review[]` and not re-sent to the judge. |
