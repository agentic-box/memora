# The dream pass (Item 242)

> **Scope, plainly.** This checks **references**: that the commits, tags, files and item numbers a
> note mentions exist or resolve. It does **not** verify behaviour or any assertion the note makes,
> and it never writes a "verified" stamp. The metadata it writes is `references_checked`.

`memora-dream` re-checks the references in stored memories against their sources. memora dedups and
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

An LLM then judges each REFERENCE as `confirmed` (it exists / resolves as the note uses it), `stale`,
`wrong` or `unverifiable`, using ONLY the evidence given, and must quote the evidence. It sees the
whole note with its tags, metadata and a deterministic modality hint (`note`, `open_task`,
`proposal`). A reference may be `wrong` only when the note asserts that the thing exists or landed;
a note that proposes it, plans it, lists it as an open task or recalls it as history never makes
absence an error (and for `open_task` / `proposal` notes the code refuses `wrong` / `stale` itself).
It may propose a corrected text when a reference is stale or wrong.

**Fail closed.** The judge's answer must be exactly `{"claims": [...], "correction": null|text}`
with one `{"id","verdict","evidence"}` row per asked claim: no missing, unknown, repeated or
non-string id, no extra key, no non-string verdict. ONE deviation makes every claim of that memory
`unverifiable` and the attempt is not stamped. Inside a well-formed answer a claim is `confirmed`
(or `stale` / `wrong`) only if its quote occurs in the evidence we supplied for that claim. No
judge, an exception, malformed output or an invented quote is `unverifiable`. The memory-level summary is `all_found` (every reference found),
`partial`, `missing`, `stale` or `unknown`. A found file proves the file exists, nothing about what
the note says of it.

**Unknown is not "not found".** A git command that fails or times out gives no evidence. "Not found"
(a SHA, tag, item or file) counts as evidence only when that mirror was fetched in THIS run; from an
unfetched or failed-fetch mirror it is unknown and the claim stays `unverifiable`. For a path claim
the judge sees the file's bytes (first `max_file_chars`) at a recorded commit, and where each
`backticked` symbol of the note occurs in it (or `NOT PRESENT`). Message claims use `history_cmd`
and need no clone. There
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

The report is the plan. `--apply` refuses a plan whose digest (over the whole report) no
longer matches, or older than 48 h. At each write it re-reads the memory and skips it unless
content, tags and non-provenance metadata are still what was judged and it is not retired;
whether it is an item record is read **then**, never taken from the plan; metadata updates carry
`expected_row`, a precondition on the UPDATE statement itself. The edge that hides a memory is one statement that also
requires the memory to be exactly the judged row (content, metadata, tags, updated_at, crossrefs blob, no tombstone,
no existing supersedes edge), so a writer that got in between leaves nothing hidden. Retirement is any of: a
tombstone, `superseded_by` to a live memory, or a forward-only `supersedes` edge held by a live memory. Only the leader runs `--apply`,
after reading the dry run.

`--apply` only **adds**, through memora's own storage functions (per-key
statements, the write gate and the D1 path unchanged):

- `references_checked` metadata: when, how, model, run id, `summary`, and counts of references
  `found`, `missing`, `stale` and `unknown`;
- a corrected memory plus a `supersedes` edge to the wrong one. An **item record is
  never superseded** (as in absorb): it gets the corrected memory with `related_to`. A partial
  failure (the copy was added, then the memory changed) writes no edge and says so;
- near-duplicates (word 4-gram Jaccard >= 0.9, same project, same numbers): a `related_to`
  edge only. Lexical overlap is a lead, not proof; hiding the older memory stays a leader
  decision;
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
| `max_seconds` | 900 | ONE deadline for the whole run: every git, history and judge call gets at most what is left (no SDK retries), the scans are skipped once it passes, and a dry run is killed (exit 4) 30 s after it. `--apply` runs every store call on a worker bounded by what is left: if the deadline passes inside a call the apply stops waiting, nothing further is dispatched (the edge after a corrected copy is never started), the call is listed under `ambiguous` (it may still commit), `aborted` is set and the exit code is 4 |
| `max_writes` | 20 | store writes per `--apply`, charged BEFORE dispatch: a metadata stamp 1, a correction its worst case 3 (copy + two edge statements; unused charge is not refunded); the rest is `deferred` |
| `max_clone_mb` | 500 | a mirror above this is never fetched (checked before the fetch) |
| `recheck_days` | 14 | a checked memory is left alone this long |
| `nice` | 10 | plus `ionice -c 3` where available |

One run at a time: a non-blocking file lock in `state_dir`; a second run refuses.
The report says why a run stopped (`stopped_by`).

A mirror's `dir` must be relative, contain no `..` and no symlink, resolve inside `git_dir` and be a
bare repository (`HEAD` + `objects/`, no `.git`, `git rev-parse --is-bare-repository` true); otherwise
it is refused and git never runs there. Fetch runs with submodule recursion, auto-gc and maintenance off.

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
