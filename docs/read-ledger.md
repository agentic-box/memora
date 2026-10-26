# Read ledger (Item 118)

Measures whether memora reads change decisions. Two local pieces: read
telemetry on the server (off by default) and a decision ledger kept by the
leader. `scripts/memora-ledger report` joins them. Nothing here touches D1, the
data volume or the database.

## Decision rule

Run for about 4 weeks. The verdict comes after **40 or more leader decisions**:
keep memora when there are **at least 4 independently verified wins**
(a decision labelled `changed` or `avoided-repeat` that the reviewer confirms
from the audit list) and the benefit outweighs the cost; scale back when there
are fewer.

## Read telemetry (server)

Off unless `MEMORA_READ_LEDGER=<path>` is set. Each call of
`memory_hybrid_search`, `memory_semantic_search`, `memory_list`, `memory_get`
and `memory_related` appends one JSON line:

| field | meaning |
|---|---|
| `ts` | UTC, millisecond |
| `caller` | the request's `X-Memora-Caller` header, when present |
| `store`, `project` | the store the call resolved to; `project` only when `metadata_filters.project` was given |
| `tool` | the tool name |
| `query_hash`, `query` | sha256 prefix of the whole query, and the query text cut to 200 characters |
| `requested_id` | `memory_id` for `memory_get` and `memory_related` |
| `memories` | `[{id, v}]` of the returned memories; `v` is `updated_at` when the response carries it |
| `count`, `latency_ms` | result count; the read's own duration |
| `error` | the error code, when the call returned or raised one |

Never logged: memory content, tags, metadata, tokens, credentials or any other
header. The query text IS logged (200 characters), so do not put a secret in a
search query.

The line is built after the read and handed to one writer thread through a
bounded queue (1000 lines), so a slow disk never delays a read or the event
loop. A line over half the cap is dropped (warned once). Queued lines are best effort: the writer is a daemon thread and is not flushed at shutdown, so a restart or redeploy can lose the last few. A full queue drops the new line (one warning, then silence); any write
error is swallowed the same way. The ids are taken before `fields=` or content
projection, so they survive both. A response of more than 200 memories logs the
first 200 ids and `"truncated": <true count>`; the report treats such a decision
as ambiguous. The total size is capped by `MEMORA_READ_LEDGER_MAX_BYTES`
(default 20 MiB, never below 64 KiB) as bytes of the live file and `<path>.1`
together: each is held to half, with ONE rotation to `<path>.1` (the older file
is overwritten by the next rotation; files left over from a larger cap are
discarded).

Turn it on for memora-all: set `DEPLOY_READ_LEDGER_DIR` in
`instances/deploy.env` (or the environment of `scripts/deploy-memora-all.sh`) to
a directory on the deploy host (it lives only in the git-ignored `deploy.env`; the
script creates it 0700 when missing). The deploy bind-mounts it at `/ledger` and sets
`MEMORA_READ_LEDGER=/ledger/read-ledger.jsonl`, so the file survives a redeploy
and is not on the data volume. Turn it off by removing the key and deploying
again (the file stays where it is). Outside memora-all, set the variable in the
server's environment.

## Decision ledger (leader)

`scripts/memora-ledger` writes a local JSONL file, `$MEMORA_DECISION_LEDGER` or
`.ledger/decisions.jsonl` (git-ignored). Decision ids are free text
(`item-118-brief`, `item-117-esc-1`).

```
memora-ledger draft <id> "<one-sentence provisional decision>" [--store S] [--caller C]   # BEFORE the search
memora-ledger label <id> unchanged|changed|avoided-repeat|misleading [--memory N ...] [--time-saved "<what>"] "<change or skipped work>"
memora-ledger audit <id> confirm|reject "<reason>" [--by <name>]            # the reviewer's verdict
memora-ledger audit <id> relabel unchanged|changed|avoided-repeat|misleading "<reason>" [--by <name>]
memora-ledger missed <id> --memory N "<what was missed>"            # a relevant memory that was not found
memora-ledger read-back --memory N [--created ISO] "<why it was read>"
```

`label` and `missed` refuse a decision with no draft; a draft is written once;
the last `label` of a decision wins. `--memory` is the memory ids the label
cites. `--time-saved` notes that memora pointed to something the code held and
saved navigation; it is counted separately from changed decisions and does not
change the label (it belongs to the decision's last `label` row).
`--created` is the memory's creation time, for the read-back timing.

### Audit

`audit` appends the reviewer's verdict on a decision that already has a draft
and a label, and records who decided (`--by`). `confirm` and `reject` take only
a reason; `relabel` takes the new label first. Every audit is stamped with the
**revision** of the leader's claim it judged (each `label` row is a revision, even
with the same category), and only audits of the latest revision count: a later
`label` leaves them in the file but stale, so a revised claim is never counted
as verified without a new audit (the report lists it as a **stale win**).

The **effective label** is the last current `relabel` audit, else the leader's
label; a later `confirm` or `reject` keeps it. A decision is a **verified win**
when its effective label is `changed` or `avoided-repeat` and its last current
audit is a `relabel` to it or a `confirm` of it. A win with no audit is
**unaudited**; a `reject` is counted apart (the label count is kept).
The reviewer works from the report's audit list (below) and writes these rows.

## Report

```
memora-ledger report [--since 7d] [--telemetry PATH | --no-telemetry]
```

**Fetch the telemetry.** The server's file is root-only (0600) inside the
container, so copy it with `docker exec`, then point the report at it:

```
mkdir -p .ledger
docker exec <container> cat /ledger/read-ledger.jsonl > .ledger/read-ledger.jsonl
docker exec <container> cat /ledger/read-ledger.jsonl.1 > .ledger/read-ledger.jsonl.1   # if it exists
memora-ledger report --telemetry .ledger/read-ledger.jsonl
```

`--telemetry` defaults to `$MEMORA_READ_LEDGER`, then `.ledger/read-ledger.jsonl`.
If neither that file nor its `.1` exists, `report` exits non-zero and names the
path it looked for. `--no-telemetry` prints a labels-only report: the search,
latency and cited-vs-returned sections read "not measured". If the file has no
line inside the decisions' overall time span, `report` prints one warning (a
stale copy). The report reads local files only and
prints counts: decisions by label; searches per decision and latency (p50, p95,
max); cited vs returned memory ids; read-backs and hours from creation to first
read; misleading and missed counts; verified, unaudited, stale and rejected wins, the
time-saved count and progress against the decision rule (N of 40 decisions, M
of 4 verified wins; a ledger with no audit rows reports zeros); and the audit list for the reviewer (every
`changed`, `avoided-repeat` and `misleading` id by effective label, with any audit shown, plus a random 20% of all ids).

A decision's reads are the telemetry calls between its `draft` and its `label`
time, narrowed to `draft --store S` and `--caller C` when given. A decision is
**ambiguous**, and its cited ids are not counted, when its reads cannot be
attributed: it overlaps another decision, labeled or still open (unless both name different `--caller`s), the window
holds reads from several stores (no `--store`) or callers (no `--caller`), or
a read was truncated. The report lists each ambiguous decision with the reason.
The first read of a memory is taken over all time, so a read-back before the
window is marked "(before the window)" instead of being replaced by a later one.
