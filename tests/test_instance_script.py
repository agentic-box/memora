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
        assert calls == ["stop", "rm", "volume", "run"], f"runtime calls escaped the fake: {calls}"
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


def _up(tmp_path, env_lines, cred_env=None, volume_exists=True):
    """Run `memora-instance.sh up t` against a fake runtime that records
    every call (one line per call, args tab-separated). Returns the calls and
    the `run` argv."""
    import json as _json

    inst = tmp_path / "instances"
    inst.mkdir(exist_ok=True)
    (inst / "t.env").write_text(
        "INSTANCE=t\nPORT=9999\n" + "".join(l + "\n" for l in env_lines)
        + f"CRED_SOURCE={tmp_path / 'cred.json'}\n")
    (tmp_path / "cred.json").write_text(_json.dumps(
        {"mcpServers": {"memora": {"env": cred_env or {"CLOUDFLARE_API_TOKEN": "tok"}}}}))
    fake = tmp_path / "fakecontainer"
    fake.write_text(
        '#!/bin/bash\n'
        '(IFS=$\'\\t\'; echo "$*") >> "$CALL_LOG"\n'
        'if [ "$1" = run ]; then printf "%s\\n" "$@" > "$ARGV_OUT"; fi\n'
        'if [ "$1 $2" = "volume inspect" ]; then [ -e "$VOL_STATE" ]; exit $?; fi\n'
        'if [ "$1 $2" = "volume create" ]; then touch "$VOL_STATE"; fi\n'
    )
    fake.chmod(0o755)
    vol_state = tmp_path / "volume-exists"
    if volume_exists:
        vol_state.touch()
    argv_out, call_log = tmp_path / "argv.txt", tmp_path / "calls.txt"
    for f in (argv_out, call_log):
        if f.exists():
            f.unlink()
    proc = subprocess.run(
        ["bash", "-c",
         f'export MEMORA_INSTANCE_DIR="{inst}" MEMORA_CONTAINER_BIN="{fake}" '
         f'MEMORA_SECRET_DIR="{tmp_path / "sec"}" ARGV_OUT="{argv_out}" '
         f'CALL_LOG="{call_log}" VOL_STATE="{vol_state}"; '
         f'"{SCRIPT}" up t'],
        capture_output=True, text=True,
    )
    calls = [l.split("\t") for l in call_log.read_text().splitlines()] if call_log.exists() else []
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
        assert verbs == [["stop"], ["rm"], ["volume", "inspect"], ["volume", "create"], ["run"]]
        assert calls[3] == ["volume", "create", "memora-t-data"]

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
        proc, calls, _ = _up(tmp_path, ['STORAGE_URI="d1://acct/db"'])
        assert proc.returncode != 0
        assert "admin token equals health token" in proc.stderr
        assert "run" not in [c[0] for c in calls]


@pytest.mark.skipif(not os.path.exists(SCRIPT), reason="deploy script not present")
def test_default_memory_is_the_measured_gate():
    """scripts/measure_memory_gate.py sets the default (§8 L2a memory gate)."""
    proc = subprocess.run(["bash", "-c", f'unset MEMORA_MEMORY; source "{SCRIPT}"; echo "$DEFAULT_MEMORY"'],
                          capture_output=True, text=True)
    assert proc.stdout.strip() == MEASURED_DEFAULT_MEMORY
