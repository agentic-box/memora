"""Offline D1 replica for the replicator tests (L3).

FakeReplica is a SQLite file with memora's D1 schema (ensure_schema through
FakeD1Connection: the same tables and D1-side epoch/embedding triggers, no
sync objects). The replicator's writer and reader are the REAL classes
(ReplicaD1Connection, D1SelectOnlyConnection) with their HTTP post patched
to run against that file, so batch fallback, the P2 check and the
SELECT-only check are exercised. Statements apply one by one with
autocommit, like D1's REST batch (no atomicity), and failures can be
injected: before anything, after applying k statements, after applying all
(response lost), or a 400 for the batch body.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from memora import schema
from memora.backends import D1DefiniteError, D1SelectOnlyConnection, LocalSQLiteBackend
from memora.replicator import ReplicaD1Connection

from tests.conftest import FakeD1Connection

URI = "d1://acct/replica-db"


class FakeReplica:
    def __init__(self, path: Path):
        self.path = Path(path)
        d1 = FakeD1Connection(self.path)
        schema.ensure_schema(d1)
        d1.close()
        self.statements: list = []   # every statement the writer sent
        self.reads: list = []        # every statement the reader sent
        self.fail_before = None
        self.apply_then_raise = None   # (k, exc): apply k statements, then raise exc
        self.reject_batch_400 = False
        self.result_override = None

    def _db(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def _run(self, db, sql, params):
        cur = db.execute(sql, tuple(params or ()))
        rows = [dict(r) for r in cur.fetchall()] if cur.description else []
        db.commit()
        return {"results": rows, "success": True, "meta": {"served_by_primary": True}}

    def post_json(self, body):
        if self.fail_before is not None:
            raise self.fail_before
        if "batch" in body and self.reject_batch_400:
            raise D1DefiniteError("D1 API error (400): batch body not supported")
        stmts = body["batch"] if "batch" in body else [body]
        db = self._db()
        out = []
        try:
            for i, st in enumerate(stmts):
                if self.apply_then_raise is not None and i == self.apply_then_raise[0]:
                    raise self.apply_then_raise[1]
                self.statements.append(st["sql"])
                out.append(self._run(db, st["sql"], st.get("params")))
            if self.apply_then_raise is not None and self.apply_then_raise[0] >= len(stmts):
                raise self.apply_then_raise[1]
        finally:
            db.close()
        if self.result_override is not None:
            out = self.result_override(out)
        return {"success": True, "result": out}

    def reader_post(self, body: bytes):
        req = json.loads(body)
        self.reads.append(req["sql"])
        db = self._db()
        try:
            res = self._run(db, req["sql"], req.get("params"))
        finally:
            db.close()
        return 200, None, json.dumps({"success": True, "result": [res]}).encode()

    def writer(self, uri=URI):
        w = ReplicaD1Connection("acct", "replica-db", "replicator-token")
        w._post_json = self.post_json
        return w

    def reader(self, uri=URI):
        r = D1SelectOnlyConnection("acct", "replica-db", "read-token")
        r._post = self.reader_post
        return r

    def rows(self, table):
        db = self._db()
        try:
            pk = ", ".join(schema.SYNC_TABLES[table])
            return [dict(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY {pk}")]
        finally:
            db.close()

    def epoch(self):
        db = self._db()
        try:
            return int(db.execute(
                "SELECT value FROM memories_meta WHERE key = 'embedding_change_epoch'").fetchone()[0])
        finally:
            db.close()


def local_store(path: Path, epoch: int = 0) -> LocalSQLiteBackend:
    backend = LocalSQLiteBackend(path)
    conn = backend.connect()
    schema.ensure_schema(conn)
    schema.install_sync(conn, URI, epoch)
    conn.close()
    return backend


def local_rows(backend, table):
    conn = backend.connect()
    try:
        pk = ", ".join(schema.SYNC_TABLES[table])
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY {pk}")]
    finally:
        conn.close()
    if table == "memories_meta":
        rows = [r for r in rows if r["key"] not in schema.SYNC_META_EXCLUDED]
    return rows


def sync_state(backend):
    conn = backend.connect()
    try:
        return dict(conn.execute("SELECT * FROM sync_state").fetchone())
    finally:
        conn.close()
