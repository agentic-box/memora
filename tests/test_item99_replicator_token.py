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
        rep = _started(local, replica)
        try:
            assert _wait(lambda: rep.status().get("write_probe") == "ok"), rep.status()
        finally:
            rep.stop()
        probes = [s for s in replica.statements if s.startswith("DELETE")]
        assert probes == ["DELETE FROM memories WHERE id = ?"]
        assert {t: replica.rows(t) for t in R.SYNC_TABLES} == before

    def test_a_token_d1_refuses_to_write_never_reports_ok_and_names_the_error(self, env):
        local, replica = env
        replica.fail_before = READ_ONLY
        rep = _started(local, replica)
        try:
            assert _wait(lambda: "7500" in (rep.status().get("last_error") or "")), rep.status()
            st = rep.status()
            assert st["write_probe"] is None and st["status"] == "backoff", st
            time.sleep(0.3)
            assert rep.status()["write_probe"] is None
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

    def test_the_probe_statement_passes_the_allow_list_and_matches_no_real_row(self):
        (sql, params), = R._build_statements("memories", [-1], None, [])
        assert R._check_statement(sql) == "delete" and params == (-1,)


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
