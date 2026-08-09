"""Embeddings credential atomicity, batch validation, cache, fingerprint.

Uses a fake openai module — no network. conftest forces EMBEDDING_MODEL=tfidf
for storage tests; this file exercises the public embeddings API with its own
env fixtures.
"""
from __future__ import annotations

import math
import sqlite3
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
    for var in _EMBED_ENV:
        monkeypatch.delenv(var, raising=False)
    yield
    emb._embedding_model_cache.clear()
    emb._warned_backends.clear()


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
        with pytest.raises(RuntimeError, match="MEMORA_EMBEDDING_STRICT"):
            emb.compute_embedding("hello", None, [], "openai")
        # Constructor must NOT have been called
        assert FakeOpenAI.instances == []
    else:
        # Non-strict: TF-IDF fallback, still no OpenAI client with cross-pair
        vec = emb.compute_embedding("hello world", None, [], "openai")
        assert FakeOpenAI.instances == []
        assert any(not k.isdigit() for k in vec.keys()) or vec  # tfidf word keys


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
    with pytest.raises(RuntimeError, match="unknown embedding backend"):
        emb.compute_embedding("x", None, [], "not-a-real-backend")


def test_n2_strict_missing_key(fake_openai, monkeypatch):
    monkeypatch.setenv("MEMORA_EMBEDDING_STRICT", "1")
    # neither MEMORA nor OPENAI keys
    with pytest.raises(RuntimeError, match="MEMORA_EMBEDDING_STRICT"):
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
    with pytest.raises(RuntimeError, match="MEMORA_EMBEDDING_STRICT"):
        emb.compute_embedding("x", None, [], "openai")


def test_n2_nonstrict_all_or_nothing_tfidf(fake_openai, monkeypatch):
    """Non-strict chunk failure returns TF-IDF for entire batch (no mixed vectors)."""
    monkeypatch.setenv("MEMORA_EMBEDDING_API_KEY", "k")
    monkeypatch.setenv("MEMORA_EMBEDDING_BASE_URL", "https://e")
    original_init = FakeOpenAI.__init__

    def init_q(self, **kwargs):
        original_init(self, **kwargs)
        # Partial: only index 0 for two inputs
        self.queue(_EmbResponse([_EmbItem(0, _dense(0))]))

    monkeypatch.setattr(FakeOpenAI, "__init__", init_q)
    out = emb.compute_embeddings_batch(
        [{"content": "alpha beta", "tags": []}, {"content": "gamma delta", "tags": []}],
        "openai",
    )
    assert len(out) == 2
    # TF-IDF word keys, not dense "0","1"
    assert "alpha" in out[0] or "beta" in out[0]
    assert all(not (len(v) > 0 and all(k.isdigit() for k in v)) for v in out)


# ---------------------------------------------------------------------------
# N5 — fingerprint forces rebuild
# ---------------------------------------------------------------------------

def _meta_conn(tmp_path) -> sqlite3.Connection:
    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE memories_meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE memories_embeddings (
            memory_id INTEGER PRIMARY KEY,
            embedding TEXT
        );
        """
    )
    return conn


def test_n5_word_key_store_labelled_openai_requires_rebuild(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
    conn = _meta_conn(tmp_path)
    # Legacy label "openai" + word-key (sparse) vector as produced by TF-IDF fallback
    emb.set_stored_embedding_model(conn, "openai")
    sparse = {"ure": 0.2, "xdg": 0.3, "zig": 0.5}
    emb.upsert_embedding(conn, 1, sparse)
    conn.commit()
    assert emb.check_embedding_model_mismatch(conn, "openai") is True


def test_n5_matching_dense_fingerprint_no_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
    conn = _meta_conn(tmp_path)
    dense = {str(i): 0.1 for i in range(8)}
    emb.upsert_embedding(conn, 1, dense)
    emb.set_stored_embedding_model(conn, "openai|text-embedding-3-small|dense:8")
    conn.commit()
    assert emb.check_embedding_model_mismatch(conn, "openai") is False


# ---------------------------------------------------------------------------
# parse_openai_embeddings_response unit (direct)
# ---------------------------------------------------------------------------

def test_parse_empty_vector_rejected():
    with pytest.raises(ValueError, match="empty"):
        emb.parse_openai_embeddings_response(
            _EmbResponse([_EmbItem(0, [])]), expected_n=1
        )


def test_parse_result_count_equals_input():
    r = emb.parse_openai_embeddings_response(_ok_response(3, dim=2, order=[2, 0, 1]), 3)
    assert len(r) == 3
    assert list(r[0].keys()) == ["0", "1"]
