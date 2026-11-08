# The tidy pass: running it (Item 252)

`memora-tidy` proposes a better-organised store for **one project**: fewer overlapping memories,
outdated ones retired, consistent tags and links, and one current `kind: digest` summary. The design,
the safety rules and the reasons are in [tidy-design.md](tidy-design.md). This page is how to run what
exists: **the dry run**. There is no apply step yet and no `--apply` flag; the command reads a copy of
the store and writes a report.

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
proposals. The apply is a later phase; `validate_set_project` and `revalidate_step` are its hooks.

## Schedule

`scripts/tidy/memora-tidy.service` and `.timer` run it weekly, dry run only; copy them to
`~/.config/systemd/user/`, edit the paths, and make sure a step you own puts a fresh store copy at the
`--db` path first. The unit must never gain an apply flag. Stop it with
`systemctl --user disable --now memora-tidy.timer`; nothing it does needs undoing.
