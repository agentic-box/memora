"""X1: local foreign-key parity with D1 (docs/local-primary-implementation.md
§9 (x)). A live primary (and the L9a shadow file) enforces foreign keys once
an audit of the existing data finds no orphan; plain local stores keep them
off; `local_primary.py fk-audit` reports orphans read-only."""
from __future__ import annotations

import ast
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from memora import backends, schema, storage, write_gate
from memora.backends import LocalSQLiteBackend, StoreLockedError
from memora.fk_audit import fk_orphans
from tests.l3_fakes import FakeReplica

REPO = Path(__file__).resolve().parent.parent
CHILDREN = ("memories_embeddings", "memories_crossrefs", "memories_events")


def _store(path: Path) -> Path:
    """A store with the full schema and one memory with every child kind."""
    b = LocalSQLiteBackend(path)
    conn = b.connect()
    try:
        schema.ensure_schema(conn)
        _family(conn, 1)
        _family(conn, 2)
        conn.commit()
    finally:
        conn.close()
    return path


def _family(conn, mid):
    conn.execute("INSERT INTO memories (id, content) VALUES (?, ?)", (mid, f"m{mid}"))
    conn.execute("INSERT INTO memories_embeddings (memory_id, embedding) VALUES (?, '[]')", (mid,))
    conn.execute("INSERT INTO memories_crossrefs (memory_id, related) VALUES (?, '[]')", (mid,))
    conn.execute("INSERT INTO memories_events (memory_id, tags) VALUES (?, '[]')", (mid,))


def _primary(path: Path, monkeypatch, name="p") -> LocalSQLiteBackend:
    b = LocalSQLiteBackend(path)
    b.store_name = name
    monkeypatch.setenv("MEMORA_REPLICAS", json.dumps({name: "d1://acct/db"}))
    return b


def _children(path: Path, mid: int):
    db = sqlite3.connect(path)
    try:
        return {t: db.execute(f"SELECT COUNT(*) FROM {t} WHERE memory_id = ?", (mid,)).fetchone()[0]
                for t in CHILDREN}
    finally:
        db.close()


def _orphan(path: Path, table="memories_embeddings", mid=777):
    db = sqlite3.connect(path)  # foreign keys off, as every local writer was before X1
    try:
        if table == "memories_embeddings":
            db.execute("INSERT INTO memories_embeddings (memory_id, embedding) VALUES (?, '[]')", (mid,))
        elif table == "memories_crossrefs":
            db.execute("INSERT INTO memories_crossrefs (memory_id, related) VALUES (?, '[]')", (mid,))
        else:
            db.execute("INSERT INTO memories_events (memory_id, tags) VALUES (?, '[]')", (mid,))
        db.commit()
    finally:
        db.close()


# ------------------------------------------------------------------ the audit

def test_the_audit_covers_every_table_schema_py_declares_references_memories(tmp_path):
    declared = set()
    src = (REPO / "memora" / "schema.py").read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "REFERENCES memories" in node.value:
            name = node.value.split("CREATE TABLE IF NOT EXISTS", 1)[1].split("(", 1)[0].strip()
            declared.add(name)
    assert declared == set(CHILDREN)
    out = fk_orphans(sqlite3.connect(_store(tmp_path / "s.db")))
    assert out["clean"] and set(out["checked"]) >= declared


@pytest.mark.parametrize("table", CHILDREN)
def test_the_audit_reports_counts_and_ids(tmp_path, table):
    path = _store(tmp_path / "s.db")
    _orphan(path, table, 777)
    _orphan(path, table, 778) if table == "memories_events" else None
    out = fk_orphans(sqlite3.connect(path))
    assert not out["clean"] and set(out["orphans"]) == {table}
    entry = out["orphans"][table]
    assert entry["parent"] == "memories" and entry["column"] == "memory_id"
    assert entry["count"] == (2 if table == "memories_events" else 1)
    assert entry["ids"] == ([777, 778] if table == "memories_events" else [777])


# ------------------------------------------------------------------ enforcement

def test_a_parent_delete_on_a_live_primary_cascades_exactly_like_d1(tmp_path, monkeypatch):
    path = _store(tmp_path / "p.db")
    replica = FakeReplica(tmp_path / "d1.db")  # foreign keys on, as D1
    d1 = replica._db()
    schema.ensure_schema(d1)
    _family(d1, 1)
    _family(d1, 2)
    d1.commit()
    b = _primary(path, monkeypatch)
    conn = b.connect()
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        conn.execute("DELETE FROM memories WHERE id = 1")
        conn.commit()
    finally:
        conn.close()
    d1.execute("DELETE FROM memories WHERE id = 1")
    d1.commit()
    d1_children = {t: d1.execute(f"SELECT COUNT(*) FROM {t} WHERE memory_id = 1").fetchone()[0] for t in CHILDREN}
    d1.close()
    assert _children(path, 1) == d1_children == {t: 0 for t in CHILDREN}
    assert _children(path, 2) == {t: 1 for t in CHILDREN}, "the other memory is untouched"


def test_a_plain_local_store_keeps_foreign_keys_off(tmp_path):
    path = _store(tmp_path / "plain.db")
    conn = LocalSQLiteBackend(path).connect()
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        conn.execute("DELETE FROM memories WHERE id = 1")
        conn.commit()
    finally:
        conn.close()
    assert _children(path, 1) == {t: 1 for t in CHILDREN}, "dark: no cascade on a plain store"


def test_an_orphan_in_a_live_primary_is_audited_with_foreign_keys_on_and_refused(tmp_path, monkeypatch):
    path = _store(tmp_path / "p.db")
    _orphan(path, "memories_crossrefs", 4242)
    b = _primary(path, monkeypatch)
    with pytest.raises(StoreLockedError, match=r"fk_audit: .*memories_crossrefs 1 .*4242"):
        b.connect()
    assert b.refused_reason.startswith("fk_audit:") and "local_primary.py fk-audit" in b.refused_reason
    with pytest.raises(StoreLockedError):
        b.connect_read_only()
    other = _primary(path, monkeypatch)  # another backend object, same file: the cached audit refuses it
    with pytest.raises(StoreLockedError, match="fk_audit"):
        other.connect()
    assert _children(path, 4242)["memories_crossrefs"] == 1, "nothing repaired automatically"


def test_startup_refuses_the_store_and_health_says_why(tmp_path, monkeypatch):
    from memora import health

    path = _store(tmp_path / "p.db")
    _orphan(path)
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"p": str(path), "q": str(tmp_path / "q.db")}))
    monkeypatch.setenv("MEMORA_REPLICAS", json.dumps({"p": "d1://acct/db"}))
    fenced = write_gate.fence_live_primaries()
    assert fenced["p"].startswith("fk_audit:") and "777" in fenced["p"]
    assert health._gate_fields("p")["refused"] == fenced["p"]
    assert write_gate.initialize_registry_gates()["p"] == {"state": "refused", "error": fenced["p"]}


def test_a_clean_live_primary_starts(tmp_path, monkeypatch):
    path = _store(tmp_path / "p.db")
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"p": str(path)}))
    monkeypatch.setenv("MEMORA_REPLICAS", json.dumps({"p": "d1://acct/db"}))
    assert write_gate.fence_live_primaries() == {"p": None}
    assert storage.backend_for("p").refused_reason is None


def test_the_default_store_refused_by_the_audit_stops_startup(tmp_path):
    path = _store(tmp_path / "p.db")
    _orphan(path)
    child = f"""
import os, sys, json
sys.path.insert(0, {str(REPO)!r})
os.environ['MEMORA_DATA_DIR'] = {str(tmp_path / 'data')!r}
os.environ['MEMORA_DATABASES'] = json.dumps({{"p": {str(path)!r}}})
os.environ['MEMORA_REPLICAS'] = json.dumps({{"p": "d1://acct/db"}})
os.environ['MEMORA_DEFAULT_DB'] = "p"
from memora import server
server._fence_live_primaries_or_exit()
print("started")
"""
    r = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "fk audit found orphans" in r.stderr and "fk_audit:" in r.stderr, r.stderr


def test_app_delete_memory_is_unchanged_on_a_live_primary(tmp_path, monkeypatch):
    """delete_memory removes the children itself; with foreign keys on the
    result is the same, and nothing raises."""
    results = {}
    for label, live in (("plain", False), ("primary", True)):
        path = _store(tmp_path / f"{label}.db")
        b = _primary(path, monkeypatch, name=label) if live else LocalSQLiteBackend(path)
        conn = b.connect()
        try:
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == int(live)
            assert storage.delete_memory(conn, 1) is True
            conn.commit()
        finally:
            conn.close()
        c = _children(path, 1)
        results[label] = {t: c[t] for t in ("memories_embeddings", "memories_crossrefs")}
        assert _children(path, 2) == {t: 1 for t in CHILDREN}
    assert results["plain"] == results["primary"] == {"memories_embeddings": 0, "memories_crossrefs": 0}


# ------------------------------------------------------------------ the shadow (L9a)

from tests.test_l9a_shadow import _assert_mirrored, _drain, world  # noqa: E402,F401  (fixture)


def test_the_shadow_replay_cascades_a_raw_parent_delete_like_d1(world):
    assert world.app.backend.enforce_foreign_keys
    conn = world.backend.connect()
    mid = conn.execute("INSERT INTO memories (content) VALUES ('p')").lastrowid
    conn.execute("INSERT INTO memories_embeddings (memory_id, embedding) VALUES (?, '[]')", (mid,))
    conn.execute("INSERT INTO memories_crossrefs (memory_id, related) VALUES (?, '[]')", (mid,))
    conn.close()
    _drain(world.app)
    conn = world.backend.connect()
    conn.execute("DELETE FROM memories WHERE id = ?", (mid,))  # D1 cascades; so must the replay
    conn.close()
    _drain(world.app)
    _assert_mirrored(world)
    assert world.app.dirty_reason is None
    assert _children(world.shadow_path, mid)["memories_embeddings"] == 0


def test_a_shadow_file_with_orphans_refuses_its_applier(world):
    from memora import shadow

    world.app.stop()
    shadow._reset_for_tests()
    backends.reset_fk_audits()
    _orphan(world.shadow_path, "memories_embeddings", 31337)
    shadow.start_shadow_appliers(reader_factory=lambda a, d, t: world.replica.reader(),
                                 replication_mode=lambda *a: "disabled")
    app = shadow.applier_for("s1")
    assert not app.alive and "fk_audit" in app.refused and "31337" in app.refused
    assert "fk_audit" in shadow.shadow_status("s1")["refused"]


# ------------------------------------------------------------------ the CLI

def _cli(*args):
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "local_primary.py"), *args],
                       capture_output=True, text=True, timeout=60)
    return r.returncode, (json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else None), r.stderr


def _snapshot_files(d: Path):
    return {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in d.iterdir()}


def test_cli_fk_audit_clean(tmp_path):
    path = _store(tmp_path / "s" / "p.db")
    before = _snapshot_files(path.parent)
    code, out, err = _cli("fk-audit", "p", "--store", str(path))
    assert code == 0 and out["ok"] is True and out["orphans"] == {}, err
    assert set(CHILDREN) <= set(out["checked"])
    assert _snapshot_files(path.parent) == before, "read-only: nothing created or changed"


def test_cli_fk_audit_reports_orphans_and_exits_5(tmp_path):
    path = _store(tmp_path / "s" / "p.db")
    _orphan(path, "memories_embeddings", 777)
    _orphan(path, "memories_events", 778)
    before = _snapshot_files(path.parent)
    code, out, err = _cli("fk-audit", "p", "--store", str(path))
    assert code == 5 and out["ok"] is False, err
    assert {t: (e["count"], e["ids"]) for t, e in out["orphans"].items()} == {
        "memories_embeddings": (1, [777]), "memories_events": (1, [778])}
    assert _snapshot_files(path.parent) == before


def test_cli_fk_audit_refuses_a_missing_store(tmp_path):
    code, out, _ = _cli("fk-audit", "p", "--store", str(tmp_path / "none.db"))
    assert code == 2 and not (tmp_path / "none.db").exists()
