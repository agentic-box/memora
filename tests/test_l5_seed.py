"""L5 piece b: seed, FTS rebuild, sequence high-water (local and D1),
snapshot, volume alert, §9 (k) (docs/local-primary-implementation.md §4).

Offline: D1 is a FakeReplica read through the real D1SelectOnlyConnection;
the operator's D1 writer is OperatorD1Writer over a double whose _send runs
the statement on the FakeReplica file (or rejects it); R2 is FsR2.
"""

from __future__ import annotations

import gzip
import json
import os
import sqlite3
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

import pytest

from memora import local_primary as lp
from memora import schema, storage
from memora.backends import D1DefiniteError, LocalSQLiteBackend
from tests.l3_fakes import FakeReplica
from tests.test_l5_export import DB, FakeBarrier, FreezeServer, make_deps, replica_exec, seed_replica, token_args

REPO = Path(__file__).resolve().parent.parent
HARNESS = REPO / "tests" / "l5_cli_harness.py"
URI = "d1://acct/replica-db"


@pytest.fixture
def replica(tmp_path):
    r = FakeReplica(tmp_path / "d1.db")
    seed_replica(r)
    return r


@pytest.fixture
def receipt(replica, tmp_path):
    return lp.export(DB, make_deps(replica, tmp_path), tmp_path / "exports")


def _rows(path, sql, params=()):
    db = sqlite3.connect(path)
    try:
        return db.execute(sql, params).fetchall()
    finally:
        db.close()


# ---------------------------------------------------------------- seed

def test_seed_builds_a_verified_store_with_sync_installed(replica, tmp_path, receipt):
    out = tmp_path / "new" / "nested" / f"{DB}.db"  # §9 (k): the parent does not exist yet
    barrier = FakeBarrier(frozen=True)
    rep = lp.seed(DB, str(receipt), out, make_deps(replica, tmp_path, barrier), replica_uri=URI)
    r = json.loads(receipt.read_text())
    assert out.is_file() and rep["out"] == str(out)
    data = sorted(t for t in r["tables"] if t != "sqlite_sequence")
    assert lp.local_stats(out, data) == {t: r["tables"][t] for t in data}
    state = _rows(out, "SELECT replica_uri, last_acked_seq, d1_epoch_expected FROM sync_state")
    assert state == [(URI, 0, r["epoch"])]
    assert _rows(out, "SELECT COUNT(*) FROM sync_outbox") == [(0,)]
    assert _rows(out, "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'trg_sync_%'")[0][0] > 0
    assert _rows(out, "SELECT COUNT(*) FROM memories_fts") == [(3,)] == [(rep["fts_rows"],)]
    assert not list(out.parent.glob("*.seed-partial*"))
    # under the freeze already in place: required at the start, re-checked at
    # every boundary, never placed or lifted (7621 P1-2)
    assert barrier.calls == ["check before the seed", "check before reading D1's sequences",
                             "check after reading D1's sequences", "check before placing the seeded store"]
    assert barrier.frozen


def test_seed_without_the_freeze_in_place_is_refused(replica, tmp_path, receipt):
    barrier = FakeBarrier(frozen=False)
    out = tmp_path / "x" / f"{DB}.db"
    with pytest.raises(lp.L5Refused, match="not frozen before the seed"):
        lp.seed(DB, str(receipt), out, make_deps(replica, tmp_path, barrier), replica_uri=URI)
    assert not out.parent.exists() and "freeze" not in barrier.calls


def test_seed_refuses_a_receipt_of_another_d1_database(replica, tmp_path, receipt):
    deps = make_deps(replica, tmp_path)
    deps.database_id = "other-db"
    with pytest.raises(lp.L5Refused, match="another D1 database"):
        lp.seed(DB, str(receipt), tmp_path / f"{DB}.db", deps, replica_uri=URI)


def test_a_seed_whose_sequences_went_below_the_export_places_nothing(replica, tmp_path, receipt, monkeypatch):
    real = schema.install_sync

    def lowering(conn, uri, epoch):
        conn.execute("UPDATE sqlite_sequence SET seq = 0 WHERE name = 'memories'")
        conn.commit()
        real(conn, uri, epoch)

    monkeypatch.setattr(schema, "install_sync", lowering)
    out = tmp_path / f"{DB}.db"
    with pytest.raises(lp.L5Refused, match="below the export's"):
        lp.seed(DB, str(receipt), out, make_deps(replica, tmp_path), replica_uri=URI)
    assert not out.exists()


def test_seed_sets_the_sequence_to_the_highest_of_local_d1_and_max_id(replica, tmp_path, receipt):
    """sqlite_sequence = max(local seq, D1 seq, max(id)) for memories and
    memories_actions: D1's live seq may be ahead (ids used then deleted),
    and a sequence may lag max(id)."""
    replica_exec(replica, "UPDATE sqlite_sequence SET seq = 40 WHERE name = 'memories'")
    out = tmp_path / f"{DB}.db"
    rep = lp.seed(DB, str(receipt), out, make_deps(replica, tmp_path), replica_uri=URI)
    assert rep["sequences"]["memories"] == {"seq": 3, "max_id": 3, "d1_seq": 40, "set": 40}
    assert dict(_rows(out, "SELECT name, seq FROM sqlite_sequence"))["memories"] == 40


def test_seed_sequence_follows_max_id_when_the_sequence_lags(replica, tmp_path):
    replica_exec(replica, "UPDATE sqlite_sequence SET seq = 1 WHERE name = 'memories'")
    receipt = lp.export(DB, make_deps(replica, tmp_path), tmp_path / "exports")
    out = tmp_path / f"{DB}.db"
    rep = lp.seed(DB, str(receipt), out, make_deps(replica, tmp_path), replica_uri=URI)
    assert rep["sequences"]["memories"]["set"] == 3
    # memories_actions has no row anywhere: a 0 row is created
    assert dict(_rows(out, "SELECT name, seq FROM sqlite_sequence"))["memories_actions"] == 0


@pytest.mark.parametrize("existing", ["", "-wal", "-shm", "-journal"])
def test_seed_never_overwrites_a_store_or_its_sidecars(replica, tmp_path, receipt, existing):
    out = tmp_path / f"{DB}.db"
    Path(f"{out}{existing}").write_bytes(b"precious")
    with pytest.raises(lp.L5Refused, match="already exists"):
        lp.seed(DB, str(receipt), out, make_deps(replica, tmp_path), replica_uri=URI)
    assert Path(f"{out}{existing}").read_bytes() == b"precious"


def test_seed_refuses_while_another_process_holds_the_primary_lock(replica, tmp_path, receipt):
    out = tmp_path / f"{DB}.db"
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl, os, sys, time\n"
         f"fd = os.open({str(out) + '.primary-lock'!r}, os.O_RDWR | os.O_CREAT, 0o644)\n"
         "fcntl.flock(fd, fcntl.LOCK_EX)\nprint('held', flush=True)\ntime.sleep(60)\n"],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(lp.L5Refused, match="held by another process"):
            lp.seed(DB, str(receipt), out, make_deps(replica, tmp_path), replica_uri=URI)
        assert not out.exists()
    finally:
        holder.kill()
        holder.wait()


def test_a_seed_that_does_not_match_the_receipt_places_nothing(replica, tmp_path, receipt, monkeypatch):
    real = schema.install_sync

    def tampering(conn, uri, epoch):
        conn.execute("UPDATE memories SET content = 'changed' WHERE id = 2")
        conn.commit()
        real(conn, uri, epoch)

    monkeypatch.setattr(schema, "install_sync", tampering)
    out = tmp_path / f"{DB}.db"
    with pytest.raises(lp.L5Refused, match=r"does not match the receipt in \['memories'"):
        lp.seed(DB, str(receipt), out, make_deps(replica, tmp_path), replica_uri=URI)
    assert not out.exists() and not list(tmp_path.glob("*.seed-partial*"))


def test_seed_refuses_an_unusable_receipt_before_touching_anything(replica, tmp_path, receipt):
    r = json.loads(receipt.read_text())
    r["verified_at_epoch"] = 0
    receipt.write_text(json.dumps(r))
    barrier = FakeBarrier(frozen=True)
    out = tmp_path / "nope" / f"{DB}.db"
    with pytest.raises(lp.L5Refused, match="older than 24 h"):
        lp.seed(DB, str(receipt), out, make_deps(replica, tmp_path, barrier), replica_uri=URI)
    assert barrier.calls == [] and not out.parent.exists()


def test_seed_rehearse_writes_to_a_temp_path(replica, tmp_path, receipt):
    out = tmp_path / f"{DB}.db"
    rep = lp.seed(DB, str(receipt), out, make_deps(replica, tmp_path), replica_uri=URI, rehearse=True)
    assert not out.exists() and Path(rep["out"]).is_file() and rep["rehearse"] is True
    assert Path(rep["out"]).name == out.name


def test_a_seeded_store_serves_as_a_live_primary_and_replicates_new_writes(replica, tmp_path, receipt, monkeypatch):
    out = tmp_path / "fresh" / f"{DB}.db"
    lp.seed(DB, str(receipt), out, make_deps(replica, tmp_path), replica_uri=URI)
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({DB: str(out)}))
    monkeypatch.setenv("MEMORA_REPLICAS", json.dumps({DB: URI}))
    backend = storage.backend_for(DB)
    assert backend.live_primary
    conn = backend.connect()
    try:
        cur = conn.execute("INSERT INTO memories (content, metadata, tags) VALUES ('after seed', '{}', '[]')")
        conn.commit()
        assert cur.lastrowid == 4
        tables = [r[0] for r in conn.execute("SELECT tbl FROM sync_outbox ORDER BY seq")]
        assert "memories" in tables
    finally:
        conn.close()


# ---------------------------------------------------------------- FTS rebuild and search parity

def test_the_fts_rebuild_writes_what_fts_upsert_writes(tmp_path):
    """§4 step 3: the rebuild's values equal _fts_upsert's, NULLs included
    (_fts_upsert writes '' for a NULL metadata or tags)."""
    conn = LocalSQLiteBackend(tmp_path / "a.db").connect()
    try:
        schema.ensure_schema(conn)
        rows = [("plain words", '{"k": 1}', '["t"]'), ("null metadata", None, '["t"]'),
                ("null tags", '{"k": 2}', None), ("both null", None, None)]
        for content, meta, tags in rows:
            mid = conn.execute("INSERT INTO memories (content, metadata, tags) VALUES (?, ?, ?)",
                               (content, meta, tags)).lastrowid
            storage._fts_upsert(conn, mid, content, meta, tags)
        conn.commit()
        q = "SELECT rowid, content, metadata, tags FROM memories_fts ORDER BY rowid"
        upserted = [tuple(r) for r in conn.execute(q)]
        assert lp.rebuild_fts(conn) == 4
        assert [tuple(r) for r in conn.execute(q)] == upserted
    finally:
        conn.close()


def _replica_from_store(store_path: Path, replica: FakeReplica) -> None:
    """Copy a local store's replicated tables into a FakeReplica (what D1
    holds for it): common columns, parents first."""
    db = replica._db()
    src = sqlite3.connect(store_path)
    try:
        db.execute("PRAGMA foreign_keys = OFF")
        for t in schema.SYNC_TABLES:
            dst_cols = [r[1] for r in db.execute(f'PRAGMA table_info("{t}")')]
            src_cols = {r[1] for r in src.execute(f'PRAGMA table_info("{t}")')}
            cols = [c for c in dst_cols if c in src_cols]
            if not cols:
                continue
            db.execute(f'DELETE FROM "{t}"')
            collist = ", ".join(f'"{c}"' for c in cols)
            for row in src.execute(f'SELECT {collist} FROM "{t}"'):
                db.execute(f'INSERT INTO "{t}" ({collist}) VALUES ({", ".join("?" * len(cols))})', tuple(row))
        seq = src.execute("SELECT seq FROM sqlite_sequence WHERE name = 'memories'").fetchone()
        db.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'memories'", (seq[0],))
        db.commit()
    finally:
        src.close()
        db.close()


def test_search_on_a_seeded_store_matches_the_original(memory_factory, tmp_path):
    """§4: keyword (FTS) and hybrid search on the seeded store return the same
    ids and scores as on the store the data came from."""
    texts = ["Kubernetes rollout of the payments service", "postgres vacuum tuning notes",
             "payments retry policy for webhooks", "grocery list: apples, pears",
             "rollout checklist with canary and payments dashboards", "vacuum cleaner warranty"]
    for i, text in enumerate(texts):
        memory_factory(content=text, tags=["alpha" if i % 2 else "beta"],
                       metadata=None if i == 3 else {"project": "p", "n": i})
    original = storage.STORAGE_BACKEND.db_path
    replica = FakeReplica(tmp_path / "d1.db")
    _replica_from_store(original, replica)
    receipt = lp.export(DB, make_deps(replica, tmp_path), tmp_path / "exports")
    seeded = tmp_path / "seeded" / f"{DB}.db"
    lp.seed(DB, str(receipt), seeded, make_deps(replica, tmp_path), replica_uri=URI)

    def results(path):
        conn = LocalSQLiteBackend(path).connect()
        try:
            out = {}
            for q in ("payments", "rollout", "vacuum", "apples", "payments rollout", "project"):
                out[f"kw {q}"] = [m["id"] for m in storage.list_memories(conn, query=q)]
                out[f"hy {q}"] = [(r["memory"]["id"], round(r["score"], 9))
                                  for r in storage.hybrid_search(conn, q, top_k=10)]
            return out
        finally:
            conn.close()

    before, after = results(original), results(seeded)
    assert before == after
    assert before["kw payments"] and before["hy vacuum"]  # the queries do find something


# ---------------------------------------------------------------- sequence high-water on D1 (H7)

class ReplicaSend:
    """The operator writer's D1Connection double: runs the statement on the
    FakeReplica file, or rejects it like D1 would, or 'accepts' it without
    applying it."""

    def __init__(self, replica, mode="apply"):
        self.replica, self.mode = replica, mode

    def _send(self, sql, params):
        if self.mode == "reject":
            raise D1DefiniteError("D1 API error (400): not authorized: SQLITE_AUTH")
        if self.mode == "apply":
            replica_exec(self.replica, sql, params)
        return {"success": True, "meta": {"changes": 1}}


def _local_store_with_ids(tmp_path, n, deleted=(), action=False):
    path = tmp_path / "local" / f"{DB}.db"
    conn = LocalSQLiteBackend(path).connect()
    try:
        schema.ensure_schema(conn)
        for i in range(n):
            conn.execute("INSERT INTO memories (content) VALUES (?)", (f"m{i}",))
        for i in deleted:
            conn.execute("DELETE FROM memories WHERE id = ?", (i,))
        if action:
            conn.execute("INSERT INTO memories_actions (memory_id, action, summary) VALUES (1, 'x', 's')")
        conn.commit()
    finally:
        conn.close()
    return path


def _seq(replica, name):
    return dict(_rows(replica.path, "SELECT name, seq FROM sqlite_sequence")).get(name)


def _seq_deps(replica, tmp_path, send, barrier=None):
    deps = make_deps(replica, tmp_path, barrier)
    deps.writer_factory = lambda: lp.OperatorD1Writer(send)
    return deps


def test_sequence_highwater_raises_d1_to_the_local_high_water(replica, tmp_path, receipt):
    replica_exec(replica, "INSERT INTO memories_actions (memory_id, action, summary) VALUES (1, 'a', 's')")
    receipt = lp.export(DB, make_deps(replica, tmp_path), tmp_path / "exports")
    local = _local_store_with_ids(tmp_path, 9, deleted=(9,), action=True)  # seq 9, max(id) 8
    barrier = FakeBarrier(frozen=True)
    send = ReplicaSend(replica)
    rep = lp.sequence_highwater(DB, str(receipt), local, _seq_deps(replica, tmp_path, send, barrier),
                                tmp_path / "exports")
    assert rep["statements"] == [(lp.SEQ_UPDATE_SQL, (9, "memories", 9))]
    assert _seq(replica, "memories") == 9 and _seq(replica, "memories_actions") == 1
    assert rep["d1_after"]["memories"] == 9
    # under the operator's freeze from the recheck to the read-back, left in place
    assert "freeze" not in barrier.calls and "thaw" not in barrier.calls and barrier.frozen
    assert barrier.calls[0] == "check before the recheck"
    i = barrier.calls.index("check before the sequence UPDATE")
    assert barrier.calls[i + 1] == "check after the sequence UPDATE"


def test_sequence_highwater_sends_nothing_when_d1_is_ahead_or_on_a_dry_run(replica, tmp_path, receipt):
    local = _local_store_with_ids(tmp_path, 2)
    rep = lp.sequence_highwater(DB, str(receipt), local, _seq_deps(replica, tmp_path, ReplicaSend(replica)),
                                tmp_path / "exports")
    assert rep["statements"] == []
    local2 = _local_store_with_ids(tmp_path / "b", 7)
    send = ReplicaSend(replica, mode="accept-only")
    deps = _seq_deps(replica, tmp_path, send)
    rep = lp.sequence_highwater(DB, str(receipt), local2, deps, tmp_path / "exports", dry_run=True)
    assert rep["statements"] == [(lp.SEQ_UPDATE_SQL, (7, "memories", 7))] and _seq(replica, "memories") == 3


def test_a_rejected_sequence_update_halts(replica, tmp_path, receipt):
    local = _local_store_with_ids(tmp_path, 7)
    with pytest.raises(lp.L5Halt, match="D1 rejected"):
        lp.sequence_highwater(DB, str(receipt), local, _seq_deps(replica, tmp_path, ReplicaSend(replica, "reject")),
                              tmp_path / "exports")
    assert _seq(replica, "memories") == 3


def test_an_accepted_but_unapplied_sequence_update_halts(replica, tmp_path, receipt):
    local = _local_store_with_ids(tmp_path, 7)
    with pytest.raises(lp.L5Halt, match="did not apply"):
        lp.sequence_highwater(DB, str(receipt), local,
                              _seq_deps(replica, tmp_path, ReplicaSend(replica, "accept-only")), tmp_path / "exports")


def test_a_missing_d1_sequence_row_halts_instead_of_inserting(replica, tmp_path, receipt):
    local = _local_store_with_ids(tmp_path, 2, action=True)  # memories_actions high-water 1, D1 has no row
    send = ReplicaSend(replica)
    with pytest.raises(lp.L5Halt, match="no sqlite_sequence row for memories_actions"):
        lp.sequence_highwater(DB, str(receipt), local, _seq_deps(replica, tmp_path, send), tmp_path / "exports")


def test_the_operator_writer_sends_only_the_allow_listed_statement(replica):
    w = lp.OperatorD1Writer(ReplicaSend(replica))
    for sql in ("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
                "UPDATE sqlite_sequence SET seq = ? WHERE name = ?",
                "DELETE FROM memories WHERE id = ?"):
        with pytest.raises(lp.L5Refused, match="allow-list"):
            w.send(sql, (1, "memories"))
    assert w.sent == []


def test_sequence_highwater_needs_a_passing_recheck(replica, tmp_path, receipt):
    local = _local_store_with_ids(tmp_path, 7)
    with pytest.raises(lp.L5Refused, match="not frozen before the recheck"):
        lp.sequence_highwater(DB, str(receipt), local,
                              _seq_deps(replica, tmp_path, ReplicaSend(replica), FakeBarrier(frozen=False)),
                              tmp_path / "exports")
    assert _seq(replica, "memories") == 3


# ---------------------------------------------------------------- snapshot and volume alert

def _store(tmp_path, n=5):
    path = tmp_path / "store" / f"{DB}.db"
    conn = LocalSQLiteBackend(path).connect()
    try:
        schema.ensure_schema(conn)
        for i in range(n):
            conn.execute("INSERT INTO memories (content) VALUES (?)", (f"snap {i}",))
        conn.commit()
    finally:
        conn.close()
    return path


def test_snapshot_uploads_a_verified_gzip_copy(tmp_path):
    store = _store(tmp_path)
    r2 = lp.FsR2(tmp_path / "r2")
    rep = lp.snapshot(DB, store, r2, tmp_path / "work", now=86400 * 20000)
    assert rep["key"] == f"{DB}/2024-10-04T000000Z.db.gz"
    copy = tmp_path / "copy.db"
    copy.write_bytes(gzip.decompress(r2.get(rep["key"])))
    assert _rows(copy, "SELECT COUNT(*) FROM memories") == [(5,)]
    assert lp._sha256_file(tmp_path / "r2" / rep["key"]) == rep["sha256"]
    assert not list((tmp_path / "work").iterdir())  # temp files cleaned


def test_snapshot_retention_keeps_the_newest_and_never_deletes_other_keys(tmp_path):
    store = _store(tmp_path)
    r2 = lp.FsR2(tmp_path / "r2")
    for day in range(1, 17):
        (tmp_path / "r2" / DB).mkdir(parents=True, exist_ok=True)
        (tmp_path / "r2" / DB / f"2026-08-{day:02d}T030000Z.db.gz").write_bytes(b"old")
    (tmp_path / "r2" / DB / "manual-before-migration.db.gz").write_bytes(b"keep me")
    rep = lp.snapshot(DB, store, r2, tmp_path / "work", keep=14)
    left = r2.list(f"{DB}/")
    ours = [k for k in left if k != f"{DB}/manual-before-migration.db.gz"]
    assert len(ours) == 14 and rep["key"] in ours
    assert f"{DB}/manual-before-migration.db.gz" in left
    assert f"{DB}/2026-08-01T030000Z.db.gz" not in left and f"{DB}/2026-08-16T030000Z.db.gz" in left
    assert len(rep["removed"]) == 3


def test_snapshot_refuses_below_twice_the_store_size(tmp_path):
    store = _store(tmp_path)
    r2 = lp.FsR2(tmp_path / "r2")
    size = store.stat().st_size
    with pytest.raises(lp.L5Refused, match="below 2x"):
        lp.snapshot(DB, store, r2, tmp_path / "work", disk_free=lambda p: 2 * size - 1)
    assert r2.list(f"{DB}/") == []


def test_snapshot_refuses_when_the_r2_read_back_differs(tmp_path):
    class Corrupting(lp.FsR2):
        def get(self, key):
            return super().get(key)[:-1]

    with pytest.raises(lp.L5Refused, match="R2 read-back"):
        lp.snapshot(DB, _store(tmp_path), Corrupting(tmp_path / "r2"), tmp_path / "work")


def test_snapshot_of_a_live_primary_with_an_open_writer(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({DB: str(store)}))
    monkeypatch.setenv("MEMORA_REPLICAS", json.dumps({DB: URI}))
    backend = storage.backend_for(DB)
    writer = backend.connect()  # WAL, sidecars present, a committed and an uncommitted write
    try:
        writer.execute("INSERT INTO memories (content) VALUES ('committed')")
        writer.commit()
        writer.execute("INSERT INTO memories (content) VALUES ('not yet')")
        rep = lp.snapshot(DB, store, lp.FsR2(tmp_path / "r2"), tmp_path / "work")
    finally:
        writer.rollback()
        writer.close()
    copy = tmp_path / "copy.db"
    copy.write_bytes(gzip.decompress((tmp_path / "r2" / rep["key"]).read_bytes()))
    assert [r[0] for r in _rows(copy, "SELECT content FROM memories WHERE id > 5")] == ["committed"]


Usage = namedtuple("Usage", "total used free")


def test_volume_check_alerts_on_low_space(tmp_path):
    store = _store(tmp_path)
    size = store.stat().st_size
    ok = lp.volume_check([store], usage=lambda p: Usage(100 * size, 50 * size, 50 * size))
    assert ok["ok"] and ok["alerts"] == []
    low = lp.volume_check([store], usage=lambda p: Usage(100 * size, 99 * size, size))
    assert not low["ok"] and any("below 2x" in a for a in low["alerts"]) and any("% of the volume" in a for a in low["alerts"])


# ---------------------------------------------------------------- §9 (k)

def test_a_live_primary_creates_its_parent_directory_before_the_lock(tmp_path, monkeypatch):
    path = tmp_path / "not" / "yet" / f"{DB}.db"
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({DB: str(path)}))
    monkeypatch.setenv("MEMORA_REPLICAS", json.dumps({DB: URI}))
    backend = storage.backend_for(DB)
    assert backend.live_primary
    conn = backend.connect()
    conn.close()
    assert path.parent.is_dir() and Path(str(path) + ".primary-lock").exists()


# ---------------------------------------------------------------- CLI, in a subprocess

def _cli(replica, *args, env_extra=None):
    env = {**os.environ, "L5_TEST_FAKE_D1": str(replica.path), "MEMORA_D1_READ_TOKEN": "read-token"}
    env.update(env_extra or {})
    r = subprocess.run([sys.executable, str(HARNESS), *args], capture_output=True, text=True,
                       timeout=120, env=env, cwd=str(REPO))
    out = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {}
    return r.returncode, out, r.stderr


def _common(tmp_path):
    return [DB, "--account", "acct", "--database-id", "replica-db", "--service-stopped",
            "--r2-dir", str(tmp_path / "r2"), "--out-dir", str(tmp_path / "exports")]


@pytest.fixture
def docker_stopped(tmp_path, monkeypatch):
    """--service-stopped in the subprocess: a docker recorder reporting
    State.Running=false."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text("#!/bin/sh\necho false\n")
    (bin_dir / "docker").chmod(0o755)
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}"}


def _cred(tmp_path, mode=0o600):
    f = tmp_path / "operator.tok"
    f.write_text("operator-edit-token")
    f.chmod(mode)
    return f


def test_cli_seed_and_sequence_highwater(replica, tmp_path, docker_stopped):
    env = docker_stopped
    code, out, err = _cli(replica, "export", *_common(tmp_path), env_extra=env)
    assert code == 0, err
    receipt = out["receipt"]
    target = tmp_path / "data" / f"{DB}.db"
    code, out, err = _cli(replica, "seed", *_common(tmp_path), "--receipt", receipt, "--out", str(target),
                          "--replica-uri", URI, env_extra=env)
    assert code == 0 and out["out"] == str(target), err
    conn = LocalSQLiteBackend(target).connect()
    try:
        for i in range(4):
            conn.execute("INSERT INTO memories (content) VALUES (?)", (f"local {i}",))
        conn.commit()
    finally:
        conn.close()
    args = ["sequence-highwater", *_common(tmp_path), "--receipt", receipt, "--local", str(target),
            "--credential-file", str(_cred(tmp_path))]
    code, out, err = _cli(replica, *args, env_extra={**env, "L5_TEST_D1_WRITES": "apply"})
    assert code == 0 and out["d1_after"]["memories"] == 7, err
    assert _seq(replica, "memories") == 7


def test_cli_seed_after_recheck_runs_under_the_same_freeze(replica, tmp_path):
    """7621 P1-2: export places the freeze, recheck and seed run under it
    (one POST, no DELETE) and only `thaw` lifts it."""
    srv = FreezeServer()
    try:
        tokens = token_args(tmp_path)
        common = [DB, "--account", "acct", "--database-id", "replica-db", "--memora-url", srv.url,
                  *tokens, "--r2-dir", str(tmp_path / "r2"),
                  "--out-dir", str(tmp_path / "exports")]
        code, out, err = _cli(replica, "export", *common)
        assert code == 0, err
        receipt = out["receipt"]
        code, out, err = _cli(replica, "recheck", *common, "--receipt", receipt)
        assert code == 0 and out["fresh_export"] is False, err
        target = tmp_path / "data" / f"{DB}.db"
        code, out, err = _cli(replica, "seed", *common, "--receipt", receipt, "--out", str(target),
                              "--replica-uri", URI)
        assert code == 0 and target.is_file(), err
        assert [m for m, _p in srv.methods() if m != "GET"] == ["POST"] and srv.state == "frozen"
        code, out, err = _cli(replica, "thaw", DB, "--memora-url", srv.url, *tokens)
        assert code == 0 and srv.state == "open"
        # a seed without the freeze is refused
        code, out, _ = _cli(replica, "seed", *common, "--receipt", receipt, "--out", str(tmp_path / "b.db"),
                            "--replica-uri", URI)
        assert code == 2 and "freeze" in out["refused"] and not (tmp_path / "b.db").exists()
    finally:
        srv.close()


def test_cli_sequence_highwater_halts_when_d1_rejects(replica, tmp_path, docker_stopped, receipt):
    local = _local_store_with_ids(tmp_path, 7)
    args = ["sequence-highwater", *_common(tmp_path), "--receipt", str(receipt), "--local", str(local),
            "--credential-file", str(_cred(tmp_path))]
    code, out, _ = _cli(replica, *args, env_extra={**docker_stopped, "L5_TEST_D1_WRITES": "reject"})
    assert code == 3 and "D1 rejected" in out["halted"]
    assert _seq(replica, "memories") == 3


def test_cli_sequence_highwater_refuses_a_readable_credential_file(replica, tmp_path, docker_stopped, receipt):
    local = _local_store_with_ids(tmp_path, 7)
    args = ["sequence-highwater", *_common(tmp_path), "--receipt", str(receipt), "--local", str(local),
            "--credential-file", str(_cred(tmp_path, 0o640))]
    code, out, _ = _cli(replica, *args, env_extra={**docker_stopped, "L5_TEST_D1_WRITES": "apply"})
    assert code == 2 and "0600" in out["refused"]
    assert _seq(replica, "memories") == 3


def test_cli_refuses_while_the_service_is_running(replica, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text("#!/bin/sh\necho true\n")
    (bin_dir / "docker").chmod(0o755)
    code, out, _ = _cli(replica, "export", *_common(tmp_path), env_extra={"PATH": f"{bin_dir}:{os.environ['PATH']}"})
    assert code == 2 and "must be stopped" in out["refused"]


def test_cli_snapshot_and_volume_check(tmp_path, replica):
    store = _store(tmp_path)
    code, out, err = _cli(replica, "snapshot", DB, "--store", str(store), "--r2-dir", str(tmp_path / "r2"),
                          "--work-dir", str(tmp_path / "work"))
    assert code == 0 and (tmp_path / "r2" / out["key"]).is_file(), err
    code, out, _ = _cli(replica, "volume-check", "--store", str(store), "--min-free-pct", "0")
    assert code == 0 and out["ok"]
    code, out, _ = _cli(replica, "volume-check", "--store", str(store), "--min-free-pct", "100.1")
    assert code == 4 and not out["ok"]


# ---------------------------------------------------------------- live: the sequence UPDATE on a throwaway D1

LIVE = ("MEMORA_D1_TEST_ACCOUNT", "MEMORA_D1_TEST_DATABASE", "MEMORA_D1_TEST_DATABASE_NAME",
        "MEMORA_D1_TEST_EDIT_TOKEN", "MEMORA_D1_TEST_READ_TOKEN")


@pytest.mark.skipif(any(not os.getenv(v) for v in LIVE) or "throwaway" not in os.getenv("MEMORA_D1_TEST_DATABASE_NAME", ""),
                    reason="needs the MEMORA_D1_TEST_* throwaway database variables")
def test_d1_accepts_the_sequence_update_on_a_throwaway_database():
    """§4 H7: whether D1 accepts `UPDATE sqlite_sequence ...`. Only on the
    throwaway database, after the API confirms its identity; the probe table
    is its own and is dropped afterwards."""
    from memora.backends import D1Connection, D1SelectOnlyConnection
    from tests.live_d1_guard import verify_throwaway

    account, database = os.environ["MEMORA_D1_TEST_ACCOUNT"], os.environ["MEMORA_D1_TEST_DATABASE"]
    ok, why = verify_throwaway(account, database, os.environ["MEMORA_D1_TEST_READ_TOKEN"],
                               os.environ["MEMORA_D1_TEST_DATABASE_NAME"])
    if not ok:
        pytest.fail(f"refusing to touch D1: {why}")
    setup = D1Connection(account, database, os.environ["MEMORA_D1_TEST_EDIT_TOKEN"])
    setup._send("CREATE TABLE IF NOT EXISTS l5_seq_probe (id INTEGER PRIMARY KEY AUTOINCREMENT, v TEXT)")
    try:
        setup._send("INSERT INTO l5_seq_probe (v) VALUES ('a')")
        reader = lp.D1Reader(D1SelectOnlyConnection(account, database, os.environ["MEMORA_D1_TEST_READ_TOKEN"]))
        before = reader.sequences()["l5_seq_probe"]
        writer = lp.OperatorD1Writer(D1Connection(account, database, os.environ["MEMORA_D1_TEST_EDIT_TOKEN"]))
        writer.send(lp.SEQ_UPDATE_SQL, (before + 100, "l5_seq_probe", before + 100))
        assert reader.sequences()["l5_seq_probe"] == before + 100
        setup._send("INSERT INTO l5_seq_probe (v) VALUES ('b')")
        rows, _ = reader.conn.execute("SELECT MAX(id) AS m FROM l5_seq_probe")
        assert rows[0]["m"] == before + 101
    finally:
        setup._send("DROP TABLE IF EXISTS l5_seq_probe")
