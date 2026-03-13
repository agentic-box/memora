"""Embedding computation, storage, and similarity functions."""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from collections import Counter
from typing import Any, Dict, List, Optional

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Cache for embedding models
_embedding_model_cache: Dict[str, Any] = {}


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

    except ImportError:
        return _compute_embedding_tfidf(text)
    except Exception:
        return _compute_embedding_tfidf(text)


def _compute_embedding_openai(text: str) -> Dict[str, float]:
    """Use OpenAI embeddings API."""
    try:
        import openai

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            return _compute_embedding_tfidf(text)

        if "openai_client" not in _embedding_model_cache:
            _embedding_model_cache["openai_client"] = openai.OpenAI(api_key=api_key)

        client = _embedding_model_cache["openai_client"]
        model_name = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")

        response = client.embeddings.create(
            input=text,
            model=model_name,
        )

        embedding = response.data[0].embedding

        return {str(i): float(val) for i, val in enumerate(embedding)}

    except ImportError:
        return _compute_embedding_tfidf(text)
    except Exception:
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
