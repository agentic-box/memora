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
4. **Digest** (Sonnet, one call, reserved first): a short summary where every line cites memory ids
   (`[m123]`, or `[@c1]` for a merged memory the plan would create) and quotes a cited note.
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

## Schedule

`scripts/tidy/memora-tidy.service` and `.timer` run it weekly, dry run only; copy them to
`~/.config/systemd/user/`, edit the paths, and make sure a step you own puts a fresh store copy at the
`--db` path first. The unit must never gain an apply flag. Stop it with
`systemctl --user disable --now memora-tidy.timer`; nothing it does needs undoing.
