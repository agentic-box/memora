"""REL1 (review 7758): credentials from mounted, read-only token files.

FOO_FILE names a file holding FOO; the D1 backend, the D1 readers, the
replicator and the operator tool read it; the server refuses to start on an
unusable file or on FOO and FOO_FILE both set. No value is ever printed.
"""

from __future__ import annotations

import os

import pytest

from memora import secret_files
from memora.secret_files import SecretFileError, check_secret_files, read_secret_file, secret

VALUE = "tok-" + "Q7" * 20  # distinctive, so a leak in any message is found
VARS = ("MEMORA_D1_READ_TOKEN", "MEMORA_D1_REPLICATOR_TOKEN", "CLOUDFLARE_API_TOKEN", "CF_API_TOKEN",
        "MEMORA_GRAPH_TOKEN")


@pytest.fixture
def env(monkeypatch):
    for v in VARS:
        monkeypatch.delenv(v, raising=False)
        monkeypatch.delenv(v + "_FILE", raising=False)
    return monkeypatch


def token_file(tmp_path, mode=0o600, content=VALUE + "\n", name="t.token"):
    p = tmp_path / name
    p.write_text(content)
    os.chmod(p, mode)
    return str(p)


class TestFileRule:
    def test_reads_and_strips(self, tmp_path):
        assert read_secret_file("X", token_file(tmp_path, content=f"  {VALUE}\n\n")) == VALUE

    def test_0400_is_accepted(self, tmp_path):
        assert read_secret_file("X", token_file(tmp_path, mode=0o400)) == VALUE

    @pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o620, 0o602, 0o610])
    def test_group_or_other_bits_refused(self, tmp_path, mode):
        with pytest.raises(SecretFileError, match="group/other accessible"):
            read_secret_file("X", token_file(tmp_path, mode=mode))

    def test_symlink_refused_not_followed(self, tmp_path):
        target = token_file(tmp_path)
        link = tmp_path / "link.token"
        link.symlink_to(target)
        with pytest.raises(SecretFileError, match="is a symlink"):
            read_secret_file("X", str(link))

    def test_directory_refused(self, tmp_path):
        d = tmp_path / "dir"
        d.mkdir(mode=0o700)
        with pytest.raises(SecretFileError, match="not a regular file"):
            read_secret_file("X", str(d))

    def test_relative_path_refused(self, tmp_path, monkeypatch):
        token_file(tmp_path)
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SecretFileError, match="not an absolute path"):
            read_secret_file("X", "t.token")

    def test_missing_refused(self, tmp_path):
        with pytest.raises(SecretFileError, match="X: "):
            read_secret_file("X", str(tmp_path / "absent"))

    def test_empty_refused(self, tmp_path):
        with pytest.raises(SecretFileError, match="empty"):
            read_secret_file("X", token_file(tmp_path, content=" \n"))

    @pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root reads a 0000 file")
    def test_unreadable_refused(self, tmp_path):
        with pytest.raises(SecretFileError, match="not readable by this process"):
            read_secret_file("X", token_file(tmp_path, mode=0o000))

    def test_owner_is_not_checked(self, tmp_path, monkeypatch):
        # a container maps the host uid elsewhere: only "readable" is required
        monkeypatch.setattr(os, "getuid", lambda: 4242, raising=False)
        assert read_secret_file("X", token_file(tmp_path)) == VALUE

    def test_no_message_carries_the_value(self, tmp_path):
        for mode in (0o640, 0o000):
            try:
                read_secret_file("X", token_file(tmp_path, mode=mode, name=f"{mode}.token"))
            except SecretFileError as exc:
                assert VALUE not in str(exc)


class TestSecret:
    def test_env_value_unchanged(self, env):
        env.setenv("MEMORA_D1_READ_TOKEN", f" {VALUE} ")
        assert secret("MEMORA_D1_READ_TOKEN") == VALUE

    def test_file_value(self, env, tmp_path):
        env.setenv("MEMORA_D1_READ_TOKEN_FILE", token_file(tmp_path))
        assert secret("MEMORA_D1_READ_TOKEN") == VALUE

    def test_neither_is_empty(self, env):
        assert secret("MEMORA_D1_READ_TOKEN") == ""

    @pytest.mark.parametrize("var", ["MEMORA_D1_READ_TOKEN", "MEMORA_D1_REPLICATOR_TOKEN", "CLOUDFLARE_API_TOKEN"])
    def test_both_set_refused(self, env, tmp_path, var):
        env.setenv(var, VALUE)
        env.setenv(var + "_FILE", token_file(tmp_path))
        with pytest.raises(SecretFileError, match="both set") as exc:
            secret(var)
        assert VALUE not in str(exc.value)

    def test_cloudflare_file_with_cf_alias_refused(self, env, tmp_path):
        env.setenv("CF_API_TOKEN", VALUE)
        env.setenv("CLOUDFLARE_API_TOKEN_FILE", token_file(tmp_path))
        with pytest.raises(SecretFileError, match="CF_API_TOKEN"):
            secret("CLOUDFLARE_API_TOKEN")

    def test_check_covers_all_three(self, env, tmp_path):
        assert set(secret_files.FILE_BACKED) == {"MEMORA_D1_READ_TOKEN", "MEMORA_D1_REPLICATOR_TOKEN",
                                                 "CLOUDFLARE_API_TOKEN", "MEMORA_GRAPH_TOKEN"}
        env.setenv("MEMORA_D1_REPLICATOR_TOKEN_FILE", token_file(tmp_path, mode=0o640))
        with pytest.raises(SecretFileError, match="MEMORA_D1_REPLICATOR_TOKEN"):
            check_secret_files()

    def test_check_reports_set(self, env, tmp_path):
        env.setenv("MEMORA_D1_READ_TOKEN_FILE", token_file(tmp_path))
        assert check_secret_files() == {"MEMORA_D1_READ_TOKEN": True, "MEMORA_D1_REPLICATOR_TOKEN": False,
                                        "CLOUDFLARE_API_TOKEN": False, "MEMORA_GRAPH_TOKEN": False}


class TestConsumersReadTheFile:
    def test_d1_backend(self, env, tmp_path):
        from memora.backends import D1Backend, parse_backend_uri

        env.setenv("CLOUDFLARE_API_TOKEN_FILE", token_file(tmp_path))
        backend = parse_backend_uri("d1://acct/db")
        assert isinstance(backend, D1Backend) and backend.api_token == VALUE

    def test_d1_backend_refuses_both(self, env, tmp_path):
        from memora.backends import parse_backend_uri

        env.setenv("CLOUDFLARE_API_TOKEN", VALUE)
        env.setenv("CLOUDFLARE_API_TOKEN_FILE", token_file(tmp_path))
        with pytest.raises(SecretFileError):
            parse_backend_uri("d1://acct/db")

    def test_select_only_reader(self, env, tmp_path):
        from memora.backends import D1SelectOnlyConnection

        env.setenv("MEMORA_D1_READ_TOKEN_FILE", token_file(tmp_path))
        assert D1SelectOnlyConnection.from_env("acct", "db")._api_token == VALUE

    def test_replicator_writer(self, env, tmp_path):
        from memora.replicator import _writer_for

        env.setenv("MEMORA_D1_REPLICATOR_TOKEN_FILE", token_file(tmp_path))
        assert _writer_for("d1://acct/db").api_token == VALUE

    def test_replicator_writer_refuses_a_bad_file(self, env, tmp_path):
        from memora.replicator import _writer_for

        env.setenv("MEMORA_D1_REPLICATOR_TOKEN_FILE", token_file(tmp_path, mode=0o644))
        with pytest.raises(SecretFileError):
            _writer_for("d1://acct/db")

    def test_replicator_writer_never_takes_the_cloudflare_file(self, env, tmp_path):
        from memora.replicator import ReplicatorConfigError, _writer_for

        env.setenv("CLOUDFLARE_API_TOKEN_FILE", token_file(tmp_path))
        with pytest.raises(ReplicatorConfigError):
            _writer_for("d1://acct/db")

    def test_operator_tool_read_token(self, env, tmp_path):
        from memora.local_primary import L5Refused, read_token

        env.setenv("MEMORA_D1_READ_TOKEN_FILE", token_file(tmp_path))
        assert read_token() == VALUE
        env.setenv("MEMORA_D1_READ_TOKEN", "other")
        with pytest.raises(L5Refused, match="both set"):
            read_token()


class TestServerStartup:
    @pytest.mark.parametrize("case", ["both", "group-readable", "symlink"])
    def test_main_refuses_to_start(self, env, tmp_path, capsys, case):
        from memora import server

        path = token_file(tmp_path, mode=0o640 if case == "group-readable" else 0o600)
        if case == "symlink":
            (tmp_path / "l.token").symlink_to(path)
            path = str(tmp_path / "l.token")
        env.setenv("MEMORA_D1_READ_TOKEN_FILE", path)
        if case == "both":
            env.setenv("MEMORA_D1_READ_TOKEN", VALUE)
        with pytest.raises(SystemExit) as exc:
            server.main(["--no-graph"])
        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert "MEMORA_D1_READ_TOKEN" in captured.err
        assert VALUE not in captured.err + captured.out
