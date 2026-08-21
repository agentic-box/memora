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
