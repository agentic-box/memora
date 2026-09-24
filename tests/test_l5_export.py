"""L5 piece a: verified export, receipt, recheck and the freeze client
(docs/local-primary-implementation.md §0 P1, §4, §9 p).

Offline: D1 is tests/l3_fakes.FakeReplica read through the REAL
D1SelectOnlyConnection (its HTTP post patched), R2 is a directory (FsR2),
and memora-all's freeze/health routes are a local HTTP server
(FreezeServer) whose answers a test can script. The subprocess tests run
scripts/local_primary.py through tests/l5_cli_harness.py.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from memora import local_primary as lp
from tests.l3_fakes import FakeReplica

REPO = Path(__file__).resolve().parent.parent
HARNESS = REPO / "tests" / "l5_cli_harness.py"
DB = "memora-main"


# ---------------------------------------------------------------- fakes

def seed_replica(replica: FakeReplica, n: int = 3) -> None:
    db = replica._db()
    try:
        for i in range(1, n + 1):
            db.execute("INSERT INTO memories (content, metadata, tags, created_at) VALUES (?, ?, ?, ?)",
                       (f"memory {i} it's quoted", json.dumps({"i": i, "f": 1.5}), json.dumps(["t"]),
                        "2026-09-01T00:00:00"))
            db.execute("INSERT INTO memories_embeddings (memory_id, embedding) VALUES (?, ?)",
                       (i, json.dumps({"x": float(i)})))
        db.execute("INSERT INTO memories_crossrefs (memory_id, related) VALUES (1, ?)",
                   (json.dumps([{"id": 2, "score": 0.5}]),))
        db.execute("INSERT INTO memories_meta (key, value) VALUES ('other', 'v')")
        db.commit()
    finally:
        db.close()


def replica_exec(replica: FakeReplica, sql: str, params=()) -> None:
    db = replica._db()
    try:
        db.execute(sql, params)
        db.commit()
    finally:
        db.close()


class FakeBarrier:
    """In-process freeze barrier: records calls; `bad_at` makes check()
    refuse at the named step boundary; require() refuses unless frozen."""

    def __init__(self, bad_at=None, frozen=False):
        self.calls = []
        self.bad_at = bad_at
        self.frozen = frozen

    def freeze(self):
        self.calls.append("freeze")
        self.frozen = True

    def check(self, where):
        self.calls.append(f"check {where}")
        if not self.frozen or (self.bad_at and self.bad_at in where):
            raise lp.L5Refused(f"not frozen {where}")

    def require(self, where):
        self.check(where)

    def thaw(self):
        self.calls.append("thaw")
        self.frozen = False


def load(path, **kw):
    return lp.load_receipt(str(path), DB, account_id="acct", database_id="replica-db", **kw)


@pytest.fixture
def replica(tmp_path):
    r = FakeReplica(tmp_path / "d1.db")
    seed_replica(r)
    return r


def make_deps(replica, tmp_path, barrier=None, reader=None, r2=None, **kw):
    return lp.Deps(reader=reader or lp.D1Reader(replica.reader()), freeze=barrier or FakeBarrier(frozen=True),
                   r2=r2 or lp.FsR2(tmp_path / "r2"), account_id="acct", database_id="replica-db",
                   log=lambda msg: None, **kw)


def d1_stats(replica):
    reader = lp.D1Reader(replica.reader())
    return lp.remote_stats(reader, reader.hashed_tables())


# ---------------------------------------------------------------- credentials

def test_credential_file_must_be_0600_regular_owned_and_non_empty(tmp_path):
    f = tmp_path / "tok"
    f.write_text("secret\n")
    f.chmod(0o600)
    assert lp.load_credential_file(str(f)) == "secret"
    f.chmod(0o640)
    with pytest.raises(lp.L5Refused, match="0600"):
        lp.load_credential_file(str(f))
    f.chmod(0o600)
    f.write_text("  \n")
    with pytest.raises(lp.L5Refused, match="empty"):
        lp.load_credential_file(str(f))
    with pytest.raises(lp.L5Refused, match="regular"):
        lp.load_credential_file(str(tmp_path))
    with pytest.raises(lp.L5Refused):
        lp.load_credential_file(str(tmp_path / "missing"))


@pytest.mark.parametrize("mode", [0o400, 0o700, 0o644, 0o660, 0o604])
def test_credential_file_mode_must_be_exactly_0600(tmp_path, mode):
    """7633 (L2a's rule): not merely "no group/other bits" -- exactly 0600,
    and the file is not chmod-ed."""
    f = tmp_path / "tok"
    f.write_text("secret")
    f.chmod(mode)
    with pytest.raises(lp.L5Refused, match="must be mode 0600"):
        lp.load_credential_file(str(f))
    assert (f.stat().st_mode & 0o777) == mode


def test_a_symlinked_credential_file_is_refused_even_to_a_0600_file(tmp_path):
    real = tmp_path / "real.tok"
    real.write_text("secret")
    real.chmod(0o600)
    link = tmp_path / "link.tok"
    link.symlink_to(real)
    with pytest.raises(lp.L5Refused, match="is a symlink"):
        lp.load_credential_file(str(link))
    assert lp.load_credential_file(str(real)) == "secret"


def test_the_freeze_client_needs_its_own_health_token():
    """7633: memora-all refuses equal admin/health tokens, so the client never
    defaults the health token to the admin token."""
    with pytest.raises(lp.L5Refused, match="needs the health token"):
        lp.FreezeClient("http://127.0.0.1:9", "admin", DB, health_token="")
    with pytest.raises(lp.L5Refused, match="must differ"):
        lp.FreezeClient("http://127.0.0.1:9", "same", DB, health_token="same")


def test_read_token_comes_from_the_file_or_the_read_token_variable_only(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMORA_D1_READ_TOKEN", raising=False)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "edit-token-must-not-be-used")
    with pytest.raises(lp.L5Refused, match="no D1 read token"):
        lp.read_token()
    monkeypatch.setenv("MEMORA_D1_READ_TOKEN", "read")
    assert lp.read_token() == "read"


# ---------------------------------------------------------------- hashing

def test_hashes_treat_d1_json_values_like_sqlite_values():
    cols = ["a", "b", "c"]
    sqlite_row = {"a": 1.0, "b": b"\x00\xff", "c": "x"}
    d1_row = {"a": 1, "b": [0, 255], "c": "x"}
    assert lp.table_stats([sqlite_row], cols) == lp.table_stats([d1_row], cols)
    assert lp.table_stats([d1_row], cols) != lp.table_stats([{**d1_row, "c": "y"}], cols)


def test_export_skips_fts_cloudflare_and_sqlite_internal_tables():
    assert not lp._user_table("memories_fts")
    assert not lp._user_table("memories_fts_data")
    assert not lp._user_table("_cf_KV")
    assert not lp._user_table("sqlite_sequence")
    assert lp._user_table("memories")


# ---------------------------------------------------------------- export (P1)

def test_export_writes_a_verified_receipt_with_matching_r2_copy(replica, tmp_path):
    barrier = FakeBarrier()
    deps = make_deps(replica, tmp_path, barrier)
    rpath = lp.export(DB, deps, tmp_path / "exports")
    r = json.loads(rpath.read_text())
    assert r["version"] == 1 and r["db"] == DB and r["method"] == "select"
    assert r["epoch"] == replica.epoch()
    assert r["tables"] == d1_stats(replica)
    assert r["tables"]["memories"]["count"] == 3
    sql = Path(r["sql_path"])
    assert lp._sha256_file(sql) == r["sql_sha256"] == r["r2_sha256"]
    assert (tmp_path / "r2" / r["r2_key"]).read_bytes() == sql.read_bytes()
    # the export loads into SQLite with the same per-table hashes and the D1 sequence
    scratch = tmp_path / "load.db"
    lp.load_sql(sql, scratch)
    assert lp.local_stats(scratch, sorted(r["tables"])) == r["tables"]
    db = sqlite3.connect(scratch)
    assert db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'memories'").fetchone()[0] == 3
    db.close()
    # the freeze spans the whole export, every boundary is re-checked (§9 p),
    # and it is left in place (7621 P1-2: only `thaw` lifts it)
    assert barrier.calls == ["freeze", "check before the export", "check after the export",
                             "check before the receipt"]
    assert barrier.frozen
    assert load(rpath)["sql_sha256"] == r["sql_sha256"]
    assert r["d1_uri"] == "d1://acct/replica-db" and "sqlite_sequence" in r["tables"]


def test_export_keeps_d1s_autoincrement_counter_past_deleted_high_ids(replica, tmp_path):
    """7621 P1-1: rows 4..9 were used and deleted on D1 (its counter is 9,
    max(id) is 3). The loaded export must issue id 10 next, not 4."""
    db = replica._db()
    try:
        for i in range(6):
            db.execute("INSERT INTO memories (content) VALUES (?)", (f"gone {i}",))
        db.execute("DELETE FROM memories WHERE id > 3")
        db.commit()
    finally:
        db.close()
    r = json.loads(lp.export(DB, make_deps(replica, tmp_path), tmp_path / "exports").read_text())
    scratch = tmp_path / "load.db"
    lp.load_sql(Path(r["sql_path"]), scratch)
    db = sqlite3.connect(scratch)
    try:
        assert db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'memories'").fetchall() == [(9,)]
        assert db.execute("INSERT INTO memories (content) VALUES ('next')").lastrowid == 10
    finally:
        db.close()


def test_sequence_rows_created_out_of_name_order_still_verify(replica, tmp_path):
    """D1 created memories_events' counter before memories_actions'; the
    load rewrites them in name order, so rowid order differs -- the hash
    orders sqlite_sequence by name on both sides."""
    replica_exec(replica, "INSERT INTO memories_events (memory_id, tags) VALUES (1, '[]')")
    replica_exec(replica, "INSERT INTO memories_actions (memory_id, action, summary) VALUES (1, 'a', 's')")
    names = [r[0] for r in sqlite3.connect(replica.path).execute("SELECT name FROM sqlite_sequence ORDER BY rowid")]
    assert names.index("memories_events") < names.index("memories_actions")
    r = json.loads(lp.export(DB, make_deps(replica, tmp_path), tmp_path / "exports").read_text())
    assert r["tables"]["sqlite_sequence"]["count"] == 3


class SequenceMovingReader(lp.D1Reader):
    """D1's counter moves right after the dump (no row or epoch change): only
    the sqlite_sequence hash can notice."""

    def __init__(self, replica, moves):
        super().__init__(replica.reader())
        self.replica, self.moves = replica, moves

    def indexes_and_triggers(self):
        out = super().indexes_and_triggers()
        if self.moves:
            self.moves -= 1
            replica_exec(self.replica, "UPDATE sqlite_sequence SET seq = seq + 5 WHERE name = 'memories'")
        return out


def test_a_sequence_that_differs_from_the_export_is_a_hash_mismatch(replica, tmp_path):
    deps = make_deps(replica, tmp_path, reader=SequenceMovingReader(replica, moves=3))
    with pytest.raises(lp.L5Refused, match=r"does not match D1 in \['sqlite_sequence'\]"):
        lp.export(DB, deps, tmp_path / "exports")
    deps = make_deps(replica, tmp_path, reader=SequenceMovingReader(replica, moves=1))
    r = json.loads(lp.export(DB, deps, tmp_path / "exports").read_text())
    assert r["tables"]["sqlite_sequence"] == d1_stats(replica)["sqlite_sequence"]


def test_recheck_notices_a_moved_sequence(replica, tmp_path, receipt):
    replica_exec(replica, "UPDATE sqlite_sequence SET seq = 50 WHERE name = 'memories'")
    out = lp.recheck(DB, str(receipt), make_deps(replica, tmp_path), tmp_path / "exports")
    assert out != receipt


class MovingEpochReader(lp.D1Reader):
    """Bumps D1's epoch during the first `moves` exports (a writer that got
    past the barrier)."""

    def __init__(self, replica, moves):
        super().__init__(replica.reader())
        self.replica, self.moves = replica, moves

    def indexes_and_triggers(self):  # once per dump, between the epoch reads
        if self.moves:
            self.moves -= 1
            replica_exec(self.replica, "UPDATE memories_meta SET value = value + 1 WHERE key = 'embedding_change_epoch'")
        return super().indexes_and_triggers()


def test_export_retries_when_the_epoch_moves_and_records_the_stable_epoch(replica, tmp_path):
    deps = make_deps(replica, tmp_path, reader=MovingEpochReader(replica, moves=2))
    r = json.loads(lp.export(DB, deps, tmp_path / "exports").read_text())
    assert r["epoch"] == replica.epoch()


def test_export_refuses_after_three_moving_epochs_and_writes_nothing(replica, tmp_path):
    barrier = FakeBarrier()
    deps = make_deps(replica, tmp_path, barrier, reader=MovingEpochReader(replica, moves=3))
    with pytest.raises(lp.L5Refused, match="epoch moved"):
        lp.export(DB, deps, tmp_path / "exports")
    assert not list((tmp_path / "exports").rglob("*.receipt.json"))
    assert not (tmp_path / "r2").exists()
    assert "thaw" not in barrier.calls and barrier.frozen


class ChangingDataReader(lp.D1Reader):
    """Changes a row's CONTENT (same count) right after each of the first
    `changes` exports, so the post-export hashes disagree with the file."""

    def __init__(self, replica, changes):
        super().__init__(replica.reader())
        self.replica, self.changes = replica, changes

    def sequences(self):
        out = super().sequences()
        if self.changes:
            self.changes -= 1
            replica_exec(self.replica, "UPDATE memories_crossrefs SET related = related || ' ' WHERE memory_id = 1")
        return out


def test_export_retries_on_a_content_mismatch_with_equal_counts(replica, tmp_path):
    deps = make_deps(replica, tmp_path, reader=ChangingDataReader(replica, changes=1))
    r = json.loads(lp.export(DB, deps, tmp_path / "exports").read_text())
    assert r["tables"] == d1_stats(replica)


def test_export_refuses_after_three_mismatches(replica, tmp_path):
    deps = make_deps(replica, tmp_path, reader=ChangingDataReader(replica, changes=3))
    with pytest.raises(lp.L5Refused, match=r"does not match D1 in \['memories_crossrefs'\]"):
        lp.export(DB, deps, tmp_path / "exports")
    assert not list((tmp_path / "exports").rglob("*.receipt.json"))


class CorruptingR2(lp.FsR2):
    def get(self, key):
        return super().get(key) + b"-- corrupted\n"


def test_export_refuses_when_the_r2_read_back_differs(replica, tmp_path):
    deps = make_deps(replica, tmp_path, r2=CorruptingR2(tmp_path / "r2"))
    with pytest.raises(lp.L5Refused, match="R2 read-back"):
        lp.export(DB, deps, tmp_path / "exports")
    assert not list((tmp_path / "exports").rglob("*.receipt.json"))


@pytest.mark.parametrize("where", ["before the export", "after the export", "before the receipt"])
def test_a_failed_freeze_check_at_any_boundary_refuses_and_keeps_the_freeze(replica, tmp_path, where):
    barrier = FakeBarrier(bad_at=where)
    with pytest.raises(lp.L5Refused, match=where):
        lp.export(DB, make_deps(replica, tmp_path, barrier), tmp_path / "exports")
    assert not list((tmp_path / "exports").rglob("*.receipt.json"))
    assert "thaw" not in barrier.calls


def test_native_export_runs_with_an_environment_built_from_scratch(replica, tmp_path, monkeypatch):
    """§4 / §6.1: wrangler sees PATH, HOME, the account and the READ token --
    nothing inherited. The recorder writes the SAME dump a SELECT export
    would, so the receipt verifies and says method=native."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    dump = tmp_path / "dump.sql"
    lp.export_select(lp.D1Reader(replica.reader()), dump)
    env_log = tmp_path / "env.json"
    npx = bin_dir / "npx"
    npx.write_text("#!/bin/sh\n"
                   f"{sys.executable} -c 'import json,os,sys; json.dump({{\"env\": dict(os.environ), \"argv\": sys.argv[1:]}}, open(\"{env_log}\", \"w\"))' \"$@\"\n"
                   'while [ "$1" != "--output" ]; do shift; done\n'
                   f'cp "{dump}" "$2"\n')
    npx.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "the-service-edit-token")
    monkeypatch.setenv("MEMORA_D1_REPLICATOR_TOKEN", "the-replicator-token")
    deps = make_deps(replica, tmp_path, native_export=True, read_token="the-read-token", d1_name="memora-d1")
    r = json.loads(lp.export(DB, deps, tmp_path / "exports").read_text())
    assert r["method"] == "native"
    seen = json.loads(env_log.read_text())
    env = {k: v for k, v in seen["env"].items() if k not in ("PWD", "SHLVL", "_", "OLDPWD")}
    assert set(env) <= {"PATH", "HOME", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN", "LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
    assert env["CLOUDFLARE_API_TOKEN"] == "the-read-token"
    assert env["CLOUDFLARE_ACCOUNT_ID"] == "acct"
    assert seen["argv"][:5] == ["wrangler", "d1", "export", "memora-d1", "--remote"]


def test_a_refused_native_export_falls_back_to_the_paged_select(replica, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "npx").write_text("#!/bin/sh\necho 'Authentication error [code: 10000]' >&2\nexit 1\n")
    (bin_dir / "npx").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    logs = []
    deps = make_deps(replica, tmp_path, native_export=True, read_token="t")
    deps.log = logs.append
    r = json.loads(lp.export(DB, deps, tmp_path / "exports").read_text())
    assert r["method"] == "select" and r["tables"] == d1_stats(replica)
    assert any("Authentication error" in m for m in logs)


# ---------------------------------------------------------------- receipts

@pytest.fixture
def receipt(replica, tmp_path):
    return lp.export(DB, make_deps(replica, tmp_path), tmp_path / "exports")


def _edit(path, **changes):
    r = json.loads(Path(path).read_text())
    r.update(changes)
    Path(path).write_text(json.dumps(r))


@pytest.mark.parametrize("change, match", [
    ({"db": "other"}, "is for 'other'"),
    ({"version": 2}, "version-1"),
    ({"r2_sha256": "0" * 64}, "not verified"),
    ({"verified_at_epoch": 0}, "older than 24 h"),
])
def test_an_unusable_receipt_is_refused(receipt, change, match):
    _edit(receipt, **change)
    with pytest.raises(lp.L5Refused, match=match):
        load(receipt)


def test_a_receipt_whose_export_file_changed_is_refused(receipt):
    sql = Path(json.loads(receipt.read_text())["sql_path"])
    sql.write_text(sql.read_text() + "-- edited\n")
    with pytest.raises(lp.L5Refused, match="missing or changed"):
        load(receipt)


def test_a_receipt_is_accepted_up_to_24_hours(receipt):
    r = json.loads(receipt.read_text())
    load(receipt, now=r["verified_at_epoch"] + lp.RECEIPT_MAX_AGE_S - 1)
    with pytest.raises(lp.L5Refused, match="older than 24 h"):
        load(receipt, now=r["verified_at_epoch"] + lp.RECEIPT_MAX_AGE_S + 1)


@pytest.mark.parametrize("account, database", [("acct", "other-db"), ("other-acct", "replica-db")])
def test_a_receipt_for_another_d1_database_with_the_same_name_is_refused(receipt, account, database):
    """7621 P1-3: the store name, data and epoch may all match; the D1
    identity must too."""
    with pytest.raises(lp.L5Refused, match="another D1 database"):
        lp.load_receipt(str(receipt), DB, account_id=account, database_id=database)
    assert load(receipt)["database_id"] == "replica-db"


@pytest.mark.parametrize("field, value", [("database_id", "other-db"), ("d1_uri", "d1://acct/other-db"),
                                          ("account_id", "x")])
def test_a_receipt_whose_d1_identity_was_edited_is_refused(receipt, field, value):
    _edit(receipt, **{field: value})
    with pytest.raises(lp.L5Refused, match="another D1 database"):
        load(receipt)


def test_recheck_refuses_a_receipt_of_another_d1_database(replica, tmp_path, receipt):
    deps = make_deps(replica, tmp_path)
    deps.database_id = "other-db"
    with pytest.raises(lp.L5Refused, match="another D1 database"):
        lp.recheck(DB, str(receipt), deps, tmp_path / "exports")


# ---------------------------------------------------------------- recheck (P1)

def test_recheck_of_an_unchanged_d1_returns_the_same_receipt_and_keeps_the_freeze(replica, tmp_path, receipt):
    barrier = FakeBarrier(frozen=True)
    out = lp.recheck(DB, str(receipt), make_deps(replica, tmp_path, barrier), tmp_path / "exports")
    assert out == receipt
    assert barrier.calls == ["check before the recheck", "check after the recheck"]  # no freeze, no thaw
    assert barrier.frozen


def test_recheck_without_a_freeze_in_place_is_refused(replica, tmp_path, receipt):
    """7621 P1-2: a recheck never places (or lifts) a freeze of its own."""
    barrier = FakeBarrier(frozen=False)
    with pytest.raises(lp.L5Refused, match="not frozen before the recheck"):
        lp.recheck(DB, str(receipt), make_deps(replica, tmp_path, barrier), tmp_path / "exports")
    assert "freeze" not in barrier.calls and not barrier.frozen


@pytest.mark.parametrize("change", [
    "UPDATE memories_embeddings SET embedding = '{\"x\": 9.0}' WHERE memory_id = 2",  # content, same count, epoch trigger
    "UPDATE memories_meta SET value = 'w' WHERE key = 'other'",                        # content only, NO epoch move
    "CREATE TABLE extra (k TEXT PRIMARY KEY)",                                          # a new table
])
def test_recheck_takes_a_fresh_export_when_d1_changed(replica, tmp_path, receipt, change):
    old = json.loads(receipt.read_text())
    replica_exec(replica, change)
    out = lp.recheck(DB, str(receipt), make_deps(replica, tmp_path), tmp_path / "exports",)
    assert out != receipt
    new = json.loads(out.read_text())
    assert new["tables"] == d1_stats(replica) and new["tables"] != old["tables"]
    assert load(receipt)["tables"] == old["tables"]  # the earlier export is kept


def test_recheck_refuses_a_stale_receipt_before_touching_d1(replica, tmp_path, receipt):
    _edit(receipt, verified_at_epoch=0)
    barrier = FakeBarrier(frozen=True)
    with pytest.raises(lp.L5Refused, match="older than 24 h"):
        lp.recheck(DB, str(receipt), make_deps(replica, tmp_path, barrier), tmp_path / "exports")
    assert barrier.calls == []


# ---------------------------------------------------------------- the freeze client (§1, §9 p)

class FreezeServer:
    """memora-all's /admin/freeze and /health/db routes, scripted. `health`
    is a list of freeze dicts returned by successive GETs (the last one
    repeats); POST moves the store to frozen unless post_status says no."""

    def __init__(self, db=DB, health=None, post_status=200, delete_status=200, already_frozen=False):
        self.db = db
        self.requests = []
        self.intents = {"state": "frozen", "open_intents": []}  # GET /admin/intents/<db>
        self.reconcile_bodies = []                              # POST /admin/reconcile/<db>/<id>
        self.reconcile_status = 200
        self.health_extra = {}  # merged into /health/db (e.g. {"journal": ...} for a d1:// store)
        self.health = list(health or [])
        self.post_status = post_status
        self.delete_status = delete_status
        self.state = "frozen" if already_frozen else "open"
        server = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self):
                server.requests.append((self.command, self.path.split("?")[0], self.headers.get("Authorization")))
                path = self.path.split("?")[0]
                if path == f"/health/db/{server.db}" and self.command == "GET":
                    if server.state == "open":
                        return self._reply(200, {"status": "ok", "freeze": {"state": "open", "in_flight": 0, "open_intents": []}})
                    fr = server.health.pop(0) if len(server.health) > 1 else (server.health[0] if server.health else None)
                    return self._reply(200, {"status": "ok", **server.health_extra,
                                             "freeze": fr or {"state": "frozen", "in_flight": 0, "open_intents": []}})
                if path == f"/admin/freeze/{server.db}" and self.command == "POST":
                    if server.post_status == 200:
                        server.state = "frozen"
                    return self._reply(server.post_status, {"state": server.state})
                if path == f"/admin/intents/{server.db}" and self.command == "GET":
                    return self._reply(200, server.intents)
                if path.startswith(f"/admin/reconcile/{server.db}/") and self.command == "POST":
                    n = int(self.headers.get("Content-Length") or 0)
                    server.reconcile_bodies.append((path.rsplit("/", 1)[1], json.loads(self.rfile.read(n) or b"{}")))
                    return self._reply(server.reconcile_status, {"outcome": "operator-accepted"}
                                       if server.reconcile_status == 200 else {"error": "evidence_changed"})
                if path == f"/admin/freeze/{server.db}" and self.command == "DELETE":
                    if server.delete_status == 200:
                        server.state = "open"
                    return self._reply(server.delete_status, {"state": server.state})
                return self._reply(404, {"status": "unknown"})

            do_GET = do_POST = do_DELETE = _handle

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def methods(self):
        return [(m, p) for m, p, _ in self.requests]


@pytest.fixture
def freeze_server():
    servers = []

    def make(**kw):
        s = FreezeServer(**kw)
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.close()


def test_freeze_client_places_checks_and_thaws_only_on_request(freeze_server):
    srv = freeze_server()
    c = lp.FreezeClient(srv.url, "admin-tok", DB, health_token="health-tok")
    c.freeze()
    c.check("mid-step")
    assert srv.state == "frozen"
    c.thaw()
    assert srv.methods() == [("GET", f"/health/db/{DB}"), ("POST", f"/admin/freeze/{DB}"),
                             ("GET", f"/health/db/{DB}"), ("GET", f"/health/db/{DB}"),
                             ("DELETE", f"/admin/freeze/{DB}")]
    auth = {(m, a) for m, _p, a in srv.requests}
    assert auth == {("GET", "Bearer health-tok"), ("POST", "Bearer admin-tok"), ("DELETE", "Bearer admin-tok")}
    assert srv.state == "open"


def test_freeze_client_leaves_an_operator_placed_freeze_in_place(freeze_server):
    srv = freeze_server(already_frozen=True)
    c = lp.FreezeClient(srv.url, "t", DB, health_token="h")
    c.freeze()
    c.require("mid-step")
    assert ("POST", f"/admin/freeze/{DB}") not in srv.methods()
    assert ("DELETE", f"/admin/freeze/{DB}") not in srv.methods()
    assert srv.state == "frozen"


@pytest.mark.parametrize("fr, match", [
    ({"state": "frozen", "in_flight": 1, "open_intents": []}, "in_flight=1"),  # §9 p: an exempt replicator send
    ({"state": "frozen-unsafe", "in_flight": 0, "open_intents": [7]}, "frozen-unsafe"),
    ({"state": "frozen", "in_flight": 0, "open_intents": [7]}, r"open_intents=\[7\]"),
    ({"state": "draining", "in_flight": 0, "open_intents": []}, "draining"),
])
def test_freeze_check_refuses_anything_but_frozen_with_nothing_in_flight(freeze_server, fr, match):
    srv = freeze_server(health=[fr])
    c = lp.FreezeClient(srv.url, "t", DB, health_token="h")
    with pytest.raises(lp.L5Refused, match=match):
        c.freeze()
    assert srv.state == "frozen"  # left in place: only `thaw` lifts it


def test_freeze_check_refuses_a_health_answer_without_freeze_fields(freeze_server):
    srv = freeze_server(db="other-db")  # /health/db/memora-main is 404 {"status": "unknown"}
    c = lp.FreezeClient(srv.url, "t", DB, health_token="h")
    with pytest.raises(lp.L5Refused, match="shows no freeze state"):
        c.check("before anything")


def test_a_refused_freeze_request_is_a_refusal(freeze_server):
    srv = freeze_server(post_status=409)
    with pytest.raises(lp.L5Refused, match=r"refused \(409\)"):
        lp.FreezeClient(srv.url, "t", DB, health_token="h").freeze()


def test_a_freeze_that_could_not_be_lifted_is_reported(freeze_server):
    srv = freeze_server(delete_status=500)
    c = lp.FreezeClient(srv.url, "t", DB, health_token="h")
    c.freeze()
    with pytest.raises(lp.L5Refused, match="NOT lifted"):
        c.thaw()


def test_require_refuses_an_open_store_and_says_how_to_freeze_it(freeze_server):
    srv = freeze_server()
    with pytest.raises(lp.L5Refused, match=f"local_primary.py freeze {DB}"):
        lp.FreezeClient(srv.url, "t", DB, health_token="h").require("before the recheck")
    assert ("POST", f"/admin/freeze/{DB}") not in srv.methods()


def test_an_unreachable_service_is_a_refusal():
    with pytest.raises(lp.L5Refused, match="not reachable"):
        lp.FreezeClient("http://127.0.0.1:9", "t", DB, health_token="h", timeout=2).check("x")


def test_service_stopped_barrier_requires_the_container_stopped_at_every_check():
    answers = iter(["false", "true"])

    def runner(argv, **kw):
        assert argv[:2] == ["docker", "inspect"]
        return subprocess.CompletedProcess(argv, 0, stdout=next(answers) + "\n", stderr="")

    b = lp.ServiceStopped(runner=runner)
    b.freeze()
    with pytest.raises(lp.L5Refused, match="State.Running=true"):
        b.check("after the export")


# ---------------------------------------------------------------- the CLI, in a subprocess

def _cli(tmp_path, replica_path, *args, env_extra=None):
    env = {**os.environ, "L5_TEST_FAKE_D1": str(replica_path), "MEMORA_D1_READ_TOKEN": "read-token"}
    env.update(env_extra or {})
    r = subprocess.run([sys.executable, str(HARNESS), *args], capture_output=True, text=True,
                       timeout=120, env=env, cwd=str(REPO))
    out = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {}
    return r.returncode, out, r.stderr


def token_args(tmp_path):
    """--admin-token-file and --health-token-file: two different 0600 files."""
    out = []
    for flag, name, value in (("--admin-token-file", "admin.tok", "admin-tok"),
                              ("--health-token-file", "health.tok", "health-tok")):
        f = tmp_path / name
        f.write_text(value)
        f.chmod(0o600)
        out += [flag, str(f)]
    return out


@pytest.fixture
def cli_env(tmp_path, freeze_server):
    srv = freeze_server()
    common = [DB, "--account", "acct", "--database-id", "replica-db", "--memora-url", srv.url,
              *token_args(tmp_path), "--r2-dir", str(tmp_path / "r2"),
              "--out-dir", str(tmp_path / "exports")]
    return srv, common


def test_cli_export_then_recheck_in_separate_processes(replica, tmp_path, cli_env):
    srv, common = cli_env
    code, out, err = _cli(tmp_path, replica.path, "export", *common)
    assert code == 0, err
    receipt = out["receipt"]
    assert json.loads(Path(receipt).read_text())["tables"] == d1_stats(replica)
    assert srv.state == "frozen"  # left in place for the recheck and the step after it
    code, out, err = _cli(tmp_path, replica.path, "recheck", *common, "--receipt", receipt)
    assert (code, out["receipt"], out["fresh_export"]) == (0, receipt, False), err
    replica_exec(replica, "UPDATE memories SET tags = '[\"u\"]' WHERE id = 3")
    code, out, err = _cli(tmp_path, replica.path, "recheck", *common, "--receipt", receipt)
    assert code == 0 and out["fresh_export"] is True, err
    assert json.loads(Path(out["receipt"]).read_text())["tables"] == d1_stats(replica)
    assert srv.state == "frozen" and ("DELETE", f"/admin/freeze/{DB}") not in srv.methods()
    code, out, err = _cli(tmp_path, replica.path, "thaw", DB, "--memora-url", srv.url, *token_args(tmp_path))
    assert code == 0 and srv.state == "open", err


def test_cli_recheck_without_a_freeze_is_refused(replica, tmp_path, cli_env):
    srv, common = cli_env
    code, out, _ = _cli(tmp_path, replica.path, "export", *common)
    receipt = out["receipt"]
    srv.state = "open"  # an operator thawed it
    code, out, _ = _cli(tmp_path, replica.path, "recheck", *common, "--receipt", receipt)
    assert code == 2 and f"local_primary.py freeze {DB}" in out["refused"]
    assert srv.state == "open"
    code, out, err = _cli(tmp_path, replica.path, "freeze", DB, "--memora-url", srv.url, *token_args(tmp_path))
    assert code == 0 and srv.state == "frozen", err
    code, out, _ = _cli(tmp_path, replica.path, "recheck", *common, "--receipt", receipt)
    assert code == 0 and out["fresh_export"] is False


def test_cli_export_refuses_when_something_is_in_flight_after_the_export(replica, tmp_path, cli_env):
    """§9 p across a process boundary: the 3rd health read (after the export)
    reports an in-flight exempt send; exit 2, no receipt, freeze kept."""
    srv, common = cli_env
    ok = {"state": "frozen", "in_flight": 0, "open_intents": []}
    srv.health = [ok, ok, {"state": "frozen", "in_flight": 1, "open_intents": []}]
    code, out, err = _cli(tmp_path, replica.path, "export", *common)
    assert code == 2 and "in_flight=1" in out["refused"], err
    assert not list((tmp_path / "exports").rglob("*.receipt.json"))
    assert srv.state == "frozen"
    # 7630 (v): the freeze is kept on purpose, so the output names the way out
    assert out["recovery"].startswith(f"to abandon the procedure: local_primary.py thaw {DB} --memora-url {srv.url}")


def test_cli_recheck_refuses_a_tampered_receipt(replica, tmp_path, cli_env):
    srv, common = cli_env
    code, out, _ = _cli(tmp_path, replica.path, "export", *common)
    _edit(out["receipt"], db="another-store")
    n = len(srv.requests)
    code, out, _ = _cli(tmp_path, replica.path, "recheck", *common, "--receipt", out["receipt"])
    assert code == 2 and "is for 'another-store'" in out["refused"]
    assert len(srv.requests) == n  # refused before the freeze


def test_cli_refuses_a_world_readable_admin_token(replica, tmp_path, cli_env):
    srv, common = cli_env
    (tmp_path / "admin.tok").chmod(0o644)
    code, out, _ = _cli(tmp_path, replica.path, "export", *common)
    assert code == 2 and "0600" in out["refused"]
    assert srv.requests == []


def test_cli_needs_a_health_token_file(replica, tmp_path, cli_env):
    srv, common = cli_env
    i = common.index("--health-token-file")
    without = common[:i] + common[i + 2:]
    code, out, _ = _cli(tmp_path, replica.path, "export", *without)
    assert code == 2 and "--health-token-file is required" in out["refused"]
    assert srv.requests == []


def test_cli_needs_a_read_token(replica, tmp_path, cli_env):
    srv, common = cli_env
    code, out, _ = _cli(tmp_path, replica.path, "export", *common, env_extra={"MEMORA_D1_READ_TOKEN": ""})
    assert code == 2 and "no D1 read token" in out["refused"]


# ---------------------------------------------------------------- live wrangler export (env-gated)

@pytest.mark.skipif(not os.getenv("MEMORA_L5_TEST_NATIVE_EXPORT"),
                    reason="set MEMORA_L5_TEST_NATIVE_EXPORT=1 plus the MEMORA_D1_TEST_* throwaway variables")
def test_native_export_of_the_throwaway_database(tmp_path):
    """Whether a D1 READ token may `wrangler d1 export` (§4). Only against
    the throwaway database, after the API confirms its identity."""
    from memora.backends import D1SelectOnlyConnection
    from tests.live_d1_guard import verify_throwaway

    account, database = os.environ["MEMORA_D1_TEST_ACCOUNT"], os.environ["MEMORA_D1_TEST_DATABASE"]
    name, token = os.environ["MEMORA_D1_TEST_DATABASE_NAME"], os.environ["MEMORA_D1_TEST_READ_TOKEN"]
    assert "throwaway" in name
    ok, why = verify_throwaway(account, database, token, name)
    if not ok:
        pytest.fail(f"refusing to touch D1: {why}")
    reader = lp.D1Reader(D1SelectOnlyConnection(account, database, token))
    deps = lp.Deps(reader=reader, freeze=FakeBarrier(frozen=True), r2=lp.FsR2(tmp_path / "r2"), account_id=account,
                   database_id=database, d1_name=name, read_token=token, native_export=True)
    r = json.loads(lp.export(name, deps, tmp_path / "exports").read_text())
    print("native export method:", r["method"])
