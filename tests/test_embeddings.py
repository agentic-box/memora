"""Embeddings credential atomicity, batch validation, cache, fingerprint.

Uses a fake openai module — no network. conftest forces EMBEDDING_MODEL=tfidf
for storage tests; this file exercises the public embeddings API with its own
env fixtures.
"""
from __future__ import annotations

import math
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import types
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

import memora.embeddings as emb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_EMBED_ENV = (
    "MEMORA_EMBEDDING_API_KEY",
    "MEMORA_EMBEDDING_BASE_URL",
    "MEMORA_EMBEDDING_STRICT",
    "MEMORA_EMBEDDING_MODEL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_EMBEDDING_MODEL",
    "SENTENCE_TRANSFORMERS_MODEL",
)


@pytest.fixture(autouse=True)
def _clean_embedding_state(monkeypatch):
    """Clear caches, warn-once state, and all embedding-related env vars."""
    emb._embedding_model_cache.clear()
    emb._warned_backends.clear()
    emb._integrity_check_cache.clear()
    for var in _EMBED_ENV:
        monkeypatch.delenv(var, raising=False)
    yield
    emb._embedding_model_cache.clear()
    emb._warned_backends.clear()
    emb._integrity_check_cache.clear()


class _EmbItem:
    def __init__(self, index: int, embedding: List[float]):
        self.index = index
        self.embedding = embedding


class _EmbResponse:
    def __init__(self, data: List[_EmbItem]):
        self.data = data


class FakeOpenAI:
    """Captures constructor kwargs and returns scripted embeddings responses."""

    instances: List["FakeOpenAI"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.embeddings = types.SimpleNamespace(create=self._create)
        self._responses: List[Any] = []
        self._create_calls: List[Dict[str, Any]] = []
        FakeOpenAI.instances.append(self)

    def queue(self, response: Any) -> None:
        self._responses.append(response)

    def queue_error(self, exc: BaseException) -> None:
        self._responses.append(exc)

    def _create(self, **kwargs):
        self._create_calls.append(kwargs)
        if not self._responses:
            raise RuntimeError("FakeOpenAI: no queued response")
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture()
def fake_openai(monkeypatch):
    FakeOpenAI.instances.clear()
    mod = types.ModuleType("openai")
    mod.OpenAI = FakeOpenAI
    monkeypatch.setitem(__import__("sys").modules, "openai", mod)
    return FakeOpenAI


def _dense(n: int, dim: int = 3, scale: float = 1.0) -> List[float]:
    return [scale * (i + 1) / dim for i in range(dim)]


def _ok_response(n: int, dim: int = 3, order: Optional[List[int]] = None) -> _EmbResponse:
    """Complete response for n inputs; optional shuffled index order."""
    indices = order if order is not None else list(range(n))
    data = [_EmbItem(i, _dense(i, dim, scale=float(i + 1))) for i in indices]
    return _EmbResponse(data)


# ---------------------------------------------------------------------------
# N1 — atomic credentials
# ---------------------------------------------------------------------------

def test_n1_neither_split_var_uses_openai_pair(fake_openai, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "llm-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    # No MEMORA_* set
    client = emb._embedding_client(__import__("openai"))
    assert client.kwargs == {
        "api_key": "llm-key",
        "base_url": "https://openrouter.ai/api/v1",
        "timeout": 90.0,
        "max_retries": 1,
    }


def test_n1_both_split_vars_use_memora_pair(fake_openai, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "llm-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("MEMORA_EMBEDDING_API_KEY", "emb-key")
    monkeypatch.setenv("MEMORA_EMBEDDING_BASE_URL", "https://api.openai.com/v1")
    client = emb._embedding_client(__import__("openai"))
    assert client.kwargs == {
        "api_key": "emb-key",
        "base_url": "https://api.openai.com/v1",
        "timeout": 90.0,
        "max_retries": 1,
    }


@pytest.mark.parametrize(
    "env_setup,strict",
    [
        ({"MEMORA_EMBEDDING_BASE_URL": "https://emb.example"}, True),
        ({"MEMORA_EMBEDDING_API_KEY": "only-key"}, True),
        ({"MEMORA_EMBEDDING_API_KEY": "", "MEMORA_EMBEDDING_BASE_URL": "https://x"}, True),
        ({"MEMORA_EMBEDDING_API_KEY": "k", "MEMORA_EMBEDDING_BASE_URL": ""}, True),
        ({"MEMORA_EMBEDDING_BASE_URL": "https://emb.example"}, False),
        ({"MEMORA_EMBEDDING_API_KEY": "only-key"}, False),
    ],
)
def test_n1_partial_or_blank_never_cross_pairs(fake_openai, monkeypatch, env_setup, strict):
    monkeypatch.setenv("OPENAI_API_KEY", "llm-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    for k, v in env_setup.items():
        monkeypatch.setenv(k, v)
    if strict:
        monkeypatch.setenv("MEMORA_EMBEDDING_STRICT", "1")

    # resolve must not return a cross-pair
    with pytest.raises(emb.EmbeddingCredentialError) as ei:
        emb.resolve_embedding_credentials()
    msg = str(ei.value)
    assert "borrow" in msg or "incomplete" in msg or "empty" in msg or "unset" in msg
    # Distinguish unset vs empty in message when relevant
    if any(v == "" for v in env_setup.values()):
        assert "empty" in msg
    if "MEMORA_EMBEDDING_API_KEY" not in env_setup and "MEMORA_EMBEDDING_BASE_URL" in env_setup:
        assert "MEMORA_EMBEDDING_API_KEY is unset" in msg
    if "MEMORA_EMBEDDING_BASE_URL" not in env_setup and "MEMORA_EMBEDDING_API_KEY" in env_setup:
        if env_setup.get("MEMORA_EMBEDDING_API_KEY") != "":
            assert "MEMORA_EMBEDDING_BASE_URL is unset" in msg

    if strict:
        with pytest.raises(emb.EmbeddingStrictError, match="MEMORA_EMBEDDING_STRICT"):
            emb.compute_embedding("hello", None, [], "openai")
        # Constructor must NOT have been called
        assert FakeOpenAI.instances == []
    else:
        # Non-strict dense: still FAIL (no TF-IDF substitute), no cross-pair client
        with pytest.raises(emb.EmbeddingProviderError, match="refusing TF-IDF"):
            emb.compute_embedding("hello world", None, [], "openai")
        assert FakeOpenAI.instances == []


def test_n1_public_paths_share_helper(fake_openai, monkeypatch):
    monkeypatch.setenv("MEMORA_EMBEDDING_API_KEY", "emb-key")
    monkeypatch.setenv("MEMORA_EMBEDDING_BASE_URL", "https://emb.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "llm-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")

    inst_before = len(FakeOpenAI.instances)
    # Single path
    FakeOpenAI.instances.clear()
    client1_mod = __import__("openai")
    # Queue response via intercepting after first construct — use monkeypatch on create
    # Public compute_embedding
    def _install_response():
        # After client is built, queue on the instance
        pass

    # Build expected: call single
    # We need to queue on whatever instance is created
    original_init = FakeOpenAI.__init__

    def init_and_queue(self, **kwargs):
        original_init(self, **kwargs)
        self.queue(_ok_response(1, dim=4))

    monkeypatch.setattr(FakeOpenAI, "__init__", init_and_queue)
    vec = emb.compute_embedding("a", None, [], "openai")
    assert len(vec) == 4
    assert FakeOpenAI.instances[-1].kwargs["api_key"] == "emb-key"
    assert FakeOpenAI.instances[-1].kwargs["base_url"] == "https://emb.example/v1"

    # Batch path — same kwargs
    emb._embedding_model_cache.clear()
    FakeOpenAI.instances.clear()

    def init_and_queue2(self, **kwargs):
        original_init(self, **kwargs)
        self.queue(_ok_response(2, dim=4))

    monkeypatch.setattr(FakeOpenAI, "__init__", init_and_queue2)
    out = emb.compute_embeddings_batch(
        [{"content": "a", "tags": []}, {"content": "b", "tags": []}],
        "openai",
    )
    assert len(out) == 2
    assert FakeOpenAI.instances[-1].kwargs["api_key"] == "emb-key"


# ---------------------------------------------------------------------------
# N3 — client cache re-key
# ---------------------------------------------------------------------------

def test_n3_credential_change_rebuilds_client(fake_openai, monkeypatch):
    monkeypatch.setenv("MEMORA_EMBEDDING_API_KEY", "key-a")
    monkeypatch.setenv("MEMORA_EMBEDDING_BASE_URL", "https://a.example")
    c1 = emb._embedding_client(__import__("openai"))
    monkeypatch.setenv("MEMORA_EMBEDDING_API_KEY", "key-b")
    monkeypatch.setenv("MEMORA_EMBEDDING_BASE_URL", "https://b.example")
    c2 = emb._embedding_client(__import__("openai"))
    assert c1 is not c2
    assert c1.kwargs["api_key"] == "key-a"
    assert c2.kwargs["api_key"] == "key-b"
    # Cache holds both fingerprints, not a single "openai_client" slot
    keys = [k for k in emb._embedding_model_cache if k.startswith("openai_client:")]
    assert len(keys) == 2
    assert all("key-a" not in k and "key-b" not in k for k in keys)  # no raw secret in key


# ---------------------------------------------------------------------------
# N2 — batch validation
# ---------------------------------------------------------------------------

def test_n2_out_of_order_complete_restored(fake_openai, monkeypatch):
    monkeypatch.setenv("MEMORA_EMBEDDING_API_KEY", "k")
    monkeypatch.setenv("MEMORA_EMBEDDING_BASE_URL", "https://e")
    original_init = FakeOpenAI.__init__

    def init_q(self, **kwargs):
        original_init(self, **kwargs)
        # indices arrive as 1, 0
        self.queue(_ok_response(2, dim=3, order=[1, 0]))

    monkeypatch.setattr(FakeOpenAI, "__init__", init_q)
    out = emb.compute_embeddings_batch(
        [{"content": "first", "tags": []}, {"content": "second", "tags": []}],
        "openai",
    )
    assert len(out) == 2
    # index 0 had scale 1, index 1 scale 2 — order restored to input order
    assert out[0]["0"] == pytest.approx(1.0 / 3)
    assert out[1]["0"] == pytest.approx(2.0 / 3)


@pytest.mark.parametrize(
    "bad_factory,match",
    [
        (lambda: _EmbResponse([]), "cardinality"),  # n=2 expected
        (lambda: _EmbResponse([_EmbItem(0, _dense(0))]), "cardinality"),
        (
            lambda: _EmbResponse([_EmbItem(0, _dense(0)), _EmbItem(0, _dense(0))]),
            "duplicate",
        ),
        (
            lambda: _EmbResponse([_EmbItem(0, _dense(0)), _EmbItem(5, _dense(0))]),
            "out of range",
        ),
        (
            lambda: _EmbResponse([_EmbItem(0, _dense(0)), _EmbItem(2, _dense(0))]),
            "out of range",
        ),
        (
            lambda: _EmbResponse([_EmbItem(1, _dense(0)), _EmbItem(2, _dense(0))]),
            "out of range|missing",
        ),
        (
            lambda: _EmbResponse([_EmbItem(0, []), _EmbItem(1, _dense(0))]),
            "empty",
        ),
        (
            lambda: _EmbResponse(
                [_EmbItem(0, [1.0, float("nan")]), _EmbItem(1, _dense(0, dim=2))]
            ),
            "non-finite",
        ),
        (
            lambda: _EmbResponse(
                [_EmbItem(0, _dense(0, dim=2)), _EmbItem(1, _dense(0, dim=4))]
            ),
            "dimension",
        ),
    ],
)
def test_n2_strict_raises_on_bad_batch(fake_openai, monkeypatch, bad_factory, match):
    monkeypatch.setenv("MEMORA_EMBEDDING_STRICT", "1")
    monkeypatch.setenv("MEMORA_EMBEDDING_API_KEY", "k")
    monkeypatch.setenv("MEMORA_EMBEDDING_BASE_URL", "https://e")
    original_init = FakeOpenAI.__init__

    def init_q(self, **kwargs):
        original_init(self, **kwargs)
        self.queue(bad_factory())

    monkeypatch.setattr(FakeOpenAI, "__init__", init_q)
    with pytest.raises(RuntimeError, match="MEMORA_EMBEDDING_STRICT"):
        emb.compute_embeddings_batch(
            [{"content": "a", "tags": []}, {"content": "b", "tags": []}],
            "openai",
        )


def test_n2_strict_unknown_backend(monkeypatch):
    monkeypatch.setenv("MEMORA_EMBEDDING_STRICT", "1")
    with pytest.raises(emb.EmbeddingStrictError, match="unknown embedding backend"):
        emb.compute_embedding("x", None, [], "not-a-real-backend")


def test_n2_strict_missing_key(fake_openai, monkeypatch):
    monkeypatch.setenv("MEMORA_EMBEDDING_STRICT", "1")
    # neither MEMORA nor OPENAI keys
    with pytest.raises(emb.EmbeddingStrictError, match="MEMORA_EMBEDDING_STRICT"):
        emb.compute_embedding("x", None, [], "openai")
    assert FakeOpenAI.instances == []


def test_n2_strict_endpoint_error(fake_openai, monkeypatch):
    monkeypatch.setenv("MEMORA_EMBEDDING_STRICT", "1")
    monkeypatch.setenv("MEMORA_EMBEDDING_API_KEY", "k")
    monkeypatch.setenv("MEMORA_EMBEDDING_BASE_URL", "https://e")
    original_init = FakeOpenAI.__init__

    def init_q(self, **kwargs):
        original_init(self, **kwargs)
        self.queue_error(RuntimeError("404 no embeddings"))

    monkeypatch.setattr(FakeOpenAI, "__init__", init_q)
    with pytest.raises(emb.EmbeddingStrictError, match="MEMORA_EMBEDDING_STRICT=1 hard-stop"):
        emb.compute_embedding("x", None, [], "openai")


def test_n2_nonstrict_dense_fails_not_tfidf(fake_openai, monkeypatch):
    """Dense backend never persists TF-IDF on failure (round 132 #3 policy)."""
    monkeypatch.setenv("MEMORA_EMBEDDING_API_KEY", "k")
    monkeypatch.setenv("MEMORA_EMBEDDING_BASE_URL", "https://e")
    original_init = FakeOpenAI.__init__

    def init_q(self, **kwargs):
        original_init(self, **kwargs)
        # Partial: only index 0 for two inputs
        self.queue(_EmbResponse([_EmbItem(0, _dense(0))]))

    monkeypatch.setattr(FakeOpenAI, "__init__", init_q)
    with pytest.raises(emb.EmbeddingProviderError, match="refusing TF-IDF"):
        emb.compute_embeddings_batch(
            [{"content": "alpha beta", "tags": []}, {"content": "gamma delta", "tags": []}],
            "openai",
        )


# ---------------------------------------------------------------------------
# N5 — fingerprint forces rebuild
# ---------------------------------------------------------------------------

def _meta_conn(tmp_path) -> sqlite3.Connection:
    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY,
            content TEXT,
            metadata TEXT,
            tags TEXT
        );
        CREATE TABLE memories_meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE memories_embeddings (
            memory_id INTEGER PRIMARY KEY,
            embedding TEXT,
            representation TEXT,
            dimension INTEGER,
            encoding_source TEXT,
            writer_token TEXT
        );
        CREATE TABLE memories_embedding_repairs (
            memory_id INTEGER PRIMARY KEY,
            repaired_generation TEXT NOT NULL,
            repaired_at TEXT
        );
        INSERT INTO memories_meta(key, value) VALUES ('embedding_change_epoch', '0');
        """
    )
    return conn


def _seed_memory(conn, memory_id: int, content: str = "x") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO memories (id, content, metadata, tags) VALUES (?,?,?,?)",
        (memory_id, content, "{}", "[]"),
    )


def test_n5_word_key_store_labelled_openai_requires_rebuild(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
    conn = _meta_conn(tmp_path)
    _seed_memory(conn, 1)
    emb.set_stored_embedding_model(conn, "openai")
    emb.upsert_embedding(conn, 1, {"ure": 0.2, "xdg": 0.3, "zig": 0.5})
    conn.commit()
    assert emb.check_embedding_model_mismatch(conn, "openai") is True


def test_n5_matching_dense_fingerprint_no_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    conn = _meta_conn(tmp_path)
    _seed_memory(conn, 1)
    dense = {str(i): 0.1 for i in range(8)}
    emb.upsert_embedding(conn, 1, dense)
    host = emb._embedding_endpoint_host()
    emb.set_stored_embedding_model(conn, f"openai|text-embedding-3-small|{host}|dense:8")
    emb.verify_embedding_integrity(conn)
    conn.commit()
    assert emb.check_embedding_model_mismatch(conn, "openai") is False


def test_n7_mixed_dense_sparse_forces_rebuild(tmp_path, monkeypatch):
    """Integrity meta marks mixed dens+sparse → mismatch (O(1), no full scan)."""
    monkeypatch.setenv("OPENAI_EMBEDDING_MODEL", "@cf/baai/bge-m3")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.cloudflare.com/client/v4/accounts/x/ai/v1")
    conn = _meta_conn(tmp_path)
    _seed_memory(conn, 1)
    _seed_memory(conn, 2)
    host = emb._embedding_endpoint_host()
    emb.upsert_embedding(conn, 1, {str(i): 0.01 for i in range(8)})
    emb.upsert_embedding(conn, 2, {"ure": 0.2, "xdg": 0.3, "zig": 0.5})
    emb.set_stored_embedding_model(conn, f"openai|@cf/baai/bge-m3|{host}|dense:8")
    conn.commit()
    assert emb.store_has_mixed_embeddings(conn) is True
    assert emb.check_embedding_model_mismatch(conn, "openai") is True


def test_n7_model_change_same_backend_name_forces_rebuild(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_EMBEDDING_MODEL", "@cf/baai/bge-m3")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.cloudflare.com/client/v4/accounts/x/ai/v1")
    conn = _meta_conn(tmp_path)
    _seed_memory(conn, 1)
    host = emb._embedding_endpoint_host()
    emb.upsert_embedding(conn, 1, {str(i): 0.01 for i in range(8)})
    emb.set_stored_embedding_model(conn, f"openai|text-embedding-3-small|{host}|dense:8")
    conn.commit()
    assert emb.check_embedding_model_mismatch(conn, "openai") is True


def test_p1_integrity_audit_tracks_mixed_then_caches_result(tmp_path, monkeypatch):
    """First-use audit derives mixed reps; repeated checks use its process cache."""
    monkeypatch.setenv("OPENAI_EMBEDDING_MODEL", "@cf/baai/bge-m3")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.cloudflare.com/client/v4/accounts/x/ai/v1")
    conn = _meta_conn(tmp_path)
    host = emb._embedding_endpoint_host()
    for i in range(1, 51):
        _seed_memory(conn, i)
        emb.upsert_embedding(conn, i, {"0": 0.1, "1": 0.2})
    _seed_memory(conn, 51)
    emb.upsert_embedding(conn, 51, {"ure": 0.5, "xdg": 0.5})
    emb.set_stored_embedding_model(conn, f"openai|@cf/baai/bge-m3|{host}|dense:2")
    audit = emb.verify_embedding_integrity(conn)
    conn.commit()
    integ = emb.get_embedding_integrity(conn)
    assert audit["mixed"] is True
    assert integ["state"] == "initialized"
    assert integ.get("mixed") is True
    assert emb.check_embedding_model_mismatch(conn, "openai") is True


def test_p1_missing_embedding_is_mismatch(tmp_path, monkeypatch):
    """P1-3: memory without embedding row → mismatch (coverage)."""
    monkeypatch.setenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    conn = _meta_conn(tmp_path)
    _seed_memory(conn, 1)
    _seed_memory(conn, 2)
    emb.upsert_embedding(conn, 1, {"0": 0.1, "1": 0.2})
    host = emb._embedding_endpoint_host()
    emb.set_stored_embedding_model(conn, f"openai|text-embedding-3-small|{host}|dense:2")
    # memory 2 has no embedding row
    conn.commit()
    assert emb.check_embedding_model_mismatch(conn, "openai") is True


def test_p1_4_mismatch_is_fast_no_payload_scan(tmp_path, monkeypatch):
    """Hot-path mismatch must not parse embedding payloads (timing sanity)."""
    import time
    monkeypatch.setenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    conn = _meta_conn(tmp_path)
    host = emb._embedding_endpoint_host()
    for i in range(1, 201):
        _seed_memory(conn, i)
        emb.upsert_embedding(conn, i, {str(j): 0.01 for j in range(32)})
    emb.set_stored_embedding_model(conn, f"openai|text-embedding-3-small|{host}|dense:32")
    emb.verify_embedding_integrity(conn)
    conn.commit()
    t0 = time.perf_counter()
    for _ in range(20):
        emb.check_embedding_model_mismatch(conn, "openai")
    elapsed = time.perf_counter() - t0
    # 20 checks on 200 dense:32 rows must be well under a full re-parse budget
    assert elapsed < 0.5, f"mismatch hot path too slow: {elapsed:.3f}s for 20 checks"


def _stamp_tfidf_store(conn) -> None:
    emb.set_stored_embedding_model(conn, emb.current_embedding_fingerprint("tfidf"))
    emb.verify_embedding_integrity(conn)
    emb.invalidate_embedding_integrity_cache(conn)


def test_d1_replace_import_epoch_reaudits_current_sql(absorb_backend):
    """D1 probe: replace advances DB epoch and rechecks the replacement SQL."""
    import memora.storage as storage

    with storage.connect() as conn:
        storage.add_memory(conn, content="replace baseline alpha sparse")
        storage.add_memory(conn, content="replace baseline beta sparse")
        _stamp_tfidf_store(conn)
        assert emb.get_embedding_integrity(conn)["reps"] == {"sparse": 2}
        storage.import_memories(conn, [{"content": "replacement only sparse"}], strategy="replace")
        status = emb.get_embedding_integrity_status(conn, "tfidf")
        assert status["mismatch"] is False

        # Hard negative control: an explicit admin audit knowingly stamps the
        # replacement generation, after which the one-row store is healthy.
        # Hard control: an external same-count mutation also advances epoch,
        # so it is not hidden behind the previous cached healthy result.
        replacement_id = conn.execute("SELECT id FROM memories").fetchone()[0]
        conn.execute(
            "UPDATE memories_embeddings SET embedding = ? WHERE memory_id = ?",
            (emb.embedding_to_json({"0": 1.0, "2": 1.0}), replacement_id),
        )
        assert conn.execute(
            "SELECT representation FROM memories_embeddings WHERE memory_id = ?", (replacement_id,)
        ).fetchone()[0] is None
        assert emb.get_embedding_integrity_status(conn, "tfidf")["mismatch"] is True


def test_d1_legacy_bootstrap_stays_uninitialized_after_one_write(absorb_backend):
    """A normal write must not turn a legacy dense+sparse store into healthy."""
    import memora.storage as storage

    with storage.connect() as conn:
        first = storage.add_memory(conn, content="legacy dense row")
        second = storage.add_memory(conn, content="legacy sparse row")
        # Simulate old/direct writers: erase any stamp and create mixed data.
        conn.execute("DELETE FROM memories_meta WHERE key = 'embedding_integrity'")
        emb.upsert_embedding(conn, first["id"], {"0": 0.5, "1": 0.5})
        emb.upsert_embedding(conn, second["id"], {"legacy": 1.0})
        _stamp = emb.current_embedding_fingerprint("tfidf")
        conn.execute(
            "INSERT OR REPLACE INTO memories_meta(key, value) VALUES ('embedding_model', ?)",
            (_stamp,),
        )
        # The one ordinary upsert that used to bootstrap metadata cannot do so.
        emb.upsert_embedding(conn, first["id"], {"0": 0.5, "1": 0.5})
        emb.invalidate_embedding_integrity_cache(conn)
        status = emb.get_embedding_integrity_status(conn, "tfidf")
        assert status["mismatch"] is True
        assert status["reason"] in {"integrity_uninitialized", "unknown_embedding_encoding"}

        # Hard negative control: a fresh, uniform store gets a complete admin
        # stamp and is accepted, proving this is not a blanket false positive.
        conn.execute("DELETE FROM memories_embeddings")
        emb.upsert_embedding(conn, first["id"], {"legacy": 1.0})
        emb.upsert_embedding(conn, second["id"], {"legacy": 1.0})
        _stamp_tfidf_store(conn)
        assert emb.get_embedding_integrity_status(conn, "tfidf")["mismatch"] is False


def test_d1_orphan_embedding_cannot_offset_missing_memory(absorb_backend):
    """Opposite anti-joins expose one missing and one orphan independently."""
    import memora.storage as storage

    with storage.connect() as conn:
        kept = storage.add_memory(conn, content="covered memory")
        missing = storage.add_memory(conn, content="missing vector memory")
        _stamp_tfidf_store(conn)
        conn.execute("DELETE FROM memories_embeddings WHERE memory_id = ?", (missing["id"],))
        conn.execute(
            "INSERT INTO memories_embeddings(memory_id, embedding) VALUES (?, ?)",
            (999999, emb.embedding_to_json({"orphan": 1.0})),
        )
        emb.invalidate_embedding_integrity_cache(conn)
        status = emb.get_embedding_integrity_status(conn, "tfidf")
        audit = status["audit"]
        assert audit["missing_count"] == 1
        assert audit["orphan_embedding_count"] == 1
        assert status["mismatch"] is True and status["reason"] == "orphan_embeddings"

        # Hard negative control: remove both independent defects, stamp, and
        # verify that a healthy store does not stay faulted.
        conn.execute("DELETE FROM memories_embeddings WHERE memory_id = 999999")
        storage._upsert_embedding(conn, missing["id"], {"missing": 1.0})
        _stamp_tfidf_store(conn)
        assert emb.get_embedding_integrity_status(conn, "tfidf")["mismatch"] is False


def test_d1_interleaved_representation_writes_cannot_lose_a_rep(absorb_backend):
    """A last-writer-wins snapshot cannot hide the other D1 representation."""
    import memora.storage as storage

    with storage.connect() as conn:
        dense = storage.add_memory(conn, content="concurrent dense")
        sparse = storage.add_memory(conn, content="concurrent sparse")
        emb.upsert_embedding(conn, dense["id"], {"0": 0.5, "1": 0.5})
        emb.upsert_embedding(conn, sparse["id"], {"word": 1.0})
        host = emb._embedding_endpoint_host()
        stored = f"openai|text-embedding-3-small|{host}|dense:2"
        emb.set_stored_embedding_model(conn, stored)
        # Simulate concurrent RMW metadata writes where the final writer drops
        # the sparse representation. SQL truth still has both rows.
        emb._write_embedding_integrity(conn, {
            "schema_version": 2, "generation": "lost-update", "state": "initialized",
            "reps": {"dense:2": 1}, "memory_count": 2, "embedding_count": 2,
            "missing_count": 0, "orphan_embedding_count": 0, "mixed": False,
            "fingerprint": stored,
        })
        emb.invalidate_embedding_integrity_cache(conn)
        status = emb.get_embedding_integrity_status(conn, "openai")
        assert status["mismatch"] is True
        assert status["reason"] == "model_or_representation_mismatch"
        assert status["audit"]["reps"] == {"dense:2": 1, "sparse": 1}

        # Hard negative control: a complete audit records both representations;
        # the remaining mismatch is explicit model/representation drift, not a
        # silently lost snapshot entry.
        emb.verify_embedding_integrity(conn)
        status = emb.get_embedding_integrity_status(conn, "openai")
        assert status["reason"] == "model_or_representation_mismatch"


def test_image_insert_stamps_nonce_before_image_processing(monkeypatch, absorb_backend):
    """Image/R2 failure can still be safely compensated under D1 autocommit."""
    import json
    import memora.storage as storage

    monkeypatch.setattr(
        storage, "_process_image_for_storage",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("R2 image failure")),
    )
    with storage.connect() as conn:
        owned = []
        with pytest.raises(storage.MemoryWriteError, match="R2 image failure"):
            storage.add_memory(
                conn,
                content="image nonce ownership needs to precede image processing",
                metadata={"images": [{"src": "fake-image"}]},
                owned_ids=owned,
                absorb_nonce="image-owner",
                absorb_operation_key="image-operation",
            )
        assert owned
        raw = conn.execute("SELECT metadata FROM memories WHERE id = ?", (owned[0],)).fetchone()[0]
        assert json.loads(raw) == {
            "absorb_nonce": "image-owner", "absorb_operation_key": "image-operation",
        }
        assert storage.delete_memory(conn, owned[0], require_absorb_nonce="image-owner") is True
        assert conn.execute("SELECT 1 FROM memories WHERE id = ?", (owned[0],)).fetchone() is None


def test_d1_lost_insert_response_recovers_owned_row(monkeypatch, absorb_backend):
    """A committed INSERT with a lost response still becomes compensation-owned."""
    import memora.storage as storage
    from memora.backends import D1Connection

    with storage.connect() as conn:
        if not isinstance(conn, D1Connection):
            pytest.skip("response-loss behavior is specific to D1's HTTP execute")
        real_execute = conn.execute
        fired = {"value": False}

        def lost_response(sql, params=None):
            if not fired["value"] and sql.lstrip().startswith("INSERT INTO memories "):
                fired["value"] = True
                real_execute(sql, params)
                raise RuntimeError("simulated D1 response lost after commit")
            return real_execute(sql, params)

        monkeypatch.setattr(conn, "execute", lost_response)
        owned = []
        with pytest.raises(storage.MemoryWriteError, match="response lost") as raised:
            storage.add_memory(
                conn,
                content="ambiguous D1 insert must be discovered by operation key",
                owned_ids=owned,
                absorb_nonce="recover-owner",
                absorb_operation_key="recover-operation",
            )
        assert raised.value.memory_id in owned
        assert conn.execute(
            "SELECT 1 FROM memories WHERE id = ?", (raised.value.memory_id,)
        ).fetchone() is not None
        assert storage.delete_memory(
            conn, raised.value.memory_id, require_absorb_nonce="recover-owner"
        ) is True


def test_schema_migration_is_safe_under_concurrent_old_store_upgrades(tmp_path):
    """Hard migration control: six independent connections race old schema."""
    from memora.schema import ensure_schema

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT NOT NULL, metadata TEXT, tags TEXT, created_at TEXT)")
    conn.execute("CREATE TABLE memories_embeddings (memory_id INTEGER PRIMARY KEY, embedding TEXT)")
    conn.execute("CREATE TABLE memories_meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()

    def migrate():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        try:
            ensure_schema(c)
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: migrate(), range(6)))
    check = sqlite3.connect(path)
    columns = {row[1] for row in check.execute("PRAGMA table_info(memories_embeddings)")}
    assert {"representation", "dimension", "encoding_source", "writer_token"} <= columns
    memory_columns = {row[1] for row in check.execute("PRAGMA table_info(memories)")}
    assert {"importance", "last_accessed", "access_count", "updated_at"} <= memory_columns
    assert check.execute("SELECT name FROM sqlite_master WHERE name = 'memories_fts'").fetchone()
    check.close()


def test_empty_embedding_update_is_rejected_before_durable_content_change(absorb_backend):
    """D1 hard case: an empty update cannot commit content ahead of its vector."""
    import memora.storage as storage

    with storage.connect() as conn:
        record = storage.add_memory(conn, content="normal content with tokens")
        with pytest.raises(ValueError, match="embedding is empty"):
            storage.update_memory(conn, record["id"], content="!!!", metadata={}, tags=[])
        assert storage.get_memory(conn, record["id"])["content"] == "normal content with tokens"


def test_building_lease_blocks_unknown_repair_then_recovers_stale_owner(absorb_backend):
    """A second worker sees building before unknown; stale owner becomes retryable."""
    import time
    import memora.storage as storage

    with storage.connect() as conn:
        record = storage.add_memory(conn, content="lease-controlled rebuild row")
        conn.execute(
            "UPDATE memories_embeddings SET representation = NULL, writer_token = NULL WHERE memory_id = ?",
            (record["id"],),
        )
        emb._write_embedding_integrity(conn, {
            "schema_version": 2, "state": "building", "generation": "active",
            "reps": {}, "fingerprint": None,
        })
        conn.execute(
            "INSERT OR REPLACE INTO memories_meta(key, value) VALUES (?, ?)",
            (emb._REBUILD_LEASE_KEY, f"owner|{int(time.time())}"),
        )
        emb.invalidate_embedding_integrity_cache(conn)
        active = emb.get_embedding_integrity_status(conn, "tfidf")
        assert active["reason"] == "integrity_building" and active["repairable"] is False
        conn.execute(
            "UPDATE memories_meta SET value = ? WHERE key = ?",
            ("dead-owner|0", emb._REBUILD_LEASE_KEY),
        )
        emb.invalidate_embedding_integrity_cache(conn)
        stale = emb.get_embedding_integrity_status(conn, "tfidf")
        assert stale["reason"] == "integrity_build_stale" and stale["repairable"] is True


def test_recurring_unknown_is_found_beyond_fresh_unknown_limit(absorb_backend):
    """The recurrence query is independent of the first 100 fresh unknowns."""
    import memora.storage as storage

    with storage.connect() as conn:
        records = [storage.add_memory(conn, content=f"recurrence probe memory {i}") for i in range(101)]
        conn.execute("UPDATE memories_embeddings SET representation = NULL, writer_token = NULL")
        conn.execute(
            "INSERT INTO memories_embedding_repairs(memory_id, repaired_generation) VALUES (?, 'old')",
            (records[-1]["id"],),
        )
        emb.invalidate_embedding_integrity_cache(conn)
        status = emb.get_embedding_integrity_status(conn, "tfidf")
        assert status["reason"] == "recurring_unknown_encoding"
        assert records[-1]["id"] in status["fault_ids"]


def test_active_lease_status_is_not_cached_past_heartbeat_expiry(absorb_backend):
    """A time-only expiry is visible even though no embedding epoch changed."""
    import memora.storage as storage

    with storage.connect() as conn:
        record = storage.add_memory(conn, content="lease cache expiry probe")
        assert emb.rebuild_all_embeddings(conn, "tfidf") == 1
        assert emb.get_embedding_integrity_status(conn, "tfidf")["mismatch"] is False
        emb._write_embedding_integrity(conn, {
            "schema_version": 2, "state": "building", "generation": "active",
            "reps": {}, "fingerprint": None,
        })
        conn.execute(
            "INSERT OR REPLACE INTO memories_meta(key, value) VALUES (?, ?)",
            (emb._REBUILD_LEASE_KEY, f"owner|{int(time.time())}"),
        )
        active = emb.get_embedding_integrity_status(conn, "tfidf")
        assert active["reason"] == "integrity_building"
        assert active["retry_after_seconds"] >= 0
        # This updates only lease metadata, deliberately leaving the embedding
        # epoch untouched. A cached active result would hide the stale owner.
        conn.execute(
            "UPDATE memories_meta SET value = ? WHERE key = ?",
            ("dead-owner|0", emb._REBUILD_LEASE_KEY),
        )
        stale = emb.get_embedding_integrity_status(conn, "tfidf")
        assert stale["reason"] == "integrity_build_stale"
        assert stale["repairable"] is True
        assert stale["lease_age_seconds"] > emb._REBUILD_LEASE_SECONDS
        assert record["id"] > 0


def test_empty_vectors_are_rejected_before_batch_or_import_writes(absorb_backend):
    """No public multi-write path may leave empty embeddings behind."""
    import memora.storage as storage

    with storage.connect() as conn:
        with pytest.raises(ValueError, match="embedding is empty"):
            storage.add_memories(conn, [{"content": "!!!"}])
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0

        result = storage.import_memories(conn, [{"content": "???"}])
        assert result["imported"] == 0
        assert result["total_errors"] == 1
        assert "embedding is empty" in result["errors"][0]["error"]
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0


def test_rebuild_migrates_existing_empty_embedding_marker(absorb_backend):
    """A pre-existing punctuation-only row is intentionally unsearchable, not missing."""
    import memora.storage as storage

    with storage.connect() as conn:
        cur = conn.execute("INSERT INTO memories(content, tags) VALUES ('!!!', '[]')")
        emb.upsert_embedding(conn, cur.lastrowid, {})
        assert emb.rebuild_all_embeddings(conn, "tfidf") == 1
        marker = conn.execute(
            "SELECT embedding, representation, encoding_source FROM memories_embeddings WHERE memory_id = ?",
            (cur.lastrowid,),
        ).fetchone()
        assert marker["embedding"] is None
        assert marker["representation"] == "empty"
        assert marker["encoding_source"] == "python"
        status = emb.get_embedding_integrity_status(conn, "tfidf")
        assert status["mismatch"] is False
        assert status["audit"]["missing_count"] == 0
        epoch_before = emb._meta_get(conn, "embedding_change_epoch")
        assert storage.semantic_search(conn, "ordinary words", auto_rebuild=False) == []
        assert emb._meta_get(conn, "embedding_change_epoch") == epoch_before
        assert storage.semantic_search(conn, "ordinary words", auto_rebuild=False) == []
        assert emb._meta_get(conn, "embedding_change_epoch") == epoch_before


def test_repair_upsert_aborts_on_stolen_lease_without_overwriting_winner(tmp_path):
    """D1-style repair statements are individually lease-fenced."""
    from memora.schema import ensure_schema

    conn = sqlite3.connect(tmp_path / "repair-fence.db")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO memories_meta(key, value) VALUES (?, ?)",
        (emb._REBUILD_LEASE_KEY, "winner|9999999999"),
    )
    conn.execute(
        "INSERT INTO memories_embedding_repairs(memory_id, repaired_generation) VALUES (7, 'winner-generation')"
    )
    with pytest.raises(emb.EmbeddingIntegrityFault, match="integrity_rebuild_lease_lost"):
        emb._upsert_embedding_repair(conn, 7, "loser-generation", "loser")
    assert conn.execute(
        "SELECT repaired_generation FROM memories_embedding_repairs WHERE memory_id = 7"
    ).fetchone()[0] == "winner-generation"
    conn.close()


def test_rebuild_metadata_and_release_are_statement_fenced(tmp_path):
    """A suspended loser cannot publish or release after the winner takes over."""
    from memora.schema import ensure_schema

    conn = sqlite3.connect(tmp_path / "metadata-fence.db")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO memories_meta(key, value) VALUES (?, ?)",
        (emb._REBUILD_LEASE_KEY, "winner|9999999999"),
    )
    emb._meta_set(conn, "embedding_model", "winner-model")
    emb._write_embedding_integrity(conn, {
        "state": "initialized", "generation": "winner", "fingerprint": "winner-model",
    })
    winner_model = emb._meta_get(conn, "embedding_model")
    winner_stamp = emb._meta_get(conn, emb._INTEGRITY_KEY)

    with pytest.raises(emb.EmbeddingIntegrityFault, match="integrity_rebuild_lease_lost"):
        emb._write_embedding_integrity(conn, {"state": "building"}, lease_owner="loser")
    with pytest.raises(emb.EmbeddingIntegrityFault, match="integrity_rebuild_lease_lost"):
        emb.set_stored_embedding_model(conn, "loser-model", lease_owner="loser")
    with pytest.raises(emb.EmbeddingIntegrityFault, match="integrity_rebuild_lease_lost"):
        emb._stamp_integrity_audit(conn, {}, "loser-model", lease_owner="loser")
    with pytest.raises(emb.EmbeddingIntegrityFault, match="integrity_rebuild_lease_lost"):
        emb._release_rebuild_lease(conn, "loser")

    assert emb._meta_get(conn, "embedding_model") == winner_model
    assert emb._meta_get(conn, emb._INTEGRITY_KEY) == winner_stamp
    assert emb._meta_get(conn, emb._REBUILD_LEASE_KEY) == "winner|9999999999"
    conn.close()


@pytest.mark.parametrize("memory_count", (0, 1), ids=("empty", "non-empty"))
def test_rebuild_finalization_metadata_survives_injected_lease_steal(
    tmp_path, monkeypatch, memory_count
):
    """Both finalization paths leave winner metadata byte-identical after a steal."""
    from memora.schema import ensure_schema

    conn = sqlite3.connect(tmp_path / f"finalize-{memory_count}.db")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    if memory_count:
        conn.execute("INSERT INTO memories(content, tags) VALUES ('finalization row', '[]')")
    conn.commit()

    winner_integrity = '{"fingerprint":"winner-model","generation":"winner","state":"initialized"}'
    original_set_model = emb.set_stored_embedding_model
    fired = {"value": False}

    def steal_before_model_publish(connection, model, *, lease_owner=None):
        if lease_owner is not None and not fired["value"]:
            fired["value"] = True
            connection.execute(
                "UPDATE memories_meta SET value = ? WHERE key = ?",
                ("winner|9999999999", emb._REBUILD_LEASE_KEY),
            )
            emb._meta_set(connection, "embedding_model", "winner-model")
            emb._meta_set(connection, emb._INTEGRITY_KEY, winner_integrity)
            connection.commit()
        return original_set_model(connection, model, lease_owner=lease_owner)

    monkeypatch.setattr(emb, "set_stored_embedding_model", steal_before_model_publish)
    with pytest.raises(emb.EmbeddingIntegrityFault, match="integrity_rebuild_lease_lost"):
        emb.rebuild_all_embeddings(conn, "tfidf")
    assert fired["value"] is True
    assert emb._meta_get(conn, "embedding_model") == "winner-model"
    assert emb._meta_get(conn, emb._INTEGRITY_KEY) == winner_integrity
    assert emb._meta_get(conn, emb._REBUILD_LEASE_KEY) == "winner|9999999999"
    conn.close()


def test_rebuild_release_fails_after_injected_post_assert_steal(tmp_path, monkeypatch):
    """The final assert/release gap cannot turn a stolen rebuild into success."""
    from memora.schema import ensure_schema

    conn = sqlite3.connect(tmp_path / "release-steal.db")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    conn.execute("INSERT INTO memories(content, tags) VALUES ('release fence row', '[]')")
    conn.commit()
    original_release = emb._release_rebuild_lease
    winner_integrity = '{"fingerprint":"winner-model","generation":"winner","state":"initialized"}'

    def steal_before_release(connection, owner):
        connection.execute(
            "UPDATE memories_meta SET value = ? WHERE key = ?",
            ("winner|9999999999", emb._REBUILD_LEASE_KEY),
        )
        emb._meta_set(connection, "embedding_model", "winner-model")
        emb._meta_set(connection, emb._INTEGRITY_KEY, winner_integrity)
        connection.commit()
        return original_release(connection, owner)

    monkeypatch.setattr(emb, "_release_rebuild_lease", steal_before_release)
    with pytest.raises(emb.EmbeddingIntegrityFault, match="integrity_rebuild_lease_lost"):
        emb.rebuild_all_embeddings(conn, "tfidf")
    assert emb._meta_get(conn, "embedding_model") == "winner-model"
    assert emb._meta_get(conn, emb._INTEGRITY_KEY) == winner_integrity
    assert emb._meta_get(conn, emb._REBUILD_LEASE_KEY) == "winner|9999999999"
    conn.close()


def test_stolen_rebuild_lease_fences_loser_vector_writes_and_certification(tmp_path, monkeypatch):
    """A slow owner loses the heartbeat lease without corrupting the winner."""
    from memora.schema import ensure_schema

    path = tmp_path / "lease-race.db"
    setup = sqlite3.connect(path)
    setup.row_factory = sqlite3.Row
    ensure_schema(setup)
    setup.executemany(
        "INSERT INTO memories(content, tags) VALUES (?, '[]')",
        [("first rebuild row",), ("second rebuild row",)],
    )
    setup.commit()
    setup.close()

    slow = sqlite3.connect(path, check_same_thread=False)
    winner = sqlite3.connect(path, check_same_thread=False)
    slow.row_factory = sqlite3.Row
    winner.row_factory = sqlite3.Row
    monkeypatch.setattr(emb, "_REBUILD_LEASE_SECONDS", 0)
    entered = threading.Event()
    release = threading.Event()
    calls = {"n": 0}
    calls_lock = threading.Lock()

    def slow_then_winner(*_args):
        with calls_lock:
            calls["n"] += 1
            call = calls["n"]
        if call == 1:
            entered.set()
            assert release.wait(timeout=5)
            return {"old-owner": 1.0}
        return {"winner": 1.0}

    monkeypatch.setattr(emb, "compute_embedding", slow_then_winner)
    loser = {}

    def run_slow():
        try:
            emb.rebuild_all_embeddings(slow, "tfidf")
        except Exception as exc:  # The test asserts the ownership fence below.
            loser["error"] = exc

    thread = threading.Thread(target=run_slow)
    thread.start()
    assert entered.wait(timeout=5)
    time.sleep(1.1)  # lease is stale at the one-second timestamp resolution
    assert emb.rebuild_all_embeddings(winner, "tfidf") == 2
    winner_stamp = emb.get_embedding_integrity(winner)
    release.set()
    thread.join(timeout=5)

    assert isinstance(loser.get("error"), emb.EmbeddingIntegrityFault)
    assert loser["error"].reason == "integrity_rebuild_lease_lost"
    assert emb.get_embedding_integrity(winner) == winner_stamp
    payloads = [
        emb.json_to_embedding(row["embedding"])
        for row in winner.execute("SELECT embedding FROM memories_embeddings ORDER BY memory_id")
    ]
    assert payloads == [{"winner": 1.0}, {"winner": 1.0}]
    assert emb.get_embedding_integrity_status(winner, "tfidf")["mismatch"] is False
    slow.close()
    winner.close()


def test_p1_dense_key_set_exact_not_prefix():
    """Sparse bag with keys 0..7 + word key must NOT classify as dense."""
    vec = {str(i): 0.1 for i in range(8)}
    vec["ure"] = 0.9
    assert emb._vector_representation(vec) == "sparse"
    assert emb._vector_representation({str(i): 0.1 for i in range(8)}) == "dense:8"


def test_p1_call_wide_dim_across_chunks(fake_openai, monkeypatch):
    """P1: 2049 inputs, chunk1 dim=1 and chunk2 dim=2 must raise under strict."""
    monkeypatch.setenv("MEMORA_EMBEDDING_STRICT", "1")
    monkeypatch.setenv("MEMORA_EMBEDDING_API_KEY", "k")
    monkeypatch.setenv("MEMORA_EMBEDDING_BASE_URL", "https://e")
    original_init = FakeOpenAI.__init__

    def init_q(self, **kwargs):
        original_init(self, **kwargs)
        # First chunk 2048 @ dim 1, second chunk 1 @ dim 2
        self.queue(_ok_response(2048, dim=1))
        self.queue(_ok_response(1, dim=2))

    monkeypatch.setattr(FakeOpenAI, "__init__", init_q)
    entries = [{"content": f"t{i}", "tags": []} for i in range(2049)]
    with pytest.raises((emb.EmbeddingStrictError, emb.EmbeddingProviderError, ValueError)):
        emb.compute_embeddings_batch(entries, "openai")


def test_p1_absorb_second_phase3_embedding_no_partial(monkeypatch, absorb_backend):
    """P1: compensation removes a prior write on SQLite and D1 semantics."""
    import memora.storage as storage

    monkeypatch.setattr(storage, "_group_facts_by_similarity", lambda pairs: [[i] for i in range(len(pairs))])

    with storage.connect() as conn:
        n_add = {"n": 0}
        real_add = storage.add_memory

        def add_once(*a, **k):
            n_add["n"] += 1
            if n_add["n"] >= 2:
                raise emb.EmbeddingStrictError(
                    "MEMORA_EMBEDDING_STRICT=1 hard-stop: simulated phase-3 failure"
                )
            return real_add(*a, **k)

        monkeypatch.setattr(storage, "add_memory", add_once)
        with pytest.raises(emb.EmbeddingStrictError):
            storage.absorb_memory(
                conn,
                facts=[
                    "first unique fact alpha for absorb phase three",
                    "second unique fact beta for absorb phase three",
                ],
            )
        n = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        assert n == 0, f"partial rows remain after phase-3 failure: {n}"


def test_p1_1_mid_add_memory_failure_still_compensated(monkeypatch, absorb_backend):
    """P1-1 hard case: FTS fails after INSERT; D1 compensation still removes it."""
    import memora.storage as storage

    monkeypatch.setattr(storage, "_group_facts_by_similarity", lambda pairs: [[i] for i in range(len(pairs))])

    real_fts = storage._fts_upsert
    calls = {"n": 0}

    def flaky_fts(conn, memory_id, *a, **k):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("simulated FTS failure after INSERT")
        return real_fts(conn, memory_id, *a, **k)

    monkeypatch.setattr(storage, "_fts_upsert", flaky_fts)

    with storage.connect() as conn:
        try:
            result = storage.absorb_memory(
                conn,
                facts=[
                    "first unique fact alpha for absorb mid fail",
                    "second unique fact beta for absorb mid fail",
                ],
            )
        except storage.MemoryWriteError:
            result = None  # full cleanup then re-raise
        n = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        if result is not None and result.get("partial"):
            assert result.get("error") == "partial_write"
            # survivors only the declared orphans
            assert n == len(result.get("orphan_ids") or [])
        else:
            assert n == 0, f"partial rows remain after mid-add failure: {n}"


def test_p1_1_embedding_upsert_failure_still_compensated(monkeypatch, absorb_backend):
    """Hard case: embedding write fails after INSERT; no D1 row may survive."""
    import memora.storage as storage

    monkeypatch.setattr(storage, "_group_facts_by_similarity", lambda pairs: [[i] for i in range(len(pairs))])
    real_upsert = storage._upsert_embedding
    calls = {"n": 0}

    def flaky_embedding(conn, memory_id, vector):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("simulated embedding upsert failure after INSERT")
        return real_upsert(conn, memory_id, vector)

    monkeypatch.setattr(storage, "_upsert_embedding", flaky_embedding)

    with storage.connect() as conn:
        with pytest.raises(storage.MemoryWriteError, match="embedding upsert"):
            storage.absorb_memory(
                conn,
                facts=[
                    "first unique fact alpha embedding failure",
                    "second unique fact beta embedding failure",
                ],
            )
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memories_embeddings").fetchone()[0] == 0


def test_p1_2_delete_false_reports_partial(monkeypatch, absorb_backend):
    """P1-2: delete_memory False must not claim cleaned; surface partial_write."""
    import memora.storage as storage

    monkeypatch.setattr(storage, "_group_facts_by_similarity", lambda pairs: [[i] for i in range(len(pairs))])

    real_add = storage.add_memory
    n_add = {"n": 0}

    def add_then_fail(*a, **k):
        n_add["n"] += 1
        if n_add["n"] == 1:
            return real_add(*a, **k)
        # second: allocate via real then raise MemoryWriteError path by failing fts
        raise RuntimeError("phase3 second job fail before add")

    monkeypatch.setattr(storage, "add_memory", add_then_fail)
    monkeypatch.setattr(storage, "delete_memory", lambda *a, **k: False)

    with storage.connect() as conn:
        result = storage.absorb_memory(
            conn,
            facts=[
                "first unique fact alpha delete false",
                "second unique fact beta delete false",
            ],
        )
        assert result.get("partial") is True
        assert result.get("error") == "partial_write"
        assert result.get("cleaned_ids") == []
        assert result.get("orphan_ids")  # at least the first written id
        survivors = conn.execute("SELECT id FROM memories ORDER BY id").fetchall()
        assert [row[0] for row in survivors] == result["orphan_ids"]


def test_d1_compensating_delete_refuses_mismatched_nonce(absorb_backend):
    """Safety control: an owned id is not enough; nonce mismatch preserves it."""
    import memora.storage as storage

    with storage.connect() as conn:
        record = storage.add_memory(
            conn,
            content="nonce-protected memory must survive a stale compensator",
            absorb_nonce="owned-by-this-absorb",
        )
        assert storage.delete_memory(
            conn, record["id"], require_absorb_nonce="some-other-absorb"
        ) is False
        assert conn.execute(
            "SELECT 1 FROM memories WHERE id = ?", (record["id"],)
        ).fetchone() is not None
        # Checking the embedding proves the refusal happened before cleanup,
        # rather than merely before the final DELETE.
        assert conn.execute(
            "SELECT 1 FROM memories_embeddings WHERE memory_id = ?", (record["id"],)
        ).fetchone() is not None


def test_d1_missing_embedding_row_is_integrity_failure(absorb_backend):
    """Coverage catches a memory row that has no embedding row on both stores."""
    import memora.storage as storage

    with storage.connect() as conn:
        record = storage.add_memory(conn, content="memory deliberately missing its vector")
        conn.execute("DELETE FROM memories_embeddings WHERE memory_id = ?", (record["id"],))
        assert emb.check_embedding_model_mismatch(conn, "tfidf") is True


def test_fake_d1_auto_commit_survives_rollback_and_exposes_last_row_id(
    fake_d1_connection,
):
    """The harness distinguishes D1's durable statements from SQLite rollback."""
    d1 = fake_d1_connection("d1.db")
    d1.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT)")
    inserted = d1.execute("INSERT INTO probe (value) VALUES (?)", ("durable",))
    d1.rollback()
    assert inserted.lastrowid == 1
    assert d1.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 1
    d1.close()

    # Negative control: restoring a real transaction makes rollback erase the
    # row.  This is the unsafe difference that local-only absorb tests mask.
    local_control = fake_d1_connection("transactional.db", transactional=True)
    local_control.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, value TEXT)")
    local_control.execute("INSERT INTO probe (value) VALUES (?)", ("rolled-back",))
    local_control.rollback()
    assert local_control.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 0
    local_control.close()


def test_nonce_refusal_negative_control_turns_red_when_guard_is_bypassed(
    monkeypatch, absorb_backend
):
    """If the nonce guard is bypassed, the refusal assertion fails and data dies."""
    import memora.storage as storage

    with storage.connect() as conn:
        record = storage.add_memory(
            conn,
            content="negative-control nonce guard must not be removed",
            absorb_nonce="correct-owner",
        )
        guarded_delete = storage.delete_memory

        def unsafe_delete(connection, memory_id, *, require_absorb_nonce=None):
            return guarded_delete(connection, memory_id, require_absorb_nonce=None)

        # Equivalent to deleting the nonce comparison: the original refusal
        # assertion is now red, and the row is destructively removed.
        monkeypatch.setattr(storage, "delete_memory", unsafe_delete)
        with pytest.raises(AssertionError):
            assert storage.delete_memory(
                conn, record["id"], require_absorb_nonce="wrong-owner"
            ) is False
        assert conn.execute(
            "SELECT 1 FROM memories WHERE id = ?", (record["id"],)
        ).fetchone() is None


def test_n6_absorb_strict_failure_is_clean_not_unboundlocal(monkeypatch, tmp_path):
    """N6: absorb_memory must re-raise EmbeddingStrictError, not UnboundLocalError."""
    import memora.storage as storage
    from memora.backends import LocalSQLiteBackend

    monkeypatch.setenv("MEMORA_EMBEDDING_STRICT", "1")
    backend = LocalSQLiteBackend(tmp_path / "absorb.db")
    monkeypatch.setattr(storage, "STORAGE_BACKEND", backend)
    monkeypatch.setattr(storage, "EMBEDDING_MODEL", "openai")

    def boom(*a, **k):
        raise emb.EmbeddingStrictError(
            "MEMORA_EMBEDDING_STRICT=1 hard-stop: backend=openai endpoint=https://x "
            "model=@cf/baai/bge-m3 — RuntimeError: 503. "
            "This is an embedding-provider/config failure, not a memora bug."
        )

    monkeypatch.setattr(storage, "_compute_embedding", boom)

    with storage.connect() as conn:
        with pytest.raises(emb.EmbeddingStrictError, match="hard-stop|provider"):
            storage.absorb_memory(conn, facts=["a fact long enough to absorb"])


# ---------------------------------------------------------------------------
# parse_openai_embeddings_response unit (direct)
# ---------------------------------------------------------------------------

def test_parse_empty_vector_rejected():
    with pytest.raises(ValueError, match="empty"):
        emb.parse_openai_embeddings_response(
            _EmbResponse([_EmbItem(0, [])]), expected_n=1
        )


def test_parse_result_count_equals_input():
    r, dim = emb.parse_openai_embeddings_response(_ok_response(3, dim=2, order=[2, 0, 1]), 3)
    assert len(r) == 3
    assert dim == 2
    assert list(r[0].keys()) == ["0", "1"]
