"""A d1:// primary refuses unless D1 read replication is disabled (Item 110).

No test calls Cloudflare. The live GET is urllib.request.urlopen, replaced
here. The conftest hook (mode "disabled") is replaced per test.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from memora import add_store as ads
from memora import backends, shadow, storage
from memora.backends import (
    D1Backend,
    D1Connection,
    ReadReplicationRefused,
    check_configured_d1_primaries,
    ensure_read_replication_disabled,
)
from memora.shadow import replication_mode_of
from memora.write_gate import data_dir

ROOT = Path(__file__).resolve().parent.parent


class _Body:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


def _urlopen_returning(payload: bytes):
    seen = []

    def urlopen(req, timeout=30):
        seen.append(req)
        return _Body(payload)

    return urlopen, seen


def _journal(database_id: str) -> Path:
    return data_dir() / "intent" / f"{database_id}.jsonl"


def test_replication_mode_of_reads_a_disabled_mode_and_fail_closes_the_rest():
    assert replication_mode_of({"result": {"read_replication": {"mode": "disabled"}}}) == "disabled"
    assert replication_mode_of({"result": {"read_replication": {"mode": "auto"}}}) == "auto"
    assert replication_mode_of({}) == "unknown"
    assert replication_mode_of({"result": {}}) == "unknown"
    assert replication_mode_of({"result": {"read_replication": {"mode": ""}}}) == "unknown"
    assert replication_mode_of({"result": {"read_replication": {"mode": None}}}) == "unknown"
    assert replication_mode_of({"result": []}) == "unknown"
    assert replication_mode_of(["nope"]) == "unknown"


def test_disabled_mode_lets_connect_open_the_journal(monkeypatch):
    calls = {"n": 0}

    def hook(account, database, token):
        calls["n"] += 1
        assert token == "sekret"
        return "disabled"

    monkeypatch.setattr(backends, "_replication_mode_hook", hook)
    conn = D1Backend("acct", "db1", "sekret").connect()
    assert isinstance(conn, D1Connection)
    assert _journal("db1").is_file()
    assert calls["n"] == 1


def test_auto_mode_is_refused_before_the_journal_and_names_the_database(monkeypatch):
    calls = {"n": 0}

    def hook(*_a):
        calls["n"] += 1
        return "auto"

    monkeypatch.setattr(backends, "_replication_mode_hook", hook)
    backend = D1Backend("acct", "db1", "sekret")
    with pytest.raises(ReadReplicationRefused, match="db1") as exc:
        backend.connect()
    text = str(exc.value)
    assert "auto" in text
    assert "disabled" in text
    assert "MEMORA_D1_ALLOW_READ_REPLICATION" in text
    assert "acct" not in text
    assert "sekret" not in text
    assert not _journal("db1").exists()
    with pytest.raises(ReadReplicationRefused):
        backend.connect()
    assert calls["n"] == 2, "a refusal is not cached"


def test_an_api_error_fail_closes_and_is_not_cached(monkeypatch):
    calls = {"n": 0}

    def hook(*_a):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.URLError("network down")
        return "disabled"

    monkeypatch.setattr(backends, "_replication_mode_hook", hook)
    backend = D1Backend("acct", "db1", "sekret")
    with pytest.raises(ReadReplicationRefused, match="URLError") as exc:
        backend.connect()
    text = str(exc.value)
    assert "network down" in text and "db1" in text
    assert "sekret" not in text and "acct" not in text
    assert not _journal("db1").exists()
    backend.connect()
    assert calls["n"] == 2
    assert _journal("db1").is_file()


def test_override_skips_the_fetch_and_warns_once(monkeypatch, caplog):
    monkeypatch.setenv("MEMORA_D1_ALLOW_READ_REPLICATION", "1")

    def boom(*_a):
        raise AssertionError("the override must not fetch the mode")

    monkeypatch.setattr(backends, "_replication_mode_hook", boom)
    backend = D1Backend("acct", "db1", "sekret")
    with caplog.at_level(logging.WARNING, logger="memora.backends"):
        backend.connect()
        backend.connect()
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
                and "MEMORA_D1_ALLOW_READ_REPLICATION" in r.getMessage()]
    assert warnings == ["D1 database db1: MEMORA_D1_ALLOW_READ_REPLICATION=1, "
                        "starting without checking read replication"]
    assert "sekret" not in warnings[0]


def test_a_true_value_does_not_override(monkeypatch):
    monkeypatch.setenv("MEMORA_D1_ALLOW_READ_REPLICATION", "true")
    monkeypatch.setattr(backends, "_replication_mode_hook", lambda *_a: "auto")
    with pytest.raises(ReadReplicationRefused, match="auto"):
        D1Backend("acct", "db1", "sekret").connect()


def test_a_passing_mode_is_cached(monkeypatch):
    calls = {"n": 0}

    def hook(*_a):
        calls["n"] += 1
        return "disabled" if calls["n"] == 1 else "auto"

    monkeypatch.setattr(backends, "_replication_mode_hook", hook)
    backend = D1Backend("acct", "db1", "sekret")
    backend.connect()
    backend.connect()
    assert calls["n"] == 1


def test_connect_uses_the_live_parser_when_the_hook_is_unset(monkeypatch):
    payload = json.dumps({"result": {"read_replication": {"mode": "disabled"}}}).encode()
    urlopen, seen = _urlopen_returning(payload)
    monkeypatch.setattr(backends, "_replication_mode_hook", None)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    backend = D1Backend("acct", "db1", "sekret")
    assert isinstance(backend.connect(), D1Connection)
    backend.connect()
    assert len(seen) == 1
    assert seen[0].full_url == "https://api.cloudflare.com/client/v4/accounts/acct/d1/database/db1"
    assert "sekret" not in seen[0].full_url
    assert seen[0].get_header("Authorization") == "Bearer sekret"
    assert shadow.read_replication_mode("acct", "db1", "sekret") == "disabled"
    assert len(seen) == 2


def test_a_live_auto_mode_and_a_bad_body_are_refused(monkeypatch):
    monkeypatch.setattr(backends, "_replication_mode_hook", None)

    def urlopen(req, timeout=30):
        assert "sekret" not in req.full_url
        return _Body(json.dumps({"result": {"read_replication": {"mode": "auto"}}}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(ReadReplicationRefused, match="db1") as exc:
        D1Backend("acct", "db1", "sekret").connect()
    assert "acct" not in str(exc.value) and "sekret" not in str(exc.value)

    def bad(req, timeout=30):
        return _Body(b"not-json")

    monkeypatch.setattr(urllib.request, "urlopen", bad)
    with pytest.raises(ReadReplicationRefused, match="JSONDecodeError") as exc:
        D1Backend("acct", "db1", "sekret").connect()
    assert "sekret" not in str(exc.value)

    def nondict(req, timeout=30):
        return _Body(b'{"result": []}')

    monkeypatch.setattr(urllib.request, "urlopen", nondict)
    with pytest.raises(ReadReplicationRefused, match="unknown"):
        D1Backend("acct", "db1", "sekret").connect()


def _d1_env(monkeypatch, raw):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN_FILE", raising=False)
    monkeypatch.delenv("CF_API_TOKEN", raising=False)
    monkeypatch.setenv("MEMORA_DATABASES", raw)
    storage._registry_cache = None


def test_a_configured_d1_primary_is_checked_and_a_local_path_is_not(monkeypatch, tmp_path):
    _d1_env(monkeypatch, json.dumps({"files": str(tmp_path / "a.db"), "cloud": "d1://acct/db1"}))
    monkeypatch.setenv("MEMORA_DEFAULT_DB", "files")
    names = []
    real = storage._backend_for_unchecked

    def spy(name):
        names.append(name)
        return real(name)

    monkeypatch.setattr(storage, "_backend_for_unchecked", spy)
    monkeypatch.setattr(backends, "_replication_mode_hook", lambda *_a: "auto")
    with pytest.raises(ReadReplicationRefused, match="db1") as exc:
        check_configured_d1_primaries()
    assert names == ["cloud"]
    assert "acct" not in str(exc.value)
    storage._registry_cache = None


def test_the_legacy_local_backend_is_not_checked(monkeypatch, tmp_path):
    from memora.backends import LocalSQLiteBackend

    monkeypatch.delenv("MEMORA_DATABASES", raising=False)
    monkeypatch.setattr(storage, "STORAGE_BACKEND", LocalSQLiteBackend(tmp_path / "m.db"))

    def boom(*_a):
        raise AssertionError("checked a local primary")

    monkeypatch.setattr(backends, "_replication_mode_hook", boom)
    check_configured_d1_primaries()


def test_the_legacy_d1_uri_is_checked(monkeypatch):
    monkeypatch.delenv("MEMORA_DATABASES", raising=False)
    monkeypatch.setattr(storage, "STORAGE_BACKEND", D1Backend("acct", "db1", "tok"))
    monkeypatch.setattr(backends, "_replication_mode_hook", lambda *_a: "auto")
    with pytest.raises(ReadReplicationRefused, match="db1") as exc:
        check_configured_d1_primaries()
    assert "acct" not in str(exc.value)


def _server_main(monkeypatch, hook):
    from memora import server

    calls = []
    monkeypatch.setattr(server, "start_graph_server", lambda *a, **k: calls.append("start_graph_server"))
    monkeypatch.setattr(server.mcp, "run", lambda *a, **k: calls.append("mcp.run"))

    def connect():
        calls.append("connect")
        raise AssertionError("connect must not run after a read-replication refusal")

    monkeypatch.setattr(server, "connect", connect)
    monkeypatch.setenv("MEMORA_TRANSPORT", "stdio")
    monkeypatch.delenv("MEMORA_SERVICE_LOCK", raising=False)
    _d1_env(monkeypatch, json.dumps({"mem": "d1://acct/db1"}))
    monkeypatch.setattr(backends, "_replication_mode_hook", hook)
    return server, calls


def test_the_server_exits_when_read_replication_is_on(monkeypatch, capsys):
    server, calls = _server_main(monkeypatch, lambda *_a: "auto")
    with pytest.raises(SystemExit) as exc:
        server.main([])
    err = capsys.readouterr().err
    assert exc.value.code == 2
    assert "Error:" in err and "db1" in err
    assert calls == []
    storage._registry_cache = None


def test_the_server_exits_when_the_mode_cannot_be_read(monkeypatch, capsys):
    def hook(*_a):
        raise urllib.error.URLError("network down")

    server, calls = _server_main(monkeypatch, hook)
    with pytest.raises(SystemExit) as exc:
        server.main([])
    err = capsys.readouterr().err
    assert exc.value.code == 2
    assert "URLError" in err and "db1" in err
    assert "Warning:" not in err
    assert calls == []
    storage._registry_cache = None


def _compare_cli():
    spec = importlib.util.spec_from_file_location("lp_cli_item110", ROOT / "scripts" / "local_primary.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    return cli


def test_compare_refuses_before_building_a_reader(monkeypatch, capsys):
    cli = _compare_cli()
    monkeypatch.setenv("MEMORA_D1_READ_TOKEN", "sekret")
    monkeypatch.setattr(backends, "_replication_mode_hook", lambda *_a: "auto")

    def ctor(*_a, **_k):
        raise AssertionError("reader built")

    monkeypatch.setattr(backends, "D1SelectOnlyConnection", ctor)
    rc = cli.main(["compare", "mem", "--mode", "nightly", "--store", "/tmp/no-such-store.db",
                   "--account", "acct", "--database-id", "db1", "--no-record"])
    assert rc == 2
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["ok"] is False
    assert "db1" in out["refused"] and "auto" in out["refused"]
    assert "sekret" not in out["refused"] and "acct" not in out["refused"]


def test_compare_builds_the_reader_when_replication_is_disabled(monkeypatch):
    cli = _compare_cli()
    monkeypatch.setenv("MEMORA_D1_READ_TOKEN", "sekret")
    monkeypatch.setattr(backends, "_replication_mode_hook", lambda *_a: "disabled")
    seen = []

    class Boom(Exception):
        pass

    def ctor(*args, **_k):
        seen.append(args)
        raise Boom("built")

    monkeypatch.setattr(backends, "D1SelectOnlyConnection", ctor)
    args = SimpleNamespace(account="acct", database_id="db1", read_token_file=None)
    with pytest.raises(Boom):
        cli._compare(args)
    assert seen and seen[0][1] == "db1"


def test_fetch_d1_info_reuses_the_parser_and_a_missing_mode_refuses(monkeypatch):
    seen = []

    def opener(req, timeout=30):
        seen.append(req.full_url)
        assert "sekret" not in req.full_url
        body = {"result": {"uuid": "db1", "name": "n", "read_replication": {"mode": "auto"}}}
        return _Body(json.dumps(body).encode())

    info = ads.fetch_d1_info("acct", "db1", "sekret", opener=opener)
    assert info["read_replication_mode"] == "auto"
    assert info["uuid"] == "db1" and info["name"] == "n"
    assert seen == ["https://api.cloudflare.com/client/v4/accounts/acct/d1/database/db1"]

    def writer():
        raise AssertionError("writer opened")

    deps = ads.BootstrapDeps(info=lambda: info, read=lambda *a: [], open_writer=writer)
    with pytest.raises(ads.AddStoreRefused, match="nothing was changed") as exc:
        ads._check_identity(deps, "db1", "n", "(identity check)")
    assert "db1" in str(exc.value) and "auto" in str(exc.value)

    wrong = {"uuid": "other-db", "name": "n", "read_replication_mode": "auto"}
    deps.info = lambda: wrong
    with pytest.raises(ads.AddStoreRefused, match="not the pinned"):
        ads._check_identity(deps, "db1", "n", "(identity check)")

    missing = {"uuid": "db1", "name": "n"}
    deps.info = lambda: missing
    with pytest.raises(ads.AddStoreRefused, match="unknown"):
        ads._check_identity(deps, "db1", "n", "(identity check)")


def test_bootstrap_writer_connect_refuses_before_the_journal(monkeypatch):
    monkeypatch.setattr(backends, "_replication_mode_hook", lambda *_a: "auto")
    with pytest.raises(ReadReplicationRefused, match="db1"):
        ads.BootstrapD1Backend("acct", "db1", "sekret").connect()
    assert not _journal("db1").exists()


def test_ensure_override_does_not_fetch(monkeypatch, caplog):
    monkeypatch.setenv("MEMORA_D1_ALLOW_READ_REPLICATION", "1")

    def boom(*_a):
        raise AssertionError("fetched")

    monkeypatch.setattr(backends, "_replication_mode_hook", boom)
    with caplog.at_level(logging.WARNING, logger="memora.backends"):
        ensure_read_replication_disabled("acct", "db1", "sekret")
    assert any("db1" in r.getMessage() for r in caplog.records)
