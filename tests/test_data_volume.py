"""The /data startup check (local-primary plan §8 L2a, §7 "Launcher").

A store kept under /data is refused unless /data is a named, writable mount.
Mount detection is modelled with an injected root and stat(): a directory and
its parent share a device (the "not a mount" case) unless stat() says otherwise.
"""
import json
import os
from pathlib import Path

import pytest

from memora import data_volume, storage
from memora.data_volume import (
    DataVolumeRefused,
    check_data_volume,
    startup_refusals,
    uri_needs_data_volume,
)

NAMED = {"MEMORA_DATA_VOLUME": "memora-all-data"}


def _mounted_stat(data_dir):
    """stat() under which data_dir sits on its own device (a mount)."""
    real = os.stat

    def fake(path):
        st = real(path)
        if Path(path) == Path(data_dir):
            vals = list(st)
            vals[2] = st.st_dev + 1  # st_dev
            return os.stat_result(vals)
        return st
    return fake


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    return d


class TestNeedsDataVolume:
    @pytest.mark.parametrize("uri,needs", [
        ("/data/memora.db", True),
        ("file:///data/re.db", True),
        ("/data/shadow/ob1.db", True),
        ("/data", True),
        ("/database/x.db", False),        # prefix of the name, not the directory
        ("/tmp/x.db", False),
        ("/data/../tmp/x.db", False),     # lexically outside /data
        ("s3://bucket/memora.db", False),
    ])
    def test_local_paths_and_s3(self, uri, needs):
        assert uri_needs_data_volume(uri, Path("/data")) is needs

    def test_d1_primaries_keep_their_journal_there(self):
        # L2: the write gate's freeze file and the intent journal.
        assert data_volume.D1_PRIMARY_USES_DATA is True
        assert uri_needs_data_volume("d1://acct/db") is True
        assert uri_needs_data_volume("d1://acct/db", d1_primary_uses_data=False) is False

    def test_the_data_dir_follows_memora_data_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEMORA_DATA_DIR", str(tmp_path))
        assert data_volume.data_dir() == tmp_path
        assert uri_needs_data_volume(str(tmp_path / "x.db")) is True
        assert uri_needs_data_volume("/data/x.db") is False


class TestCheck:
    def test_passes_on_a_named_writable_mount_and_leaves_no_probe(self, data_dir):
        assert check_data_volume(data_dir, env=NAMED, stat=_mounted_stat(data_dir)) is None
        # the intent directory the L2 journal needs now exists; probes are gone
        assert (data_dir / "intent").is_dir()
        assert list(data_dir.iterdir()) == [data_dir / "intent"]
        assert list((data_dir / "intent").iterdir()) == []

    def test_refuses_without_the_marker(self, data_dir):
        reason = check_data_volume(data_dir, env={}, stat=_mounted_stat(data_dir))
        assert reason and "MEMORA_DATA_VOLUME is not set" in reason

    def test_refuses_an_anonymous_volume(self, data_dir):
        reason = check_data_volume(data_dir, env={"MEMORA_DATA_VOLUME": "a1" * 32},
                                   stat=_mounted_stat(data_dir))
        assert reason and "anonymous" in reason

    def test_a_64_char_name_that_is_not_hex_is_accepted(self, data_dir):
        env = {"MEMORA_DATA_VOLUME": "memora-" + "x" * 57}
        assert check_data_volume(data_dir, env=env, stat=_mounted_stat(data_dir)) is None

    def test_refuses_a_non_mount(self, data_dir):
        # "root" is data_dir's parent, on the same device by construction: the
        # root-fs case. (tmp_path itself may be a tmpfs, not on "/"'s device.)
        reason = check_data_volume(data_dir, env=NAMED, root=data_dir.parent)
        assert reason and "not a mount point" in reason
        assert not (data_dir / "intent").exists(), "must refuse before writing anything"

    def test_refuses_a_missing_directory(self, tmp_path):
        reason = check_data_volume(tmp_path / "absent", env=NAMED)
        assert reason and "does not exist" in reason

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
    def test_refuses_a_read_only_data_dir(self, data_dir):
        data_dir.chmod(0o500)
        try:
            reason = check_data_volume(data_dir, env=NAMED, stat=_mounted_stat(data_dir))
        finally:
            data_dir.chmod(0o700)
        assert reason and "not writable" in reason

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
    def test_refuses_a_read_only_intent_dir(self, data_dir):
        (data_dir / "intent").mkdir()
        (data_dir / "intent").chmod(0o500)
        try:
            reason = check_data_volume(data_dir, env=NAMED, stat=_mounted_stat(data_dir))
        finally:
            (data_dir / "intent").chmod(0o700)
        assert reason and str(data_dir / "intent") in reason

    def test_a_failed_fsync_is_a_refusal_and_removes_the_probe(self, data_dir, monkeypatch):
        def boom(_fd):
            raise OSError(5, "Input/output error")
        monkeypatch.setattr(data_volume.os, "fsync", boom)
        reason = check_data_volume(data_dir, env=NAMED, stat=_mounted_stat(data_dir))
        assert reason and "fsync" in reason
        assert list(data_dir.iterdir()) == []


class TestStartupRefusals:
    REG = {"local": "/data/local.db", "cloud": "d1://acct/db", "elsewhere": "/tmp/x.db",
           "s3": "s3://bucket/k"}
    DATA = Path("/data")

    def test_stores_under_data_and_d1_primaries_are_refused(self):
        out = startup_refusals(self.REG, None, data_root=self.DATA, check=lambda: "no mount")
        assert out == {"local": "no mount", "cloud": "no mount"}

    def test_passing_check_refuses_nothing(self):
        assert startup_refusals(self.REG, None, data_root=self.DATA, check=lambda: None) == {}

    def test_check_is_not_run_when_no_store_needs_data(self):
        def never():
            raise AssertionError("checked /data for stores that do not use it")
        assert startup_refusals({"e": "/tmp/x.db", "s": "s3://b/k"}, None,
                                data_root=self.DATA, check=never) == {}

    def test_single_store_is_keyed_none(self):
        assert startup_refusals({}, "/data/memora.db", data_root=self.DATA,
                                check=lambda: "r") == {None: "r"}
        assert startup_refusals({}, "d1://a/b", data_root=self.DATA, check=lambda: "r") == {None: "r"}
        assert startup_refusals({}, "/home/u/.local/share/memora/memories.db",
                                data_root=self.DATA, check=lambda: "r") == {}


class TestRefusedStoresAreNotServed:
    @pytest.fixture(autouse=True)
    def _reset(self):
        yield
        storage.set_store_refusals({})

    def test_backend_for_and_connect_raise_for_a_refused_store_only(self, tmp_path, monkeypatch):
        reg = {"bad": str(tmp_path / "bad.db"), "good": str(tmp_path / "good.db")}
        monkeypatch.setenv("MEMORA_DATABASES", json.dumps(reg))
        monkeypatch.setenv("MEMORA_DEFAULT_DB", "good")
        storage.set_store_refusals({"bad": "/data is not a mount point"})
        with pytest.raises(DataVolumeRefused, match="not a mount point"):
            storage.backend_for("bad")
        token = storage.CURRENT_DB.set("bad")
        try:
            with pytest.raises(DataVolumeRefused):
                storage.connect()
        finally:
            storage.CURRENT_DB.reset(token)
        assert not (tmp_path / "bad.db").exists(), "a refused store was created"
        storage.backend_for("good")  # the other store still serves

    def test_single_store_refusal(self, monkeypatch):
        monkeypatch.delenv("MEMORA_DATABASES", raising=False)
        storage.set_store_refusals({None: "MEMORA_DATA_VOLUME is not set"})
        with pytest.raises(DataVolumeRefused, match="MEMORA_DATA_VOLUME"):
            storage.current_backend()

    def test_health_names_the_reason(self, tmp_path, monkeypatch):
        from memora import health

        reg = {"bad": str(tmp_path / "bad.db")}
        monkeypatch.setenv("MEMORA_DATABASES", json.dumps(reg))
        monkeypatch.setenv("MEMORA_DEFAULT_DB", "bad")
        storage.set_store_refusals({"bad": "/data is not a mount point"})
        entry = health.readiness_payload()["databases"]["bad"]
        assert entry["status"] == "error"
        assert entry["error"] == "DataVolumeRefused"
        assert "not a mount point" in entry["message"]

    def test_server_startup_records_refusals(self, monkeypatch, capsys):
        from memora import server

        # /data/local.db is NOT under the test's MEMORA_DATA_DIR (conftest).
        monkeypatch.setenv("MEMORA_DATABASES", json.dumps(
            {"local": "/data/local.db", "cloud": "d1://acct/db"}))
        monkeypatch.setenv("MEMORA_DEFAULT_DB", "cloud")
        monkeypatch.setattr(data_volume, "check_data_volume",
                            lambda *_a, **_k: "/data is not a mount point")
        server._apply_data_volume_check()
        assert storage._store_refusals == {"cloud": "/data is not a mount point"}
        assert "refusing to serve database cloud" in capsys.readouterr().err

    def test_server_startup_with_a_good_mount_refuses_nothing(self, monkeypatch):
        from memora import server

        monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"local": "/data/local.db"}))
        monkeypatch.setenv("MEMORA_DEFAULT_DB", "local")
        monkeypatch.setattr(data_volume, "check_data_volume", lambda *_a, **_k: None)
        storage.set_store_refusals({"stale": "x"})
        server._apply_data_volume_check()
        assert storage._store_refusals == {}


def test_l2_fence_and_gate_init_skip_refused_stores(tmp_path, monkeypatch):
    """A store the /data check refused gets no primary lock, gate, freeze
    file or journal: those would all be opened on the unfit /data."""
    from memora import write_gate

    reg = {"bad": "d1://acct/bad", "good": str(tmp_path / "good.db")}
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps(reg))
    monkeypatch.setenv("MEMORA_DEFAULT_DB", "good")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    storage.set_store_refusals({"bad": "/data is not a mount point"})
    real = storage.backend_for
    touched = []
    monkeypatch.setattr(storage, "backend_for", lambda n: touched.append(n) or real(n))
    try:
        assert "bad" not in write_gate.fence_live_primaries()
        summary = write_gate.initialize_registry_gates()
    finally:
        storage.set_store_refusals({})
    assert summary["bad"] == {"state": "refused", "error": "/data is not a mount point"}
    assert "bad" not in touched
    assert not (write_gate.data_dir() / "intent" / "bad.jsonl").exists()


def test_a_configuration_error_wins_over_a_data_volume_refusal(monkeypatch):
    """A misconfigured store must still abort startup (DatabaseRegistryError,
    exit 2), not be reported as merely refused."""
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"x": "d1://broken"}))
    monkeypatch.setenv("MEMORA_DEFAULT_DB", "x")
    storage.set_store_refusals({"x": "MEMORA_DATA_VOLUME is not set"})
    with pytest.raises(storage.DatabaseRegistryError):
        storage.backend_for("x")


def test_no_replicator_or_shadow_file_for_a_refused_store(tmp_path, monkeypatch):
    """L3 builds a shadow store straight from MEMORA_SHADOW_LOCAL, not through
    backend_for; a refused store must still get no replicator and no file."""
    from memora import replicator

    shadow = tmp_path / "shadow" / "bad.db"
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"bad": "d1://acct/bad"}))
    monkeypatch.setenv("MEMORA_DEFAULT_DB", "bad")
    monkeypatch.setenv("MEMORA_REPLICATION", "log")
    monkeypatch.setenv("MEMORA_SHADOW_LOCAL", json.dumps({"bad": str(shadow)}))
    storage.set_store_refusals({"bad": "/data is not a mount point"})
    try:
        out = replicator.start_replicators(start=False)
    finally:
        replicator.stop_replicators()
    assert "store refused" in out["bad"]["error"]
    assert not shadow.exists() and not shadow.parent.exists()
