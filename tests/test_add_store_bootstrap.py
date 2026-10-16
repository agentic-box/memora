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
NAME = "acebar-test"


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

    def run(self, tmp_path, db="acebar"):
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
    state = ads.read_state("acebar", tmp_path / "data")
    assert state["phase"] == "verified"
    assert (state["account_id"], state["database_id"], state["d1_name"]) == (ACCT, DBID, NAME)
    # the app's schema, meta == only the epoch row '0', business tables empty
    live = ads.schema_signature(rig._read)
    assert ads.diff_signature(live, ads.reference_signature()) == []
    assert rig._read("SELECT key, value FROM memories_meta", ()) == [{"key": "embedding_change_epoch", "value": "0"}]
    assert out["tables"] and "memories" in out["tables"]
    for table in out["tables"]:
        assert rig._read(f'SELECT COUNT(*) AS n FROM "{table}"', ())[0]["n"] == 0
    mode = (tmp_path / "data" / "bootstrap" / "acebar.json").stat().st_mode & 0o777
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


def test_a_lost_state_file_with_a_complete_schema_is_recovered_by_verification(tmp_path, rig):
    rig.run(tmp_path)
    (tmp_path / "data" / "bootstrap" / "acebar.json").unlink()
    assert rig.run(tmp_path)["status"] == "already-initialised"
    assert ads.read_state("acebar", tmp_path / "data")["phase"] == "verified"


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
    rig.configured_names = {"acebar"}
    with pytest.raises(AddStoreRefused, match="already in MEMORA_DATABASES"):
        rig.run(tmp_path)
    assert rig.writers == []


def test_bad_inputs_are_refused(tmp_path, rig):
    with pytest.raises(AddStoreRefused):
        ads.bootstrap_d1("Bad Name", account_id=ACCT, database_id=DBID, d1_name=NAME, deps=rig.deps(tmp_path))
    with pytest.raises(AddStoreRefused):
        ads.bootstrap_d1("acebar", account_id="x", database_id=DBID, d1_name=NAME, deps=rig.deps(tmp_path))
    with pytest.raises(AddStoreRefused):
        ads.bootstrap_d1("acebar", account_id=ACCT, database_id=DBID, d1_name="", deps=rig.deps(tmp_path))
    assert rig.writers == []


def test_a_read_token_that_cannot_read_is_refused_before_any_write(tmp_path, rig):
    def boom(sql):
        raise RuntimeError("HTTP 403")
    rig.read_hook = boom
    with pytest.raises(AddStoreRefused, match="cannot read"):
        rig.run(tmp_path)
    assert rig.writers == []


def test_state_for_other_ids_is_refused(tmp_path, rig):
    ads._save_state("acebar", {"account_id": ACCT, "database_id": "other-id-123456", "d1_name": NAME,
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
    assert ads.read_state("acebar", tmp_path / "data")["phase"] == "ddl_done"   # not 'verified'


# ------------------------------------------------------------------ non-empty and partial databases

def test_a_non_empty_database_with_a_foreign_schema_is_refused_with_no_ddl(tmp_path, rig):
    sqlite3.connect(rig.path).execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT)").connection.commit()
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
    (tmp_path / "data" / "bootstrap" / "acebar.json").unlink()
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
    assert ads.read_state("acebar", tmp_path / "data")["phase"] == "ddl_started"
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
    assert ads.read_state("acebar", tmp_path / "data")["phase"] == "ddl_started"
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
    (tmp_path / "data" / "bootstrap" / "acebar.json").unlink()
    with pytest.raises(AddStoreRefused, match="definition differs"):
        rig.run(tmp_path)


def test_the_epoch_row_must_be_exactly_zero(tmp_path, rig):
    rig.run(tmp_path)
    con = sqlite3.connect(rig.path)
    con.execute("UPDATE memories_meta SET value = '7'")
    con.commit()
    con.close()
    (tmp_path / "data" / "bootstrap" / "acebar.json").unlink()
    with pytest.raises(AddStoreRefused, match="epoch row"):
        rig.run(tmp_path)


# ------------------------------------------------------------------ pre-freeze and clear-freeze

def test_pre_freeze_needs_a_verified_bootstrap(tmp_path, rig):
    with pytest.raises(AddStoreRefused, match="no verified bootstrap"):
        ads.pre_freeze("acebar", data_dir=tmp_path / "data")


def test_pre_freeze_writes_the_persisted_freeze_and_is_idempotent(tmp_path, rig, monkeypatch):
    from memora import write_gate

    rig.run(tmp_path)
    monkeypatch.setenv("MEMORA_DATA_DIR", str(tmp_path / "data"))
    out = ads.pre_freeze("acebar", data_dir=tmp_path / "data")
    assert out["already_present"] is False
    assert write_gate.freeze_file("acebar").exists()
    assert ads.pre_freeze("acebar", data_dir=tmp_path / "data")["already_present"] is True


def test_clear_freeze_refuses_while_the_service_routes_the_store(tmp_path, monkeypatch):
    from memora import write_gate

    monkeypatch.setenv("MEMORA_DATA_DIR", str(tmp_path / "data"))
    write_gate.persist_freeze("acebar")
    with pytest.raises(AddStoreRefused, match="still routes"):
        ads.clear_freeze("acebar", routes={"acebar": "d1://x/y", "memora": "/data/memora.db"})
    assert write_gate.freeze_file("acebar").exists()                # the freeze stays
    assert ads.clear_freeze("acebar", routes={"memora": "/data/memora.db"}) == {"removed": True}
    assert not write_gate.freeze_file("acebar").exists()
    assert ads.clear_freeze("acebar", routes={}) == {"removed": False}


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
