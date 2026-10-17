"""Item 99: a replicator whose token cannot write is caught, not just reported "running".

Offline. The replicator's start-up write probe (a no-op DELETE by a primary key
no row has), the optional token-file keys of instances/deploy.env, and
bootstrap-d1's refusal of a replicator token that is the read token."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from memora import replicator as R
from memora.backends import D1DefiniteError
from tests.l3_fakes import URI, FakeReplica, local_store

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import deploy_config  # noqa: E402

READ_ONLY = D1DefiniteError('D1 API error (400): {"errors":[{"code":7500,"message":"You do not have permission"}]}')


def _wait(pred, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


@pytest.fixture()
def env(tmp_path):
    replica = FakeReplica(tmp_path / "replica.db")
    local = local_store(tmp_path / "local.db", epoch=replica.epoch())
    return local, replica


def _started(local, replica, **kw):
    rep = R.StoreReplicator("s1", local, URI, mode=kw.pop("mode", "write"), writer_factory=replica.writer,
                            reader_factory=replica.reader, broadcast=lambda: None, poll_s=0.05, **kw)
    rep.start()
    return rep


class TestWriteProbe:
    def test_a_write_capable_token_proves_it_and_writes_no_row(self, env):
        local, replica = env
        before = {t: replica.rows(t) for t in R.SYNC_TABLES}
        bodies = []
        post = replica.post_json
        replica.post_json = lambda body: (bodies.append(body), post(body))[1]
        rep = _started(local, replica)
        try:
            assert _wait(lambda: rep.status().get("write_probe") == "ok"), rep.status()
        finally:
            rep.stop()
        probes = [s for s in replica.statements if s.startswith("DELETE")]
        assert probes == ["DELETE FROM memories WHERE id = ?"]
        assert bodies[0] == {"batch": [{"sql": "DELETE FROM memories WHERE id = ?", "params": [None]}]}  # id = NULL matches no row
        assert {t: replica.rows(t) for t in R.SYNC_TABLES} == before

    def test_a_token_d1_refuses_to_write_never_reports_ok_and_names_the_error(self, env):
        local, replica = env
        replica.fail_before = READ_ONLY
        rep = _started(local, replica)
        try:
            assert _wait(lambda: "7500" in (rep.status().get("last_error") or "")), rep.status()
            st = rep.status()
            assert st["write_probe"] == "refused" and st["status"] == "backoff", st
            time.sleep(0.3)
            assert rep.status()["write_probe"] == "refused"
        finally:
            rep.stop()

    def test_the_probe_is_retried_and_a_later_success_is_ok(self, env):
        local, replica = env
        replica.fail_before = READ_ONLY
        rep = _started(local, replica)
        try:
            assert _wait(lambda: rep.status().get("last_error"))
            replica.fail_before = None
            assert _wait(lambda: rep.status().get("write_probe") == "ok", timeout=10), rep.status()
        finally:
            rep.stop()

    def test_the_probe_runs_once_per_process(self, env):
        local, replica = env
        rep = _started(local, replica)
        try:
            assert _wait(lambda: rep.status().get("write_probe") == "ok")
            time.sleep(0.4)
        finally:
            rep.stop()
        assert len([s for s in replica.statements if s.startswith("DELETE")]) == 1

    def test_log_mode_has_no_writer_and_no_probe(self, env):
        local, replica = env

        def no_writer(uri):
            raise AssertionError("log mode must not build a D1 writer")

        rep = R.StoreReplicator("s1", local, URI, mode="log", writer_factory=no_writer,
                                broadcast=lambda: None, poll_s=0.05)
        rep.start()
        try:
            time.sleep(0.4)
            assert rep.status().get("write_probe") is None and rep.status().get("last_error") is None
        finally:
            rep.stop()

    def test_the_probe_statement_passes_the_allow_list_and_can_match_no_row(self):
        (sql, params), = R._build_statements("memories", [None], None, [])
        assert R._check_statement(sql) == "delete" and params == (None,)


class TestOptionalTokenKeys:
    def _run(self, path, *keys):
        return subprocess.run([sys.executable, str(REPO / "scripts" / "deploy_config.py"), "--optional", str(path), *keys],
                              capture_output=True)

    def test_set_keys_come_back_and_absent_ones_are_empty(self, tmp_path):
        f = tmp_path / "deploy.env"
        f.write_text("DEPLOY_D1_REPLICATOR_TOKEN_FILE=cloudflare-api.token\n")
        r = self._run(f, "DEPLOY_D1_READ_TOKEN_FILE", "DEPLOY_D1_REPLICATOR_TOKEN_FILE")
        assert r.returncode == 0 and r.stdout.split(b"\0") == [b"", b"cloudflare-api.token", b""]

    def test_only_the_token_keys_are_optional(self, tmp_path):
        f = tmp_path / "deploy.env"
        f.write_text("DEPLOY_HOST=a\n")
        r = self._run(f, "DEPLOY_HOST")
        assert r.returncode == 2 and r.stdout == b"" and b"not an optional key" in r.stderr

    def test_a_required_key_still_must_be_present(self, tmp_path):
        f = tmp_path / "deploy.env"
        f.write_text("DEPLOY_D1_READ_TOKEN_FILE=x.token\n")
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "deploy_config.py"), str(f), "DEPLOY_HOST"],
                           capture_output=True)
        assert r.returncode == 2 and b"missing DEPLOY_HOST" in r.stderr

    def test_the_example_documents_every_optional_key(self):
        cfg = deploy_config.load(REPO / "instances" / "deploy.env.example")
        assert set(deploy_config.OPTIONAL_KEYS) <= set(cfg)
        assert cfg["DEPLOY_D1_REPLICATOR_TOKEN_FILE"] == "cloudflare-api.token"
        assert cfg["DEPLOY_D1_READ_TOKEN_FILE"] != cfg["DEPLOY_D1_REPLICATOR_TOKEN_FILE"]


class TestBootstrapRefusesAReadOnlyReplicatorToken:
    def _call(self, tmp_path, monkeypatch, repl_value):
        import local_primary as cli
        from memora import add_store as ads

        def tok(name, value):
            p = tmp_path / name
            p.write_text(value)
            p.chmod(0o600)
            return str(p)

        read, edit = tok("read.tok", "read-token-value"), tok("edit.tok", "edit-token-value")
        monkeypatch.delenv("MEMORA_D1_REPLICATOR_TOKEN", raising=False)
        if repl_value is None:
            monkeypatch.delenv("MEMORA_D1_REPLICATOR_TOKEN_FILE", raising=False)
        else:
            monkeypatch.setenv("MEMORA_D1_REPLICATOR_TOKEN_FILE", tok("repl.tok", repl_value))
        reached = []

        def fetch(*a, **k):
            reached.append(1)
            raise RuntimeError("REACHED D1")

        monkeypatch.setattr(ads, "fetch_d1_info", fetch)
        monkeypatch.setattr(ads, "configured_from_env", lambda: (set(), set()))
        argv = ["bootstrap-d1", "newstore", "--account", "a" * 32, "--database-id", "b" * 36, "--d1-name", "n",
                "--read-token-file", read, "--credential-file", edit]
        return cli, argv, reached

    def test_replicator_token_equal_to_the_read_token_is_refused_before_any_d1_call(self, tmp_path, monkeypatch, capsys):
        cli, argv, reached = self._call(tmp_path, monkeypatch, "read-token-value")
        assert cli.main(argv) == 2
        out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert "is the READ token" in out["refused"] and "DEPLOY_D1_REPLICATOR_TOKEN_FILE" in out["refused"]
        assert not reached

    @pytest.mark.parametrize("repl", ["a-write-token", None])
    def test_a_different_or_absent_replicator_token_goes_on(self, tmp_path, monkeypatch, repl):
        cli, argv, reached = self._call(tmp_path, monkeypatch, repl)
        with pytest.raises(RuntimeError, match="REACHED D1"):
            cli.main(argv)
        assert reached


def _rep(local, replica):
    rep = R.StoreReplicator("s1", local, URI, mode="write", writer_factory=replica.writer,
                            reader_factory=replica.reader, broadcast=lambda: None)
    rep._open()
    return rep


def _snapshot(replica):
    import sqlite3
    db = sqlite3.connect(replica.path)
    try:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        return {n: db.execute(f'SELECT * FROM "{n}" ORDER BY 1').fetchall() for n in names}
    finally:
        db.close()


class TestProbeSafety:
    def test_a_real_negative_id_row_with_children_and_the_epoch_are_untouched(self, env):
        """P1-1 (review 12278): D1 may hold an explicitly numbered row -1 (the id column has no CHECK)."""
        import sqlite3
        local, replica = env
        db = sqlite3.connect(replica.path)
        db.execute("INSERT INTO memories (id, content, created_at) VALUES (-1, 'imported', 't')")
        db.execute("INSERT INTO memories_crossrefs (memory_id, related) VALUES (-1, '[]')")
        db.commit()
        db.close()
        before = _snapshot(replica)
        assert any(r[0] == -1 for r in before["memories"])
        rep = _rep(local, replica)
        rep.run_once()
        assert rep.status()["write_probe"] == "ok"
        assert _snapshot(replica) == before   # every table, including the epoch in memories_meta

    @pytest.mark.parametrize("override, why", [
        (lambda out: [], "empty"),
        (lambda out: [{"success": False, "results": []}], "false success"),
        (lambda out: [{"results": []}], "no success key"),
        (lambda out: out + out, "too many"),
    ])
    def test_a_malformed_or_failed_result_never_certifies_write_capability(self, env, override, why):
        local, replica = env
        replica.result_override = override
        rep = _rep(local, replica)
        rep.run_once()          # inconclusive: no raise, never certified
        st = rep.status()
        assert st["write_probe"] == "unproven" and not rep._write_probed
        assert "write capability unproven" in st["last_error"]
        replica.result_override = None
        rep._next_probe = 0.0   # the retry is spaced; let it run now
        rep.run_once()
        assert rep.status()["write_probe"] == "ok" and rep._write_probed

    @pytest.mark.parametrize("exc", [TimeoutError("timed out"), RuntimeError("D1 HTTP 502"),
                                     D1DefiniteError('D1 API error (400): {"errors":[{"code":7400,"message":"bad"}]}')])
    def test_only_the_definite_7500_is_a_refusal_everything_else_is_unproven(self, env, exc):
        local, replica = env
        replica.fail_before = exc
        rep = _rep(local, replica)
        rep.run_once()
        assert rep.status()["write_probe"] == "unproven" and not rep._write_probed
        before = len(replica.statements)
        rep.run_once()          # spaced: not re-sent at once
        assert len(replica.statements) == before

    def test_a_halted_store_sends_nothing(self, env):
        local, replica = env
        rep = _rep(local, replica)
        rep._halt("test: halted")
        before = len(replica.statements)
        assert rep.run_once() == "halted"
        assert len(replica.statements) == before and rep.status()["write_probe"] is None

    def test_a_replica_uri_that_disagrees_with_sync_state_halts_before_any_send(self, env):
        local, replica = env
        rep = R.StoreReplicator("s1", local, "d1://acct/another-db", mode="write", writer_factory=replica.writer,
                                reader_factory=replica.reader, broadcast=lambda: None)
        rep._open()
        assert rep.run_once() == "halted"
        assert replica.statements == [] and rep.status()["write_probe"] is None

    def test_an_unresolved_marker_is_reconciled_before_any_probe(self, env):
        local, replica = env
        conn = local.connect()
        conn.execute("INSERT INTO memories (id, content, tags, created_at) VALUES (5, 'mine', '[]', 't')")
        conn.commit()
        conn.close()
        rep = _rep(local, replica)
        rep._write_probed = True     # an earlier process proved the write; this one's send is the lost one
        replica.apply_then_raise = (99, RuntimeError("response lost"))
        with pytest.raises(RuntimeError):
            rep.run_once()           # the batch reached D1; the marker stays
        replica.apply_then_raise = None
        rep2 = _rep(local, replica)
        assert rep2.run_once() == "reconciled-acked"   # reconciliation first, no probe in front of it
        assert rep2.status()["write_probe"] is None

    def test_a_freeze_waits_for_a_held_probe_request(self, env):
        import threading
        local, replica = env
        gate = local.write_gate()
        box = {}

        def go():
            box["rep"] = _rep(local, replica)
            box["rep"].run_once()

        replica.hold, replica.arrived = threading.Event(), threading.Event()
        t = threading.Thread(target=go)
        t.start()
        assert replica.arrived.wait(5)
        done = threading.Event()
        f = threading.Thread(target=lambda: (gate.freeze(timeout_s=5), done.set()))
        f.start()
        time.sleep(0.2)
        assert not done.is_set() and gate.state == "draining"   # the probe request is in flight on the gate
        replica.hold.set()
        t.join(5)
        f.join(5)
        assert done.is_set() and gate.state == "frozen"
        gate.thaw()
