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
        raise EmbeddingProviderError(
            f"sentence-transformers unavailable; refusing TF-IDF substitute: {exc}"
        ) from exc
    except EmbeddingStrictError:
        raise
    except EmbeddingProviderError:
        raise
    except Exception as exc:
        if _strict_mode():
            _strict_raise("sentence-transformers", exc)
        raise EmbeddingProviderError(
            f"sentence-transformers failed; refusing TF-IDF substitute: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


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


class EmbeddingProviderError(RuntimeError):
    """Dense embedding backend failed; refuse to persist a TF-IDF substitute.

    Used when MEMORA_EMBEDDING_MODEL is openai / sentence-transformers and the
    provider call fails. A wrong embedding is worse than a missing one.
    """



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


def parse_openai_embeddings_response(
    response: Any,
    expected_n: int,
    *,
    expected_dim: Optional[int] = None,
) -> Tuple[List[Dict[str, float]], int]:
    """Validate and reconstruct OpenAI embeddings.create response by index (N2).

    Requires:
    - len(response.data) == expected_n
    - indices exactly cover 0..n-1 with no gaps, dupes, or out-of-range
    - every vector non-empty, finite, and dimensionally uniform
    - if expected_dim is set (call-wide), every vector must match it (P1 cross-chunk)

    Returns (vectors_in_input_order, dimension).
    """
    if expected_n < 0:
        raise ValueError(f"expected_n must be >= 0, got {expected_n}")
    if expected_n == 0:
        return [], expected_dim if expected_dim is not None else 0
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
    dims: Optional[int] = expected_dim
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
    if dims is None:
        raise ValueError("embeddings response has no dimensions")

    return [by_index[i] for i in range(expected_n)], dims



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
            raise EmbeddingProviderError(
                f"incomplete embedding credentials ({exc}); "
                f"refusing TF-IDF substitute for dense backend"
            ) from exc

        # Construct client only after credentials resolved (N1: fail before construct).
        client = _embedding_client(openai)
        # Default is OpenAI's small model; Cloudflare / other hosts need OPENAI_EMBEDDING_MODEL
        # set explicitly (e.g. @cf/baai/bge-m3). A wrong default 404s on non-OpenAI endpoints.
        model_name = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")

        response = client.embeddings.create(
            input=text,
            model=model_name,
        )
        vectors, _dim = parse_openai_embeddings_response(response, expected_n=1)
        return vectors[0]

    except ImportError as exc:
        # Dense backend: never substitute TF-IDF (would corrupt a dense store).
        if _strict_mode():
            _strict_raise("openai", exc)
        raise EmbeddingProviderError(
            f"openai embeddings unavailable (package not installed); "
            f"refusing TF-IDF substitute for dense backend: {exc}"
        ) from exc
    except EmbeddingStrictError:
        raise
    except EmbeddingProviderError:
        raise
    except Exception as exc:
        if _strict_mode():
            _strict_raise("openai", exc)
        # Policy (round 132 #3): configured dense backend must FAIL THE WRITE,
        # not persist TF-IDF keyword bags into an otherwise dense store.
        raise EmbeddingProviderError(
            f"openai embeddings failed; refusing TF-IDF substitute for dense backend: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


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

    Dense backend policy: on any failure, FAIL THE CALL (never TF-IDF substitute).
    Call-wide dimension is enforced across 2048-item chunks (P1).
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
            raise EmbeddingProviderError(
                f"incomplete embedding credentials ({exc}); "
                f"refusing TF-IDF substitute for dense backend"
            ) from exc

        client = _embedding_client(openai)
        # See single-path note: override OPENAI_EMBEDDING_MODEL for non-OpenAI hosts.
        model_name = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")

        max_chunk = 2048  # OpenAI batch limit
        all_results: List[Dict[str, float]] = []
        call_dim: Optional[int] = None  # P1: one expected dim for the whole call

        for i in range(0, len(texts), max_chunk):
            chunk = texts[i : i + max_chunk]
            try:
                response = client.embeddings.create(input=chunk, model=model_name)
                chunk_vecs, chunk_dim = parse_openai_embeddings_response(
                    response,
                    expected_n=len(chunk),
                    expected_dim=call_dim,
                )
                if call_dim is None:
                    call_dim = chunk_dim
                elif chunk_dim != call_dim:
                    raise ValueError(
                        f"embeddings dimension inconsistency across batch chunks: "
                        f"chunk starting at {i} has d={chunk_dim}, call expected d={call_dim}"
                    )
                all_results.extend(chunk_vecs)
            except Exception as chunk_exc:
                if _strict_mode():
                    _strict_raise("openai-batch:chunk", chunk_exc)
                raise EmbeddingProviderError(
                    f"openai-batch chunk failed; refusing TF-IDF substitute for dense backend: "
                    f"{type(chunk_exc).__name__}: {chunk_exc}"
                ) from chunk_exc

        assert len(all_results) == len(texts), (
            f"internal: result cardinality {len(all_results)} != {len(texts)}"
        )
        return all_results

    except ImportError as exc:
        if _strict_mode():
            _strict_raise("openai-batch", exc)
        raise EmbeddingProviderError(
            f"openai-batch unavailable (package not installed); "
            f"refusing TF-IDF substitute: {exc}"
        ) from exc
    except EmbeddingStrictError:
        raise
    except EmbeddingProviderError:
        raise
    except Exception as exc:
        if _strict_mode():
            _strict_raise("openai-batch", exc)
        raise EmbeddingProviderError(
            f"openai-batch failed; refusing TF-IDF substitute for dense backend: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


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

_INTEGRITY_KEY = "embedding_integrity"


def _meta_get(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute(
        "SELECT value FROM memories_meta WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    return row["value"] if isinstance(row, sqlite3.Row) else row[0]


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO memories_meta (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def get_embedding_integrity(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Authoritative integrity snapshot (O(1) meta read)."""
    raw = _meta_get(conn, _INTEGRITY_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _write_embedding_integrity(conn: sqlite3.Connection, data: Dict[str, Any]) -> None:
    _meta_set(conn, _INTEGRITY_KEY, json.dumps(data, ensure_ascii=False, sort_keys=True))


def _empty_integrity() -> Dict[str, Any]:
    return {
        "reps": {},          # rep -> count
        "embedding_count": 0,  # non-null embedding rows
        "memory_count": 0,
        "missing_count": 0,
        "mixed": False,
        "fingerprint": None,
    }


def _reps_are_mixed(reps: Dict[str, int]) -> bool:
    kinds = {k for k, n in reps.items() if n > 0}
    if "sparse" in kinds and any(k.startswith("dense") for k in kinds):
        return True
    dense = {k for k in kinds if k.startswith("dense:")}
    return len(dense) > 1


def _refresh_coverage_counts(conn: sqlite3.Connection, integ: Dict[str, Any]) -> None:
    """Fast SQL counts (no embedding JSON parse) for coverage integrity."""
    mem_n = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    emb_n = conn.execute(
        "SELECT COUNT(*) FROM memories_embeddings "
        "WHERE embedding IS NOT NULL AND embedding != '' AND embedding != 'null'"
    ).fetchone()[0]
    integ["memory_count"] = int(mem_n)
    integ["embedding_count"] = int(emb_n)
    integ["missing_count"] = max(0, int(mem_n) - int(emb_n))


def note_embedding_write(
    conn: sqlite3.Connection,
    memory_id: int,
    vector: Dict[str, float],
    *,
    previous_rep: Optional[str] = None,
) -> None:
    """Update authoritative integrity meta after an embedding write (write-time)."""
    integ = get_embedding_integrity(conn) or _empty_integrity()
    reps: Dict[str, int] = dict(integ.get("reps") or {})
    new_rep = _vector_representation(vector)
    if previous_rep:
        reps[previous_rep] = int(reps.get(previous_rep, 1)) - 1
        if reps[previous_rep] <= 0:
            del reps[previous_rep]
    reps[new_rep] = int(reps.get(new_rep, 0)) + 1
    # drop empty/zero
    reps = {k: v for k, v in reps.items() if v > 0 and k != "empty"}
    integ["reps"] = reps
    integ["mixed"] = _reps_are_mixed(reps)
    _refresh_coverage_counts(conn, integ)
    # Stamp observed kind into fingerprint when uniform dense
    dense = [k for k in reps if k.startswith("dense:")]
    observed_dim = int(dense[0].split(":")[1]) if len(dense) == 1 and "sparse" not in reps else None
    # fingerprint is maintained by set_stored_embedding_model / rebuild; keep current if set
    if integ.get("fingerprint") is None:
        # leave for set_stored; coverage alone is enough for missing detection
        pass
    _write_embedding_integrity(conn, integ)


def note_embedding_delete(conn: sqlite3.Connection, memory_id: int, previous_rep: Optional[str]) -> None:
    """Update integrity meta after embedding row removal."""
    integ = get_embedding_integrity(conn) or _empty_integrity()
    reps: Dict[str, int] = dict(integ.get("reps") or {})
    if previous_rep and previous_rep in reps:
        reps[previous_rep] = int(reps[previous_rep]) - 1
        if reps[previous_rep] <= 0:
            del reps[previous_rep]
    integ["reps"] = reps
    integ["mixed"] = _reps_are_mixed(reps)
    _refresh_coverage_counts(conn, integ)
    _write_embedding_integrity(conn, integ)


def _previous_embedding_rep(conn: sqlite3.Connection, memory_id: int) -> Optional[str]:
    row = conn.execute(
        "SELECT embedding FROM memories_embeddings WHERE memory_id = ?",
        (memory_id,),
    ).fetchone()
    if not row:
        return None
    raw = row["embedding"] if isinstance(row, sqlite3.Row) else row[0]
    if not raw:
        return None
    return _vector_representation(json_to_embedding(raw))


def upsert_embedding(
    conn: sqlite3.Connection,
    memory_id: int,
    vector: Dict[str, float],
) -> None:
    prev = _previous_embedding_rep(conn, memory_id)
    emb_json = embedding_to_json(vector)
    conn.execute(
        """
        INSERT INTO memories_embeddings(memory_id, embedding)
        VALUES(?, ?)
        ON CONFLICT(memory_id) DO UPDATE SET embedding=excluded.embedding
        """,
        (memory_id, emb_json),
    )
    note_embedding_write(conn, memory_id, vector, previous_rep=prev)


def delete_embedding(conn: sqlite3.Connection, memory_id: int) -> None:
    prev = _previous_embedding_rep(conn, memory_id)
    conn.execute("DELETE FROM memories_embeddings WHERE memory_id = ?", (memory_id,))
    note_embedding_delete(conn, memory_id, prev)


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
    """Classify a stored vector: dense:N or sparse (word bags).

    Dense requires the EXACT key set {\"0\",\"1\",...,\"N-1\"} — not an 8-key
    numeric prefix (a sparse bag with keys 0..7 plus a word key must be sparse).
    """
    if not vector:
        return "empty"
    n = len(vector)
    expected = {str(i) for i in range(n)}
    if set(vector.keys()) == expected:
        return f"dense:{n}"
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


def audit_scan_embedding_kinds(conn: sqlite3.Connection) -> Set[str]:
    """ADMIN/MIGRATION ONLY — full parse of every embedding (never on search path).

    Prefer get_embedding_integrity() / check_embedding_model_mismatch() which are O(1).
    """
    kinds: Set[str] = set()
    rows = conn.execute(
        "SELECT embedding FROM memories_embeddings WHERE embedding IS NOT NULL"
    ).fetchall()
    for row in rows:
        raw = row["embedding"] if isinstance(row, sqlite3.Row) else row[0]
        vec = json_to_embedding(raw)
        rep = _vector_representation(vec)
        if rep == "empty":
            continue
        if rep.startswith("dense"):
            kinds.add("dense")
            kinds.add(rep)
        else:
            kinds.add("sparse")
    return kinds


# Deprecated aliases — do not use on hot path.
def scan_embedding_kinds(conn: sqlite3.Connection) -> Set[str]:
    return audit_scan_embedding_kinds(conn)


def sample_embedding_kinds(
    conn: sqlite3.Connection,
    *,
    limit: int = 0,
) -> Set[str]:
    return audit_scan_embedding_kinds(conn)


def store_has_mixed_embeddings(conn: sqlite3.Connection) -> bool:
    """True when integrity meta reports mixed reps (O(1)). Falls back to audit if meta empty."""
    integ = get_embedding_integrity(conn)
    if integ and integ.get("reps") is not None:
        if integ.get("mixed"):
            return True
        return _reps_are_mixed(dict(integ.get("reps") or {}))
    # Legacy store without integrity meta: force mismatch via caller, do not scan hot path
    return False


def get_stored_embedding_model(conn: sqlite3.Connection) -> Optional[str]:
    """Get the embedding model fingerprint (or legacy name) stored in the database."""
    return _meta_get(conn, "embedding_model")


def set_stored_embedding_model(conn: sqlite3.Connection, model: str) -> None:
    """Store the embedding model fingerprint and sync integrity fingerprint field."""
    _meta_set(conn, "embedding_model", model)
    integ = get_embedding_integrity(conn) or _empty_integrity()
    integ["fingerprint"] = model
    _refresh_coverage_counts(conn, integ)
    integ["mixed"] = _reps_are_mixed(dict(integ.get("reps") or {}))
    _write_embedding_integrity(conn, integ)
    conn.commit()


def _embedding_endpoint_host() -> str:
    """Host-only identity of the embedding endpoint (NEVER the API key)."""
    try:
        _key, base = resolve_embedding_credentials()
    except EmbeddingCredentialError:
        base = os.getenv("MEMORA_EMBEDDING_BASE_URL") or os.getenv("OPENAI_BASE_URL") or ""
    base = (base or "").strip()
    if not base:
        return "default-host"
    # strip scheme and path
    host = base
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0]
    return host or "default-host"


def current_embedding_fingerprint(
    current_model: str,
    *,
    observed_dim: Optional[int] = None,
) -> str:
    """Fingerprint: backend|model|host|repr (N5/N7 + endpoint identity).

    Includes host (not key) so switching providers with the same model string
    still forces rebuild. observed_dim fills dense:N when known.
    """
    host = _embedding_endpoint_host()
    if current_model == "openai":
        model_name = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
        kind = f"dense:{observed_dim}" if observed_dim else "dense"
        return f"openai|{model_name}|{host}|{kind}"
    if current_model == "sentence-transformers":
        model_name = os.getenv("SENTENCE_TRANSFORMERS_MODEL", "all-MiniLM-L6-v2")
        kind = f"dense:{observed_dim}" if observed_dim else "dense"
        return f"sentence-transformers|{model_name}|{host}|{kind}"
    if current_model == "tfidf":
        return f"tfidf|tfidf|{host}|sparse"
    return f"{current_model}|unknown|{host}|unset"


def check_embedding_model_mismatch(conn: sqlite3.Connection, current_model: str) -> bool:
    """O(1) integrity check from authoritative meta — NEVER scans embedding payloads.

    Mismatch when any of:
    - no memories (false) / no integrity meta while embeddings exist
    - missing_count > 0 (memory without non-null embedding)
    - mixed reps in integrity meta
    - legacy bare fingerprint (e.g. \"openai\")
    - stored fingerprint incompatible with configured backend|model|host|kind
    """
    mem_n = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    if mem_n == 0:
        return False

    integ = get_embedding_integrity(conn)
    stored = get_stored_embedding_model(conn)

    # No integrity meta yet (legacy / pre-meta store) → must rebuild to stamp meta.
    if not integ or not integ.get("reps"):
        emb_n = conn.execute("SELECT COUNT(*) FROM memories_embeddings").fetchone()[0]
        return emb_n > 0 or stored is not None

    # Coverage: missing embeddings are corrupt and invisible to search (P1-3).
    # Refresh counts with fast SQL (no payload parse) so deletes of memories are seen.
    _refresh_coverage_counts(conn, integ)
    if int(integ.get("missing_count") or 0) > 0:
        _write_embedding_integrity(conn, integ)
        return True

    if integ.get("mixed") or _reps_are_mixed(dict(integ.get("reps") or {})):
        return True

    if stored is None:
        return True
    if "|" not in stored:
        return True

    reps = {k: v for k, v in (integ.get("reps") or {}).items() if v > 0}
    if current_model in ("openai", "sentence-transformers"):
        if "sparse" in reps:
            return True
        dense_dims = sorted(k for k in reps if k.startswith("dense:"))
        observed_dim = int(dense_dims[0].split(":")[1]) if dense_dims else None
        current_fp = current_embedding_fingerprint(current_model, observed_dim=observed_dim)
        stored_parts = stored.split("|")
        current_parts = current_fp.split("|")
        if len(stored_parts) < 3 or len(current_parts) < 3:
            return stored != current_fp
        if stored_parts[:3] != current_parts[:3]:
            return True
        stored_kind = stored_parts[-1]
        current_kind = current_parts[-1]
        if stored_kind.startswith("dense:") and current_kind.startswith("dense:"):
            return stored_kind != current_kind
        if stored_kind.startswith("dense:") and current_kind == "dense":
            return False
        if stored_kind == "dense" and current_kind.startswith("dense:"):
            return False
        return stored_kind != current_kind

    if current_model == "tfidf" and any(k.startswith("dense") for k in reps):
        return True

    return stored != current_embedding_fingerprint(current_model)


def rebuild_all_embeddings(conn: sqlite3.Connection, embedding_model: str) -> int:
    """Rebuild all embeddings; stamp uniform fingerprint + integrity meta.

    Admin path may parse vectors; search path uses O(1) integrity meta only.
    """
    # Reset integrity before rebuild so counts rebuild cleanly via note_embedding_write
    _write_embedding_integrity(conn, _empty_integrity())

    rows = conn.execute(
        "SELECT id, content, metadata, tags FROM memories"
    ).fetchall()
    updated = 0
    seen_reps: Set[str] = set()
    for row in rows:
        memory_id = row["id"]
        metadata = json.loads(row["metadata"]) if row["metadata"] else None
        tags = json.loads(row["tags"]) if row["tags"] else []
        vector = compute_embedding(row["content"], metadata, tags, embedding_model)
        rep = _vector_representation(vector)
        seen_reps.add(rep)
        upsert_embedding(conn, memory_id, vector)
        updated += 1

    if updated == 0:
        set_stored_embedding_model(conn, current_embedding_fingerprint(embedding_model))
        conn.commit()
        return 0

    non_empty = {r for r in seen_reps if r != "empty"}
    if len(non_empty) != 1:
        raise EmbeddingProviderError(
            f"rebuild produced non-uniform representations {sorted(seen_reps)}; "
            f"refusing to stamp fingerprint"
        )
    rep = next(iter(non_empty))
    observed_dim = int(rep.split(":", 1)[1]) if rep.startswith("dense:") else None
    fp = current_embedding_fingerprint(embedding_model, observed_dim=observed_dim)
    if rep.startswith("dense:"):
        fp = fp.rsplit("|", 1)[0] + "|" + rep
    set_stored_embedding_model(conn, fp)
    conn.commit()
    return updated


def verify_embedding_integrity(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Admin doctor: full audit scan + compare to meta. Never call from search."""
    kinds = audit_scan_embedding_kinds(conn)
    integ = get_embedding_integrity(conn)
    _refresh_coverage_counts(conn, integ if integ else _empty_integrity())
    return {
        "audit_kinds": sorted(kinds),
        "meta": get_embedding_integrity(conn),
        "mixed_audit": (
            ("sparse" in kinds and any(k.startswith("dense") for k in kinds))
            or len({k for k in kinds if k.startswith("dense:")}) > 1
        ),
    }
