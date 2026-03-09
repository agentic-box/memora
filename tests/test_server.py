import asyncio

import pytest

import memora.server as server
import memora.storage as storage
from memora.backends import LocalSQLiteBackend


@pytest.fixture()
def local_db(tmp_path, monkeypatch):
    backend = LocalSQLiteBackend(tmp_path / "memories.db")
    monkeypatch.setattr(storage, "STORAGE_BACKEND", backend)
    monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")


def _new_memory(*args, content="Repeat memory text", tags=["task"], **kwargs):
    return asyncio.run(
        server.memory_create(*args, content=content, tags=tags, **kwargs)
    )


def test_memory_create_minimal_response_returns_id_only(local_db):
    _, r2 = (_new_memory(), _new_memory(response_mode="minimal"))

    assert r2 == {"memory": {"id": r2["memory"]["id"]}}
