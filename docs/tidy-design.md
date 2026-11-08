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
  {"op": "merge",  "sources": [12, 40, 41], "text": "...",
   "facts": [{"fact": "...", "spans": [{"id": 12, "quote": "exact substring of memory 12"}]}],
   "dropped": [{"id": 41, "quote": "exact substring", "why": "duplicate_of", "of": {"id": 12, "quote": "..."}}],
   "reason": "..."},
  {"op": "retire", "id": 7, "by": 40, "why": "obsolete|redundant", "evidence": [{"id": 7, "quote": "..."}, {"id": 40, "quote": "..."}]},
  {"op": "link",   "a": 40, "b": 55, "edge_type": "extends", "reason": "..."},
  {"op": "retag",  "id": 12, "add": ["clmux/spawn"], "remove": ["misc"], "reason": "..."},
  {"op": "keep",   "id": 9, "reason": "..."},
  {"op": "contradiction", "ids": [3, 40], "quotes": [{"id": 3, "quote": "..."}, {"id": 40, "quote": "..."}]}
]}
```

**Dispositions.** Each cluster member has exactly one *primary disposition*: a `merge.sources`
member, a `retire.id`, a `keep`, or a `contradiction` member. `link` and `retag` are *auxiliary*
operations: they hide nothing, so a member may have any number of them next to its primary
disposition. A member with no primary disposition, or two, rejects the cluster.

"Model text" means exactly: the merged body, the digest lines, and quotes/reasons. Ids, edge types,
tags and metadata are validated against code enums and the store (a tag must already exist in the
project's vocabulary or be `<project>/<existing section>`); an unknown value rejects the proposal.

### 3.1 The fact-preservation contract (merge)

The mechanical checks are **necessary, not sufficient**. The token and ratio guards (numbers, SHAs,
tags, `Item N`, paths, backticked identifiers via `extract_claims` with `limit` raised to unlimited
for tidy, plus a word-level size guard of >= 60% of the longest source) catch careless loss; they
cannot prove that a plain-prose constraint survived (example: "Uploads require admin approval" merged
into "Uploads require approval" keeps every token class and passes them). So a merge is **only a
proposal that hides sources after a leader approves it**, and the report is built so that approval is
fast and checkable:

1. **Source-fact inventory (code, not model).** Each source is split into *statements* (sentences and
   list items, deterministic splitter, no truncation). The report lists every statement with its id
   and character span.
2. **Coverage.** The model must map every statement of every source to exactly one of: a
   `facts[].spans` entry (an exact substring quote of that statement that the merged text
   expresses), or a `dropped` entry. The code checks that each quote is a verbatim substring at the
   stated memory, and that the union of spans and drops covers **every statement**; an unmapped
   statement rejects the merge. Each `facts[].fact` must be a verbatim substring of the merged
   `text`, and each span quote must be a substring of its source; so every merged sentence has a
   provenance span, and a merged sentence with no span (an invented claim) rejects the merge.
3. **Drops are not self-justifying.** `dropped.why` is only `duplicate_of` or `quoted_history`.
   `duplicate_of` requires an `of` span in another source whose normalised text (case, whitespace,
   punctuation folded) **equals or contains** the dropped statement; otherwise it is not a
   duplicate. `quoted_history` is accepted only when the statement carries a literal marker
   ("was", "previously", "used to", "before", a dated past event) *and* the leader approves it. No
   other reason exists; `superseded_value` is not allowed (replacing a value is a retire/contradiction
   decision, not a merge detail).
4. **Approval.** Every `dropped` entry with `quoted_history`, and every merge where a statement was
   reworded rather than kept verbatim, is marked `needs_approval: true` in the report. The leader's
   decision file (section 5.4) must approve each one; `--apply` skips an unapproved merge.
   Verbatim-only merges (all statements kept as exact quotes or exact duplicates) need no per-fact
   approval beyond the normal step approval.
5. **Heuristic guards stay on** (numbers, 60% size, same project/section/hierarchy, no prompt
   envelope) and reject, but a pass proves nothing; the audit trail is the span table above.

Fail-closed outcomes: any of the above failing drops the merge into `proposals_rejected` with the
reason; the cluster keeps its `link`/`retag` proposals and nothing is hidden.

**Accepted / rejected examples.**

| sources | merged text | outcome |
|---|---|---|
| A: "Uploads require admin approval. Logs are kept 30 days." / B: "Logs are kept 30 days." | "Uploads require admin approval. Logs are kept 30 days." | accepted: B's statement is `duplicate_of` A's (equal after folding); both A statements have spans |
| same | "Uploads require approval. Logs are kept 30 days." | rejected: "admin approval" has no verbatim quote in the merged text's fact span; the merged sentence `Uploads require approval.` is not a substring of any source, so it has no span |
| A: "Port is 8080 (was 8000)." / B: "Port is 8080." | "Port is 8080." | held for approval: "(was 8000)" is dropped as `quoted_history` |
| A: "Use X." / B: "Do not use X." | any single text | no merge: the cluster is a `contradiction` (see 3.3) |
| A and B share only "Item 44", each states a different constraint | "..." | rejected: shared reference is never a duplicate proof; both statements must be kept |

### 3.2 Retire

A retire hides `id` behind the newer, existing, live memory `by`. It requires **demonstrated
redundancy or obsolescence**, never topical overlap or a shared reference:

- `why: redundant`: every statement of `id` is `duplicate_of` a statement of `by` by the same
  folded-equality rule as 3.1(3) (quotes required, verified by code); or
- `why: obsolete`: `by` contains a statement the judge quotes as replacing a specific statement of
  `id`, **and the leader approves it** (`needs_approval: true` always). A retire of an `obsolete`
  kind is never applied without that approval.
- `by` must be live, newer, in the cluster, unprotected, and not part of a `contradiction`.
- A memory in a `contradiction` is **never** retired or merged: contradictions block hiding of every
  member. The model cannot resolve one by retiring a side.

### 3.3 Other operations

- **link.** A typed edge between two live memories; `related_to` is never proposed (m453). Allowed
  types: `extends`, `references`, `contradicts`.
- **retag.** Within the project's tag vocabulary (see above). Haiku proposes; the code validates.
- **keep.** Explicit no-op, recorded so acceptance rates are measurable.
- **contradiction.** Two memories that assert incompatible things, each with a verbatim quote.
  **Flagged only**: listed for a leader, never written, and every member is frozen for this run.

Roles: Haiku makes the `link`/`retag`/`keep`/`contradiction` call per cluster (grouping and tags);
if it proposes a merge or a retire, the cluster is sent to Sonnet **once more**, which writes the
merged text. So a cluster costs 1 Haiku call, plus 1 Sonnet call when text is needed.

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
   `supersedes` edge. History stays reachable (`follow` reads, `memory_get` on the old id); D1 never
   sees a `D` outbox row from tidy.
2. **Never merged or retired automatically:** `item-record` (`Mem.protected`, `dream.py:258`),
   `kind: decision` or a `decision` tag (dream's set, `dream.py:1016`), `kind: procedure`. They may
   be **linked** and **retagged only by adding**; they may sit in a cluster as context. Protection is
   read **at apply time**, never from the plan (`dream.py:1527`); a protected memory in a merge/retire
   step makes that step fail closed. **`kind: digest` has one narrow exception**, the *digest
   replacement* operation of section 6.4: allowed only when the old memory has `kind: digest`, the
   same `project`, is the one live digest of that project, and equals the judged row
   (`state_sha`); it cannot hide anything else, and a digest is never merged or retired by a
   cluster step.
3. **Caps per run** (`limits`, tested): `max_clusters` 12, `max_llm_calls` 24, `max_merges` 10,
   `max_retires` 10, `max_links` 20, `max_retags` 20, `max_writes` 40, `max_seconds` 1500, `nice` 10.
   **Write charges, before dispatch** (like dream: charged, not refunded, remainder `deferred`):
   a merge of N sources costs **1 + 2N** (the new memory, then per source the forward `supersedes`
   edge held by the new memory and the reverse edge on the source, each a separately committed
   guarded call, as `_guarded_edge`, `dream.py:1374`, does); a retire 2; a link 2 (one per side);
   a retag 1; a digest replacement 3 (create + forward + reverse); an *ambiguous* call (deadline hit
   mid-call) keeps its charge and is listed, never retried in the same run. A merge of more than 8
   sources is split. `max_writes` is therefore 40, not dream's 20, and the plan is pruned to fit
   before it is sealed, not at apply time.
4. **The plan is sealed; approvals are separate.** `plan_digest` (`dream.py:1297`) binds the whole
   report, including every body. The leader never edits the report. Acceptance goes in a separate
   `decisions.json` *(new)*: `{"report_digest": "<digest>", "approve": ["s3","s7"], "reject": ["s5"],
   "approve_dropped": ["s3:41"]}`. `--apply --plan report.json --decisions decisions.json` refuses a
   decisions file whose `report_digest` is not the report's digest, an id not in the report, or an
   approval of a step that is not `needs_approval`-eligible. It can only select among sealed steps;
   it cannot change a body, guard or order. A step not approved is not applied (default is
   reject).
5. **Apply is a leader act**, in this order (the m1383 procedure; its rules: the leader rehearses on a
   store copy and reads `sync_outbox`/`sync_state`, stops the watchdog **before** the container,
   applies in a one-off container, restarts, then checks D1):
   1. rehearse `--apply` on a **writable copy**, taken as a consistent copy (SQLite online backup or
      a copy made with the container stopped, i.e. after a checkpoint with every handle closed). Never
      delete `-wal`/`-shm` of a copy that has uncheckpointed committed rows. m1383's pitfall (a
      reused read-only copy keeps stale sidecars and fails with `wal_shm_unusable`) is avoided by
      making a **new** copy per run, not by deleting sidecars of a source; with the embedding env in a
      0600 file, never printed; diff `sync_outbox` before and after: only `U`/`I`-style rows, **no `D`**;
   2. stop the watchdog service first, then the memora container;
   3. run `memora-tidy --apply` in a one-off container (same image, data volume, plan dir read-only,
      `MEMORA_LLM_ENABLED=false`);
   4. start the container, then the watchdog;
   5. check D1: `last_acked_seq == max(sync_outbox.seq)`, no halt, no error.
   This design item does not read credentials or perform the procedure.
6. **Contradictions** are listed under `contradictions[]` and never written; members are frozen.
7. **A protected-memory edit fails closed**: a write the gate or the protected check refuses aborts
   that step (and any step that depends on it, section 6.3); it is listed under `skipped` with the reason.
8. **No LLM in apply.** `--apply` runs no model call and no git; it only executes sealed steps.

## 6. The project digest

One `kind: digest` memory per project (`metadata = {kind: "digest", project: "clmux",
digest_of: "tidy", run, covers: [ids]}`, tag `<project>/digest`), written by Sonnet. Exactly one is
live per project; each new one supersedes the previous.

### 6.1 Content and bounds

Sections, fixed: `## Current decisions`, `## Open follow-ups`, `## Constraints / pitfalls`, each line
`- <statement> [m123]`. Rules, enforced like merges (section 3.1 span contract: each line carries a
verbatim span quote from a cited memory): **every line cites at least one memory id** that exists
(or is a planned create, 6.2), is live and in the project; no number, path, SHA or identifier that is
in none of the cited memories; <= 40 lines. The digest cites; it never replaces: sources stay live.

**Input bound.** The digest call does not receive "all live memories". It receives, in priority
order: protected decisions and item-record statements, open todos/issues, memories updated in the
last 90 days, then the rest by read count (read ledger), up to `digest_max_memories` (60) and
`digest_max_chars` (60,000). The report records `digest_input: {considered, included, excluded}`, so
a reader sees what the digest did not look at; a digest says "covers N of M live memories".

### 6.2 Planned creates and symbolic references

The digest is generated in the dry run, **after** the cluster step and from the post-plan set of
memories, but a merged memory has no database id until apply. So:

- each planned create has a **symbolic id** `@c<k>` (for example `@c3`), sealed in the report with its
  full body, tags, metadata and provenance spans;
- the digest's sealed text cites `@c3` where it cites that memory; the sealed step lists
  `depends_on: ["s3"]`;
- at apply, steps run in dependency order (all cluster steps, then the digest step). When step s3 has
  written the new memory with id N, `@c3` is substituted by `N` in the digest body by code (a plain
  string replace of the sealed token, no model call).

### 6.3 Dependency failures

If s3 is skipped, deferred, ambiguous, rejected by the leader or protected-failed, the digest step is
**refused** and recorded as `deferred: dependency s3`; it is not rewritten. The previous digest stays
live. No step may cite a result that did not commit. Because apply never regenerates text, the next
weekly dry run produces a digest from the then-current store.

### 6.4 Digest replacement operation

A dedicated apply step `replace_digest` *(new)*, not a merge or retire: it creates the new
digest memory, then the forward and reverse `supersedes` edges from the new digest to the **expected
prior digest id** recorded in the sealed step. Guards, in one statement as in `_guarded_edge`: the
old memory is still exactly the judged row (`state_sha`), has `kind: digest` and the same `project`,
and has no existing superseder. Exactly-once: before creating, apply re-reads "live digests of this
project"; if there is already one that is **not** the expected prior (a half-applied earlier retry, or
a concurrent writer) the step stops as ambiguous and writes nothing. If the create succeeded but the
edges did not (partial failure), the step records the new id; a retry (the same sealed plan, or a new
one) finds two live digests and a `repair_digest` plan step is produced by the next dry run
(supersede the older one, same guards). So the system converges to one live digest, and never has
none: the old one is hidden only after the new one exists.

### 6.5 How a session finds it

By the ordinary tools:
`memory_hybrid_search(query="<project> digest", metadata_filters={"kind": "digest", "project": "<project>"}, follow="active")`
(`follow="active"` hides the superseded digest), or `memory_list` with the same filter. The memora
skill (`.claude/skills/memora/SKILL.md`) should read the digest first when it starts work on a
project; that is a one-line change in the apply phase, not in this item. `memory_digest(topic=...)`
(`server.py:2483`) should also surface it, since it aggregates active hybrid-search hits; check this
in phase 2.

## 7. Where it runs, and cost

Same user-approved exception as dream on the always-on server: `nice`/`ionice -c 3`, the run lock,
the caps above, **read-only** mirrors (tidy needs none: it reads the store copy, not git), no builds,
no tests. It reads a **copy** of the store (`--db copy.sqlite`, `open_readonly_file`), never the live
file, and has no network access except the `claude` binary's. Writes happen only in `--apply`, run by
the leader on a stopped container (section 5).

Subscription usage per weekly run, first project (`clmux`): the worst case is 12 clusters x (1 Haiku
+ 1 Sonnet) + 1 Sonnet digest = **25 calls**, over `max_llm_calls` 24. The digest call is therefore
**reserved first**: the budget for cluster calls is `max_llm_calls - 1`, so clusters are processed in
priority order (largest evidence weight first) and the rest are listed `deferred_clusters` for next
week. Typical: about 12 Haiku + 6 Sonnet + 1 Sonnet digest = 19. Each is a fresh print-mode run,
bounded by the cluster size (<= 12 memories) or the digest input bound (60,000 chars). Measure the
real figure in the first dry run (`judge.calls`, `judge.by_model`).

## 8. Report schema (sketch)

The report reuses dream's seal. `load_plan` (`dream.py:1306`) requires `version == PLAN_VERSION`,
`mode == "dry-run"`, a valid `created_ts` and a matching `digest`; tidy has its own `TIDY_PLAN_VERSION`
and `load_tidy_plan` *(new)*, a thin adapter with the same four checks (and the 48 h age limit), so
the seal is not weakened. Fields `run` and `at` are informational.

```json
{"version": "tidy-1", "mode": "dry-run", "created_ts": 0, "run": "...", "at": "...", "project": "clmux",
 "digest": "<sha256 of the rest, plan_digest>",
 "judge": {"status": "ok", "calls": {"haiku": 12, "sonnet": 7}},
 "limits": {}, "stopped_by": null, "deferred_clusters": [],
 "counts": {"live": 0, "clusters": 0, "duplicate_pairs": 0, "contradictions": 0},
 "sources": {"12": {"sha": "<state_sha>", "statements": [{"i": 0, "span": [0, 41]}]}},
 "clusters": [{"id": "c1", "members": [12,40,41], "evidence": [{"a":12,"b":40,"why":"shared:Item 244"}],
               "proposals_rejected": [{"op":"merge","reason":"unmapped_statement:41#2"}]}],
 "steps": [{"id": "s3", "op": "merge", "cluster": "c1", "create": "@c3",
            "body": "...", "tags": [], "metadata": {}, "sources": [12,40,41],
            "spans": [], "dropped": [], "charge": 7, "needs_approval": true,
            "expect": {"12": "<state_sha>", "40": "<state_sha>"}, "depends_on": []},
           {"id": "s9", "op": "replace_digest", "body": "... [@c3] ...", "supersedes": 801,
            "expect": {"801": "<state_sha>"}, "depends_on": ["s3"], "charge": 3}],
 "digest_input": {"considered": 0, "included": 0, "excluded": 0},
 "review": [{"id": 5, "references_checked": "missing", "missing": ["..."]}],
 "contradictions": [{"ids": [3,40], "quotes": []}]}
```

`steps` is the only thing `--apply` reads and it contains every sealed payload (bodies, tags,
metadata, spans, dependencies, charges, guards); nothing executable lives outside the sealed
digest. Approvals come from the separate `decisions.json` (section 5.4).

## 9. Success measures

All taken in the dry-run report and again after the apply:

- **Counts:** live memories in the project, duplicate pairs (`find_duplicate_pairs`,
  `storage.py:2398`, which is project-blind and does not exclude all retired rows: the metric filters its
  pairs to the project and to live memories, using the same retirement test as `_retired`, `dream.py:1338`), contradictions flagged.
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
