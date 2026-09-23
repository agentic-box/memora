"""scripts/preview_backfill_47.py: a READ-ONLY preview with an approval list."""

import hashlib
import json
import os

import pytest

import memora
import memora.storage as storage
from scripts import preview_backfill_47 as preview

PROJECTS = ["memora", "clmux", "acebar", "pi"]
CLMUX_TEXT = "clmux TUI sidebar tmux pane workspace switching in the clmux daemon clmuxd"
NEUTRAL_TEXT = "A neutral note about scheduling that names no project at all."


@pytest.fixture
def store(local_db, monkeypatch):
    monkeypatch.setenv("MEMORA_PROJECTS", json.dumps(PROJECTS))
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    return storage.STORAGE_BACKEND.db_path


def _row(conn, content, *, tags, metadata):
    """Insert as the legacy write path left it (section already assigned)."""
    cur = conn.execute("INSERT INTO memories (content, metadata, tags) VALUES (?, ?, ?)",
                       (content, json.dumps(metadata), json.dumps(tags)))
    return cur.lastrowid


def _seed(conn):
    ids = {}
    ids["legacy_issue"] = _row(conn, "**clmux: workspace rename does not work** " + CLMUX_TEXT,
                               tags=["memora/issues"], metadata={"type": "issue", "section": "clmux"})
    ids["conflict"] = _row(conn, "Findings about the socket server. " + CLMUX_TEXT,
                           tags=["memora/issues", "memora/absorb"], metadata={"type": "issue", "section": "clmux"})
    ids["keyword"] = _row(conn, "Observability matters because " + CLMUX_TEXT,
                          tags=["architecture"], metadata={"section": "clmux", "subsection": "architecture"})
    ids["typed_only"] = _row(conn, NEUTRAL_TEXT, tags=["memora/todos"],
                             metadata={"type": "todo", "section": "memora"})
    ids["unknown"] = _row(conn, NEUTRAL_TEXT + " Second.", tags=[], metadata={"section": "memora"})
    ids["explicit"] = _row(conn, CLMUX_TEXT, tags=["clmux/tui"], metadata={"section": "clmux"})
    conn.commit()
    return ids


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_preview_groups_proposals_and_needs_human(store, tmp_path):
    with storage.connect() as conn:
        ids = _seed(conn)
    outdir = tmp_path / "out"
    outdir.mkdir()
    before = (_digest(store), sorted(os.listdir(store.parent)))
    out = outdir / "preview.json"
    md = outdir / "preview.md"
    assert preview.main(["--out", str(out), "--markdown", str(md)]) == 0
    # Nothing written, nothing created next to the store.
    assert (_digest(store), sorted(os.listdir(store.parent))) == before

    data = json.loads(out.read_text())
    rows = {r["id"]: r for group in ("contradictions", "keyword_only") for r in data[group]}
    assert all(r["approved"] is False for r in rows.values())
    assert ids["explicit"] not in rows  # already consistent: not listed

    legacy = rows[ids["legacy_issue"]]
    assert legacy in data["contradictions"] and legacy["status"] == "proposed"
    assert legacy["proposal"]["set_metadata_project"] == "clmux"
    assert legacy["proposal"]["retag_typed"] == {"memora/issues": "clmux/issues"}

    conflict = rows[ids["conflict"]]
    assert conflict in data["contradictions"] and conflict["status"] == "needs-human"
    assert "disagrees" in conflict["reason"]

    keyword = rows[ids["keyword"]]
    assert keyword in data["keyword_only"] and keyword["status"] == "proposed"
    assert keyword["evidence"] == [{"source": "content keywords", "project": "clmux"}]
    assert keyword["proposal"]["set_metadata_project"] == "clmux"
    assert keyword["proposal"]["alternative_marker_tag"] == "clmux/architecture"

    typed_only = rows[ids["typed_only"]]
    assert typed_only in data["keyword_only"] and typed_only["status"] == "needs-human"
    assert "typed tag" in typed_only["reason"]

    unknown = rows[ids["unknown"]]
    assert unknown["status"] == "needs-human" and "no non-typed source" in unknown["reason"]

    s = data["summary"]
    assert s["contradictions"] == {"total": 2, "proposed": 1, "needs_human": 1}
    assert s["keyword_only"] == {"total": 3, "proposed": 1, "needs_human": 2}
    assert "| id | status |" in md.read_text()


def test_preview_skips_import_pending_rows(store, tmp_path):
    with storage.connect() as conn:
        _row(conn, CLMUX_TEXT, tags=["memora/issues"],
             metadata={"type": "issue", "section": "clmux", "import_attempt": "x:1:0"})
        conn.commit()
    out = tmp_path / "p.json"
    preview.main(["--out", str(out)])
    data = json.loads(out.read_text())
    assert data["contradictions"] == [] and data["keyword_only"] == []


def test_preview_refuses_a_local_wal_store_in_use_and_creates_nothing(store, tmp_path, monkeypatch):
    import sqlite3

    writer = sqlite3.connect(store)  # another process's writer, in effect
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("INSERT INTO memories (content) VALUES ('x')")
    writer.commit()
    try:
        before = sorted(os.listdir(store.parent))
        assert f"{store.name}-wal" in before and f"{store.name}-shm" in before
        with pytest.raises(SystemExit) as exc:
            preview.main(["--out", str(tmp_path / "never.json")])
        assert exc.value.code != 0 and "store in use by a writer" in str(exc.value.code)
        assert sorted(os.listdir(store.parent)) == before and not (tmp_path / "never.json").exists()
    finally:
        writer.close()
    # Writer gone (no sidecars): read immutably, nothing created.
    assert sorted(os.listdir(store.parent)) == [store.name]
    out = tmp_path / "out"
    out.mkdir()
    assert preview.main(["--out", str(out / "p.json")]) == 0
    assert sorted(os.listdir(store.parent)) == [store.name, "out"]


def test_preview_reads_only_the_header(store, tmp_path, monkeypatch):
    from pathlib import Path

    def no_whole_file(self):
        raise AssertionError("read the whole database")

    monkeypatch.setattr(Path, "read_bytes", no_whole_file)
    out = tmp_path / "out"
    out.mkdir()
    assert preview.main(["--out", str(out / "p.json")]) == 0


def test_preview_refuses_an_s3_cloud_store_before_building_any_backend(tmp_path, monkeypatch):
    """From a FRESH home and registry: refused by the configured URI, before
    any backend exists -- no cache directory, no S3 client, no output."""
    from memora.backends import CloudSQLiteBackend

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"cloudy": "s3://some-bucket/memora/memories.db",
                                                       "local": str(tmp_path / "l.db")}))
    monkeypatch.setenv("MEMORA_DEFAULT_DB", "local")
    monkeypatch.setenv("MEMORA_PROJECTS", json.dumps(PROJECTS))
    storage._registry_cache = None
    storage._registry_source = None

    def no_backend(self, *a, **k):
        raise AssertionError("a cloud backend was constructed")

    monkeypatch.setattr(CloudSQLiteBackend, "__init__", no_backend)
    for name in ("connect", "sync_before_use"):
        monkeypatch.setattr(CloudSQLiteBackend, name, lambda self, *a, **k: pytest.fail(name))
    out = tmp_path / "p.json"
    with pytest.raises(SystemExit) as exc:
        preview.main(["--out", str(out), "--db", "cloudy"])
    assert exc.value.code != 0 and "local SQLite and D1 stores only" in str(exc.value.code)
    assert not out.exists() and not (home / ".cache").exists()
    storage._registry_cache = None


def test_the_script_refuses_an_s3_storage_uri_before_importing_memora(tmp_path):
    """MEMORA_STORAGE_URI=s3://... builds the cloud backend at memora import:
    the script must refuse before that import."""
    import subprocess
    import sys
    from pathlib import Path

    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MEMORA_", "AWS_"))}
    env.update({"HOME": str(home), "MEMORA_STORAGE_URI": "s3://some-bucket/memora/memories.db"})
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run([sys.executable, str(root / "scripts" / "preview_backfill_47.py"),
                           "--out", str(tmp_path / "p.json")], env=env, capture_output=True, text=True)
    assert proc.returncode != 0 and "local SQLite and D1 stores only" in proc.stderr
    assert not (tmp_path / "p.json").exists() and not (home / ".cache").exists()



def _combined_env_run(tmp_path, argv_code):
    """Run python code in a fresh process with a registry-selected local store
    AND MEMORA_STORAGE_URI=s3://..., under a guard that aborts if a
    CloudSQLiteBackend is ever constructed."""
    import sqlite3 as _sqlite3
    import subprocess
    import sys
    from pathlib import Path

    home = tmp_path / "home"
    home.mkdir()
    local = tmp_path / "l.db"
    conn = _sqlite3.connect(local)
    conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT, metadata TEXT, tags TEXT, updated_at TEXT)")
    conn.commit()
    conn.close()
    guard = tmp_path / "guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(
        "import memora.backends as b\n"
        "def _no(self, *a, **k):\n"
        "    raise SystemExit('CloudSQLiteBackend constructed')\n"
        "b.CloudSQLiteBackend.__init__ = _no\n")
    root = Path(__file__).resolve().parents[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MEMORA_", "AWS_"))}
    env.update({
        "HOME": str(home),
        "PYTHONPATH": os.pathsep.join([str(guard), str(root)]),
        "MEMORA_DATABASES": json.dumps({"local": str(local)}),
        "MEMORA_STORAGE_URI": "s3://some-bucket/memora/memories.db",
    })
    out = tmp_path / "p.json"
    proc = subprocess.run([sys.executable, "-c", argv_code.format(out=str(out))],
                          env=env, capture_output=True, text=True, cwd=str(root))
    return proc, out, home


def test_importing_the_module_then_calling_main_constructs_no_cloud_backend(tmp_path):
    proc, out, home = _combined_env_run(tmp_path, (
        "import sys\n"
        "from scripts import preview_backfill_47 as p\n"
        "assert 'memora.storage' not in sys.modules, 'importing the module imported memora'\n"
        "import os\n"
        "rc = p.main(['--out', {out!r}, '--db', 'local'])\n"
        "assert os.environ['MEMORA_STORAGE_URI'] == 's3://some-bucket/memora/memories.db', 'env left changed'\n"
        "sys.exit(rc)\n"))
    assert proc.returncode == 0, proc.stderr
    assert out.exists() and not (home / ".cache").exists()
    assert "CloudSQLiteBackend constructed" not in proc.stderr + proc.stdout


def test_the_script_ignores_an_s3_storage_uri_when_the_registry_selects_a_local_store(tmp_path):
    """Registry selects a local store AND MEMORA_STORAGE_URI=s3://... (which
    memora.storage would build at import): the import-time backend is pinned
    to the selected store; no cloud backend, no ~/.cache."""
    import sqlite3 as _sqlite3
    import subprocess
    import sys
    from pathlib import Path

    home = tmp_path / "home"
    home.mkdir()
    local = tmp_path / "l.db"
    conn = _sqlite3.connect(local)
    conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT, metadata TEXT, tags TEXT, updated_at TEXT)")
    conn.commit()
    conn.close()
    guard = tmp_path / "guard"
    guard.mkdir()
    # Fails the run if a CloudSQLiteBackend is ever constructed in the subprocess.
    (guard / "sitecustomize.py").write_text(
        "import memora.backends as b\n"
        "def _no(self, *a, **k):\n"
        "    raise SystemExit('CloudSQLiteBackend constructed')\n"
        "b.CloudSQLiteBackend.__init__ = _no\n")
    root = Path(__file__).resolve().parents[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MEMORA_", "AWS_"))}
    env.update({
        "HOME": str(home),
        "PYTHONPATH": os.pathsep.join([str(guard), str(root)]),
        "MEMORA_DATABASES": json.dumps({"local": str(local)}),
        "MEMORA_STORAGE_URI": "s3://some-bucket/memora/memories.db",
    })
    out = tmp_path / "p.json"
    proc = subprocess.run([sys.executable, str(root / "scripts" / "preview_backfill_47.py"),
                           "--out", str(out), "--db", "local"], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert out.exists() and not (home / ".cache").exists()
    assert "CloudSQLiteBackend constructed" not in proc.stderr + proc.stdout


def test_carry_approval_copies_only_identical_proposals(store, tmp_path, capsys):
    with storage.connect() as conn:
        ids = _seed(conn)
    outdir = tmp_path / "out"
    outdir.mkdir()
    fresh = outdir / "fresh.json"
    assert preview.main(["--out", str(fresh)]) == 0
    old = json.loads(fresh.read_text())
    rows = {r["id"]: r for g in ("contradictions", "keyword_only") for r in old[g]}
    # OLD approved the two proposed rows; for the keyword row it recorded a
    # different section, and it approved a row that is now needs-human.
    rows[ids["legacy_issue"]]["approved"] = True
    rows[ids["keyword"]]["approved"] = True
    rows[ids["keyword"]]["stored"]["section"] = "memora"
    rows[ids["typed_only"]].update(approved=True, status="proposed",
                                   proposal={"set_metadata_project": "memora", "retag_typed": {}})
    old["approval"] = {"approved_by": "user", "at": "2026-09-23", "rule": "proposed rows"}
    old_path = outdir / "old.json"
    old_path.write_text(json.dumps(old))
    new = outdir / "new.json"
    capsys.readouterr()
    assert preview.main(["--out", str(new), "--carry-approval", str(old_path)]) == 0
    table = capsys.readouterr().out
    data = json.loads(new.read_text())
    approved = {r["id"] for g in ("contradictions", "keyword_only") for r in data[g] if r["approved"]}
    assert approved == {ids["legacy_issue"]}
    assert f"#{ids['legacy_issue']}\tcarried" in table
    assert f"#{ids['keyword']}\tdropped\tstored section differs" in table
    assert f"#{ids['typed_only']}\tdropped\tnew status 'needs-human'" in table
    assert data["approval"]["carried"] == 1 and data["approval"]["dropped"] == 2
    assert data["approval"]["approved_by"] == "user" and data["approval"]["carried_from"] == str(old_path)
    # The new preview carries the full fingerprints the apply step requires.
    row = next(r for r in data["contradictions"] if r["id"] == ids["legacy_issue"])
    assert row["stored"]["content_sha256"] and row["stored"]["metadata_sha256"]


def test_carry_approval_refuses_an_old_file_without_approval(store, tmp_path):
    with storage.connect() as conn:
        _seed(conn)
    outdir = tmp_path / "out"
    outdir.mkdir()
    old = outdir / "old.json"
    old.write_text(json.dumps({"contradictions": [], "keyword_only": []}))
    with pytest.raises(SystemExit, match="no approval block"):
        preview.main(["--out", str(outdir / "n.json"), "--carry-approval", str(old)])


@pytest.mark.parametrize("field", ["group", "evidence", "preview", "tags", "type", "subsection", "metadata_project"])
def test_carry_approval_names_every_differing_field(store, tmp_path, capsys, field):
    with storage.connect() as conn:
        ids = _seed(conn)
    outdir = tmp_path / "out"
    outdir.mkdir()
    fresh = outdir / "fresh.json"
    assert preview.main(["--out", str(fresh)]) == 0
    old = json.loads(fresh.read_text())
    row = next(r for r in old["contradictions"] if r["id"] == ids["legacy_issue"])
    row["approved"] = True
    if field == "group":
        old["contradictions"].remove(row)
        old["keyword_only"].append(row)
        expected = "group keyword_only -> contradictions"
    elif field == "evidence":
        row["evidence"] = [{"source": "section", "project": "memora"}]
        expected = "evidence differs"
    elif field == "preview":
        row["preview"] = "something else"
        expected = "content preview differs"
    else:
        row["stored"][field] = "changed"
        expected = f"stored {field} differs"
    old["approval"] = {"approved_by": "user", "at": "2026-09-23", "rule": "r"}
    (outdir / "old.json").write_text(json.dumps(old))
    capsys.readouterr()
    preview.main(["--out", str(outdir / "new.json"), "--carry-approval", str(outdir / "old.json")])
    assert f"#{ids['legacy_issue']}\tdropped\t{expected}" in capsys.readouterr().out
