"""scripts/migrate_data_volume.sh: the staged, verified /data copy both
launchers run inside a throwaway container (local-primary §8 L2a, review
7626 P0-1/P1-2). Here it runs directly on tmp dirs (MIGRATE_FROM/_TO)."""
import hashlib
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "migrate_data_volume.sh"
MARKER = ".memora-volume-source"


def _run(src, dst, source_id="3f" * 32, extra_env=None):
    env = dict(os.environ, MIGRATE_FROM=str(src), MIGRATE_TO=str(dst), **(extra_env or {}))
    # Invoked exactly as the launchers do: the text through sh -c.
    return subprocess.run(["sh", "-c", SCRIPT.read_text(), "migrate_data_volume", "migrate", source_id],
                          env=env, capture_output=True, text=True)


def _tree(root):
    """{relative path: sha256} of every file, control entries excluded."""
    out = {}
    for p in sorted(Path(root).rglob("*")):
        rel = p.relative_to(root).as_posix()
        if rel.split("/")[0] in (MARKER, ".memora-staging") or rel.startswith(".memora-previous-"):
            continue
        if p.is_file():
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


@pytest.fixture
def old_volume(tmp_path):
    """An instance's old /data: a WAL SQLite store with its sidecars held
    open, an intent journal with an open intent, and a freeze file."""
    src = tmp_path / "old"
    (src / "intent").mkdir(parents=True)
    (src / "freeze").mkdir()
    db = sqlite3.connect(src / "re.db")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT)")
    db.execute("INSERT INTO memories (content) VALUES ('kept')")
    db.commit()
    # Copy while the sidecars exist, as a stopped container leaves them.
    shadow = sqlite3.connect(src / "re.db")
    shadow.execute("SELECT 1").fetchone()
    (src / "intent" / "memora.jsonl").write_text('{"type":"intent","id":1,"sql":"INSERT"}\n')
    (src / "freeze" / "memora").write_text("")
    (src / ".hidden").write_text("dotfile")
    yield src
    shadow.close()
    db.close()


def test_first_copy_is_byte_identical_including_wal_sidecars_and_journal(tmp_path, old_volume):
    dst = tmp_path / "named"
    dst.mkdir()
    assert (old_volume / "re.db-wal").exists() and (old_volume / "re.db-shm").exists()
    r = _run(old_volume, dst)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().startswith("copied ")
    assert _tree(dst) == _tree(old_volume)
    marker = (dst / MARKER).read_text()
    assert f"source={'3f' * 32}" in marker and "digest=" in marker
    assert not (dst / ".memora-staging").exists()


def test_a_second_run_with_the_same_unchanged_source_is_a_no_op(tmp_path, old_volume):
    dst = tmp_path / "named"
    dst.mkdir()
    assert _run(old_volume, dst).returncode == 0
    (dst / "written-after").write_text("new data in the named volume")
    r = _run(old_volume, dst)
    assert r.returncode == 0 and r.stdout.strip().startswith("skip ")
    assert (dst / "written-after").exists(), "a skip must not touch the live volume"


def test_a_changed_source_is_recopied_and_the_live_content_kept_aside(tmp_path, old_volume):
    """Rollback: the old volume accrues writes; the next migration recopies,
    and whatever was live in the named volume is moved aside, not lost."""
    dst = tmp_path / "named"
    dst.mkdir()
    assert _run(old_volume, dst).returncode == 0
    (dst / "written-after").write_text("x")
    (old_volume / "intent" / "memora.jsonl").write_text("changed after rollback\n")
    r = _run(old_volume, dst)
    assert r.returncode == 0 and r.stdout.strip().startswith("copied ")
    assert _tree(dst) == _tree(old_volume)
    prev = [p for p in dst.iterdir() if p.name.startswith(".memora-previous-")]
    assert len(prev) == 1 and (prev[0] / "written-after").exists()


def test_a_different_source_is_recopied(tmp_path, old_volume):
    dst = tmp_path / "named"
    dst.mkdir()
    assert _run(old_volume, dst, source_id="a" * 64).returncode == 0
    r = _run(old_volume, dst, source_id="b" * 64)
    assert r.stdout.strip().startswith("copied ")
    assert "source=" + "b" * 64 in (dst / MARKER).read_text()


def test_a_leftover_staging_area_is_reset_not_overlaid(tmp_path, old_volume):
    """A failed partial copy left junk in staging; the rerun starts clean."""
    dst = tmp_path / "named"
    (dst / ".memora-staging").mkdir(parents=True)
    (dst / ".memora-staging" / "junk-from-a-failed-copy").write_text("x")
    (dst / ".memora-staging" / "re.db").write_text("truncated")
    r = _run(old_volume, dst)
    assert r.returncode == 0, r.stderr
    assert not (dst / "junk-from-a-failed-copy").exists()
    assert _tree(dst) == _tree(old_volume)


def test_a_failed_verify_leaves_the_live_volume_untouched(tmp_path, old_volume):
    dst = tmp_path / "named"
    dst.mkdir()
    (dst / "live").write_text("live data")
    fake = tmp_path / "bin"
    fake.mkdir()
    # cp that drops one file: the staged digest cannot match the source's.
    (fake / "cp").write_text('#!/bin/sh\n/bin/cp "$@" && rm -f "${3%/}/.hidden"\n')
    (fake / "cp").chmod(0o755)
    r = _run(old_volume, dst, extra_env={"PATH": f"{fake}:{os.environ['PATH']}"})
    assert r.returncode == 1 and "verify failed" in r.stderr
    assert (dst / "live").read_text() == "live data"
    assert not (dst / MARKER).exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads unreadable files")
def test_an_unreadable_source_file_fails_closed(tmp_path, old_volume):
    """Review 7637 P1-2: no pipeline may hide a failed hash; a file that
    cannot be read stops the migration before the live volume changes."""
    dst = tmp_path / "named"
    dst.mkdir()
    (dst / "live").write_text("live data")
    secret = old_volume / "intent" / "unreadable.jsonl"
    secret.write_text("x")
    secret.chmod(0)
    try:
        r = _run(old_volume, dst)
    finally:
        secret.chmod(0o600)
    assert r.returncode != 0
    assert (dst / "live").read_text() == "live data" and not (dst / MARKER).exists()


def test_a_failed_hash_stops_the_migration_even_when_the_copy_works(tmp_path, old_volume):
    """The digest itself must fail closed: a sha256sum that fails on one file
    (while cp can still copy it) must stop the run, not drop the file from
    both digests and let them match."""
    dst = tmp_path / "named"
    dst.mkdir()
    (dst / "live").write_text("live data")
    (old_volume / "flaky.bin").write_text("x")
    fake = tmp_path / "bin"
    fake.mkdir()
    real = subprocess.run(["sh", "-c", "command -v sha256sum"], capture_output=True, text=True).stdout.strip()
    (fake / "sha256sum").write_text(
        f'#!/bin/sh\ncase "$*" in *flaky*) echo "sha256sum: read error" >&2; exit 1 ;; esac\nexec "{real}" "$@"\n')
    (fake / "sha256sum").chmod(0o755)
    r = _run(old_volume, dst, extra_env={"PATH": f"{fake}:{os.environ['PATH']}"})
    assert r.returncode != 0 and "cannot hash" in r.stderr
    assert (dst / "live").read_text() == "live data" and not (dst / MARKER).exists()
