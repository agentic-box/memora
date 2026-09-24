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


class EmbeddingIntegrityFault(RuntimeError):
    """Integrity issue an automatic rebuild cannot safely satisfy."""

    def __init__(self, reason: str, memory_ids: List[int]):
        self.reason = reason
        self.memory_ids = memory_ids
        super().__init__(f"embedding integrity fault: {reason}; memory_ids={memory_ids}")


def embedding_integrity_fault_payload(exc: EmbeddingIntegrityFault) -> Dict[str, Any]:
    """Shared bounded, actionable presentation for every public surface."""
    return {
        "error": "embedding_integrity_fault",
        "reason": exc.reason,
        "memory_ids": exc.memory_ids[:100],
        "message": "Automatic rebuild skipped. Run memory_verify_integrity and repair the named writer/data.",
    }



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
    # At most two 90s attempts remain comfortably below the 300s rebuild
    # heartbeat lease. This avoids a provider call making its own owner stale.
    kwargs: Dict[str, Any] = {
        "api_key": api_key,
        "timeout": _EMBEDDING_REQUEST_TIMEOUT_SECONDS,
        "max_retries": _EMBEDDING_MAX_RETRIES,
    }
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
_INTEGRITY_SCHEMA_VERSION = 2
_REBUILD_LEASE_KEY = "embedding_rebuild_lease"
_REBUILD_LEASE_SECONDS = 300
# D1 executes a repair upsert as one HTTP statement. At its documented
# 4–8-second statement latency, 20 keeps a heartbeat well inside the 300s TTL.
_REBUILD_REPAIR_CHUNK_SIZE = 20
_EMBEDDING_REQUEST_TIMEOUT_SECONDS = 90.0
_EMBEDDING_MAX_RETRIES = 1
_DEFAULT_REBUILD_CHUNK_SIZE = 32


def _resolve_rebuild_chunk_size() -> int:
    """Row count per rebuild_all_embeddings() batch (embed call + write).

    Resolved at call time (not import time) so tests can monkeypatch the env
    var; an unset/invalid value falls back to the default rather than raising.
    """
    raw = os.getenv("MEMORA_REBUILD_CHUNK_SIZE")
    if raw is None:
        return _DEFAULT_REBUILD_CHUNK_SIZE
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_REBUILD_CHUNK_SIZE
    return value if value >= 1 else _DEFAULT_REBUILD_CHUNK_SIZE


# A semantic search opens a new connection for each MCP call, so the cache key
# must identify the underlying store rather than the Python connection object.
# Cache entries are deliberately process-local: another writer can only be
# detected by a first-use audit in a new process or an explicit verify call.
_integrity_check_cache: Dict[str, Dict[str, Any]] = {}


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


def _meta_set_for_rebuild_owner(
    conn: sqlite3.Connection,
    key: str,
    value: str,
    lease_owner: str,
) -> None:
    """Publish rebuild metadata only while the exact lease owner still holds it."""
    changed = conn.execute(
        """
        INSERT INTO memories_meta(key, value)
        SELECT ?, ?
         WHERE EXISTS (
             SELECT 1 FROM memories_meta
              WHERE key = ? AND value LIKE ?
         )
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value, _REBUILD_LEASE_KEY, f"{lease_owner}|%"),
    ).rowcount
    if not changed:
        raise EmbeddingIntegrityFault("integrity_rebuild_lease_lost", [])


def _release_rebuild_lease(conn: sqlite3.Connection, lease_owner: str) -> None:
    """Release only the current owner's lease; never report a stolen release as success."""
    changed = conn.execute(
        "DELETE FROM memories_meta WHERE key = ? AND value LIKE ?",
        (_REBUILD_LEASE_KEY, f"{lease_owner}|%"),
    ).rowcount
    if not changed:
        raise EmbeddingIntegrityFault("integrity_rebuild_lease_lost", [])


def _acquire_rebuild_lease(conn: sqlite3.Connection) -> str:
    """Atomically claim a rebuild or recover an expired owner heartbeat."""
    import time
    import uuid
    token = str(uuid.uuid4())
    now = int(time.time())
    candidate = f"{token}|{now}"
    conn.execute("INSERT OR IGNORE INTO memories_meta(key, value) VALUES (?, ?)", (_REBUILD_LEASE_KEY, candidate))
    current = _meta_get(conn, _REBUILD_LEASE_KEY)
    if current == candidate:
        conn.commit()
        return token
    try:
        _owner, started = (current or "|0").rsplit("|", 1)
        stale = now - int(started) > _REBUILD_LEASE_SECONDS
    except (ValueError, TypeError):
        stale = True
    if stale:
        changed = conn.execute(
            "UPDATE memories_meta SET value = ? WHERE key = ? AND value = ?",
            (candidate, _REBUILD_LEASE_KEY, current),
        ).rowcount
        if changed:
            conn.commit()
            return token
    raise EmbeddingIntegrityFault("integrity_building", [])


def _rebuild_lease_status(conn: sqlite3.Connection) -> Dict[str, int | bool]:
    """Return lease age from its last owner heartbeat, not rebuild start time."""
    import time
    current = _meta_get(conn, _REBUILD_LEASE_KEY)
    try:
        _owner, heartbeat = (current or "|0").rsplit("|", 1)
        age = max(0, int(time.time()) - int(heartbeat))
    except (ValueError, TypeError):
        age = _REBUILD_LEASE_SECONDS + 1
    return {
        "stale": age > _REBUILD_LEASE_SECONDS,
        "age_seconds": age,
        "retry_after_seconds": max(0, _REBUILD_LEASE_SECONDS - age),
    }


def _rebuild_lease_is_stale(conn: sqlite3.Connection) -> bool:
    return bool(_rebuild_lease_status(conn)["stale"])


def _heartbeat_rebuild_lease(conn: sqlite3.Connection, owner: str) -> bool:
    """CAS-refresh an owner's heartbeat; a stolen lease cannot be revived."""
    import time
    current = _meta_get(conn, _REBUILD_LEASE_KEY)
    if not current or not current.startswith(f"{owner}|"):
        return False
    changed = conn.execute(
        "UPDATE memories_meta SET value = ? WHERE key = ? AND value = ?",
        (f"{owner}|{int(time.time())}", _REBUILD_LEASE_KEY, current),
    ).rowcount
    if changed:
        conn.commit()
    return bool(changed)


def _assert_rebuild_lease_owner(conn: sqlite3.Connection, owner: str) -> None:
    """Fence final certification and row writes after another worker takes over."""
    if not _heartbeat_rebuild_lease(conn, owner):
        raise EmbeddingIntegrityFault("integrity_rebuild_lease_lost", [])


def _upsert_embedding_repair(
    conn: sqlite3.Connection,
    memory_id: int,
    generation: str,
    lease_owner: str,
) -> None:
    """Fence each repair statement; D1 executes bulk calls one statement at a time."""
    changed = conn.execute(
        """
        INSERT INTO memories_embedding_repairs(memory_id, repaired_generation)
        SELECT ?, ?
         WHERE EXISTS (
             SELECT 1 FROM memories_meta
              WHERE key = ? AND value LIKE ?
         )
        ON CONFLICT(memory_id) DO UPDATE SET
            repaired_generation = excluded.repaired_generation,
            repaired_at = datetime('now')
        """,
        (memory_id, generation, _REBUILD_LEASE_KEY, f"{lease_owner}|%"),
    ).rowcount
    if not changed:
        raise EmbeddingIntegrityFault("integrity_rebuild_lease_lost", [])


def get_embedding_integrity(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Return the last *audit stamp*, never an authoritative live state."""
    raw = _meta_get(conn, _INTEGRITY_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _write_embedding_integrity(
    conn: sqlite3.Connection,
    data: Dict[str, Any],
    *,
    lease_owner: Optional[str] = None,
) -> None:
    value = integrity_stamp_value(data)
    if lease_owner is None:
        _meta_set(conn, _INTEGRITY_KEY, value)
    else:
        _meta_set_for_rebuild_owner(conn, _INTEGRITY_KEY, value, lease_owner)


def _empty_integrity() -> Dict[str, Any]:
    return {
        "schema_version": _INTEGRITY_SCHEMA_VERSION,
        "generation": None,
        "state": "building",
        "reps": {},
        "embedding_count": 0,
        "memory_count": 0,
        "missing_count": 0,
        "orphan_embedding_count": 0,
        "mixed": True,
        "fingerprint": None,
    }


def _reps_are_mixed(reps: Dict[str, int]) -> bool:
    kinds = {k for k, n in reps.items() if n > 0}
    if "sparse" in kinds and any(k.startswith("dense") for k in kinds):
        return True
    dense = {k for k in kinds if k.startswith("dense:")}
    return len(dense) > 1


def _store_cache_key(conn: sqlite3.Connection) -> str:
    """Stable enough per-process identity for a local SQLite or D1 store."""
    database_id = getattr(conn, "database_id", None)
    account_id = getattr(conn, "account_id", None)
    if database_id:
        return f"d1:{account_id}:{database_id}"
    # Normal server connections have a configured backend, so this avoids even
    # a PRAGMA on steady-state semantic searches.  Keep the direct-connection
    # fallback below for unit/admin callers.
    try:
        from . import storage
        # current_backend(), NOT the module attribute: under a bound session
        # (memora #965) two databases would otherwise key from the SAME legacy
        # backend, so equal epochs could reuse another database's cached
        # integrity result. current_backend() still falls back to
        # STORAGE_BACKEND when nothing is bound, so monkeypatching it in tests
        # keeps working.
        backend = storage.current_backend()
        path = getattr(backend, "db_path", None) or getattr(backend, "cache_path", None)
        if path is not None:
            return f"backend:{type(backend).__name__}:{path}"
    except Exception:
        pass
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        if row:
            path = row[2] if not isinstance(row, sqlite3.Row) else row["file"]
            if path:
                return f"sqlite:{path}"
    except Exception:
        pass
    return f"connection:{id(conn)}"


def invalidate_embedding_integrity_cache(conn: Optional[sqlite3.Connection] = None) -> None:
    """Forget a first-use result after a known write or explicit admin action.
    Also forgets that the store's model was seen recorded (E1 review 7935
    P2): a later reset of the meta row is noticed by the next write."""
    if conn is None:
        _integrity_check_cache.clear()
        _MODEL_RECORDED.clear()
    else:
        _integrity_check_cache.pop(_store_cache_key(conn), None)
        _MODEL_RECORDED.discard(_store_cache_key(conn))


def _mark_integrity_dirty(conn: sqlite3.Connection) -> None:
    """Mark a prior audit stale without inventing counters from one writer.

    A missing/legacy stamp remains missing on purpose.  Otherwise one normal
    write could "bootstrap" a clean-looking snapshot over an old mixed store.
    """
    integ = get_embedding_integrity(conn)
    if integ and integ.get("schema_version") == _INTEGRITY_SCHEMA_VERSION:
        integ["state"] = "dirty"
        _write_embedding_integrity(conn, integ)
    invalidate_embedding_integrity_cache(conn)


def note_embedding_write(
    conn: sqlite3.Connection,
    memory_id: int,
    vector: Dict[str, float],
    *,
    previous_rep: Optional[str] = None,
) -> None:
    """Invalidate a prior audit after an embedding write.

    Do not maintain representations/counts here: SQL, direct imports, the
    worker and D1's independent statement commits can all bypass this helper.
    """
    _mark_integrity_dirty(conn)


def note_embedding_delete(conn: sqlite3.Connection, memory_id: int, previous_rep: Optional[str]) -> None:
    """Invalidate a prior audit after an embedding row is removed."""
    _mark_integrity_dirty(conn)


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


def _embedding_upsert_sql_and_params(
    memory_id: int,
    vector: Dict[str, float],
    lease_owner: Optional[str] = None,
) -> Tuple[str, tuple[Any, ...]]:
    """Build the (sql, params) pair for one embedding upsert.

    Shared by upsert_embedding() (single row) and upsert_embeddings_batch()
    (executemany) so the two paths cannot drift apart.
    """
    import uuid
    emb_json = embedding_to_json(vector)
    rep = _vector_representation(vector)
    dimension = int(rep.split(":", 1)[1]) if rep.startswith("dense:") else None
    stored_representation = "dense" if dimension is not None else rep
    sql = """
        INSERT INTO memories_embeddings(
            memory_id, embedding, representation, dimension, encoding_source, writer_token
        ) VALUES(?, ?, ?, ?, 'python', ?)
        ON CONFLICT(memory_id) DO UPDATE SET
            embedding=excluded.embedding,
            representation=excluded.representation,
            dimension=excluded.dimension,
            encoding_source=excluded.encoding_source,
            writer_token=excluded.writer_token
    """
    params: tuple[Any, ...] = (memory_id, emb_json, stored_representation, dimension, str(uuid.uuid4()))
    if lease_owner is not None:
        # The conditional source is a lease fence: once another worker owns
        # the lease, an old worker cannot interleave any more vector writes.
        sql = """
            INSERT INTO memories_embeddings(
                memory_id, embedding, representation, dimension, encoding_source, writer_token
            )
            SELECT ?, ?, ?, ?, 'python', ?
             WHERE EXISTS (
                 SELECT 1 FROM memories_meta
                  WHERE key = ? AND value LIKE ?
             )
            ON CONFLICT(memory_id) DO UPDATE SET
                embedding=excluded.embedding,
                representation=excluded.representation,
                dimension=excluded.dimension,
                encoding_source=excluded.encoding_source,
                writer_token=excluded.writer_token
        """
        params += (_REBUILD_LEASE_KEY, f"{lease_owner}|%")
    return sql, params


def upsert_embedding(
    conn: sqlite3.Connection,
    memory_id: int,
    vector: Dict[str, float],
    *,
    lease_owner: Optional[str] = None,
) -> None:
    sql, params = _embedding_upsert_sql_and_params(memory_id, vector, lease_owner)
    changed = conn.execute(sql, params).rowcount
    if lease_owner is not None and not changed:
        raise EmbeddingIntegrityFault("integrity_rebuild_lease_lost", [])


_MODEL_RECORDED: Set[str] = set()


def record_embedding_model_once(conn: sqlite3.Connection, vector: Dict[str, float], current_model: str) -> None:
    """E1: a search no longer rebuilds, so it no longer records which model
    made a store's vectors. The write path does, once: when a vector the
    CURRENT model just computed is stored and the store has no recorded
    model, record the current fingerprint (INSERT OR IGNORE, part of the
    caller's transaction, no commit). Only when the vector's kind matches the
    backend (sparse for tfidf, dense otherwise): a fallback vector never
    certifies a model. The process cache is set only once a recorded model
    has been READ back (committed state), never after this call's own
    uncommitted insert: a rolled-back first write must not stop the next one
    from recording (review 7929)."""
    key = _store_cache_key(conn)
    if key in _MODEL_RECORDED:
        return
    rep = _vector_representation(vector)
    if rep == "empty":
        return
    dense = rep.startswith("dense")
    if (current_model == "tfidf") == dense:
        return  # the vector does not match the configured backend: record nothing
    if get_stored_embedding_model(conn) is not None:
        # Cache only a COMMITTED record: inside an open transaction the row
        # read back may be this transaction's own insert, which can still
        # roll back (a local store_write). D1 statements commit at once.
        if not getattr(conn, "in_transaction", False):
            _MODEL_RECORDED.add(key)
        return
    dim = int(rep.split(":", 1)[1]) if rep.startswith("dense:") else None
    conn.execute("INSERT OR IGNORE INTO memories_meta (key, value) VALUES ('embedding_model', ?)",
                 (current_embedding_fingerprint(current_model, observed_dim=dim),))
    invalidate_embedding_integrity_cache(conn)


def upsert_embeddings_batch(
    conn: sqlite3.Connection,
    items: List[Tuple[int, Dict[str, float]]],
    *,
    lease_owner: Optional[str] = None,
) -> None:
    """Upsert many (memory_id, vector) pairs. The caller commits once for the
    whole batch instead of once per row.

    Deliberately loops upsert_embedding() (one execute() per row) rather than
    a single conn.executemany() call. SQLite's (and D1's, which is SQLite
    under the hood) changes() count for an INSERT ... ON CONFLICT DO UPDATE
    is 1 when the plain-INSERT branch is taken but 2 when the UPDATE branch
    fires on an existing row. executemany() only exposes one aggregate total
    across the whole call, so an exact "changed == len(items)" lease check
    against that aggregate is unreliable the moment a chunk mixes inserts and
    updates — which every rebuild of an already-embedded store does, i.e.
    virtually always. Checking each row's own rowcount individually, exactly
    like the single-row path always has, sidesteps the ambiguity: 0 always
    means blocked regardless of whether an allowed write would have been 1
    or 2, so the existing per-row lease fence stays exact.
    """
    for memory_id, vector in items:
        upsert_embedding(conn, memory_id, vector, lease_owner=lease_owner)


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

    Prefer get_embedding_integrity_status(): it performs this audit only on a
    process's first use (or after explicit invalidation), never per search.
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
    """Return the first-use SQL-derived result, never stale process-side reps."""
    return bool(get_embedding_integrity_status(conn, "tfidf").get("mixed"))


def get_stored_embedding_model(conn: sqlite3.Connection) -> Optional[str]:
    """Get the embedding model fingerprint (or legacy name) stored in the database."""
    return _meta_get(conn, "embedding_model")


def set_stored_embedding_model(
    conn: sqlite3.Connection,
    model: str,
    *,
    lease_owner: Optional[str] = None,
) -> None:
    """Store the selected model; an admin audit owns any integrity stamp."""
    if lease_owner is None:
        _meta_set(conn, "embedding_model", model)
    else:
        _meta_set_for_rebuild_owner(conn, "embedding_model", model, lease_owner)
    invalidate_embedding_integrity_cache(conn)
    conn.commit()


def current_embedding_fingerprint(
    current_model: str,
    *,
    observed_dim: Optional[int] = None,
) -> str:
    """Fingerprint: backend|model|repr -- the MODEL and the REPRESENTATION,
    never the endpoint host (E1, leader decision 7917). A host move alone
    (same model behind another address) must not invalidate a store; stamps
    written with the host (backend|model|host|repr) are normalised on read
    (normalize_fingerprint). observed_dim fills dense:N when known.
    """
    if current_model == "openai":
        model_name = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
        kind = f"dense:{observed_dim}" if observed_dim else "dense"
        return f"openai|{model_name}|{kind}"
    if current_model == "sentence-transformers":
        model_name = os.getenv("SENTENCE_TRANSFORMERS_MODEL", "all-MiniLM-L6-v2")
        kind = f"dense:{observed_dim}" if observed_dim else "dense"
        return f"sentence-transformers|{model_name}|{kind}"
    if current_model == "tfidf":
        return "tfidf|tfidf|sparse"
    return f"{current_model}|unknown|unset"


def unrecorded_compatibility(reps: Dict[str, int], current_model: str) -> Tuple[bool, Optional[int], str]:
    """E1b (leader 7981): a store with NO recorded model (written before E1,
    e.g. v0.4.6) is compatible with the current model when its vectors'
    representation -- and, for a dense model, their single dimension --
    match what the current model produces. Returns (compatible, dimension
    the query vector must have or None, why). The dimension of the CURRENT
    dense model is checked against the query vector by the caller."""
    kinds = {k for k, n in (reps or {}).items() if n and k != "empty"}
    if not kinds:
        return True, None, "no vectors"
    if current_model == "tfidf":
        return (kinds == {"sparse"}), None, f"vectors {sorted(kinds)} for tfidf"
    if current_model in ("openai", "sentence-transformers"):
        dims = sorted(k for k in kinds if k.startswith("dense:"))
        if kinds != set(dims) or len(dims) != 1:
            return False, None, f"vectors {sorted(kinds)} for a dense model"
        return True, int(dims[0].split(":", 1)[1]), f"vectors {dims[0]}"
    return False, None, f"unknown backend {current_model!r}"


def normalize_fingerprint(fp: Optional[str]) -> Optional[str]:
    """backend|model|repr. A stamp written before E1 carries the endpoint
    host as a third field (backend|model|host|repr): the host is dropped, so
    such a stamp compares equal when backend, model and representation
    match. Nothing is rewritten; anything else is returned unchanged."""
    if not fp:
        return fp
    parts = fp.split("|")
    if len(parts) == 4:
        return "|".join((parts[0], parts[1], parts[3]))
    return fp


def _is_numeric_gap_vector(vector: Dict[str, float]) -> bool:
    """Recognize thresholded dense vectors without loosening dense detection."""
    if not vector or not all(key.isdigit() for key in vector):
        return False
    keys = {int(key) for key in vector}
    return keys != set(range(len(keys)))


# metadata key a D1 import sets on a row until the row is complete
# (memora.storage._import_write_d1). Such a row is not a memory yet: reads
# hide it, and neither the integrity audit nor a rebuild treats it as a
# memory missing its vector -- the import, or the marker sweep, owns it.
IMPORT_MARKER_KEY = "import_attempt"


def not_import_pending_sql(col: str) -> str:
    """` AND <col is not an import-pending row>` for a metadata column.
    CASE, not OR: json_extract on malformed JSON raises, and CASE is lazy."""
    return (
        f" AND (CASE WHEN {col} IS NULL OR instr({col}, '\"{IMPORT_MARKER_KEY}\"') = 0 THEN 1"
        f" WHEN json_valid({col}) THEN json_extract({col}, '$.{IMPORT_MARKER_KEY}') IS NULL"
        f" ELSE 1 END)"
    )


def _coverage_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    """Indexed anti-joins in both directions; subtraction masks cancellations."""
    return {
        "memory_count": int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]),
        "embedding_count": int(conn.execute(
            "SELECT COUNT(*) FROM memories_embeddings "
            "WHERE embedding IS NOT NULL AND embedding != '' AND embedding != 'null'"
        ).fetchone()[0]),
        "missing_count": int(conn.execute(
            """SELECT COUNT(*) FROM memories AS m
               LEFT JOIN memories_embeddings AS e ON e.memory_id = m.id
                AND (
                    (e.embedding IS NOT NULL AND e.embedding != '' AND e.embedding != 'null')
                    OR (e.representation = 'empty' AND e.encoding_source = 'python')
                )
               WHERE e.memory_id IS NULL""" + not_import_pending_sql("m.metadata")
        ).fetchone()[0]),
        "orphan_embedding_count": int(conn.execute(
            """SELECT COUNT(*) FROM memories_embeddings AS e
               LEFT JOIN memories AS m ON m.id = e.memory_id
               WHERE m.id IS NULL"""
        ).fetchone()[0]),
    }


def audit_embedding_integrity(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Derive hot-path integrity from indexed SQL, never vector JSON."""
    reps: Dict[str, int] = {}
    rows = conn.execute(
        """
        SELECT representation, dimension, COUNT(*) AS n
          FROM memories_embeddings
         WHERE embedding IS NOT NULL AND embedding != '' AND embedding != 'null'
           AND representation IS NOT NULL
         GROUP BY representation, dimension
        """
    ).fetchall()
    for row in rows:
        representation = row["representation"] if isinstance(row, sqlite3.Row) else row[0]
        dimension = row["dimension"] if isinstance(row, sqlite3.Row) else row[1]
        count = int(row["n"] if isinstance(row, sqlite3.Row) else row[2])
        rep = f"dense:{dimension}" if representation == "dense" and dimension is not None else representation
        reps[rep] = reps.get(rep, 0) + count
    unknown_rows = conn.execute(
        """
        SELECT e.memory_id,
               CASE WHEN r.memory_id IS NULL THEN 0 ELSE 1 END AS repaired_before
          FROM memories_embeddings AS e
          LEFT JOIN memories_embedding_repairs AS r ON r.memory_id = e.memory_id
         WHERE e.embedding IS NOT NULL AND e.embedding != '' AND e.embedding != 'null'
           AND (e.representation IS NULL OR e.representation NOT IN ('dense', 'sparse'))
         ORDER BY e.memory_id
         LIMIT 101
        """
    ).fetchall()
    unknown_ids = [
        int(row["memory_id"] if isinstance(row, sqlite3.Row) else row[0])
        for row in unknown_rows[:100]
    ]
    recurring_rows = conn.execute(
        """
        SELECT e.memory_id FROM memories_embeddings AS e
        JOIN memories_embedding_repairs AS r ON r.memory_id = e.memory_id
         WHERE e.embedding IS NOT NULL AND e.embedding != '' AND e.embedding != 'null'
           AND (e.representation IS NULL OR e.representation NOT IN ('dense', 'sparse'))
         ORDER BY e.memory_id LIMIT 100
        """
    ).fetchall()
    recurring_unknown_ids = [
        int(row["memory_id"] if isinstance(row, sqlite3.Row) else row[0])
        for row in recurring_rows
    ]
    missing_ids = [
        int(row[0]) for row in conn.execute(
            """
            SELECT m.id FROM memories AS m
            LEFT JOIN memories_embeddings AS e ON e.memory_id = m.id
             AND (
                 (e.embedding IS NOT NULL AND e.embedding != '' AND e.embedding != 'null')
                 OR (e.representation = 'empty' AND e.encoding_source = 'python')
             )
            WHERE e.memory_id IS NULL""" + not_import_pending_sql("m.metadata") + """
            ORDER BY m.id LIMIT 100
            """
        ).fetchall()
    ]
    orphan_ids = [
        int(row[0]) for row in conn.execute(
            """
            SELECT e.memory_id FROM memories_embeddings AS e
            LEFT JOIN memories AS m ON m.id = e.memory_id
            WHERE m.id IS NULL ORDER BY e.memory_id LIMIT 100
            """
        ).fetchall()
    ]
    return {
        "schema_version": _INTEGRITY_SCHEMA_VERSION,
        "reps": reps,
        "mixed": _reps_are_mixed(reps),
        "unknown_encoding_ids": unknown_ids,
        "recurring_unknown_ids": recurring_unknown_ids,
        "missing_ids": missing_ids,
        "orphan_ids": orphan_ids,
        **_coverage_counts(conn),
    }


def _snapshot_matches_live(stamp: Dict[str, Any], audit: Dict[str, Any], stored: Optional[str]) -> bool:
    """A stamp detects bypasses; it never decides live integrity by itself."""
    if stamp.get("schema_version") != _INTEGRITY_SCHEMA_VERSION:
        return False
    if stamp.get("state") != "initialized" or (
            normalize_fingerprint(stamp.get("fingerprint")) != normalize_fingerprint(stored)):
        return False
    if sum(int(n) for n in (stamp.get("reps") or {}).values()) != audit["embedding_count"]:
        return False
    return all(stamp.get(key) == audit.get(key) for key in (
        "memory_count", "embedding_count", "missing_count", "orphan_embedding_count", "reps",
    ))


def _stamp_integrity_audit(
    conn: sqlite3.Connection,
    audit: Dict[str, Any],
    stored: Optional[str],
    *,
    lease_owner: Optional[str] = None,
) -> None:
    """Persist a completed SQL audit as a drift baseline, never as live truth."""
    _write_embedding_integrity(conn, integrity_stamp(audit, stored), lease_owner=lease_owner)
    conn.commit()


def integrity_stamp(audit: Dict[str, Any], stored: Optional[str]) -> Dict[str, Any]:
    """The stamp _stamp_integrity_audit writes for an audit (the operator's
    `restamp`, L6, sends exactly this value as its one D1 write)."""
    import uuid
    stamped = dict(audit)
    stamped.update({
        "fingerprint": stored,
        "schema_version": _INTEGRITY_SCHEMA_VERSION,
        "generation": str(uuid.uuid4()),
        "state": "initialized",
    })
    return stamped


def integrity_stamp_value(stamped: Dict[str, Any]) -> str:
    """The memories_meta value _write_embedding_integrity stores."""
    return json.dumps(stamped, ensure_ascii=False, sort_keys=True)


def _model_mismatch_for_reps(reps: Dict[str, int], stored: Optional[str], current_model: str) -> bool:
    if stored is None or "|" not in stored:
        return True
    stored = normalize_fingerprint(stored)
    if current_model in ("openai", "sentence-transformers"):
        if "sparse" in reps:
            return True
        dense_dims = sorted(k for k in reps if k.startswith("dense:"))
        observed_dim = int(dense_dims[0].split(":")[1]) if dense_dims else None
        current_fp = current_embedding_fingerprint(current_model, observed_dim=observed_dim)
        stored_parts, current_parts = stored.split("|"), current_fp.split("|")
        if len(stored_parts) != 3 or len(current_parts) != 3 or stored_parts[:2] != current_parts[:2]:
            return True
        stored_kind, current_kind = stored_parts[-1], current_parts[-1]
        if stored_kind.startswith("dense:") and current_kind.startswith("dense:"):
            return stored_kind != current_kind
        return not (
            (stored_kind.startswith("dense:") and current_kind == "dense")
            or (stored_kind == "dense" and current_kind.startswith("dense:"))
        )
    return current_model == "tfidf" and any(k.startswith("dense") for k in reps) or stored != current_embedding_fingerprint(current_model)


def _integrity_from_raw(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def get_embedding_integrity_status(
    conn: sqlite3.Connection,
    current_model: str,
    *,
    meta: Optional[Dict[str, Optional[str]]] = None,
) -> Dict[str, Any]:
    """Read one DB-owned epoch per search; re-derive indexed SQL only on change.

    meta: memories_meta values the caller already read in one statement
    (embedding_change_epoch, embedding_integrity); saves this check's own
    two reads. Omitted, both are read here as before.
    """
    key = _store_cache_key(conn)
    if meta is not None:
        epoch = meta.get("embedding_change_epoch") or "0"
        stamp = _integrity_from_raw(meta.get(_INTEGRITY_KEY))
    else:
        epoch = _meta_get(conn, "embedding_change_epoch") or "0"
        # Publishing a build state changes metadata rather than an embedding row,
        # so it does not advance the embedding epoch. Read this small stamp before
        # accepting a healthy cached result from another connection/process.
        stamp = get_embedding_integrity(conn)
    cached = _integrity_check_cache.get(key)
    if (
        cached is not None
        and cached.get("_epoch") == epoch
        and stamp.get("state") != "building"
    ):
        return cached

    audit = audit_embedding_integrity(conn)
    stored = get_stored_embedding_model(conn)
    if audit["orphan_embedding_count"]:
        result = {
            "mismatch": True, "repairable": False, "mixed": audit["mixed"],
            "reason": "orphan_embeddings", "fault_ids": audit["orphan_ids"], "audit": audit,
        }
    elif audit["recurring_unknown_ids"]:
        result = {
            "mismatch": True, "repairable": False, "mixed": audit["mixed"],
            "reason": "recurring_unknown_encoding",
            "fault_ids": audit["recurring_unknown_ids"], "audit": audit,
        }
    elif stamp and stamp.get("state") == "building":
        lease = _rebuild_lease_status(conn)
        stale = bool(lease["stale"])
        result = {
            "mismatch": True, "repairable": stale, "mixed": audit["mixed"],
            "reason": "integrity_build_stale" if stale else "integrity_building",
            "fault_ids": [], "audit": audit,
            "lease_age_seconds": lease["age_seconds"],
            "retry_after_seconds": lease["retry_after_seconds"],
        }
    elif audit["unknown_encoding_ids"]:
        # A legacy/unknown encoding is repaired once. Rebuild records that
        # memory id, so the next recurrence becomes the fault above.
        result = {
            "mismatch": True, "repairable": True, "mixed": audit["mixed"],
            "reason": "unknown_embedding_encoding",
            "fault_ids": audit["unknown_encoding_ids"], "audit": audit,
        }
    elif audit["missing_count"]:
        result = {
            "mismatch": True, "repairable": True, "mixed": audit["mixed"],
            "reason": "missing_embeddings", "fault_ids": audit["missing_ids"], "audit": audit,
        }
    elif audit["memory_count"] and (
        not stamp or stamp.get("schema_version") != _INTEGRITY_SCHEMA_VERSION
    ):
        result = {
            "mismatch": True, "repairable": True, "mixed": audit["mixed"],
            "reason": "integrity_uninitialized", "fault_ids": [], "audit": audit,
        }
    elif audit["mixed"] or _model_mismatch_for_reps(audit["reps"], stored, current_model):
        result = {
            "mismatch": True, "repairable": True, "mixed": audit["mixed"],
            "reason": "model_or_representation_mismatch", "fault_ids": [], "audit": audit,
        }
    else:
        result = {
            "mismatch": False, "repairable": False, "mixed": False,
            "reason": None, "fault_ids": [], "audit": audit,
        }
    result["_epoch"] = epoch
    # A lease can expire without an embedding write (and therefore without an
    # epoch change), so never cache its live timing state.
    if result["reason"] not in {"integrity_building", "integrity_build_stale"}:
        _integrity_check_cache[key] = result
    return result


def check_embedding_model_mismatch(conn: sqlite3.Connection, current_model: str) -> bool:
    """Compatibility boolean; auto-rebuild callers should inspect status."""
    return bool(get_embedding_integrity_status(conn, current_model)["mismatch"])


def _tags_or_untagged(tags_json: Optional[str]) -> Any:
    """Stored tags for embedding text; unparseable JSON reads as untagged, as
    every reader does (storage._parse_tags_json) -- a malformed import must
    not abort a rebuild, including semantic_search's auto-rebuild."""
    if not tags_json:
        return []
    try:
        return json.loads(tags_json)
    except (json.JSONDecodeError, TypeError):
        return []


def rebuild_all_embeddings(conn: sqlite3.Connection, embedding_model: str) -> int:
    """Rebuild all embeddings and finish with a SQL-derived audit stamp."""
    import uuid
    lease_owner = _acquire_rebuild_lease(conn)
    rebuild_generation = str(uuid.uuid4())
    building = _empty_integrity()
    building["state"] = "building"
    building["generation"] = rebuild_generation
    _assert_rebuild_lease_owner(conn, lease_owner)
    _write_embedding_integrity(conn, building, lease_owner=lease_owner)
    conn.commit()
    invalidate_embedding_integrity_cache(conn)

    rows = conn.execute(
        "SELECT id, content, metadata, tags FROM memories WHERE 1=1" + not_import_pending_sql("metadata")
    ).fetchall()
    repaired_rows = conn.execute(
        """
        SELECT memory_id FROM memories_embeddings
         WHERE representation IS NULL OR representation NOT IN ('dense', 'sparse', 'empty')
        """
    ).fetchall()
    repaired_ids = [int(row["memory_id"] if isinstance(row, sqlite3.Row) else row[0]) for row in repaired_rows]
    updated = 0
    seen_reps: Set[str] = set()
    chunk_size = _resolve_rebuild_chunk_size()
    total = len(rows)
    for start in range(0, total, chunk_size):
        chunk = rows[start : start + chunk_size]
        # Fence BEFORE the (potentially slow) batch embed call, same as the
        # old per-row fence before compute_embedding().
        _assert_rebuild_lease_owner(conn, lease_owner)
        entries = [
            {
                "content": row["content"],
                "metadata": json.loads(row["metadata"]) if row["metadata"] else None,
                "tags": _tags_or_untagged(row["tags"]),
            }
            for row in chunk
        ]
        # compute_embeddings_batch() already enforces strict-mode semantics
        # for every backend (raises EmbeddingStrictError, never substitutes
        # TF-IDF, under MEMORA_EMBEDDING_STRICT=1) — same contract this loop
        # relied on from compute_embedding() per row.
        vectors = compute_embeddings_batch(entries, embedding_model)
        # Fence again BEFORE writing, same as the old per-row fence before
        # upsert_embedding().
        _assert_rebuild_lease_owner(conn, lease_owner)
        items: List[Tuple[int, Dict[str, float]]] = []
        for row, vector in zip(chunk, vectors):
            seen_reps.add(_vector_representation(vector))
            items.append((row["id"], vector))
        upsert_embeddings_batch(conn, items, lease_owner=lease_owner)
        conn.commit()
        updated += len(chunk)
        _logger.info(
            "rebuild_all_embeddings: %d/%d done (chunk_size=%d)",
            updated, total, chunk_size,
        )

    if updated == 0:
        _assert_rebuild_lease_owner(conn, lease_owner)
        set_stored_embedding_model(
            conn, current_embedding_fingerprint(embedding_model), lease_owner=lease_owner
        )
        _assert_rebuild_lease_owner(conn, lease_owner)
        verify_embedding_integrity(conn, stamp=True, lease_owner=lease_owner)
        _assert_rebuild_lease_owner(conn, lease_owner)
        _release_rebuild_lease(conn, lease_owner)
        conn.commit()
        return 0

    non_empty = {r for r in seen_reps if r != "empty"}
    if len(non_empty) > 1:
        raise EmbeddingProviderError(
            f"rebuild produced non-uniform representations {sorted(seen_reps)}; "
            f"refusing to stamp fingerprint"
        )
    if non_empty:
        rep = next(iter(non_empty))
        observed_dim = int(rep.split(":", 1)[1]) if rep.startswith("dense:") else None
        fp = current_embedding_fingerprint(embedding_model, observed_dim=observed_dim)
        if rep.startswith("dense:"):
            fp = fp.rsplit("|", 1)[0] + "|" + rep
    else:
        fp = current_embedding_fingerprint(embedding_model)
    _assert_rebuild_lease_owner(conn, lease_owner)
    set_stored_embedding_model(conn, fp, lease_owner=lease_owner)
    if repaired_ids:
        for start in range(0, len(repaired_ids), _REBUILD_REPAIR_CHUNK_SIZE):
            _assert_rebuild_lease_owner(conn, lease_owner)
            for memory_id in repaired_ids[start : start + _REBUILD_REPAIR_CHUNK_SIZE]:
                _upsert_embedding_repair(conn, memory_id, rebuild_generation, lease_owner)
            conn.commit()
    _assert_rebuild_lease_owner(conn, lease_owner)
    verify_embedding_integrity(conn, stamp=True, lease_owner=lease_owner)
    _assert_rebuild_lease_owner(conn, lease_owner)
    _release_rebuild_lease(conn, lease_owner)
    conn.commit()
    return updated


def verify_embedding_integrity(
    conn: sqlite3.Connection,
    *,
    stamp: bool = True,
    lease_owner: Optional[str] = None,
) -> Dict[str, Any]:
    """Explicit admin audit; stamp a complete SQL generation when requested."""
    audit = audit_embedding_integrity(conn)
    result = dict(audit)
    result["fingerprint"] = get_stored_embedding_model(conn)
    if stamp:
        if lease_owner is not None:
            _assert_rebuild_lease_owner(conn, lease_owner)
        _stamp_integrity_audit(conn, audit, result["fingerprint"], lease_owner=lease_owner)
    invalidate_embedding_integrity_cache(conn)
    return result
