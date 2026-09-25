"""scripts/mint_api_token.sh (API1): mint one /api/v1 token -- the plain token
to a new 0600 file, its sha256 -> stores into the server's tokens file."""
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from memora import api_v1

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "mint_api_token.sh"


@pytest.fixture
def secrets(tmp_path):
    d = tmp_path / "memora-lp"
    d.mkdir(mode=0o700)
    return d


def _mint(secrets, out, *args):
    return subprocess.run(["bash", str(SCRIPT), "--dir", str(secrets), "--out", str(out), *args],
                          capture_output=True, text=True, timeout=30)


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_mints_a_token_and_its_digest_and_prints_nothing(secrets, tmp_path):
    out = tmp_path / "clmuxd.token"
    r = _mint(secrets, out, "--stores", "memora,alpha")
    assert r.returncode == 0, r.stderr
    assert r.stdout == "" and r.stderr == ""
    token = out.read_text().strip()
    assert len(token) >= 40 and _mode(out) == 0o600
    table = json.loads((secrets / "api-tokens.json").read_text())
    assert table == {hashlib.sha256(token.encode()).hexdigest(): ["alpha", "memora"]}
    assert _mode(secrets / "api-tokens.json") == 0o600


def test_the_server_reads_what_it_writes(secrets, tmp_path, monkeypatch):
    # read_token_file walks the parents up to $HOME; put the scratch dir under
    # it so the test does not depend on TMPDIR's (often world-writable) parents.
    monkeypatch.setenv("HOME", str(tmp_path))
    out = tmp_path / "t.token"
    assert _mint(secrets, out, "--stores", "memora").returncode == 0
    table = api_v1.load_token_table(str(secrets / "api-tokens.json"))
    assert table == {hashlib.sha256(out.read_text().strip().encode()).hexdigest(): frozenset({"memora"})}


def test_a_second_token_is_added_and_the_first_kept(secrets, tmp_path):
    assert _mint(secrets, tmp_path / "a.token", "--stores", "memora").returncode == 0
    assert _mint(secrets, tmp_path / "b.token", "--stores", "gamma").returncode == 0
    table = json.loads((secrets / "api-tokens.json").read_text())
    assert len(table) == 2 and sorted(table.values()) == [["gamma"], ["memora"]]


def test_an_existing_out_file_is_refused_and_nothing_written(secrets, tmp_path):
    out = tmp_path / "exists.token"
    out.write_text("keep")
    r = _mint(secrets, out, "--stores", "memora")
    assert r.returncode == 2 and "already exists" in r.stderr
    assert out.read_text() == "keep" and not (secrets / "api-tokens.json").exists()


@pytest.mark.parametrize("stores", ["", "Bad Store", "ok,../x", "a" * 65])
def test_bad_store_names_are_refused(secrets, tmp_path, stores):
    r = _mint(secrets, tmp_path / "t.token", "--stores", stores)
    assert r.returncode == 2 and not (tmp_path / "t.token").exists()


def test_a_loose_or_foreign_tokens_file_is_refused(secrets, tmp_path):
    f = secrets / "api-tokens.json"
    f.write_text("{}")
    os.chmod(f, 0o644)
    r = _mint(secrets, tmp_path / "t.token", "--stores", "memora")
    assert r.returncode == 2 and "mode 0600" in r.stderr and not (tmp_path / "t.token").exists()


def test_a_symlinked_tokens_file_is_refused(secrets, tmp_path):
    real = tmp_path / "real.json"
    real.write_text("{}")
    os.chmod(real, 0o600)
    (secrets / "api-tokens.json").symlink_to(real)
    r = _mint(secrets, tmp_path / "t.token", "--stores", "memora")
    assert r.returncode == 2 and real.read_text() == "{}"


def test_the_4k_limit_is_kept(secrets, tmp_path):
    f = secrets / "api-tokens.json"
    f.write_text(json.dumps({hashlib.sha256(str(i).encode()).hexdigest(): ["memora"] for i in range(45)}))
    os.chmod(f, 0o600)
    before = f.read_text()
    r = _mint(secrets, tmp_path / "t.token", "--stores", "memora")
    assert r.returncode == 2 and "4096" in r.stderr
    assert f.read_text() == before and not (tmp_path / "t.token").exists()


def test_a_missing_directory_is_refused(tmp_path):
    r = subprocess.run(["bash", str(SCRIPT), "--dir", str(tmp_path / "nope"), "--out", str(tmp_path / "t"),
                        "--stores", "memora"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "does not exist" in r.stderr
