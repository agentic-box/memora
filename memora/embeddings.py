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


# ---------------------------------------------------------------------------
# N1: atomic credential pairs (never cross-pair MEMORA_* with OPENAI_*)
# ---------------------------------------------------------------------------

class EmbeddingCredentialError(ValueError):
    """Raised when embedding credentials are incomplete or cross-paired."""


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
                raise RuntimeError(
                    f"MEMORA_EMBEDDING_STRICT=1 and openai credentials invalid: {exc}"
                ) from exc
            _warn_once("openai:credentials", str(exc))
            return _compute_embedding_tfidf(text)

        # Construct client only after credentials resolved (N1: fail before construct).
        client = _embedding_client(openai)
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
    except Exception as exc:
        # Re-raise our own strict credential errors unchanged.
        if isinstance(exc, RuntimeError) and str(exc).startswith("MEMORA_EMBEDDING_STRICT"):
            raise
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
            raise RuntimeError(
                f"MEMORA_EMBEDDING_STRICT=1 and unknown embedding backend {embedding_model!r}; "
                f"known: {sorted(_KNOWN_BACKENDS)}"
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
            raise RuntimeError(
                f"MEMORA_EMBEDDING_STRICT=1 and unknown embedding backend {embedding_model!r}; "
                f"known: {sorted(_KNOWN_BACKENDS)}"
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
                raise RuntimeError(
                    f"MEMORA_EMBEDDING_STRICT=1 and openai-batch credentials invalid: {exc}"
                ) from exc
            _warn_once("openai-batch:credentials", str(exc))
            return [_compute_embedding_tfidf(t) for t in texts]

        client = _embedding_client(openai)
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
    except Exception as exc:
        if isinstance(exc, RuntimeError) and str(exc).startswith("MEMORA_EMBEDDING_STRICT"):
            raise
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


def check_embedding_model_mismatch(conn: sqlite3.Connection, current_model: str) -> bool:
    """Check if current embedding fingerprint differs from stored fingerprint.

    Uses backend + actual model name + observed vector representation so a
    store full of word-key bags labelled \"openai\" still triggers rebuild when
    dense openai embeddings are configured (N5).
    """
    stored = get_stored_embedding_model(conn)
    sample = sample_embedding_vector(conn)
    # Fingerprint of what we WOULD write now (target representation for backend).
    if current_model == "tfidf":
        target_repr_vector: Optional[Dict[str, float]] = {"_": 1.0}  # sparse marker
        # Use a synthetic sparse so fingerprint says sparse
        current_fp = embedding_fingerprint(current_model, observed_vector={"token": 1.0})
    elif current_model == "openai":
        # Target is dense; if we don't know dims yet, use dense:unknown so any
        # sparse store mismatches, and legacy stored "openai" also mismatches.
        current_fp = embedding_fingerprint(
            current_model,
            observed_vector={str(i): 0.0 for i in range(1)},  # dense:1 placeholder kind
        )
        # Normalize placeholder to dense:target so we compare kind not exact dim
        # until first real vector is observed — compare structural mismatch below.
        current_fp = embedding_fingerprint(current_model, observed_vector=None)
        # Prefer: backend|model|dense as intended target
        model_name = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
        current_fp = f"openai|{model_name}|dense"
    elif current_model == "sentence-transformers":
        model_name = os.getenv("SENTENCE_TRANSFORMERS_MODEL", "all-MiniLM-L6-v2")
        current_fp = f"sentence-transformers|{model_name}|dense"
    else:
        current_fp = embedding_fingerprint(current_model, observed_vector=sample)

    if stored is None:
        count = conn.execute("SELECT COUNT(*) FROM memories_embeddings").fetchone()[0]
        if count > 0:
            return True
        return False

    # Legacy: stored is bare backend name ("openai") — always mismatch against
    # the richer fingerprint so a post-outage TF-IDF store forces rebuild.
    if "|" not in stored:
        return True

    # If store has sample vectors, refine current with observed dims when kinds match.
    if sample is not None:
        stored_kind = stored.rsplit("|", 1)[-1]
        sample_kind = _vector_representation(sample)
        if stored_kind.startswith("dense") and sample_kind.startswith("dense"):
            # Same kind family — mismatch if model/backend part differs or dims differ
            stored_prefix = stored.rsplit("|", 1)[0]
            current_prefix = current_fp.rsplit("|", 1)[0]
            if stored_prefix != current_prefix:
                return True
            return stored_kind != sample_kind  # e.g. dense:384 vs dense:1024
        if stored_kind != sample_kind and sample_kind != "empty":
            # Stored fingerprint claims one kind but data is another → rebuild
            return True
        # Compare full fingerprint including sample kind
        refined = embedding_fingerprint(current_model, observed_vector=sample)
        # For openai/st targets we store as backend|model|dense:N after rebuild
        if current_model in ("openai", "sentence-transformers"):
            model_name = (
                os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
                if current_model == "openai"
                else os.getenv("SENTENCE_TRANSFORMERS_MODEL", "all-MiniLM-L6-v2")
            )
            refined = f"{current_model}|{model_name}|{sample_kind}"
            return stored != refined
        return stored != refined

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
