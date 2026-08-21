"""Deployment-script behaviour that #996 depends on: the health token.

codex review: the token had no automated coverage at all, and its validator
was line-based -- a good first line followed by junk was accepted, and the
embedded newlines would have been written into curl's config file.
"""
import os
import subprocess

import pytest

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
        assert calls == ["stop", "rm", "run"], f"runtime calls escaped the fake: {calls}"
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
