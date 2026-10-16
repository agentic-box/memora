"""Item 99: scripts/d1_permission_probe.py, the one-time paired experiment on a throwaway D1 database.

Offline: the D1 is a SQLite file; the "read" token is refused by the fake writer exactly where D1 would refuse it."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from memora import replicator as R
from memora.backends import D1DefiniteError

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import d1_permission_probe as P  # noqa: E402

DBID = "0f0f0f0f-1111-2222-3333-444444444444"
READ, WRITE = "read-token-VALUE-1", "write-token-VALUE-2"


class FakeD1:
    def __init__(self, path):
        self.path = str(path)
        self.sent = []          # (token, statement)

    def con(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def reader_factory(self, account, dbid, token):
        outer = self

        class Reader:
            def execute(self, sql, params=()):
                c = outer.con()
                try:
                    return [dict(r) for r in c.execute(sql, tuple(params or ()))], {}
                finally:
                    c.close()
        return Reader()

    def writer_factory(self, account, dbid, token, *, read_refused=True, changes_override=None):
        outer = self

        class Writer:
            def _post_json(self, body):
                outer.sent.append((token, body["sql"]))
                c = outer.con()
                c.execute(body["sql"], tuple(body.get("params") or ()))
                c.commit()
                c.close()

            def execute_batch(self, stmts):
                for sql, params in stmts:
                    R._check_statement(sql)
                    outer.sent.append((token, sql))
                if token == READ and read_refused:
                    raise D1DefiniteError('D1 API error (400): {"errors":[{"code":7500,"message":"You do not have permission"}]}')
                c = outer.con()
                out = []
                for sql, params in stmts:
                    cur = c.execute(sql, tuple(params))
                    out.append({"success": True, "meta": {"changes": cur.rowcount if changes_override is None else changes_override}})
                c.commit()
                c.close()
                return out
        return Writer()


def _tok(tmp_path, name, value):
    p = tmp_path / name
    p.write_text(value)
    p.chmod(0o600)
    return str(p)


def _args(tmp_path, read=READ, write=WRITE):
    return SimpleNamespace(account="a" * 32, database_id=DBID, read_token_file=_tok(tmp_path, "r.tok", read),
                           write_token_file=_tok(tmp_path, "w.tok", write))


def _run(tmp_path, fake, *, name="memora-probe-1", uuid=DBID, **wkw):
    return P.run(_args(tmp_path), info=lambda *a: {"uuid": uuid, "name": name},
                 reader_factory=fake.reader_factory,
                 writer_factory=lambda acct, db, tok: fake.writer_factory(acct, db, tok, **wkw))


@pytest.fixture()
def fake(tmp_path):
    f = FakeD1(tmp_path / "d1.sqlite")
    f.con().close()
    return f


def test_the_experiment_passes_when_read_is_refused_and_write_succeeds_without_a_change(tmp_path, fake):
    report = _run(tmp_path, fake)
    assert report["ok"] is True and report["data_unchanged"] is True
    assert report["read_token"]["refused"] and report["read_token"]["code"] == 7500
    assert report["write_token"] == {"succeeded": True, "changes": 0}
    assert report["statement"] == R.write_probe_statements()[0][0] and report["params"] == [None]
    rows = [r["id"] for r in fake.con().execute("SELECT id FROM memories ORDER BY id")]
    assert rows == [-1, 1, 2]                    # the seeded -1 row survives the probe
    assert READ not in json.dumps(report) and WRITE not in json.dumps(report)


def test_the_gate_and_the_experiment_use_one_statement(tmp_path, fake):
    _run(tmp_path, fake)
    probes = [s for _, s in fake.sent if s.startswith("DELETE")]
    assert probes == [R.write_probe_statements()[0][0]] * 2    # once per token


@pytest.mark.parametrize("name", ["memora", "memora-all", "prod-memora-probe-", "", "memora-probe"])
def test_a_database_not_named_memora_probe_is_refused_before_anything_is_sent(tmp_path, fake, name):
    with pytest.raises(P.ProbeRefused, match="nothing was sent"):
        _run(tmp_path, fake, name=name)
    assert fake.sent == []
    assert not fake.con().execute("SELECT name FROM sqlite_master").fetchall()


def test_a_different_uuid_is_refused(tmp_path, fake):
    with pytest.raises(P.ProbeRefused):
        _run(tmp_path, fake, uuid="another-id")
    assert fake.sent == []


def test_a_database_with_other_tables_is_refused_unchanged(tmp_path, fake):
    c = fake.con()
    c.execute("CREATE TABLE keep (x)")
    c.commit()
    with pytest.raises(P.ProbeRefused, match="only an empty throwaway"):
        _run(tmp_path, fake)
    assert fake.sent == []


def test_the_same_token_in_both_files_is_refused(tmp_path, fake):
    args = _args(tmp_path, read="same", write="same")
    with pytest.raises(P.ProbeRefused, match="two different tokens"):
        P.run(args, info=lambda *a: {"uuid": DBID, "name": "memora-probe-1"}, reader_factory=fake.reader_factory,
              writer_factory=fake.writer_factory)
    assert fake.sent == []


def test_a_read_token_that_is_not_refused_fails_the_experiment(tmp_path, fake):
    report = _run(tmp_path, fake, read_refused=False)
    assert report["read_token"] == {"refused": False} and report["ok"] is False


def test_a_write_that_changes_a_row_fails_the_experiment(tmp_path, fake):
    report = _run(tmp_path, fake, changes_override=1)
    assert report["write_token"]["changes"] == 1 and report["ok"] is False


def test_a_changed_checksum_fails_the_experiment(tmp_path, fake, monkeypatch):
    real = P._checksums
    calls = []

    def moving(reader):
        calls.append(1)
        out = real(reader)
        return out if len(calls) < 3 else {**out, "memories": "changed"}

    monkeypatch.setattr(P, "_checksums", moving)
    report = _run(tmp_path, fake)
    assert report["data_unchanged"] is False and report["ok"] is False


def test_main_exit_codes_and_no_token_in_output(tmp_path, fake, monkeypatch, capsys):
    argv = ["--account", "a" * 32, "--database-id", DBID, "--read-token-file", _tok(tmp_path, "r.tok", READ),
            "--write-token-file", _tok(tmp_path, "w.tok", WRITE)]
    monkeypatch.setattr("memora.add_store.fetch_d1_info", lambda *a, **k: {"uuid": DBID, "name": "production"})
    assert P.main(argv) == 2
    out = capsys.readouterr().out
    assert "refused" in out and READ not in out and WRITE not in out


def test_the_image_carries_the_probe():
    assert "COPY scripts/d1_permission_probe.py scripts/d1_permission_probe.py" in (REPO / "Dockerfile").read_text()
