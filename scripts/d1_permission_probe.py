#!/usr/bin/env python3
"""Item 99: does D1 refuse the replicator's write probe for a READ token and accept it for a WRITE token?

The deploy's write check (memora/replicator.py, write_probe_statements) sends one allow-listed statement
that matches no row. Whether D1 refuses that zero-row statement for a D1:Read token is the whole point of the
check and is documented nowhere, so this script can measure it once, on a THROWAWAY database. STATUS: available, not yet run;
the deploy gate relies on the refusal as assumed (Cloudflare docs + the 2026-10-17 production 7500), not measured.

  d1_permission_probe.py --account A --database-id ID --read-token-file R --write-token-file W

Safety, in this order, before any change:
  * both token files go through load_credential_file (0600, regular, not a symlink) and must hold different tokens;
  * the database's name (looked up by id, read token) must start with `memora-probe-`: any other database,
    production included, is refused with nothing sent to it;
  * the database must hold no table except the scratch table `memories` this script creates (so it can
    never be a memora store).
Then: create the scratch table with exactly the columns the probe statement names (id INTEGER PRIMARY KEY
AUTOINCREMENT, content), seed it with three rows including an explicit id of -1, checksum every user table
(read token; sqlite_* and _cf_* internals excluded), send THE gate statement with the read token (expect D1's refusal; its code and message are
recorded), send it with the write token (expect success and zero changed rows), checksum again.
Prints one JSON report (no token values); exit 0 only if the read token was refused, the write token
succeeded, and the checksums around the two probes are equal. Only D1's definite permission refusal
(code 7500) counts as a refusal: a timeout, an HTTP 5xx or any other D1 error is reported as inconclusive and fails.
A zero changed-rows count is reported as measured only when D1's response carries it (changes_measured). Setup changes (table, seed) are reported
separately and are outside that comparison.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memora import local_primary as lp  # noqa: E402
from memora import replicator as R  # noqa: E402
from memora.backends import D1DefiniteError  # noqa: E402

NAME_PREFIX = "memora-probe-"
SCRATCH = "memories"
SEED = [(-1, "scratch row with the id -1"), (1, "scratch row 1"), (2, "scratch row 2")]


PERMISSION_CODE = 7500          # D1's "You do not have permission" (seen when a read-only token wrote)
_PHASE = ["start"]              # the step in progress, named in a failure report


class ProbeRefused(Exception):
    pass


def _checksums(reader) -> Dict[str, str]:
    rows, _ = reader.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                             "AND name NOT LIKE '_cf_%' ORDER BY name", ())
    out = {}
    for r in rows:
        name = r["name"]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ProbeRefused(f"unexpected table name {name!r}")
        data, _ = reader.execute(f'SELECT * FROM "{name}" ORDER BY 1', ())
        out[name] = hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()
    return out


def _code(text: str) -> Any:
    m = re.search(r'"code"\s*:\s*(\d+)', text) or re.search(r"\bcode[ :=]+(\d+)", text)
    return int(m.group(1)) if m else None


def run(args, *, info=None, reader_factory=None, writer_factory=None) -> Dict[str, Any]:
    from memora import add_store as ads
    from memora.backends import D1SelectOnlyConnection

    read_token = lp.load_credential_file(args.read_token_file)
    write_token = lp.load_credential_file(args.write_token_file)
    if read_token == write_token:
        raise ProbeRefused("the read and write token files hold the same token: the experiment needs two different tokens")
    info = info or ads.fetch_d1_info
    reader_factory = reader_factory or D1SelectOnlyConnection
    writer_factory = writer_factory or R.ReplicaD1Connection
    _PHASE[0] = "database lookup"
    ident = info(args.account, args.database_id, read_token)
    name = str(ident.get("name") or "")
    if ident.get("uuid") != args.database_id or not name.startswith(NAME_PREFIX):
        raise ProbeRefused(f"database {args.database_id} is named {name!r} (uuid {ident.get('uuid')!r}): only a database "
                           f"named {NAME_PREFIX}* is probed; nothing was sent to it")
    _PHASE[0] = "checksums before setup"
    reader = reader_factory(args.account, args.database_id, read_token)
    tables = _checksums(reader)
    if set(tables) - {SCRATCH}:
        raise ProbeRefused(f"the database holds tables {sorted(set(tables) - {SCRATCH})}: only an empty throwaway "
                           "database is probed; nothing was changed")
    writer = writer_factory(args.account, args.database_id, write_token)
    _PHASE[0] = "setup"
    setup = []
    for sql, params in (
        (f"CREATE TABLE IF NOT EXISTS {SCRATCH} (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT)", []),
        *[(f"INSERT OR IGNORE INTO {SCRATCH} (id, content) VALUES (?, ?)", [i, c]) for i, c in SEED],
    ):
        writer._post_json({"sql": sql, "params": params})   # raw transport: setup is outside the replicator's allow-list
        setup.append(sql.split(" (")[0])
    _PHASE[0] = "checksums before probes"
    before = _checksums(reader)
    stmts = R.write_probe_statements()
    report: Dict[str, Any] = {"database": name, "statement": stmts[0][0], "params": list(stmts[0][1]),
                              "setup": setup, "tables_before": before}
    _PHASE[0] = "read-token probe"
    try:                                                        # the READ token: D1 must DEFINITELY refuse it (7500)
        writer_factory(args.account, args.database_id, read_token).execute_batch(stmts)
        report["read_token"] = {"refused": False, "outcome": "executed"}
    except D1DefiniteError as exc:
        text = str(exc)
        code = _code(text)
        report["read_token"] = {"refused": code == PERMISSION_CODE, "code": code, "message": text[:400],
                                "outcome": "refused" if code == PERMISSION_CODE else "other D1 error (inconclusive)"}
    except Exception as exc:    # a timeout, an HTTP 5xx, an unreadable answer: says nothing about the permission
        report["read_token"] = {"refused": False, "outcome": "inconclusive (transport or server error)",
                                "error": type(exc).__name__, "message": str(exc)[:400]}
    _PHASE[0] = "write-token probe"
    results = writer.execute_batch(stmts)                       # the WRITE token: must succeed, change nothing
    ok_result = len(results) == 1 and isinstance(results[0], dict) and results[0].get("success") is True
    changes = (results[0].get("meta") or {}).get("changes") if ok_result else None
    report["write_token"] = {"succeeded": ok_result, "changes": changes, "changes_measured": changes is not None}
    _PHASE[0] = "after checksums"
    after = _checksums(reader)
    report["tables_after"] = after
    report["data_unchanged"] = before == after
    report["ok"] = bool(report["read_token"]["refused"] and ok_result and changes in (0, None)
                        and report["data_unchanged"])
    return report


def main(argv: List[str] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--account", required=True)
    ap.add_argument("--database-id", required=True)
    ap.add_argument("--read-token-file", required=True)
    ap.add_argument("--write-token-file", required=True)
    args = ap.parse_args(argv)
    try:
        report = run(args)
    except (ProbeRefused, lp.L5Refused) as exc:
        print(json.dumps({"ok": False, "refused": str(exc)}))
        return 2
    except Exception as exc:    # an operational failure: a JSON report with the phase, never a traceback
        print(json.dumps({"ok": False, "failed_phase": _PHASE[0], "error": type(exc).__name__,
                          "message": str(exc)[:400]}))
        return 3
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
