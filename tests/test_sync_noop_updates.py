"""REL1 (leader 7764): nothing is sent to D1 when nothing changed. The
AFTER UPDATE sync triggers enqueue only when a column's value changed; an
idle store makes no D1 request at all. Offline: local SQLite + FakeReplica."""

from __future__ import annotations

import time

import pytest

from memora import replicator as R
from memora import schema

from tests.l3_fakes import URI, FakeReplica, local_store
from tests.test_l3_write import _write_everything


@pytest.fixture()
def store(tmp_path):
    replica = FakeReplica(tmp_path / "replica.db")
    local = local_store(tmp_path / "local.db", epoch=replica.epoch())
    _write_everything(local)
    return local, replica


def _outbox(local):
    conn = local.connect()
    try:
        return [tuple(r) for r in conn.execute("SELECT tbl, op, pk FROM sync_outbox ORDER BY seq")]
    finally:
        conn.close()


def _exec(local, sql, params=()):
    conn = local.connect()
    try:
        n = conn.execute(sql, params).rowcount
        conn.commit()
        return n
    finally:
        conn.close()


@pytest.mark.parametrize("table", list(schema.SYNC_TABLES))
def test_a_noop_update_enqueues_nothing(store, table):
    local, _ = store
    before = _outbox(local)
    cols = schema.sync_columns(local.connect())[table]
    if table == "memories_embeddings":
        # rewriting `embedding` under the same writer_token is NOT a no-op:
        # trg_embedding_external_update marks it foreign (test below)
        cols = [c for c in cols if c != "embedding"]
    where = "key = 'other'" if table == "memories_meta" else "1 = 1"
    sets = ", ".join(f'"{c}" = "{c}"' for c in cols)  # every column rewritten to itself
    assert _exec(local, f"UPDATE {table} SET {sets} WHERE {where}") >= 1, "the UPDATE hit rows"
    assert _outbox(local) == before


@pytest.mark.parametrize("table, sql, pk", [
    ("memories", "UPDATE memories SET content = 'changed' WHERE id = 1", "[1]"),
    ("memories_embeddings", "UPDATE memories_embeddings SET dimension = 4 WHERE memory_id = 1", "[1]"),
    ("memories_crossrefs", "UPDATE memories_crossrefs SET related = '[2]' WHERE memory_id = 1", "[1]"),
    ("tombstones", "UPDATE tombstones SET reason = 'r2' WHERE memory_id = 2", '["h",2]'),
    ("tombstone_components", "UPDATE tombstone_components SET content_hash = 'h2' WHERE memory_id = 2", "[2]"),
    ("memories_actions", "UPDATE memories_actions SET summary = 's2' WHERE memory_id = 1", None),
    ("memories_meta", "UPDATE memories_meta SET value = 'v2' WHERE key = 'other'", '["other"]'),
])
def test_a_real_change_enqueues_once(store, table, sql, pk):
    local, _ = store
    before = _outbox(local)
    _exec(local, sql)
    new = _outbox(local)[len(before):]
    assert len(new) == 1 and new[0][:2] == (table, "U"), new
    if pk is not None:
        assert new[0][2] == pk


def test_a_null_to_value_change_counts(store):
    local, _ = store
    before = len(_outbox(local))
    _exec(local, "UPDATE memories SET metadata = NULL WHERE id = 1")
    mid = len(_outbox(local))
    _exec(local, "UPDATE memories SET metadata = NULL WHERE id = 1")  # NULL -> NULL: no change
    assert len(_outbox(local)) == mid
    _exec(local, "UPDATE memories SET metadata = '{}' WHERE id = 1")
    assert len(_outbox(local)) == mid + 1 and mid >= before


def test_a_pk_change_still_deletes_the_old_key(store):
    local, _ = store
    before = _outbox(local)
    _exec(local, "UPDATE memories_meta SET key = 'renamed' WHERE key = 'other'")
    new = _outbox(local)[len(before):]
    assert sorted(new) == sorted([("memories_meta", "U", '["renamed"]'), ("memories_meta", "D", '["other"]')])


def test_an_excluded_meta_key_never_enqueues(store):
    local, _ = store
    before = _outbox(local)
    _exec(local, "UPDATE memories_meta SET value = value + 1 WHERE key = 'embedding_change_epoch'")
    assert _outbox(local) == before


def test_an_idle_store_makes_no_d1_request(store):
    local, replica = store
    calls = []
    post, read = replica.post_json, replica.reader_post
    replica.post_json = lambda body: (calls.append("write"), post(body))[1]
    replica.reader_post = lambda body: (calls.append("read"), read(body))[1]
    rep = R.StoreReplicator("s1", local, URI, mode="write", writer_factory=replica.writer,
                            reader_factory=replica.reader, broadcast=lambda: None, interval_s=0.1, poll_s=0.05)
    rep.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            conn = local.connect()
            acked, head = conn.execute(
                "SELECT last_acked_seq, (SELECT MAX(seq) FROM sync_outbox) FROM sync_state").fetchone()
            conn.close()
            if acked == head:
                break
            time.sleep(0.05)
        assert acked == head, "the initial rows were replicated"
        calls.clear()
        _exec(local, "UPDATE memories SET content = content WHERE id = 1")  # a no-op write, and its commit event
        time.sleep(0.8)  # many polls and intervals
        assert calls == [], calls
    finally:
        rep.stop()


def test_a_version_1_store_is_upgraded(store):
    local, _ = store
    conn = local.connect()
    # a version-1 store: its update triggers enqueue on EVERY update (no WHEN)
    excluded = ", ".join(f"'{k}'" for k in schema.SYNC_META_EXCLUDED)
    for table, pk in schema.SYNC_TABLES.items():
        conn.execute(f"DROP TRIGGER trg_sync_{table}_update")
        when = f"WHEN NEW.key NOT IN ({excluded}) " if table == "memories_meta" else ""
        cols = ", ".join(f"NEW.{c}" for c in pk)
        conn.execute(f"CREATE TRIGGER trg_sync_{table}_update AFTER UPDATE ON {table} {when}"
                     f"BEGIN INSERT INTO sync_outbox(tbl, op, pk) VALUES ('{table}', 'U', json_array({cols})); END")
    conn.execute("UPDATE sync_state SET trigger_version = 1")
    conn.commit()
    n = len(_outbox(local))
    _exec(local, "UPDATE memories SET content = content WHERE id = 1")
    assert len(_outbox(local)) == n + 1, "a version-1 trigger enqueues a no-op update"
    schema.ensure_schema(conn)
    assert conn.execute("SELECT trigger_version FROM sync_state").fetchone()[0] == schema.SYNC_TRIGGER_VERSION
    conn.close()
    before = _outbox(local)
    _exec(local, "UPDATE memories SET content = content WHERE id = 1")
    assert _outbox(local) == before


def test_ensure_schema_after_a_column_add_refreshes_the_triggers(store):
    local, _ = store
    conn = local.connect()
    conn.execute("ALTER TABLE memories ADD COLUMN rel1_extra TEXT")
    conn.commit()
    before = _outbox(local)
    _exec(local, "UPDATE memories SET rel1_extra = 'x' WHERE id = 1")
    assert _outbox(local) == before, "stale triggers do not see the new column"
    schema.ensure_schema(conn)
    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'trg_sync_memories_update'").fetchone()[0]
    conn.close()
    assert 'OLD."rel1_extra" IS NOT NEW."rel1_extra"' in ddl
    _exec(local, "UPDATE memories SET rel1_extra = 'y' WHERE id = 1")
    assert _outbox(local)[len(before):] == [("memories", "U", "[1]")]


def test_the_fingerprint_is_stored_and_unchanged_columns_do_not_reinstall(store):
    local, _ = store
    conn = local.connect()
    fp = conn.execute("SELECT trigger_columns FROM sync_state").fetchone()[0]
    assert fp == schema._columns_fingerprint(schema.sync_columns(conn))
    conn.execute("CREATE TRIGGER trg_sync_marker AFTER INSERT ON memories BEGIN SELECT 1; END")
    conn.commit()
    schema.ensure_schema(conn)  # nothing changed: no reinstall (which would drop trg_sync_*)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'trg_sync_marker'").fetchone()
    conn.close()


def test_an_embedding_rewrite_without_a_new_writer_token_is_a_real_change(store):
    """The integrity trigger (trg_embedding_external_update) clears the
    representation of an embedding rewritten under the same writer token:
    the row changed, so it replicates."""
    local, _ = store
    before = _outbox(local)
    _exec(local, "UPDATE memories_embeddings SET embedding = embedding WHERE memory_id = 1")
    conn = local.connect()
    row = conn.execute("SELECT representation, writer_token FROM memories_embeddings WHERE memory_id = 1").fetchone()
    conn.close()
    assert tuple(row) == (None, None)
    assert ("memories_embeddings", "U", "[1]") in _outbox(local)[len(before):]


def test_a_frozen_store_reports_stale_triggers_as_a_pending_upgrade(store):
    """X2's read-only check (schema_pending) must list what ensure_schema
    would do: a column added since the triggers were installed."""
    local, _ = store
    conn = local.connect()
    assert schema.schema_pending(conn) == []
    conn.execute("ALTER TABLE memories ADD COLUMN rel1_extra TEXT")
    conn.commit()
    assert schema.schema_pending(conn) == ["sync triggers (the replicated tables' columns changed)"]
    schema.ensure_schema(conn)
    assert schema.schema_pending(conn) == []
    conn.close()
