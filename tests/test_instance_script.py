"""Deployment-script behaviour that #996 depends on: the health token.

codex review: the token had no automated coverage at all, and its validator
was line-based -- a good first line followed by junk was accepted, and the
embedded newlines would have been written into curl's config file.
"""
import os
import subprocess

import pytest

# The container memory default, set from scripts/measure_memory_gate.py
# (local-primary §8 L2a memory gate): max(768M, 1.5 x measured peak RSS).
MEASURED_DEFAULT_MEMORY = "960M"

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "scripts", "memora-instance.sh")


def token(secret_dir, instance="t"):
    """Call health_token in a shell with the script sourced."""
    proc = subprocess.run(
        ["bash", "-c",
         f'source "{SCRIPT}"; SECRET_DIR="{secret_dir}"; INSTANCE="{instance}"; health_token'],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout, proc.stderr


@pytest.mark.skipif(not os.path.exists(SCRIPT), reason="deploy script not present")
class TestHealthToken:
    def test_generates_exactly_one_line_of_the_declared_length(self, tmp_path):
        out, _ = token(str(tmp_path))
        assert len(out) == 48, f"token was {len(out)} bytes"
        assert out.isalnum()
        assert "\n" not in out

    def test_is_stable_across_calls(self, tmp_path):
        first, _ = token(str(tmp_path))
        second, _ = token(str(tmp_path))
        assert first == second, "a new token every deploy would break running clients"

    def test_file_is_0600_and_directory_0700(self, tmp_path):
        d = tmp_path / "sec"
        token(str(d))
        assert oct(os.stat(d).st_mode)[-3:] == "700"
        assert oct(os.stat(d / "t.health-token").st_mode)[-3:] == "600"

    @pytest.mark.parametrize("bad,why", [
        (b"x", "too short"),
        (b"a" * 47, "one byte short"),
        (b"a" * 49, "one byte long"),
        (b"a" * 48 + b"\n", "trailing newline"),
        # codex's case: passes a line-based check, and the newline would be
        # carried into curl's config file by command substitution.
        (b"a" * 48 + b"\nextra-line", "valid first line plus trailing data"),
        (b"a" * 40 + b"!!!!!!!!", "non-alphanumeric"),
        (b"", "empty"),
    ])
    def test_an_unusable_existing_token_is_replaced(self, tmp_path, bad, why):
        d = tmp_path / "sec"
        d.mkdir()
        f = d / "t.health-token"
        f.write_bytes(bad)
        out, err = token(str(d))
        assert len(out) == 48 and out.isalnum(), f"kept an unusable token ({why})"
        assert out.encode() != bad
        if bad:
            assert "replacing unusable health token" in err

    def test_a_world_readable_valid_token_is_kept_but_locked_down(self, tmp_path):
        d = tmp_path / "sec"
        d.mkdir()
        f = d / "t.health-token"
        f.write_text("b" * 48)
        os.chmod(f, 0o644)
        out, _ = token(str(d))
        assert out == "b" * 48, "a valid token should survive, not be rotated"
        assert oct(os.stat(f).st_mode)[-3:] == "600", "permissions were not repaired"


@pytest.mark.skipif(not os.path.exists(SCRIPT), reason="deploy script not present")
class TestRoutingIsInstanceOwned:
    """codex P1: cmd_up appends the instance's registry FIRST and every
    credential env var AFTER. A stale MEMORA_DATABASES left in a credential
    file would therefore win as the later duplicate -e, and the container
    would start against the WRONG set of databases -- silently, with
    cross-database consequences."""

    def _run_up(self, tmp_path, cred_env):
        import json as _json

        inst = tmp_path / "instances"
        inst.mkdir()
        good = _json.dumps({"right": "/tmp/right.db"})
        (inst / "t.env").write_text(
            "INSTANCE=t\nPORT=9999\n"
            f"MEMORA_DATABASES='{good}'\n"
            "MEMORA_DEFAULT_DB=right\n"
            f"CRED_SOURCE={tmp_path / 'cred.json'}\n"
        )
        (tmp_path / "cred.json").write_text(
            _json.dumps({"mcpServers": {"memora": {"env": cred_env}}}))
        # The fake records EVERY runtime call, not just `run`. codex P1:
        # intercepting only `run` left `container stop` and `container rm`
        # hitting the real host runtime, so this test could stop and delete a
        # genuine container that happened to be named memora-t.
        fake = tmp_path / "fakecontainer"
        fake.write_text(
            '#!/bin/bash\n'
            'echo "$1" >> "$CALL_LOG"\n'
            'if [ "$1" = run ]; then printf "%s\\n" "$@" > "$ARGV_OUT"; fi\n'
        )
        fake.chmod(0o755)
        argv_out = tmp_path / "argv.txt"
        call_log = tmp_path / "calls.txt"

        proc = subprocess.run(
            ["bash", "-c",
             f'export MEMORA_INSTANCE_DIR="{inst}" MEMORA_CONTAINER_BIN="{fake}" '
             f'MEMORA_SECRET_DIR="{tmp_path / "sec"}" ARGV_OUT="{argv_out}" '
             f'CALL_LOG="{call_log}"; '
             f'"{SCRIPT}" up t'],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr
        # Every runtime verb cmd_up issues must have been intercepted. If stop
        # or rm is missing here they went to the real runtime instead.
        calls = call_log.read_text().split()
        # "volume": the local registry entry gets the named /data volume
        # (inspect succeeds in the fake, so no create).
        # inspect: the current /data mount (none here); volume: the named
        # /data volume for the local entry (exists in the fake).
        assert calls == ["inspect", "volume", "stop", "rm", "run"], f"runtime calls escaped the fake: {calls}"
        return argv_out.read_text().splitlines(), proc.stdout, good

    def test_a_credential_file_cannot_override_the_instance_registry(self, tmp_path):
        wrong = '{"wrong": "/tmp/wrong.db"}'
        argv, _, good = self._run_up(tmp_path, {
            "CLOUDFLARE_API_TOKEN": "tok",
            "MEMORA_DATABASES": wrong,          # stale value from a past deploy
            "MEMORA_DEFAULT_DB": "wrong",
        })
        registries = [a for a in argv if a.startswith("MEMORA_DATABASES=")]
        defaults = [a for a in argv if a.startswith("MEMORA_DEFAULT_DB=")]
        assert len(registries) == 1, f"routing passed more than once: {registries}"
        assert len(defaults) == 1, f"default passed more than once: {defaults}"
        assert registries[0] == f"MEMORA_DATABASES={good}"
        assert defaults[0] == "MEMORA_DEFAULT_DB=right"
        assert "wrong" not in " ".join(registries + defaults)
        # the rest of the credential env must still be delivered
        assert "CLOUDFLARE_API_TOKEN=tok" in argv

    def test_up_reports_the_registry_not_an_empty_sqlite_volume(self, tmp_path):
        _, out, _ = self._run_up(tmp_path, {"CLOUDFLARE_API_TOKEN": "tok"})
        assert "registry: right" in out, out
        assert "sqlite" not in out, "a registry instance reported itself as sqlite"


FAKE_RUNTIME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_container_runtime.py")


def _up(tmp_path, env_lines, cred_env=None, volume_exists=True, runtime_env=None):
    """Run `memora-instance.sh up t` against tests/fake_container_runtime.py,
    which records every call and keeps volumes as directories under
    tmp_path/volumes. Returns (proc, calls, run argv)."""
    import json as _json

    inst = tmp_path / "instances"
    inst.mkdir(exist_ok=True)
    (inst / "t.env").write_text(
        "INSTANCE=t\nPORT=9999\n" + "".join(l + "\n" for l in env_lines)
        + f"CRED_SOURCE={tmp_path / 'cred.json'}\n")
    (tmp_path / "cred.json").write_text(_json.dumps(
        {"mcpServers": {"memora": {"env": cred_env or {"CLOUDFLARE_API_TOKEN": "tok"}}}}))
    volroot = tmp_path / "volumes"
    volroot.mkdir(exist_ok=True)
    if volume_exists:
        (volroot / "memora-t-data").mkdir(exist_ok=True)
    argv_out, call_log = tmp_path / "argv.txt", tmp_path / "calls.txt"
    for f in (argv_out, call_log):
        if f.exists():
            f.unlink()
    env = dict(os.environ, MEMORA_INSTANCE_DIR=str(inst), MEMORA_CONTAINER_BIN=FAKE_RUNTIME,
               MEMORA_SECRET_DIR=str(tmp_path / "sec"), ARGV_OUT=str(argv_out),
               CALL_LOG=str(call_log), VOLROOT=str(volroot), **(runtime_env or {}))
    proc = subprocess.run([SCRIPT, "up", "t"], env=env, capture_output=True, text=True)
    raw = call_log.read_text() if call_log.exists() else ""
    calls = [r.split("\x1f")[:-1] for r in raw.split("\x1e") if r]
    argv = argv_out.read_text().splitlines() if argv_out.exists() else []
    return proc, calls, argv


def _mounts(argv):
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]


def _envs(argv, key):
    return [argv[i + 1].split("=", 1)[1] for i, a in enumerate(argv)
            if a == "-e" and argv[i + 1].startswith(key + "=")]


@pytest.mark.skipif(not os.path.exists(SCRIPT), reason="deploy script not present")
class TestDataVolume:
    """local-primary L2a (C1): a store that keeps state under /data gets the
    NAMED volume memora-<instance>-data and the MEMORA_DATA_VOLUME marker.
    Before, only the single-store VOLUME branch mounted anything, so a
    registry with a local store wrote into an anonymous volume that the next
    `up` (stop, rm, run) replaced with an empty one."""

    @pytest.mark.parametrize("registry", [
        {"a": "/data/a.db"},                                  # local only
        {"a": "d1://acct/db1", "b": "/data/b.db"},            # mixed
        {"a": "d1://acct/db1", "b": "d1://acct/db2"},         # d1 only: the L2 journal
        {"a": "file:///data/a.db", "b": "s3://bucket/b.db"},  # file:// local
    ])
    def test_registry_needing_data_mounts_the_named_volume(self, tmp_path, registry):
        import json as _json
        proc, calls, argv = _up(tmp_path, [
            f"MEMORA_DATABASES='{_json.dumps(registry)}'", f"MEMORA_DEFAULT_DB={sorted(registry)[0]}"])
        assert proc.returncode == 0, proc.stderr
        assert _mounts(argv) == ["memora-t-data:/data"]
        assert _envs(argv, "MEMORA_DATA_VOLUME") == ["memora-t-data"]
        assert ["volume", "inspect", "memora-t-data"] in calls

    def test_registry_of_only_s3_stores_mounts_nothing(self, tmp_path):
        proc, calls, argv = _up(tmp_path, [
            """MEMORA_DATABASES='{"a": "s3://bucket/a.db"}'""", "MEMORA_DEFAULT_DB=a"])
        assert proc.returncode == 0, proc.stderr
        assert _mounts(argv) == [] and _envs(argv, "MEMORA_DATA_VOLUME") == []
        assert not any(c[0] == "volume" for c in calls)

    def test_a_missing_volume_is_created_before_run(self, tmp_path):
        proc, calls, argv = _up(tmp_path, [
            """MEMORA_DATABASES='{"a": "/data/a.db"}'""", "MEMORA_DEFAULT_DB=a"],
            volume_exists=False)
        assert proc.returncode == 0, proc.stderr
        verbs = [c[:2] if c[0] == "volume" else c[:1] for c in calls]
        assert verbs == [["inspect"], ["volume", "inspect"], ["volume", "create"], ["stop"], ["rm"], ["run"]]
        assert calls[2] == ["volume", "create", "memora-t-data"]

    def test_single_d1_store_mounts_the_named_volume(self, tmp_path):
        proc, _, argv = _up(tmp_path, ['STORAGE_URI="d1://acct/db"'])
        assert proc.returncode == 0, proc.stderr
        assert _mounts(argv) == ["memora-t-data:/data"]
        assert _envs(argv, "MEMORA_DATA_VOLUME") == ["memora-t-data"]

    def test_host_directory_volume_is_a_bind_mount_with_its_path_as_marker(self, tmp_path):
        host = tmp_path / "hostdata"
        proc, calls, argv = _up(tmp_path, [f"VOLUME={host}"])
        assert proc.returncode == 0, proc.stderr
        assert _mounts(argv) == [f"{host}:/data"]
        assert _envs(argv, "MEMORA_DATA_VOLUME") == [str(host)]
        assert not any(c[0] == "volume" for c in calls)

    def test_credential_file_cannot_override_the_marker_or_admin_token(self, tmp_path):
        proc, _, argv = _up(tmp_path, [
            """MEMORA_DATABASES='{"a": "/data/a.db"}'""", "MEMORA_DEFAULT_DB=a"],
            cred_env={"CLOUDFLARE_API_TOKEN": "tok",
                      "MEMORA_DATA_VOLUME": "0" * 64, "MEMORA_ADMIN_TOKEN": "stale"})
        assert proc.returncode == 0, proc.stderr
        assert _envs(argv, "MEMORA_DATA_VOLUME") == ["memora-t-data"]
        admin = _envs(argv, "MEMORA_ADMIN_TOKEN")
        assert len(admin) == 1 and admin[0] != "stale"

    def test_malformed_registry_fails_before_run(self, tmp_path):
        proc, calls, _ = _up(tmp_path, ["MEMORA_DATABASES='not json'"])
        assert proc.returncode != 0
        assert "run" not in [c[0] for c in calls]


@pytest.mark.skipif(not os.path.exists(SCRIPT), reason="deploy script not present")
class TestAdminToken:
    def test_up_passes_a_separate_admin_token(self, tmp_path):
        proc, _, argv = _up(tmp_path, ['STORAGE_URI="d1://acct/db"'])
        assert proc.returncode == 0, proc.stderr
        health, admin = _envs(argv, "MEMORA_HEALTH_TOKEN"), _envs(argv, "MEMORA_ADMIN_TOKEN")
        assert len(health) == 1 and len(admin) == 1
        assert len(admin[0]) == 48 and admin[0].isalnum()
        assert admin[0] != health[0]
        f = tmp_path / "sec" / "t.admin-token"
        assert f.read_text() == admin[0]
        assert oct(os.stat(f).st_mode)[-3:] == "600"

    def test_admin_token_equal_to_health_token_refuses(self, tmp_path):
        sec = tmp_path / "sec"
        sec.mkdir()
        (sec / "t.health-token").write_text("z" * 48)
        (sec / "t.admin-token").write_text("z" * 48)
        os.chmod(sec / "t.admin-token", 0o600)
        proc, calls, _ = _up(tmp_path, ['STORAGE_URI="d1://acct/db"'])
        assert proc.returncode != 0
        assert "admin token equals health token" in proc.stderr
        assert "run" not in [c[0] for c in calls]


@pytest.mark.skipif(not os.path.exists(SCRIPT), reason="deploy script not present")
class TestAdminTokenFileMustBePrivate:
    """Review 7626 P1-3: an existing admin-token file is used only when it is
    a regular file, owned by this user, mode 0600. Anything else may have
    been exposed: refused (never chmod-ed into shape), before any runtime
    call, with the fix printed."""

    def _sec(self, tmp_path):
        sec = tmp_path / "sec"
        sec.mkdir(exist_ok=True)
        return sec

    def test_0600_is_accepted(self, tmp_path):
        f = self._sec(tmp_path) / "t.admin-token"
        f.write_text("q" * 48)
        os.chmod(f, 0o600)
        proc, _, argv = _up(tmp_path, ['STORAGE_URI="d1://acct/db"'])
        assert proc.returncode == 0, proc.stderr
        assert _envs(argv, "MEMORA_ADMIN_TOKEN") == ["q" * 48]

    @pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o700])
    def test_a_permissive_mode_is_refused_and_not_repaired(self, tmp_path, mode):
        f = self._sec(tmp_path) / "t.admin-token"
        f.write_text("q" * 48)
        os.chmod(f, mode)
        proc, calls, _ = _up(tmp_path, ['STORAGE_URI="d1://acct/db"'])
        assert proc.returncode != 0 and "mode 0600" in proc.stderr and "rm " in proc.stderr
        assert calls == [], f"runtime touched before the refusal: {calls}"
        assert oct(os.stat(f).st_mode)[-3:] == oct(mode)[-3:], "the file must not be chmod-ed into shape"

    def test_a_symlink_is_refused(self, tmp_path):
        target = tmp_path / "elsewhere"
        target.write_text("q" * 48)
        os.chmod(target, 0o600)
        (self._sec(tmp_path) / "t.admin-token").symlink_to(target)
        proc, calls, _ = _up(tmp_path, ['STORAGE_URI="d1://acct/db"'])
        assert proc.returncode != 0 and "regular file" in proc.stderr and calls == []

    def test_a_file_owned_by_someone_else_is_refused(self, tmp_path, monkeypatch):
        # Ownership cannot be changed without root: report a different uid
        # to the checker instead (python3 on PATH is wrapped).
        f = self._sec(tmp_path) / "t.admin-token"
        f.write_text("q" * 48)
        os.chmod(f, 0o600)
        wrap = tmp_path / "pybin"
        wrap.mkdir()
        (wrap / "sitecustomize.py").write_text("import os\nos.getuid = lambda: 424242\n")
        proc, calls, _ = _up(tmp_path, ['STORAGE_URI="d1://acct/db"'],
                             runtime_env={"PYTHONPATH": str(wrap)})
        assert proc.returncode != 0 and "owned by" in proc.stderr and calls == []


@pytest.mark.skipif(not os.path.exists(SCRIPT), reason="deploy script not present")
class TestUpgradeFromAnAnonymousVolume:
    """Review 7626 P0-1: `up` on an instance whose container still mounts an
    anonymous /data volume must carry that data into the named volume --
    staged, verified, while stopped -- keep the old container for rollback,
    and abort before run if the copy fails."""

    REG = ["""MEMORA_DATABASES='{"re": "/data/re.db", "memora": "d1://acct/db"}'""", "MEMORA_DEFAULT_DB=re"]
    ANON = "5e" * 32

    def _old_volume(self, tmp_path):
        import sqlite3
        old = tmp_path / "volumes" / self.ANON
        (old / "intent").mkdir(parents=True)
        db = sqlite3.connect(old / "re.db")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT)")
        db.execute("INSERT INTO memories (content) VALUES ('must survive')")
        db.commit()
        keep = sqlite3.connect(old / "re.db")  # sidecars stay while copied
        keep.execute("SELECT 1").fetchone()
        (old / "intent" / "memora.jsonl").write_text('{"type":"intent","id":7,"sql":"INSERT INTO memories"}\n')
        return old, (db, keep)

    @staticmethod
    def _files(root):
        import hashlib
        from pathlib import Path
        return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(Path(root).rglob("*")) if p.is_file()
                and not p.name.startswith(".memora-volume-source")}

    def test_upgrade_copies_everything_byte_identical_and_keeps_the_old_container(self, tmp_path):
        old, conns = self._old_volume(tmp_path)
        assert (old / "re.db-wal").exists() and (old / "re.db-shm").exists()
        proc, calls, argv = _up(tmp_path, self.REG, volume_exists=False,
                                runtime_env={"CURRENT_MOUNT": self.ANON})
        assert proc.returncode == 0, proc.stderr
        new = tmp_path / "volumes" / "memora-t-data"
        # Compared before closing: a close checkpoints the source's WAL.
        assert self._files(new) == self._files(old)
        for c in conns:
            c.close()
        assert {"re.db", "re.db-wal", "re.db-shm", "intent/memora.jsonl"} <= set(self._files(new))
        assert f"source={self.ANON}" in (new / ".memora-volume-source").read_text()
        verbs = [c[0] for c in calls]
        stop, copy = verbs.index("stop"), next(i for i, c in enumerate(calls)
                                              if c[0] == "run" and "migrate_data_volume" in c)
        rename = verbs.index("rename")
        assert stop < copy < rename < len(calls) - 1, "copy while stopped, keep the old one, then run"
        assert calls[rename][1] == "memora-t" and calls[rename][2].startswith("memora-t-pre-data-volume-")
        assert "rm" not in verbs, "the old container is kept for rollback"
        assert _mounts(argv) == ["memora-t-data:/data"]

    def test_a_second_up_is_a_no_op(self, tmp_path):
        old, conns = self._old_volume(tmp_path)
        _up(tmp_path, self.REG, volume_exists=False, runtime_env={"CURRENT_MOUNT": self.ANON})
        for c in conns:
            c.close()
        new = tmp_path / "volumes" / "memora-t-data"
        (new / "written-by-the-new-container").write_text("x")
        before = self._files(new)
        proc, calls, _ = _up(tmp_path, self.REG, runtime_env={"CURRENT_MOUNT": "memora-t-data"})
        assert proc.returncode == 0, proc.stderr
        assert not any("migrate_data_volume" in c for c in calls)
        assert self._files(new) == before

    def test_a_failed_copy_aborts_before_run_and_leaves_the_old_container(self, tmp_path):
        old, conns = self._old_volume(tmp_path)
        proc, calls, argv = _up(tmp_path, self.REG, volume_exists=False,
                                runtime_env={"CURRENT_MOUNT": self.ANON, "COPY_RC": "1"})
        for c in conns:
            c.close()
        assert proc.returncode != 0
        assert "container start memora-t" in proc.stderr.replace(FAKE_RUNTIME, "container")
        verbs = [c[0] for c in calls]
        assert "rm" not in verbs and "rename" not in verbs and argv == []
        assert (old / "re.db").exists(), "the old volume is untouched"

    def test_a_still_running_container_is_not_copied(self, tmp_path):
        self._old_volume(tmp_path)
        proc, calls, argv = _up(tmp_path, self.REG, volume_exists=False,
                                runtime_env={"CURRENT_MOUNT": self.ANON, "RUNNING_LIST": "memora-t running"})
        assert proc.returncode != 0 and "still running" in proc.stderr
        assert not any("migrate_data_volume" in c for c in calls) and argv == []

    def test_without_rename_the_container_is_removed_and_the_volume_kept(self, tmp_path):
        old, conns = self._old_volume(tmp_path)
        proc, calls, _ = _up(tmp_path, self.REG, volume_exists=False,
                             runtime_env={"CURRENT_MOUNT": self.ANON, "RENAME_RC": "1"})
        for c in conns:
            c.close()
        assert proc.returncode == 0, proc.stderr
        assert "old volume" in proc.stdout and "rm" in [c[0] for c in calls]
        assert (old / "re.db").exists()


@pytest.mark.skipif(not os.path.exists(SCRIPT), reason="deploy script not present")
def test_default_memory_is_the_measured_gate():
    """scripts/measure_memory_gate.py sets the default (§8 L2a memory gate)."""
    proc = subprocess.run(["bash", "-c", f'unset MEMORA_MEMORY; source "{SCRIPT}"; echo "$DEFAULT_MEMORY"'],
                          capture_output=True, text=True)
    assert proc.stdout.strip() == MEASURED_DEFAULT_MEMORY
