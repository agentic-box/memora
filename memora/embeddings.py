"""Embedding computation, storage, and similarity functions."""
from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
from collections import Counter
from typing import Any, Dict, List, Optional, Set

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Cache for embedding models
_embedding_model_cache: Dict[str, Any] = {}

_logger = logging.getLogger(__name__)

# Backend-name set for warn-once suppression so a persistent API outage
# does not flood the log. One warning per (backend, reason) pair per
# process lifetime is enough to surface the silent-fallback class that
# Memora issue #457 documented.
_warned_backends: Set[str] = set()


def _strict_mode() -> bool:
    """True when MEMORA_EMBEDDING_STRICT=1 (or yes/true/on). In strict mode
    the configured backend is allowed no silent fallback — any failure
    raises so the user sees it loudly instead of getting TF-IDF embeddings
    under the rug (memora #457)."""
    return os.getenv("MEMORA_EMBEDDING_STRICT", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _warn_once(backend_reason: str, detail: str) -> None:
    """Log a fallback-to-TFIDF warning at most once per process for a given
    (backend, reason) key. The first failure is the informative one; repeats
    on every embedding call would be noise."""
    if backend_reason in _warned_backends:
        return
    _warned_backends.add(backend_reason)
    _logger.warning(
        "memora.embeddings: %s failed, falling back to TF-IDF: %s. "
        "Set MEMORA_EMBEDDING_STRICT=1 to fail fast instead. "
        "Further failures from this backend will be suppressed this process.",
        backend_reason, detail,
    )


def _strict_raise(backend: str, exc: BaseException) -> None:
    """Raise when MEMORA_EMBEDDING_STRICT=1 so configuration drift surfaces
    immediately instead of silently degrading to TF-IDF."""
    raise RuntimeError(
        f"MEMORA_EMBEDDING_STRICT=1 and {backend} embedding failed: "
        f"{type(exc).__name__}: {exc}"
    ) from exc


def _get_embedding_text(
    content: str,
    metadata: Optional[Dict[str, Any]],
    tags: List[str],
) -> str:
    """Combine content, metadata, and tags into a single text for embedding."""
    parts: List[str] = [content]

    if metadata:
        try:
            metadata_str = json.dumps(metadata, ensure_ascii=False)
        except (TypeError, ValueError):
            metadata_str = str(metadata)
        parts.append(metadata_str)

    if tags:
        parts.append(" ".join(tags))

    return " \n ".join(parts)


def _compute_embedding_tfidf(text: str) -> Dict[str, float]:
    """TF-IDF style bag-of-words embedding (default, no dependencies)."""
    tokens = _TOKEN_RE.findall(text.lower())
    if not tokens:
        return {}

    counts = Counter(tokens)
    total = sum(counts.values())
    if not total:
        return {}

    return {token: count / total for token, count in counts.items()}


def _compute_embedding_sentence_transformers(text: str) -> Dict[str, float]:
    """Use sentence-transformers for better semantic embeddings."""
    try:
        if "sentence_transformers" not in _embedding_model_cache:
            from sentence_transformers import SentenceTransformer
            model_name = os.getenv("SENTENCE_TRANSFORMERS_MODEL", "all-MiniLM-L6-v2")
            _embedding_model_cache["sentence_transformers"] = SentenceTransformer(model_name)

        model = _embedding_model_cache["sentence_transformers"]
        embedding = model.encode(text, convert_to_numpy=True)

        return {str(i): float(val) for i, val in enumerate(embedding)}

    except ImportError as exc:
        if _strict_mode():
            _strict_raise("sentence-transformers", exc)
        _warn_once(
            "sentence-transformers:ImportError",
            "package not installed (pip install sentence-transformers)",
        )
        return _compute_embedding_tfidf(text)
    except Exception as exc:
        if _strict_mode():
            _strict_raise("sentence-transformers", exc)
        _warn_once(
            "sentence-transformers:runtime",
            f"{type(exc).__name__}: {exc}",
        )
        return _compute_embedding_tfidf(text)


def _embedding_credentials() -> tuple:
    """Credentials for the EMBEDDING endpoint, which need not be the LLM's.

    The LLM client (storage.get_llm_client) reads OPENAI_API_KEY/OPENAI_BASE_URL
    directly, so a deployment that routes chat through one provider and
    embeddings through another had no way to express that: pointing OPENAI_* at
    an embedding host silently broke absorption, and leaving it broke embeddings.
    MEMORA_EMBEDDING_* overrides that split, falling back to OPENAI_* so existing
    single-provider installs keep working untouched.
    """
    api_key = os.getenv("MEMORA_EMBEDDING_API_KEY") or os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("MEMORA_EMBEDDING_BASE_URL") or os.getenv("OPENAI_BASE_URL")
    return api_key, base_url


def _embedding_client(openai_module):
    """Build (and cache) the embedding client on its own credentials."""
    if "openai_client" not in _embedding_model_cache:
        api_key, base_url = _embedding_credentials()
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        _embedding_model_cache["openai_client"] = openai_module.OpenAI(**kwargs)
    return _embedding_model_cache["openai_client"]


def _compute_embedding_openai(text: str) -> Dict[str, float]:
    """Use OpenAI embeddings API."""
    try:
        import openai

        api_key, _ = _embedding_credentials()
        if not api_key:
            if _strict_mode():
                raise RuntimeError(
                    "MEMORA_EMBEDDING_STRICT=1 and no embedding API key is set "
                    "(MEMORA_EMBEDDING_API_KEY or OPENAI_API_KEY)"
                )
            _warn_once(
                "openai:no-api-key",
                "no embedding API key (MEMORA_EMBEDDING_API_KEY / OPENAI_API_KEY)",
            )
            return _compute_embedding_tfidf(text)

        client = _embedding_client(openai)
        model_name = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")

        response = client.embeddings.create(
            input=text,
            model=model_name,
        )

        embedding = response.data[0].embedding

        return {str(i): float(val) for i, val in enumerate(embedding)}

    except ImportError as exc:
        if _strict_mode():
            _strict_raise("openai", exc)
        _warn_once(
            "openai:ImportError",
            "package not installed (pip install openai)",
        )
        return _compute_embedding_tfidf(text)
    except Exception as exc:
        if _strict_mode():
            _strict_raise("openai", exc)
        _warn_once(
            "openai:runtime",
            f"{type(exc).__name__}: {exc}",
        )
        return _compute_embedding_tfidf(text)


def compute_embedding(
    content: str,
    metadata: Optional[Dict[str, Any]],
    tags: List[str],
    embedding_model: str = "tfidf",
) -> Dict[str, float]:
    """Compute embedding using configured backend."""
    text = _get_embedding_text(content, metadata, tags)

    if embedding_model == "sentence-transformers":
        return _compute_embedding_sentence_transformers(text)
    elif embedding_model == "openai":
        return _compute_embedding_openai(text)
    else:
        return _compute_embedding_tfidf(text)


def compute_embeddings_batch(
    entries: List[Dict[str, Any]],
    embedding_model: str = "tfidf",
) -> List[Dict[str, float]]:
    """Compute embeddings for multiple entries in a single batch API call.

    Each entry must have: content (str), metadata (Optional[Dict]), tags (List[str]).
    Uses the same text assembly path as compute_embedding() for identical payloads.
    Falls back to per-item sequential on error to preserve TF-IDF fallback semantics.
    """
    if not entries:
        return []

    # Assemble texts using the same path as compute_embedding()
    texts = [
        _get_embedding_text(e["content"], e.get("metadata"), e.get("tags", []))
        for e in entries
    ]

    if embedding_model == "openai":
        return _compute_embeddings_openai_batch(texts)
    else:
        # For non-OpenAI backends, fall back to sequential
        return [compute_embedding(e["content"], e.get("metadata"), e.get("tags", []), embedding_model) for e in entries]


def _compute_embeddings_openai_batch(texts: List[str]) -> List[Dict[str, float]]:
    """Batch OpenAI embedding computation with chunking and error fallback."""
    try:
        import openai

        api_key, _ = _embedding_credentials()
        if not api_key:
            if _strict_mode():
                raise RuntimeError(
                    "MEMORA_EMBEDDING_STRICT=1 and no embedding API key is set "
                    "(MEMORA_EMBEDDING_API_KEY or OPENAI_API_KEY)"
                )
            _warn_once(
                "openai-batch:no-api-key",
                "no embedding API key (MEMORA_EMBEDDING_API_KEY / OPENAI_API_KEY)",
            )
            return [_compute_embedding_tfidf(t) for t in texts]

        client = _embedding_client(openai)
        model_name = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")

        max_chunk = 2048  # OpenAI batch limit
        all_results: List[Dict[str, float]] = []

        for i in range(0, len(texts), max_chunk):
            chunk = texts[i : i + max_chunk]
            try:
                response = client.embeddings.create(input=chunk, model=model_name)
                # Sort by index to preserve order
                sorted_data = sorted(response.data, key=lambda d: d.index)
                for emb in sorted_data:
                    all_results.append({str(j): float(v) for j, v in enumerate(emb.embedding)})
            except Exception as chunk_exc:
                # Strict mode propagates the chunk error; default preserves
                # the per-item fallback behavior but emits a warn-once so
                # the failure is observable (memora #457).
                if _strict_mode():
                    _strict_raise("openai-batch:chunk", chunk_exc)
                _warn_once(
                    "openai-batch:chunk",
                    f"{type(chunk_exc).__name__}: {chunk_exc}",
                )
                for text in chunk:
                    all_results.append(_compute_embedding_openai(text))

        return all_results

    except ImportError as exc:
        if _strict_mode():
            _strict_raise("openai-batch", exc)
        _warn_once(
            "openai-batch:ImportError",
            "package not installed (pip install openai)",
        )
        return [_compute_embedding_tfidf(t) for t in texts]
    except Exception as exc:
        if _strict_mode():
            _strict_raise("openai-batch", exc)
        _warn_once(
            "openai-batch:runtime",
            f"{type(exc).__name__}: {exc}",
        )
        return [_compute_embedding_tfidf(t) for t in texts]


# --- Serialization ---

def embedding_to_json(vector: Dict[str, float]) -> Optional[str]:
    if not vector:
        return None
    items = sorted(vector.items())
    return json.dumps(items, ensure_ascii=False)


def json_to_embedding(data: Optional[str]) -> Dict[str, float]:
    if not data:
        return {}
    try:
        items = json.loads(data)
    except json.JSONDecodeError:
        return {}
    if isinstance(items, list):
        return {str(token): float(weight) for token, weight in items}
    return {}


# --- Similarity ---

def embedding_norm(vector: Dict[str, float]) -> float:
    return math.sqrt(sum(weight * weight for weight in vector.values()))


def cosine_similarity(vec_a: Dict[str, float], vec_b: Dict[str, float]) -> float:
    if not vec_a or not vec_b:
        return 0.0
    dot = 0.0
    for token, weight in vec_a.items():
        dot += weight * vec_b.get(token, 0.0)
    norm_a = embedding_norm(vec_a)
    norm_b = embedding_norm(vec_b)
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


# --- DB operations ---

def upsert_embedding(
    conn: sqlite3.Connection,
    memory_id: int,
    vector: Dict[str, float],
) -> None:
    emb_json = embedding_to_json(vector)
    conn.execute(
        """
        INSERT INTO memories_embeddings(memory_id, embedding)
        VALUES(?, ?)
        ON CONFLICT(memory_id) DO UPDATE SET embedding=excluded.embedding
        """,
        (memory_id, emb_json),
    )


def delete_embedding(conn: sqlite3.Connection, memory_id: int) -> None:
    conn.execute("DELETE FROM memories_embeddings WHERE memory_id = ?", (memory_id,))


def get_embeddings_for_ids(
    conn: sqlite3.Connection,
    memory_ids: List[int],
    *,
    batch_size: int = 50,
) -> Dict[int, Dict[str, float]]:
    if not memory_ids:
        return {}
    mapping: Dict[int, Dict[str, float]] = {}
    for i in range(0, len(memory_ids), batch_size):
        batch = memory_ids[i : i + batch_size]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"SELECT memory_id, embedding FROM memories_embeddings WHERE memory_id IN ({placeholders})",
            batch,
        ).fetchall()
        for row in rows:
            mapping[row["memory_id"]] = json_to_embedding(row["embedding"])
    return mapping


# --- Model management ---

def get_stored_embedding_model(conn: sqlite3.Connection) -> Optional[str]:
    """Get the embedding model name stored in the database."""
    row = conn.execute(
        "SELECT value FROM memories_meta WHERE key = 'embedding_model'"
    ).fetchone()
    return row["value"] if row else None


def set_stored_embedding_model(conn: sqlite3.Connection, model: str) -> None:
    """Store the embedding model name in the database."""
    conn.execute(
        """
        INSERT INTO memories_meta (key, value) VALUES ('embedding_model', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (model,),
    )
    conn.commit()


def check_embedding_model_mismatch(conn: sqlite3.Connection, current_model: str) -> bool:
    """Check if current embedding model differs from stored model."""
    stored = get_stored_embedding_model(conn)
    if stored is None:
        count = conn.execute("SELECT COUNT(*) FROM memories_embeddings").fetchone()[0]
        if count > 0:
            return True
        return False
    return stored != current_model


def rebuild_all_embeddings(conn: sqlite3.Connection, embedding_model: str) -> int:
    """Rebuild all embeddings using given embedding model."""
    rows = conn.execute(
        "SELECT id, content, metadata, tags FROM memories"
    ).fetchall()
    updated = 0
    for row in rows:
        memory_id = row["id"]
        metadata = json.loads(row["metadata"]) if row["metadata"] else None
        tags = json.loads(row["tags"]) if row["tags"] else []
        vector = compute_embedding(row["content"], metadata, tags, embedding_model)
        upsert_embedding(conn, memory_id, vector)
        updated += 1
    set_stored_embedding_model(conn, embedding_model)
    conn.commit()
    return updated
