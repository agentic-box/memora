"""Runs scripts/local_primary.py in a subprocess with D1 replaced by a
FakeReplica file (L5_TEST_FAKE_D1). Test-only: the operator tool itself has
no such switch. The reader is still the REAL D1SelectOnlyConnection; only
its HTTP post is redirected, and any write attempt fails loudly."""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from memora import backends  # noqa: E402
from tests.l3_fakes import FakeReplica  # noqa: E402


def main() -> int:
    replica = FakeReplica.__new__(FakeReplica)  # an existing file: do not re-run the schema
    replica.path = Path(os.environ["L5_TEST_FAKE_D1"])
    replica.reads = []
    backends.D1SelectOnlyConnection._post = lambda self, body: replica.reader_post(body)

    mode = os.environ.get("L5_TEST_D1_WRITES", "")

    from memora.sql_classify import READ, classify_statement

    def run(sql, params):
        db = replica._db()
        try:
            cur = db.execute(sql, tuple(params or ()))
            rows = [dict(r) for r in cur.fetchall()] if cur.description else []
            db.commit()
            return {"success": True, "result": [{"results": rows, "success": True,
                                                  "meta": {"changes": max(cur.rowcount, 0),
                                                           "served_by_primary": True}}]}
        finally:
            db.close()

    def d1_send(self, sql, params=None):
        # A D1Connection read (the read-only integrity audit, L6) is served
        # from the FakeReplica. A write only when a test asks: "apply" runs
        # it, "reject" fails like D1 would; otherwise it is a test failure.
        if classify_statement(sql).kind == READ:
            return run(sql, params)
        if mode == "apply":
            db = replica._db()
            try:
                db.execute(sql, tuple(params or ()))
                db.commit()
            finally:
                db.close()
            return {"success": True, "meta": {"changes": 1}}
        if mode == "reject":
            raise backends.D1DefiniteError("D1 API error (400): not authorized")
        raise AssertionError("this L5 path must never use a D1 writer")

    backends.D1Connection._send = d1_send
    sys.argv[0] = str(REPO / "scripts" / "local_primary.py")
    import importlib.util

    spec = importlib.util.spec_from_file_location("local_primary_cli", sys.argv[0])
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    return cli.main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
