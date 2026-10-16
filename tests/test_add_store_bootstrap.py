"""Item 95: bootstrap-d1 (memora/add_store.py) against a fake D1.

The fake is ScratchD1Connection: a real D1Connection subclass whose HTTP leg is
a local SQLite file, so ensure_schema takes its D1 branches. The statement
guard is the production one (the same mixin the HTTP connection uses).
"""

import json
import sqlite3

import pytest

from memora import add_store as ads
from memora import schema
from memora.add_store import AddStoreRefused
from memora.backends import D1Connection

ACCT = "a" * 32
DBID = "0f0f0f0f-1111-2222-3333-444444444444"
NAME = "newstore-test"


class Rig:
    """A fake D1 database file plus the injectable dependencies."""

    def __init__(self, tmp_path, *, info=None):
        self.path = str(tmp_path / "d1.sqlite")
        sqlite3.connect(self.path).close()
        self.info = info or {"uuid": DBID, "name": NAME}
        self.info_calls = 0
        self.writers = []
        self.read_hook = None
        self.info_hook = None
        self.configured_ids = set()
        self.configured_names = set()

    def _read(self, sql, params):
        if self.read_hook:
            self.read_hook(sql)
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(sql, params).fetchall()]
        finally:
            con.close()

    def _info(self):
        self.info_calls += 1
        if self.info_hook:
            return self.info_hook(self.info_calls)
        return dict(self.info)

    def _writer(self):
        w = ads.GuardedScratchD1Connection(self.path, ACCT, DBID)
        self.writers.append(w)
        return w

    def deps(self, tmp_path):
        return ads.BootstrapDeps(
            info=self._info, read=self._read, open_writer=self._writer,
            configured_ids=self.configured_ids, configured_names=self.configured_names,
            data_dir=tmp_path / "data")

    def run(self, tmp_path, db="newstore"):
        return ads.bootstrap_d1(db, account_id=ACCT, database_id=DBID, d1_name=NAME, deps=self.deps(tmp_path))

    def sent(self):
        return [s for w in self.writers for s in w.sent]


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


# ------------------------------------------------------------------ the statement guard

def test_the_guard_is_a_d1_connection_so_ensure_schema_takes_the_d1_branches(tmp_path):
    con = ads.GuardedScratchD1Connection(str(tmp_path / "g.sqlite"))
    assert isinstance(con, D1Connection)
    schema.ensure_schema(con)
    names = {r["name"] for r in con._sql.execute("SELECT name FROM sqlite_master")}
    assert "memories" in names and "memories_meta" in names
    assert not any(n.startswith("memories_fts") for n in names)       # D1 has no FTS5
    assert "api_idempotency" not in names                              # non-D1 only
    assert not any(n.startswith("sync_") or n.startswith("trg_sync_") for n in names)  # local only


def test_every_statement_ensure_schema_sends_passes_the_guard_and_data_is_one_row(tmp_path):
    con = ads.GuardedScratchD1Connection(str(tmp_path / "g.sqlite"))
    schema.ensure_schema(con)
    writes = [s for s in con.sent if not s.upper().startswith(("SELECT", "PRAGMA"))]
    assert writes, "the initializer sent no writes?"
    data = [s for s in writes if s.upper().startswith(("INSERT", "UPDATE", "DELETE"))]
    assert data == ["INSERT OR IGNORE INTO memories_meta(key, value) VALUES ('embedding_change_epoch', '0')"]


@pytest.mark.parametrize("sql", [
    "DROP TABLE memories",
    "DELETE FROM memories",
    "UPDATE memories SET content = 'x'",
    "INSERT INTO memories(content) VALUES ('x')",
    "INSERT OR IGNORE INTO memories_meta(key, value) VALUES ('embedding_change_epoch', '5')",
    "INSERT OR IGNORE INTO memories_meta(key, value) VALUES ('other', '0')",
    "CREATE TABLE IF NOT EXISTS t (a); DROP TABLE memories_meta",
    "CREATE TABLE IF NOT EXISTS arbitrary (a)",                       # an IF NOT EXISTS shape is not enough
    "CREATE INDEX IF NOT EXISTS idx_arbitrary ON memories(content)",
    "CREATE TRIGGER IF NOT EXISTS trg_arbitrary AFTER INSERT ON memories BEGIN UPDATE memories_meta SET value = 1; END",
    "ALTER TABLE memories ADD COLUMN arbitrary TEXT",
    "CREATE TABLE IF NOT EXISTS t AS SELECT * FROM memories",
    "CREATE TABLE t (a)",                                    # not IF NOT EXISTS
    "ALTER TABLE memories DROP COLUMN content",
    "ALTER TABLE memories RENAME TO other",
    "CREATE TRIGGER IF NOT EXISTS trg_evil AFTER INSERT ON memories BEGIN DELETE FROM memories; END",
    "CREATE TRIGGER IF NOT EXISTS trg_embedding_epoch_memories_insert AFTER INSERT ON memories "
    "BEGIN DELETE FROM memories; END",
    "SELECT 1; DROP TABLE memories",
    "PRAGMA journal_mode = DELETE",
    "BEGIN",
])
def test_the_guard_refuses_everything_ensure_schema_does_not_send(sql):
    with pytest.raises(AddStoreRefused):
        ads.check_bootstrap_statement(sql)


def test_the_guard_refuses_parameters_and_runs_before_the_send(tmp_path):
    con = ads.GuardedScratchD1Connection(str(tmp_path / "g.sqlite"))
    with pytest.raises(AddStoreRefused):
        con.execute("INSERT OR IGNORE INTO memories_meta(key, value) VALUES (?, ?)", ("embedding_change_epoch", "0"))
    with pytest.raises(AddStoreRefused):
        con.executescript("CREATE TABLE IF NOT EXISTS x (a); DELETE FROM x")
    # executescript sends statement by statement: the allowed CREATE went, the DELETE never reached the HTTP leg
    assert not any(sql.upper().startswith(("DELETE", "INSERT")) for sql in con.sent)


def test_selects_and_table_info_pass():
    for sql in ("SELECT 1", "PRAGMA table_info(memories)", "SELECT name FROM sqlite_master"):
        ads.check_bootstrap_statement(sql)


# ------------------------------------------------------------------ bootstrap, happy path

def test_bootstrap_initialises_a_pinned_empty_database(tmp_path, rig):
    out = rig.run(tmp_path)
    assert out["status"] == "initialised"
    state = ads.read_state("newstore", tmp_path / "data")
    assert state["phase"] == "verified"
    assert (state["account_id"], state["database_id"], state["d1_name"]) == (ACCT, DBID, NAME)
    # the app's schema, meta == only the epoch row '0', business tables empty
    live = ads.schema_signature(rig._read)
    assert ads.diff_signature(live, ads.reference_signature()) == []
    assert rig._read("SELECT key, value FROM memories_meta", ()) == [{"key": "embedding_change_epoch", "value": "0"}]
    assert out["tables"] and "memories" in out["tables"]
    for table in out["tables"]:
        assert rig._read(f'SELECT COUNT(*) AS n FROM "{table}"', ())[0]["n"] == 0
    mode = (tmp_path / "data" / "bootstrap" / "newstore.json").stat().st_mode & 0o777
    assert mode == 0o600


def test_nothing_but_the_initializer_statements_were_sent(tmp_path, rig):
    rig.run(tmp_path)
    for sql in rig.sent():
        ads.check_bootstrap_statement(sql)                       # each one is allowed
    assert not any(s.upper().startswith(("DELETE", "DROP", "UPDATE")) for s in rig.sent())


def test_a_complete_matching_database_is_verified_without_ddl(tmp_path, rig):
    rig.run(tmp_path)
    before = len(rig.sent())
    out = rig.run(tmp_path)
    assert out["status"] == "already-initialised"
    assert len(rig.writers) == 1 and len(rig.sent()) == before    # no writer opened, nothing sent


def test_a_complete_database_without_a_bootstrap_state_is_refused_not_adopted(tmp_path, rig):
    """P2-1: a lost state file is a manual recovery decision; a database already holding a matching schema is
    not known to be ours and is never adopted automatically."""
    rig.run(tmp_path)
    (tmp_path / "data" / "bootstrap" / "newstore.json").unlink()
    with pytest.raises(AddStoreRefused, match="no bootstrap state"):
        rig.run(tmp_path)
    assert ads.read_state("newstore", tmp_path / "data") is None            # nothing was written either
    assert len(rig.writers) == 1


def test_a_known_interrupted_bootstrap_with_a_complete_schema_is_verified_without_ddl(tmp_path, rig):
    """State says ddl_done (the crash came before 'verified'): a complete matching schema is verified only."""
    rig.run(tmp_path)
    ads._save_state("newstore", {"account_id": ACCT, "database_id": DBID, "d1_name": NAME, "phase": "ddl_done"},
                    tmp_path / "data")
    out = rig.run(tmp_path)
    assert out["status"] == "already-initialised" and len(rig.writers) == 1
    assert ads.read_state("newstore", tmp_path / "data")["phase"] == "verified"


# ------------------------------------------------------------------ the pins and gates

def test_a_wrong_name_is_refused_before_any_write(tmp_path, rig):
    rig.info = {"uuid": DBID, "name": "memora"}
    with pytest.raises(AddStoreRefused, match="not the pinned"):
        rig.run(tmp_path)
    assert rig.writers == []


def test_a_wrong_id_is_refused_before_any_write(tmp_path, rig):
    rig.info = {"uuid": "ffffffff-0000-0000-0000-000000000000", "name": NAME}
    with pytest.raises(AddStoreRefused):
        rig.run(tmp_path)
    assert rig.writers == []


def test_an_id_already_configured_is_refused(tmp_path, rig):
    rig.configured_ids = {DBID}
    with pytest.raises(AddStoreRefused, match="already configured"):
        rig.run(tmp_path)
    assert rig.writers == [] and rig.info_calls == 0


def test_a_store_name_already_configured_is_refused(tmp_path, rig):
    rig.configured_names = {"newstore"}
    with pytest.raises(AddStoreRefused, match="already in MEMORA_DATABASES"):
        rig.run(tmp_path)
    assert rig.writers == []


def test_bad_inputs_are_refused(tmp_path, rig):
    with pytest.raises(AddStoreRefused):
        ads.bootstrap_d1("Bad Name", account_id=ACCT, database_id=DBID, d1_name=NAME, deps=rig.deps(tmp_path))
    with pytest.raises(AddStoreRefused):
        ads.bootstrap_d1("newstore", account_id="x", database_id=DBID, d1_name=NAME, deps=rig.deps(tmp_path))
    with pytest.raises(AddStoreRefused):
        ads.bootstrap_d1("newstore", account_id=ACCT, database_id=DBID, d1_name="", deps=rig.deps(tmp_path))
    assert rig.writers == []


def test_a_read_token_that_cannot_read_is_refused_before_any_write(tmp_path, rig):
    def boom(sql):
        raise RuntimeError("HTTP 403")
    rig.read_hook = boom
    with pytest.raises(AddStoreRefused, match="cannot read"):
        rig.run(tmp_path)
    assert rig.writers == []


def test_state_for_other_ids_is_refused(tmp_path, rig):
    ads._save_state("newstore", {"account_id": ACCT, "database_id": "other-id-123456", "d1_name": NAME,
                               "phase": "verified"}, tmp_path / "data")
    with pytest.raises(AddStoreRefused, match="other ids"):
        rig.run(tmp_path)
    assert rig.writers == []


def test_emptiness_is_rechecked_immediately_before_the_ddl(tmp_path, rig):
    calls = {"n": 0}

    def hook(sql):
        # the first emptiness read is empty; before the DDL a table has appeared
        if "FROM sqlite_master" in sql and "type, name, tbl_name" in sql:
            calls["n"] += 1
            if calls["n"] == 2:
                sqlite3.connect(rig.path).execute("CREATE TABLE intruder (a)").connection.commit()
    rig.read_hook = hook
    with pytest.raises(AddStoreRefused, match="changed between"):
        rig.run(tmp_path)
    assert rig.writers == []


def test_identity_is_rechecked_after_the_ddl(tmp_path, rig):
    def info(n):
        return {"uuid": DBID, "name": NAME} if n < 3 else {"uuid": DBID, "name": "swapped"}
    rig.info_hook = info
    with pytest.raises(AddStoreRefused, match="after the DDL"):
        rig.run(tmp_path)
    assert ads.read_state("newstore", tmp_path / "data")["phase"] == "ddl_done"   # not 'verified'


# ------------------------------------------------------------------ non-empty and partial databases

def test_a_non_empty_database_with_a_foreign_schema_is_refused_with_no_ddl(tmp_path, rig):
    sqlite3.connect(rig.path).execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT)").connection.commit()
    ads._save_state("newstore", {"account_id": ACCT, "database_id": DBID, "d1_name": NAME, "phase": "ddl_started"},
                    tmp_path / "data")                                       # a known interrupted run, but a foreign schema
    with pytest.raises(AddStoreRefused, match="not what the app's initializer builds"):
        rig.run(tmp_path)
    assert rig.writers == []


def test_a_database_with_data_is_refused(tmp_path, rig):
    rig.run(tmp_path)
    con = sqlite3.connect(rig.path)
    con.execute("INSERT INTO memories(content) VALUES ('a user memory')")
    con.execute("UPDATE memories_meta SET value = '0'")   # the epoch trigger bumped it; isolate the emptiness check
    con.commit()
    con.close()
    with pytest.raises(AddStoreRefused, match="not empty"):
        rig.run(tmp_path)


def test_a_crash_mid_ddl_is_not_replayed_automatically(tmp_path, rig):
    class Crash(RuntimeError):
        pass

    real = rig._writer

    def crashing():
        w = real()
        orig = w._send
        seen = {"n": 0}

        def send(sql, params=None):
            if not sql.lstrip().upper().startswith(("SELECT", "PRAGMA")):
                seen["n"] += 1
                if seen["n"] == 4:
                    raise Crash("connection lost")
            return orig(sql, params)
        w._send = send
        return w
    rig._writer = crashing
    with pytest.raises(Crash):
        rig.run(tmp_path)
    assert ads.read_state("newstore", tmp_path / "data")["phase"] == "ddl_started"
    rig._writer = real
    n_writers = len(rig.writers)
    with pytest.raises(AddStoreRefused, match="no automatic replay"):
        rig.run(tmp_path)                                         # partial schema: refuse with the checkpoint
    assert len(rig.writers) == n_writers                          # no second DDL pass was opened


def test_a_crash_before_the_first_statement_can_be_retried(tmp_path, rig):
    class Crash(RuntimeError):
        pass

    real = rig._writer
    rig._writer = lambda: (_ for _ in ()).throw(Crash("cannot open"))
    with pytest.raises(Crash):
        rig.run(tmp_path)
    assert ads.read_state("newstore", tmp_path / "data")["phase"] == "ddl_started"
    rig._writer = real
    assert rig.run(tmp_path)["status"] == "initialised"           # still zero objects: a clean retry


def test_a_right_named_object_with_a_wrong_definition_is_rejected(tmp_path, rig):
    rig.run(tmp_path)
    con = sqlite3.connect(rig.path)
    con.execute("DROP INDEX idx_memories_embeddings_representation")
    con.execute("CREATE INDEX idx_memories_embeddings_representation ON memories_embeddings(dimension)")
    con.commit()
    con.close()
    diffs = ads.diff_signature(ads.schema_signature(rig._read), ads.reference_signature())
    assert any("idx_memories_embeddings_representation" in d for d in diffs)
    with pytest.raises(AddStoreRefused, match="definition differs"):
        rig.run(tmp_path)


def test_the_epoch_row_must_be_exactly_zero(tmp_path, rig):
    rig.run(tmp_path)
    con = sqlite3.connect(rig.path)
    con.execute("UPDATE memories_meta SET value = '7'")
    con.commit()
    con.close()
    with pytest.raises(AddStoreRefused, match="epoch row"):
        rig.run(tmp_path)


# ------------------------------------------------------------------ pre-freeze and clear-freeze

def test_pre_freeze_needs_a_verified_bootstrap(tmp_path, rig):
    with pytest.raises(AddStoreRefused, match="no verified bootstrap"):
        ads.pre_freeze("newstore", data_dir=tmp_path / "data")


def test_pre_freeze_writes_the_persisted_freeze_and_is_idempotent(tmp_path, rig, monkeypatch):
    from memora import write_gate

    rig.run(tmp_path)
    monkeypatch.setenv("MEMORA_DATA_DIR", str(tmp_path / "data"))
    out = ads.pre_freeze("newstore", data_dir=tmp_path / "data")
    assert out["already_present"] is False
    assert write_gate.freeze_file("newstore").exists()
    assert ads.pre_freeze("newstore", data_dir=tmp_path / "data")["already_present"] is True


def test_clear_freeze_refuses_while_the_service_routes_the_store(tmp_path, monkeypatch):
    from memora import write_gate

    monkeypatch.setenv("MEMORA_DATA_DIR", str(tmp_path / "data"))
    write_gate.persist_freeze("newstore")
    with pytest.raises(AddStoreRefused, match="still routes"):
        ads.clear_freeze("newstore", routes={"newstore": "d1://x/y", "memora": "/data/memora.db"})
    assert write_gate.freeze_file("newstore").exists()                # the freeze stays
    assert ads.clear_freeze("newstore", routes={"memora": "/data/memora.db"}) == {"removed": True}
    assert not write_gate.freeze_file("newstore").exists()
    assert ads.clear_freeze("newstore", routes={}) == {"removed": False}


# ------------------------------------------------------------------ config parsing

def test_configured_from_env_reads_ids_and_names():
    env = {
        "MEMORA_DATABASES": json.dumps({"memora": "/data/memora.db", "alpha": "d1://acct1/idalpha01"}),
        "MEMORA_REPLICAS": json.dumps({"memora": "d1://acct1/idmemora1"}),
    }
    ids, names = ads.configured_from_env(env)
    assert ids == {"idalpha01", "idmemora1"} and names == {"memora", "alpha"}
    assert ads.configured_from_env({}) == (set(), set())
    with pytest.raises(AddStoreRefused):
        ads.configured_from_env({"MEMORA_DATABASES": "not json"})


def test_the_allow_list_is_exactly_what_the_initializer_sends(tmp_path):
    writes = ads.reference_writes()
    con = ads.ScratchD1Connection(str(tmp_path / "r.sqlite"))
    schema.ensure_schema(con)
    sent = {ads._norm_stmt(q) for q in con.sent if not q.upper().startswith(("SELECT", "PRAGMA"))}
    assert sent == set(writes)                                         # no more, no less
    assert any(w.startswith("INSERT OR IGNORE INTO memories_meta") for w in writes)
    assert sum(w.startswith("CREATE TRIGGER") for w in writes) == 8


# ------------------------------------------------------------------ status: the preconditions of the resumable steps

def test_status_needs_a_verified_bootstrap_for_exactly_the_pinned_ids_and_the_freeze(tmp_path, rig, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv("MEMORA_DATA_DIR", str(data))
    pins = dict(account_id=ACCT, database_id=DBID, d1_name=NAME, data_dir=data)
    with pytest.raises(AddStoreRefused, match="no bootstrap state"):
        ads.status("newstore", **pins)
    rig.run(tmp_path)
    with pytest.raises(AddStoreRefused, match="persisted freeze .* is missing"):
        ads.status("newstore", **pins)
    assert ads.status("newstore", require_freeze=False, **pins)["phase"] == "verified"
    ads.pre_freeze("newstore", data_dir=data)
    assert ads.status("newstore", **pins)["freeze_file"] is True
    for field, other in (("database_id", "another-db-id-1"), ("account_id", "b" * 32), ("d1_name", "someone-else")):
        with pytest.raises(AddStoreRefused, match="other"):
            ads.status("newstore", **{**pins, field: other})
    ads._save_state("newstore", {"account_id": ACCT, "database_id": DBID, "d1_name": NAME, "phase": "ddl_started"}, data)
    with pytest.raises(AddStoreRefused, match="not verified"):
        ads.status("newstore", **pins)


# ------------------------------------------------------------------ verify_replication (the first real write)

class Repl:
    """A fake replicated store for verify_replication: health, local rows, D1 rows."""

    def __init__(self):
        self.local = {1: row(1, "a"), 2: row(2, "b"), 3: row(3, "c")}
        self.remote = {k: dict(v) for k, v in self.local.items()}
        self.h = {"http": 200, "status": "ok", "registry_entry": "/data/newstore.db", "freeze": {"state": "open"},
                  "replication": {"mode": "write", "interval_s": 60.0, "status": "running", "halted_reason": None,
                                  "head_seq": 12, "last_acked_seq": 12, "lag_rows": 0}}
        self.now = 0.0
        self.polls = 0
        self.on_poll = None

    def health(self):
        self.polls += 1
        if self.on_poll:
            self.on_poll(self)
        return self.h

    def sleep(self, s):
        self.now += s

    def run(self, **kw):
        args = dict(interval_s=60, expect_min_rows=3, timeout_s=150, health=self.health,
                    local_rows=lambda: list(self.local.values()), d1_rows=lambda ids: {i: self.remote[i] for i in ids if i in self.remote},
                    d1_count=lambda: len(self.remote), transactional=True, sleep=self.sleep, monotonic=lambda: self.now)
        args.update(kw)
        return ads.verify_replication("newstore", **args)


def row(i, text):
    return {"id": i, "content": text, "metadata": None, "tags": '["x"]', "created_at": "2026-10-16 10:00:00", "updated_at": None}


def failed(report):
    return [c["check"] for c in report["checks"] if not c["ok"]]


def test_verify_replication_passes_when_everything_is_proven():
    r = Repl()
    out = r.run()
    assert out["ok"] is True and out["rows"] == 3 and not failed(out)


@pytest.mark.parametrize("patch,check", [
    (lambda r: r.h["replication"].update(mode="log"), "replication mode write"),
    (lambda r: r.h["replication"].update(interval_s=5.0), "replication interval"),
    (lambda r: r.h["replication"].update(status="halted", halted_reason="delete_guard"), "replicator running, not halted"),
    (lambda r: r.h.update(freeze={"state": "frozen"}), "thawed"),
    (lambda r: r.h.update(registry_entry="d1://a/b"), "served from the local file"),
    (lambda r: r.h.update(status="degraded"), "health ok"),
])
def test_verify_replication_refuses_wrong_preconditions_without_waiting(patch, check):
    r = Repl()
    patch(r)
    out = r.run()
    assert out["ok"] is False and check in failed(out) and out["stopped"] == "preconditions"
    assert r.now == 0.0


def test_verify_replication_requires_the_store_to_be_a_transactional_primary():
    out = Repl().run(transactional=False)
    assert "writes are transactional (a local SQLite primary)" in failed(out)


def test_verify_replication_fails_when_the_ack_never_reaches_head():
    r = Repl()
    r.h["replication"].update(head_seq=20, last_acked_seq=12, lag_rows=8)
    out = r.run()
    assert out["ok"] is False and out["stopped"] == "ack did not reach head in time"
    assert r.now >= 150                                           # it waited the whole bound, no longer


def test_verify_replication_waits_for_a_late_ack():
    r = Repl()
    r.h["replication"].update(head_seq=20, last_acked_seq=12, lag_rows=8)

    def drain(rep):
        if rep.now >= 60:
            rep.h["replication"].update(last_acked_seq=20, lag_rows=0)
    r.on_poll = drain
    out = r.run()
    assert out["ok"] is True and 60 <= out["waited_s"] < 150


def test_verify_replication_needs_a_write_to_have_happened():
    r = Repl()
    r.h["replication"].update(head_seq=0, last_acked_seq=0)
    out = r.run(timeout_s=9)
    assert out["ok"] is False                                       # an empty store proves nothing


def test_verify_replication_detects_a_missing_a_differing_row_and_a_count_mismatch():
    r = Repl()
    del r.remote[2]
    out = r.run()
    assert "every local row is in D1 (read back through the read token)" in failed(out)
    r = Repl()
    r.remote[3]["content"] = "changed in D1"
    assert "every row's columns are identical in D1" in failed(r.run())
    r = Repl()
    r.remote[99] = row(99, "extra in D1")
    assert "D1 row count equals the local count" in failed(r.run())
    r = Repl()
    assert "at least 5 local rows" in failed(r.run(expect_min_rows=5))
