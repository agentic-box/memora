# The dream pass (Item 242)

`memora-dream` re-checks stored memories against their sources. memora dedups and
supersedes when it writes, but never re-checks a stored claim; a wrong note keeps
misleading later sessions. This pass finds such notes and records provenance.

## What it checks

For a bounded batch it extracts the claims a source can settle, **deterministically**
(the LLM never decides what a claim is):

| claim | source | evidence |
|---|---|---|
| commit SHA (7-40 hex) | git mirror | exists, date, subject, branches and tags containing it |
| version tag `vX.Y.Z[.W]` | git mirror | tag exists, the commit it points to |
| `Item N` | git mirror | commits whose subject or body mentions item N |
| repo file path | git mirror | file(s) matching the path at the repo's `ref` |
| message id (`mail #N`, `msg id N`) | `history_cmd` | what the command prints for that id |

An LLM then judges each claim as `confirmed`, `stale`, `wrong` or `unverifiable`,
using ONLY the evidence given, and must quote the evidence. It may propose a
corrected text when a claim is stale or wrong.

**Fail closed.** A claim is `confirmed` only if the judge answered, the answer is
valid JSON of the exact shape, the claim id and verdict are known, and the quoted
evidence occurs in the evidence we supplied. Anything else (no judge, an exception,
malformed output, an invented quote, a duplicate answer) is `unverifiable`. There
is no default model: `MEMORA_DREAM_MODEL` must be set, with `OPENAI_API_KEY` and
optionally `OPENAI_BASE_URL`; with none of them the run still reports, every claim
`unverifiable`, and exits 3. A memory that was not actually judged is not stamped,
so the next run retries it.

**Priority.** Volatile facts first (hosts, versions, flags, live state), then item
records and decisions, then frequently read memories (counts from the read ledger,
`read_ledger` in the config), then the longest-unchecked. A memory checked within
`recheck_days` is skipped; superseded memories are skipped.

## Dry run, then apply

```sh
memora-dream --out report.json            # dry run: reads, judges, writes only the report (0600)
memora-dream --db copy.sqlite --out r.json  # same against a read-only copy of a store
memora-dream --apply --plan report.json   # applies exactly that report; no LLM, no git
```

The report is the plan. `--apply` refuses a plan whose digest no longer matches
(edited), older than 48 h, and skips any memory whose content changed after it was
judged. Only the leader runs `--apply`, after reading the dry run.

`--apply` only **adds**, through memora's own storage functions (per-key
statements, the write gate and the D1 path unchanged):

- `verified` (all judged claims confirmed) or `unconfirmed` (anything else) metadata,
  with how, when, model, run id and claim tallies;
- a corrected memory plus a `supersedes` edge to the wrong one. An **item record is
  never superseded** (as in absorb): it gets the corrected memory with `related_to`;
- near-duplicates (word 4-gram Jaccard >= 0.9): a `supersedes` edge newer -> older
  (`related_to` when either is an item record);
- a dangling-link note (`dangling_links` metadata). The dead edge itself is **not**
  removed: that would be an edit in place.

It never deletes a memory, edits content in place or removes an edge. A provenance
update changes `updated_at` and re-embeds the memory like any metadata update.

## Hard limits

Part of the design, in `limits` of the config and tested:

| limit | default | effect |
|---|---|---|
| `max_memories` | 25 | memories judged per run |
| `max_llm_calls` | 25 | judge calls per run (one per memory) |
| `max_seconds` | 900 | wall time; the run stops between memories |
| `max_writes` | 20 | storage write calls per `--apply`; the rest is listed as `deferred` |
| `max_clone_mb` | 500 | a mirror above this is never fetched (checked before the fetch) |
| `recheck_days` | 14 | a checked memory is left alone this long |
| `nice` | 10 | plus `ionice -c 3` where available |

One run at a time: a non-blocking file lock in `state_dir`; a second run refuses.
The report says why a run stopped (`stopped_by`).

Git is plumbing only (`cat-file`, `rev-parse`, `log`, `ls-tree`, `for-each-ref`,
`fetch`, ...), against mirror clones; anything else raises. No build, no test and
nothing from a clone is ever executed. A claim that would need a build or a test
to confirm stays `unconfirmed`.

## Setup on the host (the user does this)

1. Make the dedicated mirror directory and seed it with **read-only** credentials
   (a deploy key or a read-only token, never the push key):
   `git clone --mirror <read-only-url> <git_dir>/<name>.git` for each repo.
   The pass only ever runs `fetch --prune origin` in them.
2. Write `~/.config/memora/dream.json` from `scripts/dream/dream.json.example`
   (git-ignored; host names, paths and repo names live only there) and
   `~/.config/memora/dream.env` with `MEMORA_DREAM_MODEL`, `OPENAI_API_KEY`,
   optionally `OPENAI_BASE_URL`, plus the usual store variables.
3. Try it by hand: `memora-dream --out /tmp/r.json`, read the report.
4. Only then install the schedule: copy `scripts/dream/memora-dream.{service,timer}`
   to `~/.config/systemd/user/`, adjust paths, `systemctl --user daemon-reload`,
   `systemctl --user enable --now memora-dream.timer`. The unit runs the **dry
   run** only and writes `dream-<UTC>.json` reports; it must never contain `--apply`.

Stop it: `systemctl --user disable --now memora-dream.timer`. Nothing it did needs
undoing (it only added); a wrong provenance stamp is superseded by a later run.

## Reading a report

`counts.claims` and `counts.memories` give verdict totals; `findings[].claims[]` has
each claim with `source` (the evidence we gave) and the judge's quote; `git` says
whether each mirror was fetched (a claim about a SHA absent from a stale mirror is
not proof). `stopped_by` names the cap that ended the run, `judge.status` is
`ok` or why the judge was unavailable.
