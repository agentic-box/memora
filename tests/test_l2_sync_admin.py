"""L2 piece (b): sync outbox/state/shadow DDL and triggers, the admin
endpoint handlers, reconciliation evidence, health fields and startup.
docs/local-primary-implementation.md §1, §2.9. Offline only.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from memora import admin, backends, intent_journal, reconcile, schema, storage, write_gate
from memora.backends import LocalSQLiteBackend

from tests.conftest import FakeD1Connection
from tests.test_l2_gate_journal import FakeD1Server


# ------------------------------------------------------------------ schema

def _store(tmp_path, name="s.db"):
    backend = LocalSQLiteBackend(tmp_path / name)
    conn = backend.connect()
    schema.ensure_schema(conn)
    return backend, conn


def _outbox(conn):
    return [(r[0], r[1], json.loads(r[2])) for r in conn.execute("SELECT tbl, op, pk FROM sync_outbox ORDER BY seq")]


def _sync_triggers(conn):
    return sorted(r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_sync_%'"))


EXPECTED_TRIGGERS = sorted(
    [f"trg_sync_{t}_{a}" for t in schema.SYNC_TABLES for a in ("insert", "update", "delete", "update_pk")]
)


def test_no_sync_state_no_triggers(tmp_path):
    backend, conn = _store(tmp_path)
    schema.ensure_schema(conn)
    assert _sync_triggers(conn) == []
    assert not schema._has_table(conn, "sync_outbox") and not schema._has_table(conn, "sync_state")
    conn.close()


def test_install_sync_creates_28_triggers_and_state(tmp_path):
    backend, conn = _store(tmp_path)
    schema.install_sync(conn, "d1://acct/db", 42)
    assert _sync_triggers(conn) == EXPECTED_TRIGGERS and len(EXPECTED_TRIGGERS) == 28
    row = conn.execute("SELECT replica_uri, last_acked_seq, d1_epoch_expected, trigger_version, "
                       "log_cursor_seq, compare_consumed_seq FROM sync_state").fetchone()
    assert tuple(row) == ("d1://acct/db", 0, 42, schema.SYNC_TRIGGER_VERSION, 0, 0)
    assert _outbox(conn) == []
    with pytest.raises(ValueError, match="already installed"):
        schema.install_sync(conn, "d1://acct/db", 42)
    conn.close()


def test_triggers_capture_every_table_in_commit_order(tmp_path):
    backend, conn = _store(tmp_path)
    schema.install_sync(conn, "d1://acct/db", 0)
    conn.execute("INSERT INTO memories (id, content) VALUES (1, 'a')")
    conn.execute("UPDATE memories SET content = 'b' WHERE id = 1")
    conn.execute("INSERT INTO memories_embeddings (memory_id, embedding) VALUES (1, '{}')")
    conn.execute("INSERT INTO memories_crossrefs (memory_id, related) VALUES (1, '[]')")
    conn.execute("INSERT INTO tombstones (content_hash, memory_id) VALUES ('h', 1)")
    conn.execute("INSERT INTO tombstone_components (memory_id) VALUES (1)")
    conn.execute("INSERT INTO memories_actions (memory_id, action, summary) VALUES (1, 'x', 'y')")
    conn.execute("INSERT INTO memories_meta (key, value) VALUES ('other', 'v')")
    conn.execute("DELETE FROM tombstones WHERE content_hash = 'h' AND memory_id = 1")
    conn.execute("DELETE FROM memories_embeddings WHERE memory_id = 1")
    conn.commit()
    got = _outbox(conn)
    action_id = conn.execute("SELECT id FROM memories_actions").fetchone()[0]
    assert got == [
        ("memories", "U", [1]), ("memories", "U", [1]),
        # the existing trg_embedding_external_insert UPDATEs the new row: a second U
        ("memories_embeddings", "U", [1]), ("memories_embeddings", "U", [1]), ("memories_crossrefs", "U", [1]),
        ("tombstones", "U", ["h", 1]), ("tombstone_components", "U", [1]),
        ("memories_actions", "U", [action_id]), ("memories_meta", "U", ["other"]),
        ("tombstones", "D", ["h", 1]), ("memories_embeddings", "D", [1]),
    ]
    conn.close()


def test_meta_exclusions_and_epoch_bumps_never_enqueued(tmp_path):
    backend, conn = _store(tmp_path)
    schema.install_sync(conn, "d1://acct/db", 0)
    before = conn.execute("SELECT value FROM memories_meta WHERE key = 'embedding_change_epoch'").fetchone()[0]
    conn.execute("INSERT INTO memories (id, content) VALUES (7, 'epoch trigger fires')")
    for key in schema.SYNC_META_EXCLUDED:
        conn.execute("INSERT OR REPLACE INTO memories_meta (key, value) VALUES (?, 'x')", (key,))
        conn.execute("UPDATE memories_meta SET value = 'y' WHERE key = ?", (key,))
    conn.execute("DELETE FROM memories_meta WHERE key = 'embedding_rebuild_lease'")
    conn.commit()
    after = conn.execute("SELECT value FROM memories_meta WHERE key = 'embedding_change_epoch'").fetchone()[0]
    assert after != before  # the epoch really moved
    assert _outbox(conn) == [("memories", "U", [7])]
    conn.close()


def test_pk_change_enqueues_old_key_delete(tmp_path):
    backend, conn = _store(tmp_path)
    schema.install_sync(conn, "d1://acct/db", 0)
    conn.execute("INSERT INTO memories (id, content) VALUES (1, 'a')")
    conn.execute("UPDATE memories SET id = 99 WHERE id = 1")
    conn.commit()
    got = _outbox(conn)
    # SQLite runs the update_pk trigger first; both keys are covered either way
    assert got[0] == ("memories", "U", [1])
    assert sorted(got[1:]) == [("memories", "D", [1]), ("memories", "U", [99])]
    conn.close()


def test_rolled_back_transaction_leaves_no_outbox_rows(tmp_path):
    backend, conn = _store(tmp_path)
    schema.install_sync(conn, "d1://acct/db", 0)
    conn.execute("INSERT INTO memories (id, content) VALUES (1, 'a')")
    conn.rollback()
    assert _outbox(conn) == []
    conn.close()


def test_trigger_version_upgrade(tmp_path):
    backend, conn = _store(tmp_path)
    schema.install_sync(conn, "d1://acct/db", 0)
    conn.execute("DROP TRIGGER trg_sync_memories_insert")
    conn.execute("UPDATE sync_state SET trigger_version = 0")
    conn.commit()
    schema.ensure_schema(conn)
    assert _sync_triggers(conn) == EXPECTED_TRIGGERS
    assert conn.execute("SELECT trigger_version FROM sync_state").fetchone()[0] == schema.SYNC_TRIGGER_VERSION
    conn.close()


def test_d1_store_has_no_sync_objects(tmp_path):
    d1 = FakeD1Connection(tmp_path / "d1.db")
    schema.ensure_schema(d1)
    d1._conn.execute("CREATE TABLE sync_state (id INTEGER, trigger_version INTEGER)")  # even if one appeared
    d1._conn.execute("INSERT INTO sync_state VALUES (1, 0)")
    schema._ensure_sync_outbox(d1)
    names = [r[0] for r in d1._conn.execute("SELECT name FROM sqlite_master WHERE name LIKE '%sync%' OR name LIKE 'trg_sync_%'")]
    assert names == ["sync_state"]
    fresh = FakeD1Connection(tmp_path / "fresh-d1.db")
    schema.ensure_schema(fresh)
    with pytest.raises(ValueError, match="local stores"):
        schema.install_sync(fresh, "d1://a/b", 0)
    with pytest.raises(ValueError, match="local shadow file"):
        schema.install_shadow_state(fresh)
    assert fresh._conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE '%sync%' OR name LIKE 'shadow_state'").fetchone()[0] == 0


def test_shadow_state_row(tmp_path):
    backend, conn = _store(tmp_path, "shadow.db")
    schema.install_shadow_state(conn)
    schema.install_shadow_state(conn)
    assert tuple(conn.execute("SELECT id, dirty, clean_shutdown, clean_nights FROM shadow_state").fetchall()[0]) == (1, 0, 0, 0)
    conn.close()


# ------------------------------------------------------------------ admin + evidence

@pytest.fixture()
def registry(tmp_path, monkeypatch):
    server = FakeD1Server(tmp_path / "d1.db")
    monkeypatch.setattr(backends.D1Connection, "_send", lambda self, sql, params=None: server.send(sql, params))
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "edit-token")
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"loc": str(tmp_path / "loc.db"), "remote": "d1://acct/db1"}))
    with LocalSQLiteBackend(tmp_path / "loc.db").connect() as c:
        c.execute("CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY, content TEXT)")
    return server


def test_admin_auth_placeholder_refuses_by_default():
    assert admin.require_admin(object()) == (403, {"error": "admin_auth_not_configured"})


def test_freeze_and_thaw_handlers(registry):
    status, body = admin.freeze_store("loc", timeout_s=1)
    assert status == 200 and body["state"] == "frozen" and body["in_flight"] == 0
    assert write_gate.freeze_file("loc").exists()
    with pytest.raises(write_gate.StoreReadOnlyError):
        storage.backend_for("loc").connect().execute("INSERT INTO memories (content) VALUES ('x')")
    status, body = admin.thaw_store("loc")
    assert status == 200 and body["state"] == "open" and not write_gate.freeze_file("loc").exists()
    assert admin.freeze_store("nope")[0] == 404


def test_freeze_handler_times_out_with_in_flight_list(registry):
    conn = storage.backend_for("loc").connect()
    conn.execute("INSERT INTO memories (content) VALUES ('stuck')")
    status, body = admin.freeze_store("loc", timeout_s=0.2)
    assert status == 409 and body["error"] == "freeze_timeout" and body["in_flight"][0]["statement"] == "INSERT"
    assert body["state"] == "open" and not write_gate.freeze_file("loc").exists()
    conn.commit()
    conn.close()


def test_thaw_refused_for_configured_read_only(registry, monkeypatch):
    monkeypatch.setenv("MEMORA_READONLY_DBS", "loc")
    assert admin.thaw_store("loc")[0] == 409


class _Reader:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def _open_intent(registry, sql, params):
    registry.raise_after_commit = TimeoutError("unknown outcome")
    with pytest.raises(TimeoutError):
        storage.backend_for("remote").connect().execute(sql, params)
    registry.raise_after_commit = None


def test_every_open_intent_needs_accept(registry):
    _open_intent(registry, "INSERT INTO memories (content) VALUES (?)", ("present",))
    _open_intent(registry, "UPDATE memories SET content = ? WHERE id = ?", ("present", 1))  # a no-op
    _open_intent(registry, "DELETE FROM memories WHERE id = ?", (999,))                     # absent key
    backend = storage.backend_for("remote")
    journal = backend.journal()
    sent = max(r["sent_at"] for r in journal.open_intents())
    reader = _Reader([([{"id": 1, "content": "present"}], {"served_by_primary": True})])
    ev = reconcile.gather_evidence(backend, journal, reader=reader, now=sent + 61)
    assert {v["status"] for v in ev.values()} == {"read"}
    assert journal.status()[0] == [1, 2, 3]  # evidence never resolves anything
    assert reader.calls[0][0].startswith("SELECT * FROM memories WHERE content IS ?")


def test_evidence_waits_until_the_bound(registry):
    _open_intent(registry, "INSERT INTO memories (content) VALUES (?)", ("x",))
    backend = storage.backend_for("remote")
    rec = backend.journal().open_intents()[0]
    reader = _Reader([([], {"served_by_primary": True})])
    ev = reconcile.gather_evidence(backend, backend.journal(), reader=reader, now=rec["sent_at"] + 10)
    assert ev[1]["status"] == "waiting" and reader.calls == []


def test_evidence_retries_until_served_by_primary(registry):
    _open_intent(registry, "UPDATE memories SET content = ? WHERE id = ?", ("x", 5))
    backend = storage.backend_for("remote")
    rec = backend.journal().open_intents()[0]
    reader = _Reader([([], {"served_by_primary": False}), ([], {"served_by_primary": False}),
                      ([{"id": 5}], {"served_by_primary": True})])
    slept = []
    ev = reconcile.gather_evidence(backend, backend.journal(), reader=reader, now=rec["sent_at"] + 61,
                                   sleep=slept.append)
    assert ev[1]["served_by_primary"] is True and len(reader.calls) == 3 and len(slept) == 2
    assert reader.calls[0] == ("SELECT * FROM memories WHERE id = ?", [5])


def test_evidence_without_read_token_is_reported_not_raised(registry, monkeypatch):
    monkeypatch.delenv("MEMORA_D1_READ_TOKEN", raising=False)
    _open_intent(registry, "DELETE FROM memories WHERE id = ?", (3,))
    backend = storage.backend_for("remote")
    rec = backend.journal().open_intents()[0]
    ev = reconcile.gather_evidence(backend, backend.journal(), now=rec["sent_at"] + 61)
    assert ev[1]["status"] == "error" and "MEMORA_D1_READ_TOKEN" in ev[1]["error"]


def test_accept_intent_requires_receipt_operator_id_decision_and_current_evidence(registry, tmp_path):
    _open_intent(registry, "INSERT INTO memories (content) VALUES (?)", ("x",))
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"db": "remote", "verified_at": "2026-09-23T00:00:00Z"}))
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"db": "other", "verified_at": "t"}))
    status, listing = admin.list_intents("remote", reader=_Reader([([], {"served_by_primary": True})]))
    digest = listing["open_intents"][0]["evidence_sha256"]
    ok = dict(operator="spok", body_intent_id=1, decision="not-applied", evidence_digest=digest)

    def accept(receipt=str(good), iid=1, **over):
        return admin.accept_intent("remote", iid, receipt, **{**ok, **over})

    assert accept(receipt=None)[1]["error"] == "receipt_required"
    assert accept(receipt=str(bad))[1]["error"] == "receipt_invalid"
    assert accept(operator=" ")[1]["error"] == "operator_required"
    assert accept(body_intent_id=2)[1]["error"] == "intent_id_mismatch"
    assert accept(decision="maybe")[1]["error"] == "decision_required"
    assert accept(evidence_digest="0" * 64)[1]["error"] == "evidence_changed"
    assert accept(iid=99, body_intent_id=99)[0] == 404
    assert storage.backend_for("remote").journal().status()[0] == [1]  # nothing accepted so far
    status, body = accept()
    assert status == 200 and body["open_intents"] == []
    recs = [json.loads(l) for l in storage.backend_for("remote").journal().path.read_text().splitlines()]
    assert recs[-1] == {**recs[-1], "outcome": "operator-accepted", "operator": "spok", "decision": "not-applied",
                        "evidence_sha256": digest}
    assert recs[-1]["receipt_sha256"]
    admin.freeze_store("remote", timeout_s=1)
    assert storage.backend_for("remote").write_gate().state == "frozen"


def test_accept_needs_evidence_to_have_been_gathered(registry, tmp_path):
    _open_intent(registry, "INSERT INTO memories (content) VALUES (?)", ("x",))
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"db": "remote", "verified_at": "t"}))
    status, body = admin.accept_intent("remote", 1, str(good), operator="spok", body_intent_id=1,
                                       decision="applied", evidence_digest="x")
    assert status == 409 and body["error"] == "no_evidence_yet"


def test_list_intents_handler(registry):
    _open_intent(registry, "INSERT INTO memories (content) VALUES (?)", ("x",))
    status, body = admin.list_intents("remote", reader=_Reader([([], {"served_by_primary": True})]))
    assert status == 200 and [i["id"] for i in body["open_intents"]] == [1]
    assert body["open_intents"][0]["evidence"]["status"] == "waiting"
    assert admin.list_intents("loc")[0] == 400


def test_http_routes_refuse_until_auth_is_installed(registry, monkeypatch):
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    routes = []

    class _MCP:
        def custom_route(self, path, methods):
            def deco(fn):
                routes.append(Route(path, fn, methods=methods))
                return fn
            return deco

    admin.register_admin_routes(_MCP())
    client = TestClient(Starlette(routes=routes))
    for method, path in (("post", "/admin/freeze/loc"), ("delete", "/admin/freeze/loc"),
                         ("get", "/admin/intents/remote"), ("post", "/admin/reconcile/remote/1")):
        r = getattr(client, method)(path, **({"json": {}} if path.startswith("/admin/reconcile") else {}))
        assert r.status_code == 403, path
    monkeypatch.setattr(admin, "_admin_auth", lambda request: None)
    r = client.post("/admin/freeze/loc?timeout_s=1")
    assert r.status_code == 200 and r.json()["state"] == "frozen"
    assert client.delete("/admin/freeze/loc").json()["state"] == "open"


def test_health_gate_fields(registry):
    from memora import health

    admin.freeze_store("loc", timeout_s=1)
    assert health._gate_fields("loc")["freeze"]["state"] == "frozen"
    _open_intent(registry, "INSERT INTO memories (content) VALUES (?)", ("x",))
    fields = health._gate_fields("remote")
    assert fields["freeze"]["open_intents"] == [1] and fields["journal"]["path"].endswith("remote.jsonl")
    payload = health._with_gate_fields({"databases": {"loc": {"status": "ok"}}})
    assert payload["databases"]["loc"]["freeze"]["state"] == "frozen"
    assert health._gate_fields("(default)") == {}


def test_startup_initialises_gates_journals_and_primary_locks(registry, tmp_path, monkeypatch):
    write_gate.persist_freeze("loc")
    monkeypatch.setenv("MEMORA_REPLICAS", json.dumps({"loc": "d1://acct/db1"}))
    write_gate._reset_for_tests()
    summary = write_gate.initialize_registry_gates()
    assert summary["loc"]["state"] == "frozen" and summary["remote"]["state"] == "open"
    assert (write_gate.data_dir() / "intent" / "remote.lock").exists()
    assert write_gate.fence_live_primaries() == {"loc": None}
    import subprocess
    import sys

    code = ("import sys; sys.path.insert(0, %r)\nfrom memora import backends\n"
            "try:\n    backends.acquire_primary_lock(%r)\nexcept backends.StoreLockedError:\n    sys.exit(3)\n"
            ) % (str(__import__("pathlib").Path(__file__).resolve().parent.parent), str(tmp_path / "loc.db"))
    try:
        assert subprocess.run([sys.executable, "-c", code], timeout=60).returncode == 3
    finally:
        backends.release_primary_lock(tmp_path / "loc.db")


def test_refused_store_is_reported_by_health(registry, tmp_path, monkeypatch):
    from memora import health

    monkeypatch.setenv("MEMORA_REPLICAS", json.dumps({"loc": "d1://acct/db1"}))
    backend = storage.backend_for("loc")
    backend.refused_reason = "held by another process"
    assert health._gate_fields("loc")["refused"] == "held by another process"
    assert write_gate.initialize_registry_gates()["loc"]["state"] == "refused"
    with pytest.raises(backends.StoreLockedError):
        backend.connect_read_only()
    backend.refused_reason = None
