# The tidy pass (Item 251): design

> **Status: phase 2 (dry run) is implemented** in `memora/tidy.py` (Item 252, v0.5.7.10; fixes in sections 13 and 14, v0.5.7.11 and v0.5.7.12).
> **Phase 4 is partly implemented** (`memora/tidy_apply.py`): `--apply` writes the metadata-only steps `set_project`,
> `link` and add-only `retag` (Item 255, section 15) and the three digest steps `bootstrap_digest`, `replace_digest`,
> `repair_digest` (Item 259, section 16). Merges and retires are still not applied. Names marked *(new)* were new at
> design time; line numbers are those of v0.5.7.9. Where the implementation differs from this design,
> section 12 says how and why.
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
   "statements": [{"src": [12, 0], "outcome": "kept", "out": 0},
                  {"src": [41, 0], "outcome": "duplicate", "of": [12, 1]},
                  {"src": [40, 2], "outcome": "reworded", "out": 3, "why": "..."},
                  {"src": [41, 1], "outcome": "dropped", "why": "quoted_history"}],
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
2. **Coverage is by COMPLETE statements.** The model must assign every inventoried statement of
   every source exactly one outcome:
   - `kept`: the statement appears in the merged text **in full**, byte-identical after only
     whitespace folding, as a whole statement of the merged text (the merged text is split by the same
     splitter; the match is statement-to-statement). The step lists `{src: [id, i], out: j}`. A quote
     of part of a statement certifies nothing: a statement is covered only by a full-statement match.
   - `duplicate`: the statement is **exactly equal** (same folding as above, **full-statement
     equality**, no containment, no prefix/suffix) to a statement of another source that is itself
     `kept`. Negation and conditions are therefore preserved by construction: "Use X." and
     "Do not use X." are different statements; "Allow 30 days" and "Do not allow 30 days" likewise.
   - `reworded` (anything else: a statement changed, shortened, combined, or only partly present),
     or `dropped` (not expressed at all, e.g. `quoted_history`). Both are **held for explicit
     per-statement leader approval**, listed with the source statement and the merged statement
     (or none) side by side.
   An unassigned statement, or a merged statement that is `kept` against no source statement and is
   not listed as `reworded`, **rejects the merge** (nothing is hidden; invented content has no source).
3. **Rejected vs held, one rule.** The merge is **rejected** (mechanical, never offered for approval)
   when: a statement is unassigned; a `kept`/`duplicate` claim is not a full-statement match; the
   merged text has a statement with no source and not listed as `reworded`; sources disagree on
   project/section/hierarchy; the cluster has a `contradiction`; any protected member. The merge is
   **held for approval** when it is mechanically well-formed but contains any `reworded` or
   `dropped` statement. It is **eligible without per-statement approval** only when every statement is
   `kept` or `duplicate`. Even then it is a step the leader must approve (5.4).
4. **Metadata policy.** `project`, `section`, `hierarchy` must be equal across sources (else reject).
   `kind` and `type` must be equal or absent (else reject). `resolution`, `status` and `provenance`
   style scalars that differ are not unioned: the merged memory takes none of them and records
   `metadata.tidy.conflicts = {field: {id: value}}`, which makes the step `needs_approval`. Lists
   (tags) are unioned; `references_checked` of the sources is not copied (the merged text is new).
5. **Heuristic guards stay on** (numbers, SHAs, tags, 60% size guard, no prompt envelope) and reject,
   but a pass proves nothing; the audit trail is the statement table.

Fail-closed outcomes: any failure above drops the merge into `proposals_rejected` with the reason;
the cluster keeps its `link`/`retag` proposals and nothing is hidden.

**Worked validation table (a).** Source statements are written `A1`, `B1`, ...; merged statements `M1`...

| case | sources | merged text | inventory outcome | result |
|---|---|---|---|---|
| exact duplicate | A: "Uploads require admin approval." (A1) "Logs are kept 30 days." (A2) / B: "Logs are kept 30 days." (B1) | "Uploads require admin approval. Logs are kept 30 days." | A1 kept=M1, A2 kept=M2, B1 duplicate of A2 (full equality) | well-formed, all kept/duplicate: eligible; leader approves step; applied: 1 create + 2 edges per source = 1+2*2 = 5 |
| Use X / Do not use X | A: "Use X." / B: "Do not use X." | "Do not use X." | A1: not equal to any statement of another source (B1 is "Do not use X."), not kept in M; so A1 is `dropped` | the cluster is already a `contradiction` (Haiku, with quotes); a contradiction freezes every member: merge **rejected**, nothing hidden. Even if the contradiction call were missed, A1 `dropped` would only be held for approval, never auto-eligible; containment is not a duplicate |
| partial-quote constraint | A: "Uploads require admin approval." / B: "Logs are kept 30 days." | "Uploads require approval. Logs are kept 30 days." | A1: no full-statement match ("Uploads require approval." differs) => A1 `reworded` into M1; B1 kept=M2 | **held** for approval, shown side by side ("admin" removed). It is not rejected because it is well-formed; it is never applied without the leader approving `s:A1->M1` |
| approved rewording | same as above, leader approves A1->M1 | same | A1 reworded (approved), B1 kept | applied: 5 charges; the decision file lists `approve_statements: ["s3:A1"]` |
| unmapped | A: two statements, model maps one | any | A2 unassigned | **rejected**: `unmapped_statement:A#2` |
| invented | any | adds "Retries are capped at 3." with no source statement | M3 has no source, not listed `reworded` | **rejected**: `statement_without_source` |

### 3.2 Retire

A retire hides `id` behind the newer, existing, live memory `by`. It requires **demonstrated
redundancy or obsolescence**, never topical overlap or a shared reference:

- `why: redundant`: every statement of `id` is a `duplicate` (full-statement equality, 3.1(2)) of a statement of `by` (quotes required, verified by code); or
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
  still needs section 3.2: every statement a `duplicate` of the newer memory (`redundant`), or a quoted
  replacement plus the leader's approval (`obsolete`). A shared reference is never enough.
- A memory with `all_found` is not "verified"; it only means references resolve.

## 5. Safety (the rules, enforced in code, each with a test)

1. **Never delete.** There is no delete path in `memora/tidy.py`. Hiding a memory is only a
   `supersedes` edge. History stays reachable (`follow` reads, `memory_get` on the old id); D1 never
   sees a `D` outbox row from tidy.
2. **Never merged or retired automatically:** `item-record` (`Mem.protected`, `dream.py:258`),
   `kind: decision` or a `decision` tag (dream's set, `dream.py:1016`), `kind: procedure`. They may
   be **linked** and **retagged only by adding**; they may sit in a cluster as context. Protection is
   read **at apply time**, never from the plan (`dream.py:1527`); a protected memory in a merge/retire
   step makes that step fail closed. **`kind: digest` has one narrow exception**: the three digest
   steps of section 6.4 (`bootstrap_digest`, `replace_digest`, `repair_digest`), each with its own
   state guard over the project's live digests; they cannot hide anything but a digest of the same
   project, and a digest is never merged or retired by a cluster step.
3. **Caps per run** (`limits`, tested): `max_clusters` 12, `max_llm_calls` 24, `max_merges` 10,
   `max_retires` 10, `max_links` 20, `max_retags` 20, `max_writes` 40, `max_seconds` 1500, `nice` 10.
   **Write charges, before dispatch** (like dream: charged, not refunded, remainder `deferred`):
   a merge of N sources costs **1 + 2N** (the new memory, then per source the forward `supersedes`
   edge held by the new memory and the reverse edge on the source, each a separately committed
   guarded call, as `_guarded_edge`, `dream.py:1374`, does); a retire 2; a link 2 (one per side);
   a retag 1; a digest bootstrap 1, replacement 3 (create + forward + reverse), repair 2 (both edges, no create); an *ambiguous* call (deadline hit
   mid-call) keeps its charge and is listed, never retried in the same run. A merge of more than 8
   sources is split. `max_writes` is therefore 40, not dream's 20, and the plan is pruned to fit
   before it is sealed, not at apply time.
4. **The plan is sealed; approvals are separate.** `plan_digest` (`dream.py:1297`) binds the whole
   report, including every body. The leader never edits the report. Acceptance goes in a separate
   `decisions.json` *(new)*: `{"report_digest": "<digest>", "approve": ["s3","s7"], "reject": ["s5"],
   "approve_statements": ["s3:A1"]}`. `--apply --plan report.json --decisions decisions.json` refuses a
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
      0600 file, never printed; diff `sync_outbox` before and after: only op `U` rows as m1383 saw, **no `D` rows**;
   2. stop the watchdog service first, then the memora container;
   3. run `memora-tidy --apply` in a one-off container (same image, the container's embedding/container env taken from `docker inspect` into a temp file
      deleted afterwards, the data volume, the secrets dir read-only, a writable work dir holding the plan (result and journal are written there, Item 255),
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
  full body, tags, metadata and statement table;
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

### 6.4 Digest operations: `replace_digest`, `repair_digest`, bootstrap

Digests are the only protected kind with a write path, through three dedicated steps, never through
merge or retire. All three run the same checks inside the edge statement (`_guarded_edge`,
`dream.py:1374`): the memory being hidden is exactly its judged row (`state_sha`), `kind: digest`,
same `project`, no existing superseder. Ordinary item/decision/procedure protection is unchanged.

State is the set L of **live digests of the project** (digest-kind memories not retired).

- **`bootstrap_digest`** (|L| = 0): create the digest; no edge. Guard: re-read L now; if |L| != 0
  the step stops (nothing written). Charge 1.
- **`replace_digest`** (|L| = 1, L = {P}, P is the sealed `expect`): create new digest D, then the
  forward edge D supersedes P, then the reverse edge on P. Guard before create: re-read L; if L is
  not exactly {P} with P's `state_sha` unchanged, stop as `ambiguous`/`stale` and write nothing.
  Charge 3.
- **`repair_digest`** (|L| = 2, L = {P, D}): produced by a dry run that sees two live digests, and
  sealed with **both** ids and both `state_sha`. The winner is the one with the later `created_at`
  whose metadata has `digest_of: "tidy"` and the same `project` (a digest from another source or
  project is never a winner or a loser: the step is not produced and the case goes to `review[]`).
  Guard: both ids are `kind: digest`, same project, still exactly their judged rows, L is exactly
  {P, D} now. It **creates nothing** and writes only the forward edge (winner supersedes loser)
  and the reverse edge on the loser. Charge 2. Its dependencies are none (it cites no planned
  create); if the winner's own citations are stale it is still the right repair, and the next
  `replace_digest` refreshes the text.
- A state with |L| >= 3, or two live digests where one is not tidy-made, is not repaired
  automatically: it is a `review[]` entry for the leader.

**State-transition table (b).** IDs: P=801 (the previous digest), D=905 (the new one, symbolic
`@d1` in the plan). Charges are the section 5.3 charges.

| # | state before | step and call outcome | state after | next run / replay |
|---|---|---|---|---|
| 1 | L={} | `bootstrap_digest`: create 905 ok (charge 1) | L={905} | replay of the same plan: re-read L={905}, expect L={} fails: **no-op**, nothing written |
| 2 | L={801} | `replace_digest`: create 905 ok, forward edge ok, reverse edge ok (charge 3) | L={905} (801 hidden) | replay: L={905} != expect {801}: stopped before any write, reported `already_applied` |
| 3 | L={801} | create 905 ok, forward edge **fails** (guard refused or error) (charge 3 spent) | L={801, 905} | next dry run sees |L|=2, both tidy-made, 905 later: seals `repair_digest(801, 905)` |
| 4 | L={801, 905} | `repair_digest`: forward 905 supersedes 801 ok, reverse ok (charge 2) | L={905} | replay: L={905} != {801, 905}: stopped, no write |
| 5 | L={801} | create 905 ok, forward ok, **reverse fails** | 801 is retired by the forward edge (retirement is any forward `supersedes` edge held by a live memory, `dream.py:1338`) | L={905}; the missing reverse edge is reported `partial` and needs no step: retirement is forward-only, so 801 is hidden and exactly one digest is live |
| 6 | L={801} | the deadline passes inside the create call | unknown: 905 may exist | the step is `ambiguous`, charge kept, nothing retried in this run; next dry run reads L: if {801, 905} -> row 3; if {801} -> new `replace_digest` |
| 7 | L={801, 777, 905} (|L|=3) | no automatic step | unchanged | `review[]` entry for the leader |
| 8 | L={801} but a cited cluster step s3 was skipped | `replace_digest` depends_on s3: **refused**, `deferred: dependency s3` | L={801} | nothing written; the next run regenerates |

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
            "statements": [], "charge": 7, "needs_approval": true,
            "expect": {"12": "<state_sha>", "40": "<state_sha>", "41": "<state_sha>"}, "depends_on": []},
           {"id": "s9", "op": "replace_digest", "create": "@d1", "body": "... [@c3] ...", "supersedes": 801,
            "expect": {"801": "<state_sha>"}, "depends_on": ["s3"], "charge": 3}],
 "digest_input": {"considered": 0, "included": 0, "excluded": 0},
 "review": [{"id": 5, "references_checked": "missing", "missing": ["..."]}],
 "contradictions": [{"ids": [3,40], "quotes": []}]}
```

`steps` is the only thing `--apply` reads and it contains every sealed payload (bodies, tags,
metadata, statement tables, dependencies, charges, guards); nothing executable lives outside the sealed
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
- **Retrieval:** a fixed query set (about 20 real queries for the project, recorded once in a private
  file outside git, `~/.config/memora/tidy-queries.json`; Item 258) is run before and after with
  `memora-tidy-eval` on store copies (docs/tidy.md "Measuring"): top-5 hit contains an expected
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
| 5 | One lock per project or one global? | One global, and the **same lock file path** as dream (`run_lock`, `dream.py:167`), so `state_dir` must be shared between the two jobs; otherwise cross-job exclusion does not exist. |
| 6 | Cosine threshold 0.80 and cluster size 12: right? | Start there, make them config; tune from the first report's `evidence` data. |
| 7 | Does the digest need a staleness mark when cited memories later change? | Yes, cheaply: the next run regenerates it; between runs `covers` + `updated_at` lets a reader see drift. Not in phase 2. |
| 8 | Where does the digest stay for projects with no `metadata.project`? | Skipped: tidy refuses a null project. |
| 9 | Is a rejected merge retried next week? | Yes, but a cluster whose merge was rejected twice is listed under `review[]` and not re-sent to the judge. |

## 12. Implementation notes (Item 252): where it differs, and why

- **Models are config values** (`models.group`, `models.write` in `tidy.json`), not the
  `MEMORA_TIDY_MODEL_*` variables of section 1. No default, no fallback; the judge is `ClaudeCodeJudge`
  with the tidy system prompt (`TidyJudge`). `MEMORA_DREAM_JUDGE` is not read: tidy has no OpenAI path.
- **Retire is mechanical, so only merges call Sonnet** (section 3 said a retire also goes to Sonnet).
  `redundant` is proved by code (full-statement equality); `obsolete` carries the judge's statement
  indices (`replaces`), is always `needs_approval`, and needs no second call. A cluster costs one Haiku
  call plus one Sonnet call only when it has merge intents.
- **`--db` (a store copy) and a project are required**; tidy never opens the live store and has no
  `--apply`. The lock is dream's `run_lock` on `<state_dir>/dream.lock`, shared only while both use the
  same `state_dir` (the default of both is `~/.local/state/memora-dream`).
- **Statement identity includes its context.** A header or a parent bullet above a statement is part of
  its `ctx`, and `kept`/`duplicate` compare `(ctx, text)`, so a condition or scope cannot be dropped
  silently; merging flat notes from different sections therefore yields held rewordings. Unsupported
  forms (code fences, tables, html, indented blocks, control characters, notes over the dream note cap)
  make a memory context-only: kept, linked, retagged by adding, never merged or retired.
- **Links and retags that touch a memory hidden by the same plan are dropped** with a reason instead of
  rejecting the cluster.
- **`max_writes` is 40** and is checked while the plan is built (the digest step's charge is reserved
  first), so a sealed plan never exceeds it. Cluster work may use `max_llm_calls - 1` calls; the digest
  call is the reserved one and is skipped only when the live digests call for a repair or a review.
- **`repair_digest` winner** is the later `created_at` (id as tie-break) of two tidy-made digests.
- **Apply-time hooks exist but nothing calls them yet:** `load_tidy_plan`, `revalidate_step` (every
  `expect`, citations, dependencies, symbols), `check_edge_phase` (separate expected state for the
  forward and the reverse edge) and `substitute_symbols` (token-aware: `@c3` never touches `@c30`).
- **Not built in this phase:** the acceptance-rate file (section 9; the retrieval check is `memora-tidy-eval`, Item 258), the
  `decisions.json` consumer, and the snapshot step before the unit (the operator makes the store copy).

## 13. Item 254: fixes from the first real run

- **Digest input.** A reserved share of the char and count bounds goes to the newest memories (by
  `updated_at`) before the rank fill (`digest_recent_share`, default 0.33, recorded in `digest_input`).
  The first real run ranked by read count and missed Items 240 to 249.
- **Overtaken decisions.** Each candidate is shown the newer live memories that share a non-generic
  reference or at least two rare words with it. The digest answer gains an optional `overtaken` section;
  a line there must cite an older note and a newer note offered for it. `digest_flags` records where
  every candidate with newer memories ended up, and `review[]` gets a retire-or-contradiction candidate
  per overtaken line. Nothing is retired by tidy. This is a prompt rule plus a flag, not a proof: a
  decision the writer keeps as current is listed for the leader, not blocked.
- **Repair call.** A rejected cluster gets one more Haiku call that carries the validator's error;
  the second answer is validated the same way and the cluster is rejected if it fails.
- **Project backfill** (`--backfill`): see `docs/tidy.md`. Deterministic signals first, Haiku only for
  ambiguous ones. Report only; `set_project` is validated (`validate_set_project`) and revalidated at
  apply time (`has_project`). Path signals, strongest first: the path exists in exactly one project's
  **dream mirror tree** (`mirrors` in `tidy.json`, dream's `git_dir`/`repos` shape; read-only `ls-tree`
  through `GitSource`, never a fetch; an absent, refused, failed or multi-project mirror contributes
  nothing and is named in the report's `mirrors`), then a path only one project's labelled memories
  mention, then an optional `repo_paths` prefix. A path in several trees or a bare file name is no signal.
- **Caps.** Backfill has its own cap (`max_backfill`, one write each, apart from `max_writes` 40, which
  bounds a tidy plan); it is applied before any model call. A digest candidate that was left out
  although newer memories exist becomes a `potential_overtaken` review row (not proven).

## 14. Item 256: digest coverage and line fidelity

The first real run wrote 9 lines against a cap of 40 from 57 notes, and one line (Item 246) said the
opposite of its quote. Two causes, two fixes.

- **Coverage.** The prompt no longer asks for a "short summary". It asks for every decision, follow-up
  and constraint that is still current, requires an item record or decision to appear in a current line
  or an `overtaken` line, and forbids dropping a decision silently. `digest_coverage` in the report lists
  the included item records and decisions and where each went (`cited`, `overtaken`, `left_out`). The
  digest is **thin** when fewer are placed than `min(ceil(digest_min_coverage * N), digest_max_lines)`,
  default `digest_min_coverage` 0.5. Why 0.5: some left-out notes are legitimate (a duplicate, a
  routine record, a decision a merge absorbed), so the bar is "about half"; why the line cap: a digest
  cannot place more notes than it has lines. A thin digest still seals; it adds a `thin_digest` row to
  `review[]` for the leader, because the right response (rerun, read the left-out notes) is a human call.
- **Line fidelity.** The existing checks prove the quote is verbatim and that no number, path or
  identifier is invented. They cannot see a `text` that contradicts the quote it carries. One extra
  Haiku call (`digest_check`) reads every line against the notes it cites and returns a strict
  per-line verdict; unsupported lines are dropped and recorded in `rejected` (`op: digest_line`, reason
  `check: ...`). One call for all lines, not one per line, and it is reserved with the digest call
  (cluster work may use `max_llm_calls - 2`). A deterministic check alone was rejected: wording that
  reverses a quote shares all its tokens with it. The check **fails open**: with no judge, no call or
  time budget left, a failed call or an unusable answer (wrong shape, missing or repeated verdicts),
  the lines stay and `digest_fidelity.status` reads `unchecked:<why>`. The report is a dry run read by a
  leader; losing the whole digest to a flaky check would cost more than flagging it. Coverage is
  computed after the drop.

## 15. Item 255: the apply step, metadata-only (sections 5.4 to 5.8 for these ops)

- **Scope.** `memora-tidy --apply --plan REPORT --decisions DECISIONS` (code in `memora/tidy_apply.py`; `tidy.py`
  stays write-free and only dispatches the flag). Supported: `set_project`, `link`, `retag` (add only). Any other op is
  listed `skipped: op not supported by this apply` and never written, approved or not.
- **Checks before any write.** `load_tidy_plan` (version, mode, age, digest); a decisions file whose
  `report_digest` differs, that names an id not in the report, or that is malformed is refused. No model, git or
  network in apply; it runs under dream's `run_lock` and writes the configured store (never `--db`).
- **Approvals.** `approve` and `reject` by step id, plus `approve_ops` (leader addition): `{"op": "set_project",
  "min_confidence": "medium"}` approves every step of that op at or above the confidence; `{"op": "link"}` all of them.
  A reject always wins. A `needs_approval` step is approved by id only. Steps without a confidence (link, retag)
  never meet a `min_confidence` floor. Anything not approved is not applied.
- **Guards, against the store at apply time.** The step's `expect` state sha (a memory this run already changed
  still matches, through the sha this run recorded after its own write); `set_project` only on a memory that still
  has no project (one that already has the planned project is "already done"); protected memories (`tidy.is_protected`,
  read now: item records, decisions, procedures, digests) get links and added tags only, so a `set_project` on one is
  skipped; retired memories are skipped; a link never replaces an existing edge other than the automatic `related_to`
  (a `supersedes`/`superseded_by` edge is left alone); a link whose edge already exists on both sides is "already done".
- **Writes.** `update_memory(..., embed=False)` for set_project and retag: metadata/tags only, the FTS row is refreshed,
  **no embedding is computed and the stored vector is not touched** (it still reflects the old metadata until the next
  reindex), no content change, no model call. `add_link` for links. The D1 outbox therefore receives `U` rows only.
  A typed system tag of a memory that gets a project is re-prefixed by the store's normal rule; it is reported as
  `tags_retargeted`.
- **Caps.** Charged before dispatch: `set_project` 1 against `max_backfill`; `link` 2 and `retag` 1 against
  `max_writes`. The remainder is `deferred` and a rerun picks it up. Deadline (`max_seconds`) as in dream: an
  in-flight call is `ambiguous`, nothing further is dispatched.
- **Replay.** `<plan>.applied.json` holds, per memory and judged sha, the shas this apply's own mutations produce, written
  ahead of each write (docs/tidy.md); the plan directory must be writable.
- **Result.** `<plan>.apply-<UTC time>.json` next to the plan (0600): `applied`, `skipped`, `deferred`, `ambiguous`
  with step ids and reasons, plus counts. Rerunning the same plan and decisions applies nothing: finished steps are
  skipped as "already done".
- **Not here.** Merge, retire, the digest steps, `approve_statements`, and the success measures of section 9.

## 16. Item 259: the apply step, the project digest (sections 6.2 to 6.4)

- **Ops.** `bootstrap_digest`, `replace_digest`, `repair_digest`, each with the state guard of 6.4 re-read over the
  project's live digests at apply time (live = kind digest, this project, not retired by any source the store honours).
  Bootstrap: none live. Replace: exactly `{P}`, `P` still its judged row. Repair: exactly the two sealed ids, both still
  judged, both tidy-made, the winner later. Any other state writes nothing and is "stale". Charges 1, 3, 2.
- **First create in apply.** The digest goes through `storage.add_memory` (embedding computed like any create; this is
  the one apply that needs the embedding environment). No absorb, dedup, suggestion or model call; the similarity
  crossref pass is switched off for the row (`indexing.skip_crossrefs`), so no automatic `related_to` edge appears. The
  outbox gets `I`/`U` rows, never `D`. Supersession is the two guarded edges of 6.4, `dream._guarded_edge` for both; the old
  digest is never deleted or edited.
- **Approval.** The dry run marks digest steps `needs_approval: false` (section 6.4's repair had said so too). The
  apply approves a digest step **by id only**, regardless of the flag, and refuses a decisions file whose `approve_ops`
  names a digest op.
- **Dependencies and citations (6.2, 6.3).** `depends_on` not committed (a `merge` is unsupported, so never) gives
  `deferred: dependency sN`. A symbolic `@c` citation, in `cites` or the body, is refused (only merges make them).
  Numbered citations must be live memories of the project at apply time, else the step is refused; the body is never rewritten.
- **Idempotence.** The live digest of the same run and body is recognised in the store: bootstrap and replace reruns are
  "already done"; a rerun after a crash between the create and the edges writes the missing edges and creates nothing
  (6.4 table rows 3 and 5 are resumed directly instead of waiting for a `repair_digest`). The created id is also kept in the replay journal.
- **Round 1 (review).** Every digest write is one statement carrying the exact expected state of its phase (create:
  `add_memory(insert_guard=)`, an atomic conditional insert; forward edge: both rows, no superseder, live set within
  the pair; reverse edge: the loser's only superseder is the winner, winner unchanged and unhidden). A partial create (row
  inserted, vector or index missing) is completed by the rerun before anything is hidden, and "ours" requires the sealed
  metadata, tags and body. Resumes revalidate citations and all judged rows.
- **Round 2 (review).** The snapshot that is validated is the snapshot the statement guards authorise (no refresh
  between check and guard); citations are part of the conditional insert; completing a partial create requires valid
  citations and is guarded by `expected_row`; one SQL retirement predicate (tombstone, `supersedes` edge, `superseded_by`
  naming a live memory) serves every statement.
- **Round 3 (review).** The completion of a partial create carries the same operation state as the insert: the reindex
  computes its embedding before its UPDATE, so `storage.update_memory(update_guard=)` appends citations, the live-digest
  set (and for a replacement the expected old digest) to the exact-row UPDATE; the forward edge of a replacement also
  requires the citations.
- **Derived metadata.** The store canonicalises metadata on every update (`section`/`subsection` become
  `hierarchy.path`). `set_project` and `retag` now compute the expected post-write row with the store's own
  canonicaliser, so a correct write is no longer reported as an `anomaly` (the live Item 255 run reported 17). The
  journal's parent directory is fsynced after the rename.
