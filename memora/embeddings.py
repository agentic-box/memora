"""Embedding computation, storage, and similarity functions."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sqlite3
from collections import Counter
from typing import Any, Dict, List, Optional, Set, Tuple

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Cache for embedding models / clients. OpenAI clients are keyed by
# (base_url, key-fingerprint) — never store the raw secret (N3).
_embedding_model_cache: Dict[str, Any] = {}

_logger = logging.getLogger(__name__)

# Backend-name set for warn-once suppression so a persistent API outage
# does not flood the log. One warning per (backend, reason) pair per
# process lifetime is enough to surface the silent-fallback class that
# Memora issue #457 documented.
_warned_backends: Set[str] = set()

# Known embedding backends. Unknown names fall through to TF-IDF unless strict.
_KNOWN_BACKENDS = frozenset({"openai", "sentence-transformers", "tfidf"})


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
    immediately instead of silently degrading to TF-IDF.

    Includes endpoint hint so operators see a provider outage, not an internal bug (N6).
    """
    endpoint = os.getenv("MEMORA_EMBEDDING_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "(default host)"
    model = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
    raise EmbeddingStrictError(
        f"MEMORA_EMBEDDING_STRICT=1 hard-stop (no TF-IDF fallback): "
        f"backend={backend} endpoint={endpoint} model={model} — "
        f"{type(exc).__name__}: {exc}. "
        f"This is an embedding-provider/config failure, not a memora bug. "
        f"Fix the provider or unset MEMORA_EMBEDDING_STRICT to allow TF-IDF fallback."
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


# ---------------------------------------------------------------------------
# N1: atomic credential pairs (never cross-pair MEMORA_* with OPENAI_*)
# ---------------------------------------------------------------------------

class EmbeddingCredentialError(ValueError):
    """Raised when embedding credentials are incomplete or cross-paired."""


class EmbeddingStrictError(RuntimeError):
    """MEMORA_EMBEDDING_STRICT=1 hard-stopped embedding (no silent TF-IDF).

    Callers should surface this as a provider/config failure, not as an
    internal memora bug. Message always starts with MEMORA_EMBEDDING_STRICT=1.
    """


def _env_is_set(name: str) -> bool:
    """True when the variable is present in the process environment.

    Distinguishes unset (absent) from empty-string (present but blank).
    """
    return name in os.environ


def _key_fingerprint(api_key: str) -> str:
    """Short non-reversible fingerprint of a secret for cache keys. Never log the raw key."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def resolve_embedding_credentials(
    env: Optional[Dict[str, str]] = None,
) -> Tuple[str, str]:
    """Resolve embedding API credentials as an ATOMIC provider pair (N1).

    Rules:
    - Fall back to the OPENAI_* pair only when BOTH MEMORA_EMBEDDING_API_KEY and
      MEMORA_EMBEDDING_BASE_URL are wholly ABSENT from the environment.
    - If EITHER MEMORA_* var is present (including empty string), do NOT borrow
      the missing field from OPENAI_* — that would send one provider's secret
      to another provider's host (credential disclosure).
    - When the MEMORA_* pair is selected, both values must be non-empty.
    - When the OPENAI_* pair is selected, the key must be non-empty; base_url
      may be empty only to mean the SDK default host — we still pass base_url
      explicitly when set so the SDK cannot silently pick up a different
      OPENAI_BASE_URL after we intended a split config.

    Returns (api_key, base_url). base_url may be "" for the default OpenAI host
    only on the OPENAI_* path. Raises EmbeddingCredentialError on incomplete pairs.
    """
    e = env if env is not None else os.environ
    mem_key_set = "MEMORA_EMBEDDING_API_KEY" in e
    mem_base_set = "MEMORA_EMBEDDING_BASE_URL" in e

    if mem_key_set or mem_base_set:
        # Atomic MEMORA_* pair — never mix with OPENAI_*.
        missing = []
        if not mem_key_set:
            missing.append("MEMORA_EMBEDDING_API_KEY is unset")
        if not mem_base_set:
            missing.append("MEMORA_EMBEDDING_BASE_URL is unset")
        key = e.get("MEMORA_EMBEDDING_API_KEY", "") if mem_key_set else ""
        base = e.get("MEMORA_EMBEDDING_BASE_URL", "") if mem_base_set else ""
        blanks = []
        if mem_key_set and not str(key).strip():
            blanks.append("MEMORA_EMBEDDING_API_KEY is empty")
        if mem_base_set and not str(base).strip():
            blanks.append("MEMORA_EMBEDDING_BASE_URL is empty")
        if missing or blanks:
            parts = missing + blanks
            raise EmbeddingCredentialError(
                "incomplete MEMORA_EMBEDDING_* pair (atomic credentials; "
                "will not borrow OPENAI_* fields): " + "; ".join(parts)
            )
        return str(key), str(base)

    # Both MEMORA_* wholly absent → OPENAI_* pair.
    key = e.get("OPENAI_API_KEY", "") or ""
    base = e.get("OPENAI_BASE_URL", "") or ""
    if not str(key).strip():
        raise EmbeddingCredentialError(
            "no embedding API key: set MEMORA_EMBEDDING_API_KEY+MEMORA_EMBEDDING_BASE_URL "
            "(atomic pair) or OPENAI_API_KEY (with optional OPENAI_BASE_URL)"
        )
    return str(key), str(base)


def _embedding_credentials() -> Tuple[str, str]:
    """Credentials for the EMBEDDING endpoint (atomic pair). See resolve_embedding_credentials."""
    return resolve_embedding_credentials()


def _openai_client_kwargs(api_key: str, base_url: str) -> Dict[str, Any]:
    """Build OpenAI() kwargs. Always pass base_url when non-empty so the SDK
    cannot recover a different host from process env after a split config."""
    kwargs: Dict[str, Any] = {"api_key": api_key}
    if base_url and str(base_url).strip():
        kwargs["base_url"] = str(base_url).strip()
    return kwargs


def _embedding_client(openai_module):
    """Build (and cache) the embedding client keyed by credential fingerprint (N3)."""
    api_key, base_url = _embedding_credentials()
    cache_key = f"openai_client:{base_url}:{_key_fingerprint(api_key)}"
    if cache_key not in _embedding_model_cache:
        kwargs = _openai_client_kwargs(api_key, base_url)
        _embedding_model_cache[cache_key] = openai_module.OpenAI(**kwargs)
    return _embedding_model_cache[cache_key]


# ---------------------------------------------------------------------------
# N2: response validation — reconstruct BY INDEX, never by arrival order
# ---------------------------------------------------------------------------

def _vector_from_list(values: Any) -> Dict[str, float]:
    """Convert a dense embedding list to our dict representation, validating values."""
    if values is None:
        raise ValueError("embedding vector is None")
    if not hasattr(values, "__len__") or len(values) == 0:
        raise ValueError("embedding vector is empty")
    out: Dict[str, float] = {}
    for i, val in enumerate(values):
        f = float(val)
        if not math.isfinite(f):
            raise ValueError(f"non-finite embedding value at position {i}: {val!r}")
        out[str(i)] = f
    return out


def parse_openai_embeddings_response(response: Any, expected_n: int) -> List[Dict[str, float]]:
    """Validate and reconstruct OpenAI embeddings.create response by index (N2).

    Requires:
    - len(response.data) == expected_n
    - indices exactly cover 0..n-1 with no gaps, dupes, or out-of-range
    - every vector non-empty, finite, and dimensionally uniform across the chunk
    Reconstructs by index so out-of-order complete responses restore input order.
    """
    if expected_n < 0:
        raise ValueError(f"expected_n must be >= 0, got {expected_n}")
    if expected_n == 0:
        return []
    if response is None:
        raise ValueError("embeddings response is None")
    data = getattr(response, "data", None)
    if data is None:
        raise ValueError("embeddings response has no data")
    if len(data) != expected_n:
        raise ValueError(
            f"embeddings response cardinality {len(data)} != expected {expected_n}"
        )

    by_index: Dict[int, Dict[str, float]] = {}
    dims: Optional[int] = None
    for item in data:
        idx = getattr(item, "index", None)
        if idx is None:
            raise ValueError("embeddings response item missing index")
        if not isinstance(idx, int) or isinstance(idx, bool):
            raise ValueError(f"embeddings response index is not an int: {idx!r}")
        if idx < 0 or idx >= expected_n:
            raise ValueError(
                f"embeddings response index {idx} out of range for n={expected_n}"
            )
        if idx in by_index:
            raise ValueError(f"embeddings response duplicate index {idx}")
        emb = getattr(item, "embedding", None)
        vec = _vector_from_list(emb)
        d = len(vec)
        if dims is None:
            dims = d
        elif d != dims:
            raise ValueError(
                f"embeddings dimension inconsistency: index {idx} has d={d}, expected d={dims}"
            )
        by_index[idx] = vec

    missing = [i for i in range(expected_n) if i not in by_index]
    if missing:
        raise ValueError(f"embeddings response missing indices: {missing}")

    return [by_index[i] for i in range(expected_n)]


def _handle_credential_or_api_failure(backend: str, exc: BaseException, texts: Optional[List[str]] = None):
    """Strict raises; non-strict warns once and returns TF-IDF (cardinality preserved)."""
    if _strict_mode():
        if isinstance(exc, EmbeddingCredentialError):
            raise RuntimeError(
                f"MEMORA_EMBEDDING_STRICT=1 and {backend} credentials invalid: {exc}"
            ) from exc
        _strict_raise(backend, exc)
    _warn_once(backend, f"{type(exc).__name__}: {exc}")
    if texts is not None:
        return [_compute_embedding_tfidf(t) for t in texts]
    return None


def _compute_embedding_openai(text: str) -> Dict[str, float]:
    """Use OpenAI embeddings API (single input)."""
    try:
        import openai

        try:
            api_key, base_url = _embedding_credentials()
        except EmbeddingCredentialError as exc:
            if _strict_mode():
                raise EmbeddingStrictError(
                    f"MEMORA_EMBEDDING_STRICT=1 hard-stop: incomplete embedding credentials "
                    f"({exc}). Not a memora bug — fix MEMORA_EMBEDDING_* / OPENAI_* config."
                ) from exc
            _warn_once("openai:credentials", str(exc))
            return _compute_embedding_tfidf(text)

        # Construct client only after credentials resolved (N1: fail before construct).
        client = _embedding_client(openai)
        # Default is OpenAI's small model; Cloudflare / other hosts need OPENAI_EMBEDDING_MODEL
        # set explicitly (e.g. @cf/baai/bge-m3). A wrong default 404s on non-OpenAI endpoints.
        model_name = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")

        response = client.embeddings.create(
            input=text,
            model=model_name,
        )
        vectors = parse_openai_embeddings_response(response, expected_n=1)
        return vectors[0]

    except ImportError as exc:
        if _strict_mode():
            _strict_raise("openai", exc)
        _warn_once(
            "openai:ImportError",
            "package not installed (pip install openai)",
        )
        return _compute_embedding_tfidf(text)
    except EmbeddingStrictError:
        raise
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

    if embedding_model not in _KNOWN_BACKENDS:
        # N2: reject unknown backend under strict (typo must not silently TF-IDF).
        if _strict_mode():
            raise EmbeddingStrictError(
                f"MEMORA_EMBEDDING_STRICT=1 hard-stop: unknown embedding backend "
                f"{embedding_model!r}; known: {sorted(_KNOWN_BACKENDS)}. "
                f"Not a memora bug — fix MEMORA_EMBEDDING_MODEL."
            )
        _warn_once(
            f"unknown-backend:{embedding_model}",
            f"unknown embedding_model {embedding_model!r}, using TF-IDF",
        )
        return _compute_embedding_tfidf(text)

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

    Non-strict failure policy (N2, explicit): ALL-OR-NOTHING per call. A failed
    chunk or invalid response falls back to TF-IDF for the entire batch so the
    store never mixes dense and keyword vectors from one call. Result length
    always equals input length.
    """
    if not entries:
        return []

    if embedding_model not in _KNOWN_BACKENDS:
        if _strict_mode():
            raise EmbeddingStrictError(
                f"MEMORA_EMBEDDING_STRICT=1 hard-stop: unknown embedding backend "
                f"{embedding_model!r}; known: {sorted(_KNOWN_BACKENDS)}. "
                f"Not a memora bug — fix MEMORA_EMBEDDING_MODEL."
            )
        _warn_once(
            f"unknown-backend:{embedding_model}",
            f"unknown embedding_model {embedding_model!r}, using TF-IDF",
        )
        texts = [
            _get_embedding_text(e["content"], e.get("metadata"), e.get("tags", []))
            for e in entries
        ]
        return [_compute_embedding_tfidf(t) for t in texts]

    # Assemble texts using the same path as compute_embedding()
    texts = [
        _get_embedding_text(e["content"], e.get("metadata"), e.get("tags", []))
        for e in entries
    ]

    if embedding_model == "openai":
        return _compute_embeddings_openai_batch(texts)
    else:
        # For non-OpenAI backends, fall back to sequential
        return [
            compute_embedding(
                e["content"], e.get("metadata"), e.get("tags", []), embedding_model
            )
            for e in entries
        ]


def _compute_embeddings_openai_batch(texts: List[str]) -> List[Dict[str, float]]:
    """Batch OpenAI embedding computation with chunking and validated responses.

    Non-strict: all-or-nothing TF-IDF for the whole call on any failure (N2).
    Strict: raise on credential, API, or validation errors.
    """
    try:
        import openai

        try:
            _embedding_credentials()  # validate before construct
        except EmbeddingCredentialError as exc:
            if _strict_mode():
                raise EmbeddingStrictError(
                    f"MEMORA_EMBEDDING_STRICT=1 hard-stop: incomplete embedding credentials "
                    f"({exc}). Not a memora bug — fix MEMORA_EMBEDDING_* / OPENAI_* config."
                ) from exc
            _warn_once("openai-batch:credentials", str(exc))
            return [_compute_embedding_tfidf(t) for t in texts]

        client = _embedding_client(openai)
        # See single-path note: override OPENAI_EMBEDDING_MODEL for non-OpenAI hosts.
        model_name = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")

        max_chunk = 2048  # OpenAI batch limit
        all_results: List[Dict[str, float]] = []

        for i in range(0, len(texts), max_chunk):
            chunk = texts[i : i + max_chunk]
            try:
                response = client.embeddings.create(input=chunk, model=model_name)
                # N2: validate cardinality, indices, vectors — reconstruct by index.
                chunk_vecs = parse_openai_embeddings_response(response, expected_n=len(chunk))
                all_results.extend(chunk_vecs)
            except Exception as chunk_exc:
                if _strict_mode():
                    _strict_raise("openai-batch:chunk", chunk_exc)
                # All-or-nothing: discard any partial dense results from earlier
                # chunks and return TF-IDF for the entire call (no mixed store).
                _warn_once(
                    "openai-batch:chunk",
                    f"{type(chunk_exc).__name__}: {chunk_exc}",
                )
                return [_compute_embedding_tfidf(t) for t in texts]

        assert len(all_results) == len(texts), (
            f"internal: result cardinality {len(all_results)} != {len(texts)}"
        )
        return all_results

    except ImportError as exc:
        if _strict_mode():
            _strict_raise("openai-batch", exc)
        _warn_once(
            "openai-batch:ImportError",
            "package not installed (pip install openai)",
        )
        return [_compute_embedding_tfidf(t) for t in texts]
    except EmbeddingStrictError:
        raise
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


# --- Model management (N5: fingerprint = backend + model name + dims/kind) ---

def _vector_representation(vector: Dict[str, float]) -> str:
    """Classify a stored vector: dense:N (numeric keys) or sparse (word bags)."""
    if not vector:
        return "empty"
    keys = list(vector.keys())
    sample = keys[: min(8, len(keys))]
    if sample and all(k.isdigit() for k in sample):
        return f"dense:{len(vector)}"
    return "sparse"


def embedding_fingerprint(
    embedding_model: str,
    *,
    observed_vector: Optional[Dict[str, float]] = None,
    openai_model_name: Optional[str] = None,
    st_model_name: Optional[str] = None,
) -> str:
    """Stable fingerprint for rebuild decisions (N5).

    Format: ``backend|model_name|repr`` where repr is dense:N, sparse, or empty.
    Tracks more than MEMORA_EMBEDDING_MODEL alone so word-key TF-IDF bags
    labelled \"openai\" force a rebuild when real dense embeddings are configured.
    """
    backend = embedding_model if embedding_model in _KNOWN_BACKENDS else f"unknown:{embedding_model}"
    if embedding_model == "openai":
        model_name = openai_model_name or os.getenv(
            "OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"
        )
    elif embedding_model == "sentence-transformers":
        model_name = st_model_name or os.getenv(
            "SENTENCE_TRANSFORMERS_MODEL", "all-MiniLM-L6-v2"
        )
    else:
        model_name = "tfidf"
    repr_ = _vector_representation(observed_vector) if observed_vector is not None else "unset"
    return f"{backend}|{model_name}|{repr_}"


def sample_embedding_vector(conn: sqlite3.Connection) -> Optional[Dict[str, float]]:
    """Return one stored embedding vector for fingerprinting, or None."""
    row = conn.execute(
        "SELECT embedding FROM memories_embeddings WHERE embedding IS NOT NULL LIMIT 1"
    ).fetchone()
    if not row:
        return None
    return json_to_embedding(row["embedding"])


def sample_embedding_kinds(
    conn: sqlite3.Connection,
    *,
    limit: int = 500,
) -> Set[str]:
    """Scan stored vectors and return kind tags: {'dense'}, {'sparse'}, or both (N7).

    Dense = numeric keys (openai / sentence-transformers). Sparse = word bags (TF-IDF).
    A store containing BOTH is corrupt for cosine search (shared-key similarity → 0.0)
    and must force rebuild regardless of memories_meta.
    """
    kinds: Set[str] = set()
    rows = conn.execute(
        "SELECT embedding FROM memories_embeddings WHERE embedding IS NOT NULL LIMIT ?",
        (limit,),
    ).fetchall()
    for row in rows:
        vec = json_to_embedding(row["embedding"])
        rep = _vector_representation(vec)
        if rep == "empty":
            continue
        if rep.startswith("dense"):
            kinds.add("dense")
            # also track dims for mixed-dim dense
            kinds.add(rep)  # dense:N
        else:
            kinds.add("sparse")
    return kinds


def store_has_mixed_embeddings(conn: sqlite3.Connection) -> bool:
    """True when dense and sparse vectors coexist, or dense dims disagree (N7)."""
    kinds = sample_embedding_kinds(conn)
    if "sparse" in kinds and any(k.startswith("dense") for k in kinds):
        return True
    dense_dims = {k for k in kinds if k.startswith("dense:")}
    return len(dense_dims) > 1


def get_stored_embedding_model(conn: sqlite3.Connection) -> Optional[str]:
    """Get the embedding model fingerprint (or legacy name) stored in the database."""
    row = conn.execute(
        "SELECT value FROM memories_meta WHERE key = 'embedding_model'"
    ).fetchone()
    return row["value"] if row else None


def set_stored_embedding_model(conn: sqlite3.Connection, model: str) -> None:
    """Store the embedding model fingerprint in the database."""
    conn.execute(
        """
        INSERT INTO memories_meta (key, value) VALUES ('embedding_model', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (model,),
    )
    conn.commit()


def current_embedding_fingerprint(current_model: str) -> str:
    """Fingerprint for the configured embedding endpoint (backend|model|target-kind).

    Includes the actual model name (OPENAI_EMBEDDING_MODEL / ST model), not just
    MEMORA_EMBEDDING_MODEL=\"openai\", so endpoint/model changes force rebuild (N5/N7).
    """
    if current_model == "openai":
        model_name = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
        return f"openai|{model_name}|dense"
    if current_model == "sentence-transformers":
        model_name = os.getenv("SENTENCE_TRANSFORMERS_MODEL", "all-MiniLM-L6-v2")
        return f"sentence-transformers|{model_name}|dense"
    if current_model == "tfidf":
        return "tfidf|tfidf|sparse"
    return embedding_fingerprint(current_model)


def check_embedding_model_mismatch(conn: sqlite3.Connection, current_model: str) -> bool:
    """True when stored embeddings are incompatible with the configured backend (N5/N7).

    Rebuild is required when any of:
    - memories_meta fingerprint ≠ configured backend|model|kind
    - legacy bare name in meta (e.g. \"openai\") — too coarse to trust
    - MIXED store: sparse word-keys coexist with dense vectors (cosine → 0.0)
    - mixed dense dimensions (e.g. 384 and 1024)
    - sample kind is sparse while current backend is dense (or vice versa)
    """
    count = conn.execute("SELECT COUNT(*) FROM memories_embeddings").fetchone()[0]
    if count == 0:
        return False

    # N7: mixed representation is always corrupt for cosine search.
    if store_has_mixed_embeddings(conn):
        return True

    stored = get_stored_embedding_model(conn)
    sample = sample_embedding_vector(conn)
    current_fp = current_embedding_fingerprint(current_model)

    if stored is None:
        return True

    # Legacy bare backend name never equals rich fingerprint.
    if "|" not in stored:
        return True

    # Data kind vs configured target
    if sample is not None:
        sample_kind = _vector_representation(sample)
        if current_model in ("openai", "sentence-transformers"):
            if sample_kind == "sparse":
                return True
            if sample_kind.startswith("dense:"):
                # Refine current with observed dims; mismatch if dims or model differ
                model_name = (
                    os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
                    if current_model == "openai"
                    else os.getenv("SENTENCE_TRANSFORMERS_MODEL", "all-MiniLM-L6-v2")
                )
                refined = f"{current_model}|{model_name}|{sample_kind}"
                # If stored claims different dims or model, rebuild
                if stored != refined and stored != current_fp:
                    # stored may be openai|model|dense without dims — still check prefix
                    stored_prefix = "|".join(stored.split("|")[:2])
                    current_prefix = f"{current_model}|{model_name}"
                    if stored_prefix != current_prefix:
                        return True
                    stored_kind = stored.rsplit("|", 1)[-1]
                    if stored_kind.startswith("dense:") and stored_kind != sample_kind:
                        return True
                    if stored_kind == "dense" and sample_kind.startswith("dense:"):
                        # stored without dims, data has dims — ok until model changes
                        return False
                    return stored != refined
                return False
        if current_model == "tfidf" and sample_kind.startswith("dense"):
            return True

    return stored != current_fp


def rebuild_all_embeddings(conn: sqlite3.Connection, embedding_model: str) -> int:
    """Rebuild all embeddings using given embedding model."""
    rows = conn.execute(
        "SELECT id, content, metadata, tags FROM memories"
    ).fetchall()
    updated = 0
    last_vector: Optional[Dict[str, float]] = None
    for row in rows:
        memory_id = row["id"]
        metadata = json.loads(row["metadata"]) if row["metadata"] else None
        tags = json.loads(row["tags"]) if row["tags"] else []
        vector = compute_embedding(row["content"], metadata, tags, embedding_model)
        upsert_embedding(conn, memory_id, vector)
        last_vector = vector
        updated += 1
    # Store rich fingerprint (N5)
    if embedding_model == "openai":
        model_name = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
        kind = _vector_representation(last_vector) if last_vector else "empty"
        fp = f"openai|{model_name}|{kind}"
    elif embedding_model == "sentence-transformers":
        model_name = os.getenv("SENTENCE_TRANSFORMERS_MODEL", "all-MiniLM-L6-v2")
        kind = _vector_representation(last_vector) if last_vector else "empty"
        fp = f"sentence-transformers|{model_name}|{kind}"
    else:
        fp = embedding_fingerprint(embedding_model, observed_vector=last_vector)
    set_stored_embedding_model(conn, fp)
    conn.commit()
    return updated
