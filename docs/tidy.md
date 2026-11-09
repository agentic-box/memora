# The tidy pass: running it (Item 252)

`memora-tidy` proposes a better-organised store for **one project**: fewer overlapping memories,
outdated ones retired, consistent tags and links, and one current `kind: digest` summary. The design,
the safety rules and the reasons are in [tidy-design.md](tidy-design.md). This page is how to run what
exists: **the dry run**, and the **apply** of the metadata-only steps (see "Apply" below). The dry run reads
a copy of the store and writes a report; only `--apply` writes the store.

```sh
memora-tidy --db store-copy.sqlite --project clmux --out report.json
```

- `--db` is a read-only **copy** of the store (made with SQLite's online backup, or by copying the file
  while the server is stopped). The command never opens the live store.
- `--project` (or `project` in the config) names the one project to tidy.
- `--out FILE` / `--out-dir DIR` write the report (0600) as `tidy-<project>-<UTC>.json`.

Exit codes: 0 done, 2 error (bad config, no copy, lock held), 3 judge unavailable (the report is still
written, with no proposals), 4 wall-time watchdog.

## Config

`~/.config/memora/tidy.json` (git-ignored; start from `scripts/tidy/tidy.json.example`):

| key | meaning |
|---|---|
| `models` | `{"group": "haiku", "write": "sonnet"}`: required, no default. Both run through Claude Code (`claude -p`) on the subscription, exactly like `MEMORA_DREAM_JUDGE=claude-code` ([dream.md](dream.md)); `MEMORA_DREAM_CLAUDE_BIN` names the binary. |
| `limits` | caps: `max_clusters` 12, `max_llm_calls` 24, `max_merges` 10, `max_retires` 10, `max_links` 20, `max_retags` 20, `max_writes` 40, `max_seconds` 1500, `nice` 10, and the grouping and digest bounds in `TidyLimits`. |
| `state_dir` | where the run lock lives. Default `~/.local/state/memora-dream`, the same as dream, so the two never run at once. Give them different directories and they do. |
| `read_ledger` | optional read-count ledger, used to rank the digest input. |
| `projects` | backfill candidates, in addition to every project the store already uses (`--backfill`). |
| `mirrors` | backfill: dream's `{"git_dir": ..., "repos": {name: {"dir", "ref", "projects"}}}`; the file lists of those bare mirrors are a path signal (read-only, no fetch). |
| `repo_paths` | backfill: `{"clmux": ["Sources/"]}`, path prefixes that identify a project's tree. |
| `limits.digest_min_coverage` | share (0 to 1, default 0.5) of the decision-bearing candidates (item records, decisions) a digest must place, at most `digest_max_lines`; fewer makes the report flag it thin. |
| `limits.digest_recent_share` | share (0 to 1, default 0.33) of the digest input reserved for the newest memories. Also `newer_per_note` 3, `newer_note_chars` 600, `newer_min_words` 2. |
| `limits.max_backfill` | `set_project` steps per backfill run (200); `backfill_batch` 8 ambiguous memories per Haiku call. |

## What a run does

1. **Group** (no model): the live memories of the project become an evidence graph. Edges: cosine of the
   stored embeddings (recomputed; a stored `related_to` score is never evidence), shared references
   (items, files, shas, tags), typed links, shared project-prefixed tags, dream's near-duplicate scan.
   Clusters of 2 to 12 memories.
2. **Judge each cluster** (Haiku, one call): every member gets exactly one primary disposition (keep,
   contradiction, merge source, retire) plus optional links and retags. One deviation from the schema
   rejects the cluster.
3. **Write merges** (Sonnet, one call per cluster that has merges). The code checks each merge statement
   by statement ([tidy-design.md](tidy-design.md) 3.1). A merge that is not word for word is **held**
   for explicit approval; one that cannot be justified is **rejected**.
   A cluster the validator rejects gets **one repair call** carrying the validator's error (it counts
   against `max_llm_calls`); a second failure still rejects the cluster.
4. **Digest** (Sonnet, one call, then a Haiku line check; both reserved first): every decision,
   follow-up and constraint that is still current, one line each (at most `digest_max_lines`, 40), not a
   short summary. Every line cites memory ids (`[m123]`, or `[@c1]` for a merged memory the plan would
   create) and quotes a cited note. An item record or decision must appear in a current line or an
   `Overtaken` line. The input reserves `digest_recent_share` of its bounds for the **newest**
   memories, then fills by rank and read count (`digest_input.recent_ids` lists what the reserve took).
   Beside each candidate the writer is shown the **newer** memories on its topic and must not state the
   decision as current if one of them contradicts or completes it: it writes an `Overtaken` line citing
   both ids.
   Then **one** extra Haiku call (`digest_check`) reads all lines against the notes they cite; a line
   whose text the notes do not support (for example one that reverses its quote) is dropped and listed
   in `rejected`. The check fails open: if it cannot run, the lines stay and `digest_fidelity.status`
   says `unchecked:<why>`. Both calls count against `max_llm_calls` and are reserved before cluster work.
5. **Seal**: the report carries a `digest` over everything else (`tidy.plan_digest`); editing it is
   detectable (`tidy.load_tidy_plan`).

## Reading the report

`counts` (live, clusters, steps, total write `charge`, duplicate pairs, contradictions), `judge.calls`,
`stopped_by`, `deferred_clusters` and `deferred` (what a cap pushed to next week), `clusters[]` (members,
the evidence for each pair, `proposals_rejected` with reasons), `steps[]` (the sealed proposals: `merge`,
`retire`, `link`, `retag`, and one digest step), `contradictions[]` (flagged for a leader, never
resolved), `review[]` (memories whose `references_checked` is missing, stale or partial, and unusual
digest states), `sources` (the statement inventory of every judged memory), `digest_input` (what the
digest did and did not look at).

A step has `status: eligible` (every statement kept or an exact duplicate) or `held` (it contains a
reworded or dropped statement, or a metadata conflict, and needs the leader's explicit approval). Item
records, decisions, procedures and digests are never merged or retired.

## Is the digest thin or misstated?

- `digest_coverage`: `decision_notes` (item records and decisions that were in the digest input),
  `cited`, `overtaken`, `left_out`, `expected_placed`, `thin`, `reason`, `left_out_ids`, and `notes[]`
  (each id and where it went). A thin digest also gets a `kind: thin_digest` row in `review[]`: read
  the left-out notes before trusting it, or rerun.
- `digest_fidelity`: `status` (`ok`, or `unchecked:<why>`), `checked` lines and `dropped` lines. Dropped
  lines are in `rejected[]` with `op: digest_line` and a reason starting `check:`.

## Overtaken decisions and the leader

`digest_flags[]` lists every digest candidate that had newer memories beside it and where the writer put
it (`decisions`, `followups`, `constraints`, `overtaken`, or `left_out`): read the ones that stayed
current. A candidate left out gets a `kind: potential_overtaken` row. `review[]` gets a `kind: overtaken` row (older ids, newer ids) for each `Overtaken` line. It is a
**retire or contradiction candidate for you to decide**; tidy never retires it.

## Project backfill

`memora-tidy --config ... --db <store copy> --backfill --out-dir ...` (no `--project`). For each live
memory with no project it proposes a `set_project` step: `{target, project, evidence[], confidence,
source}`. `source: signals` means the evidence was decisive without a model (a project-prefixed tag 3, an
item record's `metadata.repo` 5, a path that exists in exactly one project's mirror tree, that only one
project mentions, or that a `repo_paths` prefix covers 2 each; the top score is at least 3 and double the runner-up) and `confidence` is `high`. Ambiguous cases
go to Haiku in batches; it may only pick a project that had a signal, must quote the note, and its
confidence is `medium` or `low`; "unknown" lands in `unresolved`, as do memories with no signal at all
(no call is spent on them). A project is never overwritten. `lists` separates protected memories and item
records from the rest (it is metadata only, but you may want to read them first); `by_project` counts the
proposals. See "Apply" for how they reach the store.

## Apply (metadata-only steps)

`memora-tidy --apply --plan report.json --decisions decisions.json [--config ...]` writes the sealed report's
**`set_project`, `link` and `retag`** steps (tags are only ever added) and the three **digest steps**
(`bootstrap_digest`, `replace_digest`, `repair_digest`, see "The project digest" below). The metadata steps never change
a memory's text, never hide one, never delete, compute no embedding and call no model. **Not supported yet:** `merge`,
`retire` (and any unknown op): they are listed as `skipped: op not supported by this apply` and are never written,
even when approved.

Decisions file (`report_digest` must be the report's `digest`; the leader never edits the report):

```json
{"report_digest": "<digest of the report>",
 "approve_ops": [{"op": "set_project", "min_confidence": "medium"}, {"op": "link"}, {"op": "retag"}],
 "approve": ["b12"], "reject": ["b40", "s3"]}
```

`approve_ops` approves every step of the op at or above the confidence; `approve` adds steps by id; `reject`
always wins; a step the report marks `needs_approval` is approved by id only; anything not approved is not
applied. **Digest steps are always approved by id** (`"approve": ["s23"]`), whatever the report says: the dry run
marks them `needs_approval: false`, but the apply ignores that for a step that creates a memory, and a decisions
file whose `approve_ops` names a digest op is refused. A file with another report's digest, an id that is not in the report, or a malformed shape is refused.
`memora-tidy --apply --plan ... --decisions ... --check` prints what would be attempted, by op and reason, and
touches no store (no lock, no config).

Each step is checked against the store when it runs (state sha, still no project, protection read now, not
retired, an existing edge is "done", a different edge is never replaced). Protected memories (item records,
decisions, procedures, digests) receive links and added tags, not a project label. Caps (config `limits`):
`max_backfill` for `set_project` (set it to the size of the report, the default is 200; a rerun continues where
it stopped), `max_writes` for links (2 each) and tags (1 each). The result is written next to the plan as
`<plan>.apply-<UTC time>.json` with `applied`, `skipped`, `deferred`, `ambiguous` (ids and reasons) and counts.
Rerunning the same plan and decisions applies nothing new.

**The project digest (Item 259).** A digest step is the only step that creates a memory: a new `kind: digest`
memory (`project`, `digest_of: tidy`, `run`, `covers`, tag `<project>/digest`) holding the sealed body, written by the
store's own `add_memory` (it also gets `indexing.skip_crossrefs`, so it receives no automatic `related_to` edges).
**Its embedding is computed like any create, so this is the one apply that needs the embedding environment**
(`MEMORA_EMBEDDING_*` or `OPENAI_*`, as for any write; keep it in a 0600 file); `MEMORA_LLM_ENABLED=false` still holds and
no absorb, dedup, suggestion, consolidation or model call runs. The outbox gets `I`/`U` rows, never `D`.

- `bootstrap_digest` runs only when the project has **no live digest** (kind digest, this project, not hidden).
- `replace_digest` runs only when the live digests are exactly `{P}` and `P` is still the judged row (state sha); it
  creates `D`, then writes `D supersedes P` and the reverse `P superseded_by D`, each one statement that also requires
  `P` to be unchanged and unhidden. `P` is kept as it was and stays reachable.
- `repair_digest` creates nothing: when exactly two tidy-made digests are live, both still the judged rows, and the
  winner is the later one, it writes the two edges only.
- Every other live-digest state is "stale" and writes nothing. Each write is ONE statement that carries the
  operation's expected state: the create is `add_memory(insert_guard=...)` (an atomic `INSERT ... SELECT ... WHERE`
  requiring the sealed live-digest set, so a digest that appears while the embedding is computed makes it write
  nothing); the forward edge requires both rows as read, no superseder, and no live digest outside the pair; the reverse
  edge requires the state after our own forward edge (the winner is the loser's only superseder, the winner unchanged
  and unhidden, only the winner live).
- The edges are written against the very snapshot that was validated (a repair: the sealed expectation of both rows;
  a replacement: this step's own sealed digest, and the loser's judged sha), never against a fresh re-read that was
  not validated; the reverse phase validates a new snapshot. The create statement also requires every citation to be a
  live memory of the project (one retirement definition shared by all statements), and the completion of a partial
  create is guarded by the exact row it validated (`expected_row`) and requires the citations to hold now.
- A create that fails after the row was inserted (vector or index) is not a digest yet: nothing is hidden, and a rerun
  completes it through the store's reindex before anything else. A digest is recognised as this step's only when its
  sealed metadata, tags and body match. Several matching live digests are "ambiguous", not done.
- A resume (after a crash, a forward-only write) repeats every check: the citations, the loser's judged state, for
  a repair both judged rows, both tidy-made and the winner later.
- A digest step whose `depends_on` names a step that did not commit (including an unsupported `merge`) is **deferred**
  as `deferred: dependency sN`. A digest citing a symbolic id (`@c3`, only a merge can make one) is refused. Every
  numbered citation must be a live memory of the project right now; otherwise the step is refused. The body is never rewritten.
- Charges (against `max_writes`, before dispatch): bootstrap 1, replace 3, repair 2.
- **Reruns create nothing.** The digest of the run is recognised in the store (same run and body, live), so a rerun is
  "already done"; after a crash between the create and the edges, the rerun writes the missing edges only. `created`
  step ids are also kept in the replay journal.

**The replay journal.** Next to the plan the apply keeps `<plan>.applied.json` (0600, bound to the report's
digest; another report's journal is ignored). A step that rewrites a memory changes the state sha that later steps
of the same plan were judged against, so before each such write the apply records, durably, the sha the sealed
mutation will produce (write-ahead, fsynced). A rerun, after a cap, a deadline, an ambiguous call or a crash at any
point, accepts for that memory exactly the judged sha or a recorded predicted sha and nothing else: a foreign edit
still refuses ("state sha mismatch"), and a step already finished is "already done". If the journal cannot be
written, nothing is written. The journal is trusted state, not a secret: keep it with the plan; deleting it only
makes later steps on already-changed memories refuse (a new dry run then re-plans). **The directory of the plan must
be writable** (the result and the journal go there); with a read-only plan mount, copy the plan to a work directory
first.

Procedure (design 5.5; the leader runs it, the unit never does):

1. Rehearse on a **new, writable, consistent copy** of the store (an online backup, or a copy made with the
   container stopped; never reuse an old copy's `-wal`/`-shm`). Point the store environment at the copy
   (embedding settings in a 0600 file, never printed), copy the plan and decisions into a work directory, run `--apply`, and diff `sync_outbox` before and after:
   only `U` rows, **no `D` rows**; the memories' text unchanged; a second run applies nothing.
2. Live: stop the watchdog, then the memora container; run `--apply` in a one-off container (same image, the
   data volume, a writable work directory that holds the plan, the decisions file and, afterwards, the result and the
   `.applied.json` journal (keep them together for reruns), `MEMORA_LLM_ENABLED=false`); start the container, then
   the watchdog; check that D1's `last_acked_seq` equals `max(sync_outbox.seq)` with no halt and no error.

The run scripts and the systemd unit never contain `--apply` (tested).

## Measuring (Item 258)

`memora-tidy-eval` answers "do real queries find the right memory?" with a number you can repeat before and
after each apply. It never writes: it opens a store **copy** read-only, runs no model, and calls the search
the MCP tools use (`retrieval.search_v2`, `follow="active"`, top 5) without the read ledger. Only the query
embedding is computed, with the store's embedding environment; if that is unavailable it stops (exit 2)
rather than report keyword-only results.

```sh
memora-tidy-eval --db before-copy.sqlite --out before.json     # take the copy, run, then apply the tidy
memora-tidy-eval --db after-copy.sqlite  --out after.json
memora-tidy-eval --compare before.json after.json
```

The queries file is private and stays out of git: `~/.config/memora/tidy-queries.json`, or `--queries PATH`.
Format (see `scripts/tidy/queries.json.example`): `{"queries": [{"q": text, "project": name|null,
"expect": [memory ids], "digest": true?}]}`.

- Each query runs unscoped, and scoped by `project` when it has one (the project filter of the plain JSON
  API; the MCP tool has no such argument).
- An expected id that was superseded counts as hit by its live successor; a retired one with no successor
  can only miss.
- Report: per query hit or miss, the rank of the first expected memory and the returned ids; totals
  recall@5 and MRR, unscoped and scoped. `digest: true` also checks that the project's live `kind: digest`
  memory is rank 1 of the scoped search (`n/a` when there is no digest yet).
- Same copy and queries give the same report (`created` aside). Ties keep the search's own order.

## Schedule

`scripts/tidy/memora-tidy.service` and `.timer` run it weekly, dry run only; copy them to
`~/.config/systemd/user/`, edit the paths, and make sure a step you own puts a fresh store copy at the
`--db` path first. The unit must never gain an apply flag. Stop it with
`systemctl --user disable --now memora-tidy.timer`; nothing it does needs undoing.
