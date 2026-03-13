import json
import socket
import time
import urllib.error
import urllib.request

import pytest

import memora
import memora.storage as storage
from memora.backends import LocalSQLiteBackend
from memora.graph.server import start_graph_server


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
