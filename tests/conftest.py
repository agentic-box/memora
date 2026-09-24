import json
import os
import socket
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import memora
import memora.storage as storage
from memora.backends import D1Connection, LocalSQLiteBackend
from memora.graph.server import start_graph_server


@pytest.fixture(autouse=True)
def _isolated_write_gate_state(tmp_path_factory, monkeypatch):
    """Each test gets its own data dir for the write gate's freeze files and
    the D1 intent journal (docs/local-primary-implementation.md §1), and
    fresh in-process gate and journal registries. Nothing is ever written to
    the real /data."""
    from memora import intent_journal, write_gate

    monkeypatch.setenv("MEMORA_DATA_DIR", str(tmp_path_factory.mktemp("memora-data")))
    monkeypatch.delenv("MEMORA_READONLY_DBS", raising=False)
    write_gate._reset_for_tests()
    intent_journal._reset_for_tests()
    # server.main() records /data refusals and installs the admin auth hook
    # (L2a); neither may leak into the next test.
    from memora import admin, storage

    saved_auth = admin._admin_auth
    storage.set_store_refusals({})
    yield
    storage.set_store_refusals({})
    admin.set_admin_auth(saved_auth)
    write_gate._reset_for_tests()
    intent_journal._reset_for_tests()
    from memora import backends as _backends

    for key in list(_backends._PRIMARY_LOCKS):  # primary locks a test took in-process
        os.close(_backends._PRIMARY_LOCKS.pop(key))
    _backends.reset_fk_audits()  # the per-process fk audit cache (plan §9 x)


class FakeD1Connection(D1Connection):
    """Offline D1 behavioral double backed by SQLite.

    The real D1Connection sends one statement per HTTP request, so every
    statement is durable before the next one begins.  In particular, its
    ``commit`` and ``rollback`` methods are no-ops.  This double keeps those
    semantics while using a local SQLite file for tests.  It deliberately
    subclasses D1Connection so production's D1 branches (notably no FTS5) are
    exercised as well.

    ``transactional`` exists only for negative controls: it restores local
    SQLite transaction behavior, showing a test is sensitive to D1's lack of
    rollback rather than merely passing on the local backend.
    """

    def __init__(self, db_path: Path, *, transactional: bool = False):
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._transactional = transactional
        self.statement_count = 0
        # Optional (sql, params) -> bool. When True, raise after counting the statement.
        self.fail_when = None

    @staticmethod
    def _is_savepoint(sql: str) -> bool:
        return sql.lstrip().upper().startswith(("SAVEPOINT", "RELEASE", "ROLLBACK TO"))

    def execute(self, sql: str, params=None):
        # D1's HTTP API has no transaction/savepoint surface.  A no-op lets
        # callers that defensively issue savepoints run under the same limit.
        if self._is_savepoint(sql):
            return self._conn.execute("SELECT 1 WHERE 0")
        self.statement_count += 1
        # Cloudflare D1 rejects a statement with more than 100 bound
        # parameters; enforce it so a query that only works locally fails here.
        if params is not None and len(params) > 100:
            raise RuntimeError(f"D1 query failed: too many SQL variables ({len(params)} > 100)")
        if self.fail_when is not None and self.fail_when(sql, () if params is None else params):
            raise RuntimeError("injected D1 statement failure")
        cur = self._conn.execute(sql, () if params is None else params)
        if not self._transactional:
            self._conn.commit()
        return cur

    def executemany(self, sql: str, params_list):
        last = None
        for params in params_list:
            last = self.execute(sql, params)
        return last if last is not None else self._conn.execute("SELECT 1 WHERE 0")

    def executescript(self, sql_script: str):
        last = None
        for statement in (part.strip() for part in sql_script.split(";")):
            if statement:
                last = self.execute(statement)
        return last if last is not None else self._conn.execute("SELECT 1 WHERE 0")

    @property
    def request_count(self):
        # Mirrors D1Connection.request_count: one per statement == one HTTPS POST.
        return self.statement_count

    def cursor(self):
        return self

    def commit(self):
        # Cloudflare D1 already committed the individual HTTP statement.
        if self._transactional:
            self._conn.commit()

    def rollback(self):
        # D1 cannot roll back prior HTTP statements.
        if self._transactional:
            self._conn.rollback()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class FakeD1Backend:
    """Minimal backend for FakeD1Connection; no network or sync behavior."""

    def __init__(self, db_path: Path, *, transactional: bool = False):
        self.db_path = Path(db_path)
        self.transactional = transactional
        self.connections = []

    def connect(self, *, check_same_thread: bool = True) -> FakeD1Connection:
        conn = FakeD1Connection(self.db_path, transactional=self.transactional)
        self.connections.append(conn)
        return conn

    def sync_before_use(self):
        pass

    def sync_after_write(self):
        pass

    def get_info(self):
        return {"backend_type": "fake_d1", "db_path": str(self.db_path)}


@pytest.fixture(params=("sqlite", "fake_d1"), ids=("sqlite", "fake-d1"))
def absorb_backend(request, tmp_path, monkeypatch):
    """Storage backend matrix for absorb failure tests.

    Fake D1 is intentionally statement-autocommit with no rollback, matching
    the production HTTP adapter rather than SQLite's safe transaction case.
    """
    if request.param == "sqlite":
        backend = LocalSQLiteBackend(tmp_path / "absorb.db")
    else:
        backend = FakeD1Backend(tmp_path / "absorb-d1.db")
    monkeypatch.setattr(storage, "STORAGE_BACKEND", backend)
    monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
    return backend


@pytest.fixture()
def fake_d1_connection(tmp_path):
    """Factory for direct D1 transaction-semantics probes."""
    def _create(name: str, *, transactional: bool = False) -> FakeD1Connection:
        return FakeD1Connection(tmp_path / name, transactional=transactional)

    return _create


@pytest.fixture()
def fake_d1_backend(tmp_path, monkeypatch):
    """Shared-file FakeD1 backend patched onto storage."""
    backend = FakeD1Backend(tmp_path / "fake-d1.db")
    monkeypatch.setattr(storage, "STORAGE_BACKEND", backend)
    monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
    with storage.connect() as conn:
        conn.commit()
    return backend


@pytest.fixture(autouse=True)
def clean_aws_env(monkeypatch):
    for var in ("AWS_ENDPOINT_URL", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_CONFIG_FILE",
                "AWS_SHARED_CREDENTIALS_FILE"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def local_db(tmp_path, monkeypatch):
    backend = LocalSQLiteBackend(tmp_path / "memories.db")
    monkeypatch.setattr(storage, "STORAGE_BACKEND", backend)
    monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    with storage.connect() as conn:
        conn.commit()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_server(url: str, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{url}/api/graph", timeout=0.5)
            return
        except Exception as exc:  # pragma: no cover - polling
            last_error = exc
            time.sleep(0.1)
    raise AssertionError(f"Graph server did not start: {last_error}")


@pytest.fixture()
def graph_server_url(local_db) -> str:
    port = _free_port()
    start_graph_server("127.0.0.1", port)
    url = f"http://127.0.0.1:{port}"
    _wait_for_server(url)
    return url


@pytest.fixture()
def graph_request(graph_server_url):
    def _request(method: str, path: str, payload=None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{graph_server_url}{path}",
            data=body,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    return _request


@pytest.fixture()
def memory_factory(local_db):
    def _create_memory(**overrides):
        payload = {
            "content": "Graph memory",
            "metadata": None,
            "tags": ["alpha"],
        }
        payload.update(overrides)
        with storage.connect() as conn:
            return storage.add_memory(conn, **payload)

    return _create_memory


@pytest.fixture()
def supersede_gate_open(monkeypatch):
    """Confirm every classifier UPDATE without the supersede gate.

    For tests of supersede MECHANICS (leaf resolution, fork collapse,
    write-boundary races, compensation) that stub the classifier's UPDATE and
    are not about whether it should have been accepted. The gate itself is
    covered in tests/test_absorb_supersede_gate.py.
    """
    def confirm(fact, leaf, suggested_tags, **_kw):
        return {"leaf_id": leaf["id"], "verdict": "supersede", "gate": "test",
                "reason": "gate open in test", "score": 1.0, "old_text": leaf.get("content", "")}

    monkeypatch.setattr(storage, "_absorb_check_supersede", confirm)


@pytest.fixture(autouse=True)
def _clear_query_embedding_cache():
    """semantic_search's query-embedding LRU is process-global; tests fake
    _compute_embedding in different ways, so never let one test's vector
    serve another's query."""
    storage._query_embedding_cache.clear()
    yield
    storage._query_embedding_cache.clear()
