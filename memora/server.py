"""MCP-compatible memory server backed by SQLite."""
from __future__ import annotations

import argparse
import asyncio
import functools
import logging
import os
import re
import time
from typing import Any, Dict, List, Literal, Optional

from mcp.server.fastmcp import FastMCP

from .cloud_sync import schedule_sync as _schedule_cloud_graph_sync
from .hierarchy import (
    build_hierarchy_tree,
    build_tag_hierarchy,
    extract_hierarchy_path,
    find_similar_paths,
    get_existing_hierarchy_paths,
    suggest_hierarchy_from_similar,
)
from .storage import (
    _redact_secrets,
    absorb_memory,
    add_link,
    add_memories,
    add_memory,
    boost_memory,
    clear_events,
    collect_all_tags,
    connect,
    delete_memories,
    delete_memory,
    detect_clusters,
    export_memories,
    find_invalid_tag_entries,
    generate_insights,
    get_crossrefs,
    get_hierarchy_paths,
    get_memories_metadata_batch,
    get_memory,
    get_statistics,
    hybrid_search,
    import_memories,
    list_absorb_inflight,
    list_memories,
    poll_events,
    rebuild_crossrefs,
    rebuild_embeddings,
    remove_link,
    semantic_search,
    sync_to_cloud,
    update_crossrefs,
    update_memory,
    DEFAULT_FOLLOW_GET,
    DEFAULT_FOLLOW_LIST,
    resolve_follow,
)

logger = logging.getLogger(__name__)


def _integrity_fault_response(exc: Exception) -> Optional[Dict[str, Any]]:
    from .embeddings import EmbeddingIntegrityFault, embedding_integrity_fault_payload
    return embedding_integrity_fault_payload(exc) if isinstance(exc, EmbeddingIntegrityFault) else None


def _safe_error(e: Exception, context: str = "operation") -> Dict[str, str]:
    """Return sanitized error for unexpected exceptions. Log full details internally."""
    logger.error("Failed %s: %s", context, e, exc_info=True)
    return {"error": f"{context}_failed", "message": f"The {context} failed. Check server logs for details."}


_DOCUMENT_TYPES = ("document_fragment", "document_root")


def _is_doc_memory(metadata: Optional[Dict[str, Any]]) -> bool:
    """Check if metadata indicates a document root or fragment."""
    if not metadata:
        return False
    return metadata.get("type") in _DOCUMENT_TYPES


# Content type inference patterns
TYPE_PATTERNS: List[tuple[str, str]] = [
    (r'^(?:TODO|TASK)[:>\s]', 'todo'),
    (r'^(?:BUG|ISSUE|FIX|ERROR)[:>\s]', 'issue'),
    (r'^(?:NOTE|TIP|INFO)[:>\s]', 'note'),
    (r'^(?:IDEA|FEATURE|ENHANCEMENT)[:>\s]', 'idea'),
    (r'^(?:QUESTION|\?)[:>\s]', 'question'),
    (r'^(?:WARN|WARNING|CAUTION)[:>\s]', 'warning'),
]

# Duplicate detection threshold
DUPLICATE_THRESHOLD = 0.85

# Auto-assign hierarchy when top suggestion confidence >= this threshold
AUTO_HIERARCHY_THRESHOLD = 0.5

# Allow token-efficient small MCP response payload
CREATE_RESPONSE_MODES = {"full", "minimal"}

# Tool cooldowns (seconds) — prevents resource exhaustion via repeated expensive operations
_TOOL_COOLDOWNS: Dict[str, int] = {
    "memory_rebuild_embeddings": 300,
    "memory_rebuild_crossrefs": 300,
    "memory_find_duplicates": 120,
    "memory_detect_supersessions": 30,
    "memory_backfill_tags": 10,
    "memory_migrate_images": 300,
    "memory_insights": 120,
    "memory_export": 60,
    "memory_import": 60,
}
_tool_last_call: Dict[str, float] = {}
_tool_running: Dict[str, bool] = {}


def _check_tool_cooldown(tool_name: str) -> Optional[str]:
    """Check if a tool is running or within its cooldown period. Returns error message or None."""
    cooldown = _TOOL_COOLDOWNS.get(tool_name)
    if not cooldown:
        return None
    # Single-flight: reject if already running
    if _tool_running.get(tool_name):
        return f"{tool_name} is already running."
    last = _tool_last_call.get(tool_name, 0)
    elapsed = time.time() - last
    if elapsed < cooldown:
        return f"Rate limited. Try again in {int(cooldown - elapsed)}s."
    _tool_running[tool_name] = True
    return None


def _finish_tool(tool_name: str) -> None:
    """Mark a tool as finished and record its completion time."""
    _tool_running.pop(tool_name, None)
    _tool_last_call[tool_name] = time.time()


def _infer_type(content: str) -> Optional[str]:
    """Infer memory type from content prefix patterns."""
    for pattern, type_name in TYPE_PATTERNS:
        if re.match(pattern, content, re.IGNORECASE):
            return type_name
    return None


def _suggest_tags(content: str, inferred_type: Optional[str]) -> List[str]:
    """Suggest tags based on content and inferred type."""
    suggestions = []

    # Type-based suggestions
    if inferred_type == 'todo':
        suggestions.append('memora/todos')
    elif inferred_type == 'issue':
        suggestions.append('memora/issues')
    elif inferred_type in ('note', 'idea', 'question'):
        suggestions.append('memora/knowledge')

    return suggestions


from .graph import export_graph_html, start_graph_server  # noqa: E402

logger = logging.getLogger(__name__)


def _read_int_env(var_name: str, fallback: int) -> int:
    try:
        return int(os.getenv(var_name, fallback))
    except (TypeError, ValueError):
        return fallback


VALID_TRANSPORTS = {"stdio", "sse", "streamable-http"}

_env_transport = os.getenv("MEMORA_TRANSPORT", "stdio")
DEFAULT_TRANSPORT = _env_transport if _env_transport in VALID_TRANSPORTS else "stdio"
DEFAULT_HOST = os.getenv("MEMORA_HOST", "127.0.0.1")
DEFAULT_PORT = _read_int_env("MEMORA_PORT", 8000)
DEFAULT_GRAPH_PORT = _read_int_env("MEMORA_GRAPH_PORT", 8765)

mcp = FastMCP("Memory MCP Server", host=DEFAULT_HOST, port=DEFAULT_PORT)


def _collapse_nullable_anyof(schema: Any) -> Any:
    """Recursively collapse Pydantic's `Optional[X]` → `anyOf:[{X},{null}]`
    pattern into the bare `X` branch. Anthropic's tool input_schema
    validator rejects `anyOf` / `oneOf` / `allOf`; the `null` branch
    is redundant once we know the field has a `default` of None.

    Returns the (possibly mutated) schema. Mutates dicts in place
    where possible so referential structure is preserved.
    """
    if isinstance(schema, dict):
        # Collapse {anyOf:[{...},{type:"null"}]} (or 1-element variants)
        # before recursing into children.
        if "anyOf" in schema and isinstance(schema["anyOf"], list):
            non_null = [b for b in schema["anyOf"]
                        if not (isinstance(b, dict) and b.get("type") == "null")]
            if len(non_null) == 1 and isinstance(non_null[0], dict):
                # Merge the surviving branch's keys into `schema`,
                # dropping `anyOf` itself. Preserve sibling keys like
                # `title`, `default`, `description` on `schema`.
                merged = dict(non_null[0])
                for k, v in schema.items():
                    if k == "anyOf":
                        continue
                    merged.setdefault(k, v)
                schema.clear()
                schema.update(merged)
        for v in schema.values():
            _collapse_nullable_anyof(v)
    elif isinstance(schema, list):
        for v in schema:
            _collapse_nullable_anyof(v)
    return schema


def _sanitize_tool_schemas(server: FastMCP) -> None:
    """Walk every registered tool's `parameters` dict and strip
    `anyOf` nullable unions. Safe to call multiple times — the
    transform is idempotent.
    """
    tools = getattr(getattr(server, "_tool_manager", None), "_tools", {})
    for tool in tools.values():
        params = getattr(tool, "parameters", None)
        if isinstance(params, dict):
            _collapse_nullable_anyof(params)


def _with_connection(func=None, *, writes=False):
    """Decorator that manages database connections and cloud sync.

    Opens a connection, runs the function, closes the connection,
    and syncs to cloud storage only after write operations — all on a
    worker thread. sqlite3 connections are created and closed inside that
    same thread (no shared pool). Callers must await the wrapper.

    Args:
        writes: If True, syncs to cloud after operation. If False, skips sync (read-only).
    """
    def decorator(func):
        def _run(*args, **kwargs):
            conn = connect()
            try:
                result = func(conn, *args, **kwargs)
                if writes:
                    sync_to_cloud()
                return result
            finally:
                conn.close()

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            return await asyncio.to_thread(_run, *args, **kwargs)

        wrapper.__memora_blocking__ = _run  # type: ignore[attr-defined]
        return wrapper

    # Allow using as @_with_connection or @_with_connection(writes=True)
    if func is not None:
        return decorator(func)
    return decorator


async def _in_worker(fn, /, *args, **kwargs):
    """Run a blocking connect()/storage body off the event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


@_with_connection(writes=True)
def _create_memory(
    conn,
    content: str,
    metadata: Optional[Dict[str, Any]],
    tags: Optional[list[str]],
):
    return add_memory(conn, content=content.strip(), metadata=metadata, tags=tags or [])


@_with_connection
def _get_memory(conn, memory_id: int, follow: Optional[str] = None):
    return get_memory(conn, memory_id, follow=follow)


@_with_connection(writes=True)
def _update_memory(
    conn,
    memory_id: int,
    content: Optional[str],
    metadata: Optional[Dict[str, Any]],
    tags: Optional[list[str]],
    replace_metadata: bool = False,
):
    return update_memory(
        conn,
        memory_id,
        content=content,
        metadata=metadata,
        tags=tags,
        replace_metadata=replace_metadata,
    )


@_with_connection(writes=True)
def _delete_memory(conn, memory_id: int, reason: Optional[str] = None):
    return delete_memory(conn, memory_id, reason=reason)


@_with_connection
def _get_hierarchy_paths(conn):
    return get_hierarchy_paths(conn)


@_with_connection
def _get_memories_metadata_batch(conn, memory_ids: List[int]):
    return get_memories_metadata_batch(conn, memory_ids)


@_with_connection
def _list_memories(
    conn,
    query: Optional[str],
    metadata_filters: Optional[Dict[str, Any]],
    limit: Optional[int],
    offset: Optional[int],
    date_from: Optional[str],
    date_to: Optional[str],
    tags_any: Optional[List[str]],
    tags_all: Optional[List[str]],
    tags_none: Optional[List[str]],
    sort_by_importance: bool = False,
    follow: Optional[str] = None,
):
    return list_memories(
        conn, query, metadata_filters, limit, offset,
        date_from, date_to, tags_any, tags_all, tags_none,
        sort_by_importance=sort_by_importance,
        follow=follow,
    )


@_with_connection(writes=True)
def _boost_memory(conn, memory_id: int, boost_amount: float):
    return boost_memory(conn, memory_id, boost_amount)


@_with_connection(writes=True)
def _create_memories(conn, entries: List[Dict[str, Any]]):
    return add_memories(conn, entries)


@_with_connection(writes=True)
def _absorb_memory(
    conn,
    facts: List[str],
    source: str,
    confidence: float,
    context: Optional[str],
    metadata: Optional[Dict[str, Any]],
    tags: Optional[List[str]],
    dry_run: bool,
):
    return absorb_memory(
        conn,
        facts,
        source=source,
        confidence=confidence,
        context=context,
        metadata=metadata,
        tags=tags,
        dry_run=dry_run,
    )


@_with_connection(writes=True)
def _detect_supersessions(
    conn,
    min_similarity: float,
    limit: int,
    dry_run: bool,
    tags_any: Optional[List[str]],
    min_confidence: float,
):
    from .storage import detect_supersessions
    return detect_supersessions(
        conn,
        min_similarity=min_similarity,
        limit=limit,
        dry_run=dry_run,
        tags_any=tags_any,
        min_confidence=min_confidence,
    )


@_with_connection(writes=True)
def _delete_memories(conn, ids: List[int], reason: Optional[str] = None):
    return delete_memories(conn, ids, reason=reason)


@_with_connection
def _collect_tags(conn):
    return collect_all_tags(conn)


@_with_connection
def _find_invalid_tags(conn):
    from . import TAG_WHITELIST

    return find_invalid_tag_entries(conn, TAG_WHITELIST)


@_with_connection(writes=True)  # May write crossrefs if refresh=True
def _get_related(conn, memory_id: int, refresh: bool) -> List[Dict[str, Any]]:
    if refresh:
        update_crossrefs(conn, memory_id)
    refs = get_crossrefs(conn, memory_id)
    if not refs and not refresh:
        update_crossrefs(conn, memory_id)
        refs = get_crossrefs(conn, memory_id)
    return refs


@_with_connection(writes=True)
def _rebuild_crossrefs(conn):
    return rebuild_crossrefs(conn)


@_with_connection
def _semantic_search(
    conn,
    query: str,
    metadata_filters: Optional[Dict[str, Any]],
    top_k: Optional[int],
    min_score: Optional[float],
    follow: Optional[str] = None,
):
    return semantic_search(
        conn,
        query,
        metadata_filters=metadata_filters,
        top_k=top_k,
        min_score=min_score,
        follow=follow,
    )


def _resolve_search_cap(
    *,
    limit: Optional[int],
    top_k: Optional[int],
    default: int,
) -> int:
    """Honor MCP ``limit`` or legacy ``top_k``. Unknown ``limit`` used to be dropped
    by the schema, so hybrid defaulted to 10 — the live ``count=10`` quirk."""
    requested = limit if limit is not None else top_k
    if requested is None:
        return default
    return max(1, int(requested))


@_with_connection
def _hybrid_search(
    conn,
    query: str,
    semantic_weight: float,
    top_k: int,
    min_score: float,
    metadata_filters: Optional[Dict[str, Any]],
    date_from: Optional[str],
    date_to: Optional[str],
    tags_any: Optional[List[str]],
    tags_all: Optional[List[str]],
    tags_none: Optional[List[str]],
    follow: Optional[str] = None,
):
    return hybrid_search(
        conn,
        query,
        semantic_weight=semantic_weight,
        top_k=top_k,
        min_score=min_score,
        metadata_filters=metadata_filters,
        date_from=date_from,
        date_to=date_to,
        tags_any=tags_any,
        tags_all=tags_all,
        tags_none=tags_none,
        follow=follow,
    )


def _digest_memory_preview(memory: Dict[str, Any], preview_chars: int) -> Dict[str, Any]:
    content = memory.get("content", "") or ""
    preview = content[:preview_chars] + "…" if len(content) > preview_chars else content
    return {
        "id": memory["id"],
        "content_preview": preview,
        "tags": memory.get("tags", []),
        "metadata": memory.get("metadata"),
        "created_at": memory.get("created_at"),
        "updated_at": memory.get("updated_at"),
    }


def _dedupe_preserve_order(ids: List[int]) -> List[int]:
    seen = set()
    out = []
    for memory_id in ids:
        if memory_id in seen:
            continue
        seen.add(memory_id)
        out.append(memory_id)
    return out


def _append_digest_memory(
    items: List[Dict[str, Any]],
    seen_ids: set[int],
    memory: Dict[str, Any],
    preview_chars: int,
    *,
    score: Optional[float] = None,
    source: Optional[str] = None,
) -> None:
    memory_id = memory["id"]
    if memory_id in seen_ids:
        return
    seen_ids.add(memory_id)
    preview = _digest_memory_preview(memory, preview_chars)
    if score is not None:
        preview["score"] = score
    if source is not None:
        preview["source"] = source
    items.append(preview)


def _has_digest_tag(memory: Dict[str, Any], tag: str) -> bool:
    return tag in set(memory.get("tags") or [])


def _digest_bucket_type(bucket_tag: str) -> Optional[str]:
    if bucket_tag == "memora/todos":
        return "todo"
    if bucket_tag == "memora/issues":
        return "issue"
    return None


def _memory_metadata_type(memory: Dict[str, Any]) -> Optional[str]:
    metadata = memory.get("metadata")
    if isinstance(metadata, dict):
        value = metadata.get("type")
        if isinstance(value, str):
            return value
    return None


def _active_item_matches_bucket(memory: Dict[str, Any], bucket_tag: str) -> bool:
    bucket_type = _digest_bucket_type(bucket_tag)
    return _has_digest_tag(memory, bucket_tag) or (
        bucket_type is not None and _memory_metadata_type(memory) == bucket_type
    )


def _metadata_filters_for_bucket_type(
    metadata_filters: Optional[Dict[str, Any]],
    bucket_type: str,
) -> Optional[Dict[str, Any]]:
    combined = dict(metadata_filters or {})
    existing_type = combined.get("type")
    if existing_type is not None and existing_type != bucket_type:
        return None
    combined["type"] = bucket_type
    return combined


def _list_digest_bucket(
    conn,
    topic: str,
    bucket_tag: str,
    k: int,
    metadata_filters: Optional[Dict[str, Any]],
    date_from: Optional[str],
    date_to: Optional[str],
    tags_any: Optional[List[str]],
    tags_all: Optional[List[str]],
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    seen_ids: set[int] = set()

    combined_tags_all = list(tags_all or [])
    if bucket_tag not in combined_tags_all:
        combined_tags_all.append(bucket_tag)
    for item in list_memories(
        conn,
        query=topic,
        metadata_filters=metadata_filters,
        limit=k,
        date_from=date_from,
        date_to=date_to,
        tags_any=tags_any,
        tags_all=combined_tags_all,
        tags_none=None,
        follow="active",
    ):
        if item["id"] not in seen_ids:
            seen_ids.add(item["id"])
            items.append(item)

    bucket_type = _digest_bucket_type(bucket_tag)
    if bucket_type is None:
        return items

    typed_metadata_filters = _metadata_filters_for_bucket_type(metadata_filters, bucket_type)
    if typed_metadata_filters is None:
        return items

    for item in list_memories(
        conn,
        query=topic,
        metadata_filters=typed_metadata_filters,
        limit=k,
        date_from=date_from,
        date_to=date_to,
        tags_any=tags_any,
        tags_all=tags_all,
        tags_none=None,
        follow="active",
    ):
        if item["id"] not in seen_ids:
            seen_ids.add(item["id"])
            items.append(item)

    return items


@_with_connection
def _build_memory_digest(
    conn,
    topic: str,
    k: int,
    include_lineage: bool,
    include_todos: bool,
    include_related_hops: int,
    preview_chars: int,
    metadata_filters: Optional[Dict[str, Any]],
    date_from: Optional[str],
    date_to: Optional[str],
    tags_any: Optional[List[str]],
    tags_all: Optional[List[str]],
    seed_ids: Optional[List[int]],
    debug: bool,
) -> Dict[str, Any]:
    results = hybrid_search(
        conn,
        topic,
        top_k=k,
        metadata_filters=metadata_filters,
        date_from=date_from,
        date_to=date_to,
        tags_any=tags_any,
        tags_all=tags_all,
        follow="active",
    )

    memory_ids: List[int] = []
    memories: List[Dict[str, Any]] = []
    active_memories: List[Dict[str, Any]] = []
    ranked_candidates: List[Dict[str, Any]] = []
    seen_memory_ids: set[int] = set()
    for result in results:
        memory = result.get("memory", result)
        memory_id = memory["id"]
        score = result.get("score")
        ranked_candidates.append({
            "id": memory_id,
            "score": score,
            "source": "hybrid_search",
        })
        if memory_id in seen_memory_ids:
            continue
        memory_ids.append(memory_id)
        active_memories.append(memory)
        _append_digest_memory(memories, seen_memory_ids, memory, preview_chars, score=score)

    missing_seed_ids: List[int] = []
    for seed_id in seed_ids or []:
        if seed_id in seen_memory_ids:
            continue
        seed = get_memory(conn, seed_id)
        if not seed:
            missing_seed_ids.append(seed_id)
            continue
        memory_ids.append(seed["id"])
        active_memories.append(seed)
        _append_digest_memory(memories, seen_memory_ids, seed, preview_chars, source="seed")
        ranked_candidates.append({
            "id": seed["id"],
            "score": None,
            "source": "seed",
        })

    lineage_chains: List[Dict[str, Any]] = []
    lineage_ids: List[int] = []
    if include_lineage:
        seen_chains: set[tuple[int, ...]] = set()
        for memory_id in memory_ids:
            record = get_memory(conn, memory_id, follow="full_history")
            if not record:
                continue
            history = record.get("history") or [record]
            chain_ids = [m["id"] for m in history]
            if len(chain_ids) <= 1:
                continue
            chain_key = tuple(chain_ids)
            if chain_key in seen_chains:
                continue
            seen_chains.add(chain_key)
            lineage_ids.extend(chain_ids)
            lineage_chains.append({
                "ids": chain_ids,
                "active_ids": [mid for mid in chain_ids if mid in memory_ids],
                "memories": [
                    _digest_memory_preview(m, preview_chars)
                    for m in history
                ],
            })

    related_ids: List[int] = []
    related_edges: List[Dict[str, Any]] = []
    if include_related_hops > 0 and memory_ids:
        frontier = list(memory_ids)
        visited = set(memory_ids)
        related_seen: set[int] = set()
        for hop in range(1, min(include_related_hops, 3) + 1):
            next_frontier: List[int] = []
            for source_id in frontier:
                for ref in get_crossrefs(conn, source_id):
                    edge_type = ref.get("edge_type", "related_to")
                    if edge_type in {"supersedes", "superseded_by"}:
                        continue
                    try:
                        target_id = int(ref.get("id"))
                    except (TypeError, ValueError):
                        continue
                    target = get_memory(conn, target_id)
                    if not target:
                        continue
                    if target_id not in related_seen:
                        related_seen.add(target_id)
                        related_ids.append(target_id)
                    if target_id not in visited:
                        visited.add(target_id)
                        next_frontier.append(target_id)
                    related_edges.append({
                        "from": source_id,
                        "to": target_id,
                        "hop": hop,
                        "edge_type": edge_type,
                        "score": ref.get("score"),
                    })
            frontier = next_frontier
            if not frontier:
                break

    todos: List[Dict[str, Any]] = []
    issues: List[Dict[str, Any]] = []
    if include_todos:
        todo_seen: set[int] = set()
        issue_seen: set[int] = set()
        for memory in active_memories:
            if _active_item_matches_bucket(memory, "memora/todos"):
                _append_digest_memory(todos, todo_seen, memory, preview_chars, source="active_hit")
            if _active_item_matches_bucket(memory, "memora/issues"):
                _append_digest_memory(issues, issue_seen, memory, preview_chars, source="active_hit")
        for item in _list_digest_bucket(
            conn,
            topic,
            "memora/todos",
            k,
            metadata_filters,
            date_from,
            date_to,
            tags_any,
            tags_all,
        ):
            _append_digest_memory(todos, todo_seen, item, preview_chars, source="topic_match")
        for item in _list_digest_bucket(
            conn,
            topic,
            "memora/issues",
            k,
            metadata_filters,
            date_from,
            date_to,
            tags_any,
            tags_all,
        ):
            _append_digest_memory(issues, issue_seen, item, preview_chars, source="topic_match")

    source_ids = _dedupe_preserve_order(
        memory_ids
        + lineage_ids
        + related_ids
        + [item["id"] for item in todos]
        + [item["id"] for item in issues]
    )

    digest = {
        "topic": topic,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "memory_ids": memory_ids,
        "memories": memories,
        "lineage_chains": lineage_chains,
        "related_ids": _dedupe_preserve_order(related_ids),
        "related_edges": related_edges,
        "todos": todos,
        "issues": issues,
        "source_ids": source_ids,
    }
    if debug:
        digest["debug"] = {
            "ranked_candidates": ranked_candidates,
            "applied_filters": {
                "tags_any": tags_any,
                "tags_all": tags_all,
                "metadata_filters": metadata_filters,
                "date_from": date_from,
                "date_to": date_to,
            },
            "filter_note": (
                "Hybrid search and separately discovered TODO/issue matches honor filters; "
                "seed_ids are included explicitly and bypass filters."
            ),
            "seed_ids": seed_ids or [],
            "missing_seed_ids": missing_seed_ids,
        }
    return digest


@_with_connection(writes=True)
def _rebuild_embeddings(conn):
    return rebuild_embeddings(conn)


@_with_connection
def _get_statistics(conn):
    return get_statistics(conn)


@_with_connection
def _generate_insights(conn, period: str, stale_days: int, include_llm_analysis: bool):
    return generate_insights(conn, period, stale_days, include_llm_analysis)


@_with_connection
def _export_memories(conn):
    return export_memories(conn)


@_with_connection(writes=True)
def _import_memories(conn, data: List[Dict[str, Any]], strategy: str):
    return import_memories(conn, data, strategy)


@mcp.tool()
async def memory_create(
    content: str,
    metadata: Optional[Dict[str, Any]] = None,
    tags: Optional[list[str]] = None,
    suggest_similar: bool = True,
    similarity_threshold: float = 0.2,
    response_mode: Literal["full", "minimal"] = "full",
) -> Dict[str, Any]:
    """Create a new memory entry.

    Args:
        content: The memory content text
        metadata: Optional metadata dictionary
        tags: Optional list of tags
        suggest_similar: If True, find similar memories and suggest consolidation (default: True)
        similarity_threshold: Minimum similarity score for suggestions (default: 0.2)
        response_mode: "full" (default) or "minimal" response payload size
    """
    if response_mode not in CREATE_RESPONSE_MODES:
        valid = ", ".join(sorted(CREATE_RESPONSE_MODES))
        return {
            "error": "invalid_input",
            "message": f"response_mode must be one of: {valid}",
        }

    # Check hierarchy path BEFORE creating to detect new paths
    new_path = extract_hierarchy_path(metadata)
    existing_paths = (
        await _get_hierarchy_paths()
        if new_path
        else []
    )
    path_is_new = bool(new_path) and (new_path not in existing_paths)

    # Initialize warnings dict
    warnings: Dict[str, Any] = {}

    # Auto-redact secrets/PII from content BEFORE saving
    redacted_content = content.strip()
    try:
        redacted_content, secrets_redacted = _redact_secrets(redacted_content)
        if secrets_redacted:
            warnings["secrets_redacted"] = secrets_redacted
    except Exception as exc:
        logger.warning("Secret redaction failed, storing original content: %s", exc)

    try:
        record = await _create_memory(content=redacted_content, metadata=metadata, tags=tags or [])
    except ValueError as exc:
        return {"error": "invalid_input", "message": str(exc)}

    result: Dict[str, Any] = {"memory": record}

    # Warn if a new hierarchy path was created and suggest similar existing paths
    if path_is_new:
        similar = find_similar_paths(new_path, existing_paths)
        if similar:
            warnings["new_hierarchy_path"] = f"New hierarchy path created: {new_path}"
            result["existing_similar_paths"] = similar
            result["hint"] = "Did you mean to use one of these existing paths? Use memory_update to change if needed."

    # Use cross-refs (related memories) for consolidation hints and duplicate detection
    # Cross-refs use full embedding context (content + metadata + tags) so are more accurate
    related_memories = record.get("related", []) if record else []
    if suggest_similar and related_memories:
        # Filter by threshold
        above_threshold = [m for m in related_memories if m and m.get("score", 0) >= similarity_threshold]
        if above_threshold:
            result["similar_memories"] = above_threshold
            result["consolidation_hint"] = (
                f"Found {len(above_threshold)} similar memories. "
                "Consider: (1) merge content with memory_update, or (2) delete redundant ones with memory_delete."
            )
            # Check for potential duplicates (>0.85 similarity)
            duplicates = [m for m in above_threshold if m.get("score", 0) >= DUPLICATE_THRESHOLD]
            if duplicates:
                warnings["duplicate_warning"] = (
                    f"Very similar memory exists (>={int(DUPLICATE_THRESHOLD*100)}% match). "
                    f"Memory #{duplicates[0]['id']} has {int(duplicates[0]['score']*100)}% similarity."
                )

    # Add warnings to result if any
    if warnings:
        result["warnings"] = warnings

    # Infer type and suggest tags (only if user didn't provide tags)
    try:
        suggestions: Dict[str, Any] = {}
        inferred_type = _infer_type(redacted_content)
        if inferred_type:
            suggestions["type"] = inferred_type

        suggested_tags = _suggest_tags(redacted_content, inferred_type)
        # Only suggest tags not already applied
        existing_tags = set(tags or [])
        new_suggestions = [t for t in suggested_tags if t not in existing_tags]
        if new_suggestions:
            suggestions["tags"] = new_suggestions

        # Suggest hierarchy placement based on related memories (cross-refs)
        # (only if user didn't provide a hierarchy path)
        if not new_path and related_memories:
            related_ids = [m["id"] for m in related_memories if m.get("id") is not None]
            metadata_batch = await _get_memories_metadata_batch(related_ids) if related_ids else {}
            hierarchy_suggestions = suggest_hierarchy_from_similar(
                related_memories,
                metadata_by_id=metadata_batch,
            )
            if hierarchy_suggestions:
                top = hierarchy_suggestions[0]
                if top.get("confidence", 0) >= AUTO_HIERARCHY_THRESHOLD:
                    # Auto-apply the top hierarchy suggestion
                    auto_meta = {}
                    if top.get("section"):
                        auto_meta["section"] = top["section"]
                    if top.get("subsection"):
                        auto_meta["subsection"] = top["subsection"]
                    if auto_meta:
                        memory_id = record.get("id") if record else None
                        if memory_id is not None:
                            await _update_memory(memory_id, None, auto_meta, None)
                            result["auto_hierarchy"] = {
                                "path": top["path"],
                                "section": top.get("section"),
                                "subsection": top.get("subsection"),
                                "confidence": top["confidence"],
                                "source_memory_ids": top.get("similar_memory_ids", []),
                            }
                            result["auto_hierarchy_hint"] = (
                                f"Auto-assigned hierarchy '{'/'.join(top['path'])}' "
                                f"(confidence: {top['confidence']}) based on similar memories. "
                                "Use memory_update to change if needed."
                            )
                else:
                    # Below threshold — suggest but don't apply
                    suggestions["hierarchy"] = hierarchy_suggestions
                    suggestions["hierarchy_hint"] = (
                        "Similar memories are organized under these paths. "
                        "Use memory_update to add section/subsection metadata."
                    )

        if suggestions:
            result["suggestions"] = suggestions
    except Exception as exc:
        logger.warning(
            "Memory suggestion pipeline failed for memory id=%s: %s",
            (record or {}).get("id"),
            exc,
        )

    if response_mode == "minimal":
        minimal_result: Dict[str, Any] = {"memory": {"id": result["memory"]["id"]}}
        if "similar_memories" in result:
            minimal_result["similar_memories"] = result["similar_memories"]
        if "consolidation_hint" in result:
            minimal_result["consolidation_hint"] = result["consolidation_hint"]
        duplicate_warning = result.get("warnings", {}).get("duplicate_warning")
        if duplicate_warning:
            minimal_result["warnings"] = {"duplicate_warning": duplicate_warning}

        _schedule_cloud_graph_sync()
        return minimal_result

    _schedule_cloud_graph_sync()
    return result


@mcp.tool()
async def memory_create_issue(
    content: str,
    status: str = "open",
    closed_reason: Optional[str] = None,
    severity: str = "minor",
    component: Optional[str] = None,
    category: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new issue/bug memory.

    Args:
        content: Description of the issue
        status: Issue status - "open" (default) or "closed"
        closed_reason: If closed, the reason - "complete" or "not_planned"
        severity: Issue severity - "critical", "major", "minor" (default)
        component: Component/area affected (e.g., "graph", "storage", "api")
        category: Issue category (e.g., "bug", "enhancement", "performance")

    Returns:
        Created issue memory with auto-assigned tag "memora/issues"
    """
    # Validate status
    valid_statuses = {"open", "closed"}
    if status not in valid_statuses:
        return {"error": "invalid_status", "message": f"Status must be one of: {', '.join(valid_statuses)}"}

    # Validate closed_reason if status is closed
    if status == "closed":
        valid_reasons = {"complete", "not_planned"}
        if not closed_reason:
            return {"error": "missing_closed_reason", "message": "closed_reason required when status is 'closed'"}
        if closed_reason not in valid_reasons:
            return {"error": "invalid_closed_reason", "message": f"closed_reason must be one of: {', '.join(valid_reasons)}"}

    # Validate severity
    valid_severities = {"critical", "major", "minor"}
    if severity not in valid_severities:
        return {"error": "invalid_severity", "message": f"Severity must be one of: {', '.join(valid_severities)}"}

    # Build metadata
    metadata: Dict[str, Any] = {
        "type": "issue",
        "status": status,
        "severity": severity,
    }
    if closed_reason:
        metadata["closed_reason"] = closed_reason
    if component:
        metadata["component"] = component
    if category:
        metadata["category"] = category

    # Create with auto-tag
    tags = ["memora/issues"]

    try:
        record = await _create_memory(content.strip(), metadata, tags)
    except ValueError as exc:
        return {"error": "invalid_input", "message": str(exc)}

    _schedule_cloud_graph_sync()
    return {"memory": record}


@mcp.tool()
async def memory_create_todo(
    content: str,
    status: str = "open",
    closed_reason: Optional[str] = None,
    priority: str = "medium",
    category: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new TODO/task memory.

    Args:
        content: Description of the task
        status: Task status - "open" (default) or "closed"
        closed_reason: If closed, the reason - "complete" or "not_planned"
        priority: Task priority - "high", "medium" (default), "low"
        category: Task category (e.g., "cloud-backend", "graph-visualization", "docs")

    Returns:
        Created TODO memory with auto-assigned tag "memora/todos"
    """
    # Validate status
    valid_statuses = {"open", "closed"}
    if status not in valid_statuses:
        return {"error": "invalid_status", "message": f"Status must be one of: {', '.join(valid_statuses)}"}

    # Validate closed_reason if status is closed
    if status == "closed":
        valid_reasons = {"complete", "not_planned"}
        if not closed_reason:
            return {"error": "missing_closed_reason", "message": "closed_reason required when status is 'closed'"}
        if closed_reason not in valid_reasons:
            return {"error": "invalid_closed_reason", "message": f"closed_reason must be one of: {', '.join(valid_reasons)}"}

    # Validate priority
    valid_priorities = {"high", "medium", "low"}
    if priority not in valid_priorities:
        return {"error": "invalid_priority", "message": f"Priority must be one of: {', '.join(valid_priorities)}"}

    # Build metadata
    metadata: Dict[str, Any] = {
        "type": "todo",
        "status": status,
        "priority": priority,
    }
    if closed_reason:
        metadata["closed_reason"] = closed_reason
    if category:
        metadata["category"] = category

    # Create with auto-tag
    tags = ["memora/todos"]

    try:
        record = await _create_memory(content.strip(), metadata, tags)
    except ValueError as exc:
        return {"error": "invalid_input", "message": str(exc)}

    _schedule_cloud_graph_sync()
    return {"memory": record}


@mcp.tool()
async def memory_create_section(
    content: str,
    section: Optional[str] = None,
    subsection: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new section/subsection header memory.

    Section memories are organizational placeholders that:
    - Are NOT visible in the graph visualization
    - Are NOT included in duplicate detection
    - Do NOT compute embeddings or cross-references

    Args:
        content: Title/description of the section
        section: Parent section name (e.g., "Architecture", "API")
        subsection: Subsection path (e.g., "endpoints/auth")

    Returns:
        Created section memory with auto-assigned tag "memora/sections"
    """
    # Build metadata
    metadata: Dict[str, Any] = {
        "type": "section",
    }
    if section:
        metadata["section"] = section
    if subsection:
        metadata["subsection"] = subsection

    # Create with auto-tag
    tags = ["memora/sections"]

    try:
        record = await _create_memory(content.strip(), metadata, tags)
    except ValueError as exc:
        return {"error": "invalid_input", "message": str(exc)}

    _schedule_cloud_graph_sync()
    return {"memory": record}


_FIELDS_UNIVERSAL = frozenset({
    "id", "content", "content_preview", "tags", "created_at", "updated_at",
    "metadata", "importance", "importance_score", "last_accessed", "access_count",
})
_FIELDS_SEARCH_ONLY = frozenset({"score"})
_FIELDS_ALL = _FIELDS_UNIVERSAL | _FIELDS_SEARCH_ONLY


def _project_fields(
    memory_dict: Dict[str, Any],
    fields: Optional[List[str]],
    *,
    is_search: bool = False,
) -> Dict[str, Any]:
    """Project a serialised memory dict to only the requested fields.

    ``id`` is always included. Unknown field names raise ValueError.
    ``score`` is rejected for non-search tools.
    """
    if not fields:
        return memory_dict

    requested = set(fields)
    allowed = _FIELDS_ALL if is_search else _FIELDS_UNIVERSAL
    unknown = requested - allowed
    if unknown:
        # Check if it's a search-only field used on a non-search tool
        search_only_misuse = unknown & _FIELDS_SEARCH_ONLY
        if search_only_misuse and not is_search:
            raise ValueError(
                f"Field(s) {sorted(search_only_misuse)} are only available on search tools, "
                f"not on memory_list or memory_get"
            )
        truly_unknown = unknown - _FIELDS_SEARCH_ONLY
        if truly_unknown:
            raise ValueError(f"Unknown field(s): {sorted(truly_unknown)}")

    # Always include id
    requested.add("id")

    # Forgiving content auto-promotion: if caller asked for the "other" content
    # variant that wasn't materialised, swap in the one that exists.
    has_content = "content" in memory_dict
    has_preview = "content_preview" in memory_dict
    warning = None

    if "content" in requested and not has_content and has_preview:
        requested.discard("content")
        requested.add("content_preview")
        warning = "fields requested 'content' but content_mode=preview; returned content_preview instead"
    elif "content_preview" in requested and not has_preview and has_content:
        requested.discard("content_preview")
        requested.add("content")
        warning = "fields requested 'content_preview' but content_mode=full; returned content instead"

    projected = {k: v for k, v in memory_dict.items() if k in requested}
    if warning:
        projected["_field_warning"] = warning
    return projected


def _apply_content_projection(
    items: List[Dict[str, Any]],
    content_mode: str = "preview",
    preview_chars: int = 200,
) -> List[Dict[str, Any]]:
    """Project content fields on already-serialised memory dicts.

    Applied at the **tool boundary** — ``_serialise_row`` always keeps full
    ``content`` so internal code (``_search_by_vector``, crossref scan) still
    works.

    When ``content_mode="preview"`` the ``content`` key is replaced by
    ``content_preview`` (first ``preview_chars`` chars + trailing ``…`` if
    truncated).  When ``content_mode="full"`` the row is returned unchanged.
    """
    if content_mode == "full":
        return items

    projected: List[Dict[str, Any]] = []
    for item in items:
        row = dict(item)  # shallow copy
        full = row.pop("content", "") or ""
        if len(full) > preview_chars:
            row["content_preview"] = full[:preview_chars] + "…"
        else:
            row["content_preview"] = full
        projected.append(row)
    return projected


@mcp.tool()
async def memory_list(
    query: Optional[str] = None,
    metadata_filters: Optional[Dict[str, Any]] = None,
    limit: Optional[int] = 20,
    offset: Optional[int] = 0,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    tags_any: Optional[List[str]] = None,
    tags_all: Optional[List[str]] = None,
    tags_none: Optional[List[str]] = None,
    sort_by_importance: bool = False,
    content_mode: str = "preview",
    preview_chars: int = 120,
    fields: Optional[List[str]] = None,
    follow: Optional[str] = None,
) -> Dict[str, Any]:
    """List memories, optionally filtering by substring query or metadata.

    Returns compact previews by default to reduce context usage.
    Use ``content_mode="full"`` when you need the complete content.
    Use ``memory_get`` to fetch full content for specific IDs.

    Args:
        query: Optional text search query
        metadata_filters: Optional metadata filters
        limit: Maximum results (default: 20). Pass -1 for unlimited.
        offset: Number of filtered results to skip (default: 0)
        date_from: Optional date filter (ISO format or relative like "7d", "1m", "1y")
        date_to: Optional date filter (ISO format or relative like "7d", "1m", "1y")
        tags_any: Match memories with ANY of these tags (OR logic)
        tags_all: Match memories with ALL of these tags (AND logic)
        tags_none: Exclude memories with ANY of these tags (NOT logic)
        sort_by_importance: Sort results by importance score (default: False, sorts by date)
        content_mode: "preview" (default) returns truncated content_preview; "full" returns complete content
        preview_chars: Max chars for preview (default: 120, ignored when content_mode="full")
        fields: Optional list of fields to return (e.g. ["id","content_preview","tags"]). None returns all fields.
        follow: Lineage mode. Default ``active`` (excludes superseded memories).
                ``latest`` resolves each hit to its current version;
                ``full_history`` expands supersession chains;
                ``all`` is the explicit unfiltered forensic escape hatch (includes superseded).
                Omitting follow is NOT unfiltered — it means the safe default.
    """
    try:
        effective_follow = resolve_follow(follow, default=DEFAULT_FOLLOW_LIST)
    except ValueError as exc:
        return {"error": "invalid_follow", "message": str(exc)}
    try:
        items = await _list_memories(
            query, metadata_filters, limit, offset,
            date_from, date_to, tags_any, tags_all, tags_none,
            sort_by_importance,
            follow=effective_follow,
        )
    except ValueError as exc:
        return {"error": "invalid_filters", "message": str(exc)}
    items = _apply_content_projection(items, content_mode, preview_chars)
    response: Dict[str, Any] = {"count": len(items)}
    if fields:
        try:
            items = [_project_fields(item, fields, is_search=False) for item in items]
        except ValueError as exc:
            return {"error": "invalid_fields", "message": str(exc)}
        # Hoist per-item warnings to envelope level and remove from items
        warnings = set()
        for item in items:
            w = item.pop("_field_warning", None)
            if w:
                warnings.add(w)
        if warnings:
            response["warning"] = "; ".join(sorted(warnings))
    response["memories"] = items
    return response


@mcp.tool()
async def memory_list_compact(
    query: Optional[str] = None,
    metadata_filters: Optional[Dict[str, Any]] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = 0,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    tags_any: Optional[List[str]] = None,
    tags_all: Optional[List[str]] = None,
    tags_none: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """[Deprecated] List memories in compact format (id, preview, tags only).

    Prefer ``memory_list`` which now defaults to compact previews with richer
    fields and configurable ``content_mode``/``preview_chars``.

    Returns minimal fields: id, content preview (first 80 chars), tags, and created_at.

    Args:
        query: Optional text search query
        metadata_filters: Optional metadata filters
        limit: Maximum number of results to return (default: unlimited)
        offset: Number of results to skip (default: 0)
        date_from: Optional date filter (ISO format or relative like "7d", "1m", "1y")
        date_to: Optional date filter (ISO format or relative like "7d", "1m", "1y")
        tags_any: Match memories with ANY of these tags (OR logic)
        tags_all: Match memories with ALL of these tags (AND logic)
        tags_none: Exclude memories with ANY of these tags (NOT logic)
    """
    try:
        items = await _list_memories(query, metadata_filters, limit, offset, date_from, date_to, tags_any, tags_all, tags_none)
    except ValueError as exc:
        return {"error": "invalid_filters", "message": str(exc)}

    # Convert to compact format
    compact_items = []
    for item in items:
        content = item.get("content", "")
        preview = content[:80] + "..." if len(content) > 80 else content
        compact_items.append({
            "id": item["id"],
            "preview": preview,
            "tags": item.get("tags", []),
            "created_at": item.get("created_at"),
        })

    return {"count": len(compact_items), "memories": compact_items}


@mcp.tool()
async def memory_create_batch(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Create multiple memories in one call."""
    try:
        records = await _create_memories(entries)
    except ValueError as exc:
        return {"error": "invalid_batch", "message": str(exc)}
    _schedule_cloud_graph_sync()
    return {"count": len(records), "memories": records}


@mcp.tool()
async def memory_delete_batch(ids: List[int], reason: Optional[str] = None) -> Dict[str, Any]:
    """Delete multiple memories by id.

    Args:
        ids: Memory IDs to delete
        reason: Optional tombstone reason (default "deleted")
    """
    deleted = await _delete_memories(ids, reason=reason)
    _schedule_cloud_graph_sync()
    return {"deleted": deleted}


@mcp.tool()
async def memory_absorb(
    facts: List[str],
    source: str = "manual",
    confidence: float = 0.8,
    context: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    tags: Optional[list[str]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Intelligently absorb facts into memory with dedup and consolidation.

    For each fact: searches for similar existing memories, classifies the
    relationship via LLM (duplicate/update/contradict/related/new), then
    takes the appropriate action. Related new facts are automatically
    consolidated into single, richer memories via LLM synthesis.

    Args:
        facts: List of fact strings to absorb (can be granular — related ones get merged)
        source: Origin of facts — "manual", "session_end", "post_tool", "import"
        confidence: Caller's certainty about these facts (0.0-1.0, default: 0.8)
        context: Optional surrounding context to help disambiguate facts
        metadata: Optional metadata to attach to created memories
        tags: Optional tags to attach to created memories
        dry_run: If True, preview what would happen without writing anything
    """
    if not facts:
        return {"error": "invalid_input", "message": "facts list is empty"}
    if len(facts) > 20:
        return {"error": "invalid_input", "message": "max 20 facts per call"}

    try:
        result = await _absorb_memory(
            facts, source, confidence, context, metadata, tags, dry_run,
        )
    except ValueError as exc:
        return {"error": "invalid_input", "message": str(exc)}
    except Exception as exc:
        logger.error("memory_absorb failed: %s", exc, exc_info=True)
        return _safe_error(exc, "memory_absorb")

    # Never schedule graph sync on partial/error absorb (P1-2).
    if result.get("partial") or result.get("error"):
        return result
    wrote = result.get("created", 0) + result.get("superseded", 0) + result.get("contradicted", 0) + result.get("linked", 0)
    if not dry_run and wrote > 0:
        _schedule_cloud_graph_sync()

    return result


@mcp.tool()
async def memory_store_document(
    content: str,
    document_key: str,
    version: int = 1,
    tags: Optional[list[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    skip_fragment_crossrefs: bool = True,
) -> Dict[str, Any]:
    """Store a structured document as a root memory + searchable fragments.

    Parses markdown into typed fragments (claims, plan items, references,
    risks, section chunks) that are individually searchable while the full
    document remains retrievable as a unit.

    Args:
        content: Full markdown document content
        document_key: Stable identifier (e.g. "research/memora-enhancements-2026-04-08")
        version: Document version (default: 1). If >1, supersedes previous version.
        tags: Tags applied to root and fragments
        metadata: Additional metadata merged into root and fragments
        skip_fragment_crossrefs: If True, fragments skip crossref computation (default: True)

    Returns:
        {document_key, root_id, fragment_count, node_map: {node_kind: [ids]}}
    """
    from .document import parse_document

    try:
        plan = parse_document(
            content, document_key, version=version,
            tags=tags, metadata=metadata,
            skip_fragment_crossrefs=skip_fragment_crossrefs,
        )
    except Exception as exc:
        return {"error": "parse_error", "message": str(exc)}

    # 1. Create document root
    try:
        root = await _create_memory(
            content=plan.root_content,
            metadata=plan.root_metadata,
            tags=plan.root_tags,
        )
    except ValueError as exc:
        return {"error": "invalid_input", "message": f"Root creation failed: {exc}"}

    root_id = root["id"]

    # 2. Create fragments via batch insert
    fragment_entries = []
    for frag in plan.fragments:
        entry: Dict[str, Any] = {
            "content": frag.content,
            "metadata": frag.metadata,
            "tags": list(plan.root_tags),
        }
        fragment_entries.append(entry)

    fragment_ids: List[int] = []
    node_map: Dict[str, List[int]] = {}

    if fragment_entries:
        try:
            records = await _create_memories(fragment_entries)
        except ValueError as exc:
            return {
                "error": "fragment_error",
                "message": str(exc),
                "root_id": root_id,
                "partial": True,
            }

        # 3. Link fragments to root and build node_map
        for record, frag in zip(records, plan.fragments):
            fid = record["id"]
            fragment_ids.append(fid)
            node_map.setdefault(frag.node_kind, []).append(fid)

            # Link fragment → root
            try:
                await _add_link(fid, root_id, "extends", bidirectional=True)
            except Exception:
                pass  # non-fatal — link failure shouldn't block storage

        # 4. Link claims to references by matching URLs
        await _link_claims_to_references(records, plan.fragments, node_map)

    # 5. If version > 1, find and supersede previous root
    if version > 1:
        await _supersede_previous_version(root_id, document_key, version)

    _schedule_cloud_graph_sync()

    return {
        "document_key": document_key,
        "version": version,
        "root_id": root_id,
        "fragment_count": len(fragment_ids),
        "node_map": node_map,
    }


async def _link_claims_to_references(
    records: List[Dict[str, Any]],
    fragments: list,
    node_map: Dict[str, List[int]],
) -> None:
    """Link claim memories to reference memories by matching source URLs."""
    ref_ids = node_map.get("reference", [])
    if not ref_ids:
        return

    # Build URL → ref_id map
    url_to_ref: Dict[str, int] = {}
    for record, frag in zip(records, fragments):
        if frag.node_kind == "reference":
            for url in frag.metadata.get("source_urls", []):
                url_to_ref[url] = record["id"]

    # Link claims that mention reference URLs
    for record, frag in zip(records, fragments):
        if frag.node_kind != "claim":
            continue
        content_lower = frag.content.lower()
        for url, ref_id in url_to_ref.items():
            if url.lower() in content_lower:
                try:
                    await _add_link(record["id"], ref_id, "references", bidirectional=True)
                except Exception:
                    pass


async def _supersede_previous_version(
    new_root_id: int,
    document_key: str,
    new_version: int,
) -> None:
    """Find the immediate predecessor root and create a supersedes link (chain, not fan-out)."""
    try:
        results = await _list_memories(
            query=None,
            metadata_filters={"document_key": document_key, "type": "document_root"},
            limit=50, offset=0,
            date_from=None, date_to=None,
            tags_any=None, tags_all=None, tags_none=None,
            sort_by_importance=False,
        )
        # Find the immediate predecessor (highest version < new_version)
        predecessor_id = None
        predecessor_version = -1
        for mem in results:
            meta = mem.get("metadata", {})
            v = meta.get("document_version", 0)
            if v < new_version and v > predecessor_version and mem["id"] != new_root_id:
                predecessor_id = mem["id"]
                predecessor_version = v
        if predecessor_id is not None:
            await _add_link(new_root_id, predecessor_id, "supersedes", bidirectional=True)
    except Exception:
        pass  # non-fatal


@mcp.tool()
async def memory_get_document(
    document_key: str,
    content_mode: str = "preview",
    preview_chars: int = 120,
    node_kinds: Optional[List[str]] = None,
    version: Optional[int] = None,
) -> Dict[str, Any]:
    """Retrieve a stored document and its fragments by document key.

    Args:
        document_key: The document identifier used during storage
        content_mode: "preview" (default) or "full" for fragment content
        preview_chars: Max chars for preview mode (default: 120)
        node_kinds: Optional filter — e.g. ["claim", "plan_item"] for specific fragment types
        version: Optional version filter. If omitted, returns the latest version.

    Returns:
        {root: {...}, fragments: [...] ordered by ordinal, document_key, version}
    """
    filters: Dict[str, Any] = {"document_key": document_key}
    if version is not None:
        filters["document_version"] = version

    # Don't use follow="active" — it would hide superseded roots,
    # breaking historical version retrieval. Filter manually instead.
    results = await _list_memories(
        query=None,
        metadata_filters=filters,
        limit=-1, offset=0,
        date_from=None, date_to=None,
        tags_any=None, tags_all=None, tags_none=None,
        sort_by_importance=False,
    )
    memories = results if isinstance(results, list) else results.get("memories", [])
    if not memories:
        return {"error": "not_found", "message": f"No document found with key '{document_key}'"}

    # Separate root from fragments
    root = None
    fragments = []
    for mem in memories:
        meta = mem.get("metadata", {})
        if meta.get("type") == "document_root":
            # If no version specified, pick the highest version root
            if root is None:
                root = mem
            elif meta.get("document_version", 0) > root.get("metadata", {}).get("document_version", 0):
                root = mem
        elif meta.get("type") == "document_fragment":
            fragments.append(mem)

    if root is None:
        return {"error": "not_found", "message": f"No document root found for key '{document_key}'"}

    # Filter fragments by version (match root's version)
    root_version = root.get("metadata", {}).get("document_version", 1)
    fragments = [
        f for f in fragments
        if f.get("metadata", {}).get("document_version", 1) == root_version
    ]

    # Filter by node_kinds if specified
    if node_kinds:
        fragments = [
            f for f in fragments
            if f.get("metadata", {}).get("node_kind") in node_kinds
        ]

    # Sort by ordinal
    fragments.sort(key=lambda f: f.get("metadata", {}).get("ordinal", 0))

    # Apply content mode
    if content_mode == "preview":
        for frag in fragments:
            if "content" in frag and len(frag["content"]) > preview_chars:
                frag["content_preview"] = frag["content"][:preview_chars] + "..."
                del frag["content"]
            elif "content" in frag:
                frag["content_preview"] = frag["content"]
                del frag["content"]
        # Root always returns full content for document retrieval
    elif content_mode != "full":
        return {"error": "invalid_input", "message": f"content_mode must be 'preview' or 'full'"}

    return {
        "document_key": document_key,
        "version": root_version,
        "root": root,
        "fragments": fragments,
        "fragment_count": len(fragments),
    }


@mcp.tool()
async def memory_delete_document(
    document_key: str,
    version: Optional[int] = None,
) -> Dict[str, Any]:
    """Delete a stored document and all its fragments.

    Args:
        document_key: The document identifier
        version: Optional — delete only this version. If omitted, deletes all versions.

    Returns:
        {deleted_roots: count, deleted_fragments: count, deleted_ids: [...]}
    """
    filters: Dict[str, Any] = {"document_key": document_key}
    if version is not None:
        filters["document_version"] = version

    results = await _list_memories(
        query=None,
        metadata_filters=filters,
        limit=-1, offset=0,
        date_from=None, date_to=None,
        tags_any=None, tags_all=None, tags_none=None,
        sort_by_importance=False,
    )
    memories = results if isinstance(results, list) else results.get("memories", [])
    if not memories:
        return {"error": "not_found", "message": f"No document found with key '{document_key}'"}

    # Only delete memories that are actually document roots or fragments
    memories = [
        m for m in memories
        if m.get("metadata", {}).get("type") in ("document_root", "document_fragment")
    ]
    if not memories:
        return {"error": "not_found", "message": f"No document memories found for key '{document_key}'"}

    ids = [m["id"] for m in memories]
    root_count = sum(
        1 for m in memories
        if m.get("metadata", {}).get("type") == "document_root"
    )
    fragment_count = len(ids) - root_count

    deleted = await _delete_memories(ids)
    _schedule_cloud_graph_sync()

    return {
        "deleted_roots": root_count,
        "deleted_fragments": fragment_count,
        "deleted_ids": ids,
        "total_deleted": deleted,
    }


def _apply_search_fields_projection(
    results: List[Dict[str, Any]],
    fields: List[str],
) -> tuple:
    """Project fields on search results ([{memory, score}, ...]).

    If ``"score"`` is in ``fields``, keep the ``{memory, score}`` envelope.
    Otherwise, flatten to a list of projected memory dicts (score dropped).

    Returns ``(projected_results, warning_or_none)``.
    """
    requested = set(fields)
    include_score = "score" in requested

    warnings = set()
    projected = []
    for entry in results:
        memory = entry.get("memory", entry)
        mem_projected = _project_fields(memory, fields, is_search=True)
        w = mem_projected.pop("_field_warning", None)
        if w:
            warnings.add(w)
        if include_score:
            projected.append({"memory": mem_projected, "score": entry.get("score")})
        else:
            projected.append(mem_projected)
    warning = "; ".join(sorted(warnings)) if warnings else None
    return projected, warning


@mcp.tool()
async def memory_get(
    memory_id: int,
    include_images: bool = False,
    fields: Optional[List[str]] = None,
    follow: Optional[str] = None,
) -> Dict[str, Any]:
    """Retrieve a single memory by id (full content by default).

    Args:
        memory_id: ID of the memory to retrieve
        include_images: If False, strip image data from metadata to reduce response size
        fields: Optional list of fields to return (e.g. ["id","content","tags"]). None returns all fields.
        follow: Lineage mode. Default ``latest`` (resolve superseded id to the current leaf).
                ``full_history`` adds a ``history`` key with all versions root-to-leaf;
                ``all`` returns the exact requested id with no chain walk (forensic).
                Omitting follow is NOT unfiltered — it means resolve to latest.
    """
    try:
        effective_follow = resolve_follow(follow, default=DEFAULT_FOLLOW_GET, for_get=True)
        record = await _get_memory(memory_id, follow=effective_follow)
    except ValueError as exc:
        return {"error": "invalid_follow", "message": str(exc)}
    if not record:
        return {"error": "not_found", "id": memory_id}

    def _strip_images(mem: Dict) -> None:
        meta = mem.get("metadata") or {}
        if meta.get("images"):
            mem["metadata"]["images"] = [
                {"caption": img.get("caption", "")} for img in meta["images"]
            ]

    if not include_images:
        _strip_images(record)
        # Also strip images from history entries
        for hist_mem in record.get("history", []):
            _strip_images(hist_mem)

    if fields:
        try:
            record = _project_fields(record, fields, is_search=False)
        except ValueError as exc:
            return {"error": "invalid_fields", "message": str(exc)}

    response: Dict[str, Any] = {"memory": record}
    w = record.pop("_field_warning", None) if isinstance(record, dict) else None
    if w:
        response["warning"] = w
    return response


@mcp.tool()
async def memory_update(
    memory_id: int,
    content: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    tags: Optional[list[str]] = None,
    replace_metadata: bool = False,
) -> Dict[str, Any]:
    """Update an existing memory.

    Metadata updates merge into existing metadata by default. Set a metadata
    key to null/None to delete that key. Pass replace_metadata=True only when
    intentionally replacing the whole metadata object.
    """
    try:
        record = await _update_memory(memory_id, content, metadata, tags, replace_metadata)
    except ValueError as exc:
        return {"error": "invalid_input", "message": str(exc)}
    if not record:
        return {"error": "not_found", "id": memory_id}
    _schedule_cloud_graph_sync()
    return {"memory": record}


@mcp.tool()
async def memory_delete(
    memory_id: int,
    force: bool = False,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Delete a memory by id.

    Args:
        memory_id: Memory ID to delete
        force: If True, allow deleting document fragments/roots.
               Use memory_delete_document() instead for clean document removal.
        reason: Optional tombstone reason (default "deleted")
    """
    if not force:
        mem = await _get_memory(memory_id)
        if mem and _is_doc_memory(mem.get("metadata")):
            doc_key = (mem.get("metadata") or {}).get("document_key", "unknown")
            return {
                "error": "protected_fragment",
                "message": (
                    f"Memory #{memory_id} is part of document '{doc_key}'. "
                    f"Use memory_delete_document() for clean removal, "
                    f"or pass force=True to delete this memory only."
                ),
            }

    if await _delete_memory(memory_id, reason=reason):
        _schedule_cloud_graph_sync()
        return {"status": "deleted", "id": memory_id}
    return {"error": "not_found", "id": memory_id}


@mcp.tool()
async def memory_tags() -> Dict[str, Any]:
    """Return the allowlisted tags."""
    from . import list_allowed_tags

    return {"allowed": list_allowed_tags()}


@mcp.tool()
async def memory_tag_hierarchy(include_root: bool = False) -> Dict[str, Any]:
    """Return stored tags organised as a namespace hierarchy."""

    tags = await _collect_tags()
    tree = build_tag_hierarchy(tags)
    if not include_root and isinstance(tree, dict):
        tree = tree.get("children", [])
    return {"count": len(tags), "hierarchy": tree}


@mcp.tool()
async def memory_validate_tags(include_memories: bool = True) -> Dict[str, Any]:
    """Validate stored tags against the allowlist and report invalid entries."""
    from . import list_allowed_tags

    invalid_full = await _find_invalid_tags()
    allowed = list_allowed_tags()
    existing = await _collect_tags()
    response: Dict[str, Any] = {"allowed": allowed, "existing": existing, "invalid_count": len(invalid_full)}
    if include_memories:
        response["invalid"] = invalid_full
    return response


@mcp.tool()
async def memory_hierarchy(
    query: Optional[str] = None,
    metadata_filters: Optional[Dict[str, Any]] = None,
    include_root: bool = False,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    tags_any: Optional[List[str]] = None,
    tags_all: Optional[List[str]] = None,
    tags_none: Optional[List[str]] = None,
    compact: bool = True,
) -> Dict[str, Any]:
    """Return memories organised into a hierarchy derived from their metadata.

    Args:
        compact: If True (default), return only id, preview (first 80 chars), and tags
                 per memory to reduce response size. Set to False for full memory data.
    """
    try:
        items = await _list_memories(query, metadata_filters, None, 0, date_from, date_to, tags_any, tags_all, tags_none)
    except ValueError as exc:
        return {"error": "invalid_filters", "message": str(exc)}

    hierarchy = build_hierarchy_tree(items, include_root=include_root, compact=compact)
    return {"count": len(items), "hierarchy": hierarchy}


@mcp.tool()
async def memory_verify_integrity() -> Dict[str, Any]:
    """Read-only embedding integrity doctor with bounded offending ids."""
    def _run() -> Dict[str, Any]:
        from .embeddings import get_embedding_integrity_status, verify_embedding_integrity
        conn = connect()
        try:
            audit = verify_embedding_integrity(conn, stamp=False)
            status = get_embedding_integrity_status(conn, os.getenv("MEMORA_EMBEDDING_MODEL", "tfidf"))
            inflight = list_absorb_inflight(conn)
            response = {
                "status": status,
                "audit": audit,
                "absorb_inflight": inflight,
            }
            if inflight["orphaned"]:
                response["absorb_inflight_remediation"] = (
                    "Expired absorb in-flight records own unreaped partial rows. "
                    "Automatic delete is disabled: an orphan is preferable to "
                    "silently deleting a slow-but-live absorb. Investigate the "
                    "owned_memory_ids and the writer; do not assume they are dead."
                )
            if status["mismatch"]:
                response["remediation"] = (
                    "Repair or remove named orphan/unknown writer rows, then run an explicit "
                    "embedding rebuild. External writers must populate representation, dimension, "
                    "encoding_source, and writer_token."
                )
            else:
                response["remediation"] = "no action needed"
            return response
        finally:
            conn.close()

    return await _in_worker(_run)


@mcp.tool()
async def memory_semantic_search(
    query: str,
    top_k: Optional[int] = None,
    limit: Optional[int] = None,
    metadata_filters: Optional[Dict[str, Any]] = None,
    min_score: Optional[float] = None,
    content_mode: str = "preview",
    preview_chars: int = 300,
    fields: Optional[List[str]] = None,
    follow: Optional[str] = None,
) -> Dict[str, Any]:
    """Perform a semantic search using vector embeddings.

    Returns compact previews by default. Use content_mode="full" for complete content.

    Args:
        query: Search query text
        top_k: Maximum number of results (legacy name; default 5 if neither limit nor top_k)
        limit: Alias for top_k — preferred name; honored so callers matching memory_list work
        metadata_filters: Optional metadata filters
        min_score: Minimum similarity score threshold
        content_mode: "preview" (default) returns truncated content_preview; "full" returns complete content
        preview_chars: Max chars for preview (default: 300, ignored when content_mode="full")
        fields: Optional list of fields to return. Include "score" to keep {memory, score} envelope;
                omit "score" for flat list of memory dicts.
        follow: Lineage mode. Default ``active`` (excludes superseded memories).
                ``latest`` / ``full_history`` as documented on memory_list;
                ``all`` is the explicit unfiltered forensic escape hatch.
                Omitting follow is NOT unfiltered — it means the safe default.
    """
    try:
        effective_follow = resolve_follow(follow, default=DEFAULT_FOLLOW_LIST)
    except ValueError as exc:
        return {"error": "invalid_follow", "message": str(exc)}

    cap = _resolve_search_cap(limit=limit, top_k=top_k, default=5)
    try:
        results = await _semantic_search(
            query,
            metadata_filters,
            cap,
            min_score,
            follow=effective_follow,
        )
    except ValueError as exc:
        return {"error": "invalid_filters", "message": str(exc)}
    except Exception as exc:
        if fault := _integrity_fault_response(exc):
            return fault
        raise
    # Project content at tool boundary — search results are [{score, memory}, ...]
    for entry in results:
        if "memory" in entry:
            [projected] = _apply_content_projection([entry["memory"]], content_mode, preview_chars)
            entry["memory"] = projected
    response: Dict[str, Any] = {"count": len(results)}
    if fields:
        try:
            results, warning = _apply_search_fields_projection(results, fields)
        except ValueError as exc:
            return {"error": "invalid_fields", "message": str(exc)}
        if warning:
            response["warning"] = warning
    response["results"] = results
    return response


@mcp.tool()
async def memory_hybrid_search(
    query: str,
    semantic_weight: float = 0.6,
    top_k: Optional[int] = None,
    limit: Optional[int] = None,
    min_score: float = 0.0,
    metadata_filters: Optional[Dict[str, Any]] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    tags_any: Optional[List[str]] = None,
    tags_all: Optional[List[str]] = None,
    tags_none: Optional[List[str]] = None,
    content_mode: str = "preview",
    preview_chars: int = 300,
    fields: Optional[List[str]] = None,
    follow: Optional[str] = None,
) -> Dict[str, Any]:
    """Perform a hybrid search combining keyword (FTS) and semantic (vector) search.

    Uses Reciprocal Rank Fusion (RRF) to merge results from both search methods,
    providing better results than either method alone.

    Returns compact previews by default. Use content_mode="full" for complete content.
    Use memory_get to fetch full content for specific IDs.

    Args:
        query: Search query text
        semantic_weight: Weight for semantic results (0-1). Higher values favor semantic similarity.
                        Keyword weight = 1 - semantic_weight. Default: 0.6 (60% semantic, 40% keyword)
        top_k: Maximum number of results (legacy name; default 10 if neither limit nor top_k)
        limit: Alias for top_k — preferred name; honored so callers matching memory_list work
        min_score: Minimum combined score threshold (default: 0.0)
        metadata_filters: Optional metadata filters
        date_from: Optional date filter (ISO format or relative like "7d", "1m", "1y")
        date_to: Optional date filter (ISO format or relative)
        tags_any: Match memories with ANY of these tags (OR logic)
        tags_all: Match memories with ALL of these tags (AND logic)
        tags_none: Exclude memories with ANY of these tags (NOT logic)
        content_mode: "preview" (default) returns truncated content_preview; "full" returns complete content
        preview_chars: Max chars for preview (default: 300, ignored when content_mode="full")
        fields: Optional list of fields to return. Include "score" to keep {memory, score} envelope;
                omit "score" for flat list of memory dicts.
        follow: Lineage mode. Default ``active`` (excludes superseded memories).
                ``latest`` / ``full_history`` as documented on memory_list;
                ``all`` is the explicit unfiltered forensic escape hatch.
                Omitting follow is NOT unfiltered — it means the safe default.

    Returns:
        Dictionary with count and list of results, each containing score and memory
    """
    try:
        effective_follow = resolve_follow(follow, default=DEFAULT_FOLLOW_LIST)
    except ValueError as exc:
        return {"error": "invalid_follow", "message": str(exc)}
    cap = _resolve_search_cap(limit=limit, top_k=top_k, default=10)
    try:
        results = await _hybrid_search(
            query,
            semantic_weight,
            cap,
            min_score,
            metadata_filters,
            date_from,
            date_to,
            tags_any,
            tags_all,
            tags_none,
            follow=effective_follow,
        )
    except ValueError as exc:
        return {"error": "invalid_filters", "message": str(exc)}
    except Exception as exc:
        if fault := _integrity_fault_response(exc):
            return fault
        raise
    # Project content at tool boundary — search results are [{score, memory}, ...]
    for entry in results:
        if "memory" in entry:
            [projected] = _apply_content_projection([entry["memory"]], content_mode, preview_chars)
            entry["memory"] = projected
    response: Dict[str, Any] = {"count": len(results)}
    if fields:
        try:
            results, warning = _apply_search_fields_projection(results, fields)
        except ValueError as exc:
            return {"error": "invalid_fields", "message": str(exc)}
        if warning:
            response["warning"] = warning
    response["results"] = results
    return response


@mcp.tool()
async def memory_digest(
    topic: str,
    k: int = 20,
    include_lineage: bool = True,
    include_todos: bool = True,
    include_related_hops: int = 1,
    synthesize: bool = False,
    preview_chars: int = 240,
    tags_any: Optional[List[str]] = None,
    tags_all: Optional[List[str]] = None,
    metadata_filters: Optional[Dict[str, Any]] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    seed_ids: Optional[List[int]] = None,
    debug: bool = False,
) -> Dict[str, Any]:
    """Return a deterministic digest of memories related to a topic.

    The digest is an aggregation surface for agents that need current context,
    not a narrative generator. It combines active hybrid-search hits, optional
    supersession lineage, related memory ids, and matching TODO/issue memories.
    Raw source ids are always returned so callers can inspect primitives if the
    digest is too broad or too narrow.

    Args:
        topic: Subject to digest.
        k: Maximum active search hits and TODO/issue matches to include.
        include_lineage: Include supersession history for active hits.
        include_todos: Include matching memora/todos and memora/issues entries.
        include_related_hops: Number of cross-reference hops to collect, capped at 3.
        synthesize: Reserved for future LLM synthesis. False by default.
        preview_chars: Preview length per returned memory.
        tags_any: Restrict hybrid search and discovered TODO/issue matches to memories with any of these tags.
        tags_all: Restrict hybrid search and discovered TODO/issue matches to memories with all of these tags.
        metadata_filters: Optional metadata filters.
        date_from: Optional created_at lower bound (ISO or relative like "7d").
        date_to: Optional created_at upper bound (ISO or relative like "7d").
        seed_ids: Explicit memory ids to include as source memories and expand lineage/related from.
        debug: Include ranked candidates, applied filters, and seed/filter notes.
    """
    topic = topic.strip()
    if not topic:
        return {"error": "invalid_input", "message": "topic must not be empty"}
    if k < 1:
        return {"error": "invalid_input", "message": "k must be >= 1"}
    if k > 100:
        return {"error": "invalid_input", "message": "k must be <= 100"}
    if include_related_hops < 0:
        return {"error": "invalid_input", "message": "include_related_hops must be >= 0"}
    if preview_chars < 40:
        return {"error": "invalid_input", "message": "preview_chars must be >= 40"}
    if seed_ids is not None:
        if len(seed_ids) > 100:
            return {"error": "invalid_input", "message": "seed_ids must contain <= 100 ids"}
        try:
            seed_ids = [int(memory_id) for memory_id in seed_ids]
        except (TypeError, ValueError):
            return {"error": "invalid_input", "message": "seed_ids must be a list of integer ids"}
        if any(memory_id < 1 for memory_id in seed_ids):
            return {"error": "invalid_input", "message": "seed_ids must be positive integer ids"}

    try:
        digest = await _build_memory_digest(
            topic,
            k,
            include_lineage,
            include_todos,
            include_related_hops,
            preview_chars,
            metadata_filters,
            date_from,
            date_to,
            tags_any,
            tags_all,
            seed_ids,
            debug,
        )
    except ValueError as exc:
        return {"error": "invalid_filters", "message": str(exc)}
    except Exception as exc:
        if fault := _integrity_fault_response(exc):
            return fault
        raise
    digest["parameters"] = {
        "k": k,
        "include_lineage": include_lineage,
        "include_todos": include_todos,
        "include_related_hops": min(include_related_hops, 3),
        "synthesize": synthesize,
        "preview_chars": preview_chars,
        "tags_any": tags_any,
        "tags_all": tags_all,
        "metadata_filters": metadata_filters,
        "date_from": date_from,
        "date_to": date_to,
        "seed_ids": seed_ids or [],
        "debug": debug,
    }
    if synthesize:
        digest["warnings"] = [
            "synthesize=true is reserved for a future LLM synthesis mode; v1 returns deterministic aggregation only."
        ]
    return digest


@mcp.tool()
async def memory_rebuild_embeddings() -> Dict[str, Any]:
    """Recompute embeddings for all memories. Rate limited: 300s cooldown."""
    if msg := _check_tool_cooldown("memory_rebuild_embeddings"):
        return {"error": "rate_limited", "message": msg}
    try:
        updated = await _rebuild_embeddings()
        return {"updated": updated}
    finally:
        _finish_tool("memory_rebuild_embeddings")


@mcp.tool()
async def memory_related(memory_id: int, refresh: bool = False) -> Dict[str, Any]:
    """Return cross-referenced memories for a given entry.

    Consistency: the ``related`` graph is **eventually consistent**. When a
    new memory A is created or updated with references to an existing B,
    A.related is computed fresh but B.related is not re-cascaded — it stays
    valid until either a full rebuild or an explicit refresh. Pass
    ``refresh=True`` to recompute this memory's crossrefs on the fly (the
    strong-consistency path). For a full rebuild across the store, call
    ``memory_rebuild_crossrefs``.
    """

    related = await _get_related(memory_id, refresh)
    return {"id": memory_id, "related": related}


@mcp.tool()
async def memory_rebuild_crossrefs() -> Dict[str, Any]:
    """Recompute cross-reference links for all memories. Rate limited: 300s cooldown.

    Use this periodically (or after bulk imports) to close the eventual-consistency
    gap in the ``related`` graph — see ``memory_related`` for the consistency model.
    """
    if msg := _check_tool_cooldown("memory_rebuild_crossrefs"):
        return {"error": "rate_limited", "message": msg}
    try:
        updated = await _rebuild_crossrefs()
        return {"updated": updated}
    finally:
        _finish_tool("memory_rebuild_crossrefs")


@mcp.tool()
async def memory_stats() -> Dict[str, Any]:
    """Get statistics and analytics about stored memories."""

    return await _get_statistics()


@mcp.tool()
async def memory_insights(
    period: str = "7d",
    include_llm_analysis: bool = True,
) -> Dict[str, Any]:
    """Analyze stored memories and produce actionable insights.

    Returns activity summary, open items, consolidation suggestions,
    and optional LLM-powered pattern detection.

    Args:
        period: Time period to analyze (e.g., "7d", "1m", "1y")
        include_llm_analysis: If True, use LLM to detect patterns and themes

    Returns:
        Dictionary with:
        - activity_summary: Created counts by type and tag
        - open_items: Open TODOs and issues with stale detection
        - consolidation_candidates: Similar memory pairs that could be merged
        - llm_analysis: Themes, focus areas, gaps, and summary (or null)
    Rate limited: 120s cooldown.
    """
    if msg := _check_tool_cooldown("memory_insights"):
        return {"error": "rate_limited", "message": msg}
    try:
        stale_days = int(os.getenv("MEMORA_STALE_DAYS", "14"))
        return await _generate_insights(period, stale_days, include_llm_analysis)
    finally:
        _finish_tool("memory_insights")


@mcp.tool()
async def memory_boost(
    memory_id: int,
    boost_amount: float = 0.5,
) -> Dict[str, Any]:
    """Boost a memory's importance score.

    Manually increase a memory's base importance to make it rank higher in
    importance-sorted searches. The boost is permanent and cumulative.

    Args:
        memory_id: ID of the memory to boost
        boost_amount: Amount to add to base importance (default: 0.5)
                      Common values: 0.25 (small), 0.5 (medium), 1.0 (large)

    Returns:
        Updated memory with new importance score, or error if not found
    """
    record = await _boost_memory(memory_id, boost_amount)
    if not record:
        return {"error": "not_found", "id": memory_id}
    _schedule_cloud_graph_sync()
    return {"memory": record, "boosted_by": boost_amount}


@_with_connection(writes=True)
def _add_link(conn, from_id: int, to_id: int, edge_type: str, bidirectional: bool):
    return add_link(conn, from_id, to_id, edge_type, bidirectional)


@_with_connection(writes=True)
def _remove_link(conn, from_id: int, to_id: int, bidirectional: bool):
    return remove_link(conn, from_id, to_id, bidirectional)


@_with_connection
def _detect_clusters(conn, min_cluster_size: int, min_score: float, algorithm: str = "connected_components"):
    return detect_clusters(conn, min_cluster_size, min_score, algorithm)


@mcp.tool()
async def memory_link(
    from_id: int,
    to_id: int,
    edge_type: str = "references",
    bidirectional: bool = True,
) -> Dict[str, Any]:
    """Create an explicit typed link between two memories.

    Args:
        from_id: Source memory ID
        to_id: Target memory ID
        edge_type: Type of relationship. Options:
            - "references" (default): General reference
            - "implements": Source implements/realizes target
            - "supersedes": Source replaces/updates target
            - "extends": Source builds upon target
            - "contradicts": Source conflicts with target
            - "related_to": Generic relationship
        bidirectional: If True, also create reverse link (default: True)

    Returns:
        Dict with created links and their types
    """
    try:
        result = await _add_link(from_id, to_id, edge_type, bidirectional)
        _schedule_cloud_graph_sync()
        return result
    except ValueError as e:
        return {"error": "invalid_input", "message": str(e)}


@mcp.tool()
async def memory_unlink(
    from_id: int,
    to_id: int,
    bidirectional: bool = True,
) -> Dict[str, Any]:
    """Remove a link between two memories.

    Args:
        from_id: Source memory ID
        to_id: Target memory ID
        bidirectional: If True, also remove reverse link (default: True)

    Returns:
        Dict with removed links
    """
    result = await _remove_link(from_id, to_id, bidirectional)
    _schedule_cloud_graph_sync()
    return result


@mcp.tool()
async def memory_clusters(
    min_cluster_size: int = 2,
    min_score: float = 0.3,
    algorithm: str = "connected_components",
) -> Dict[str, Any]:
    """Detect clusters of related memories.

    Args:
        min_cluster_size: Minimum memories to form a cluster (default: 2)
        min_score: Minimum similarity score to consider connected (default: 0.3)
        algorithm: "connected_components" (default) or "louvain"
                   Louvain uses embedding similarity for content-based clustering.

    Returns:
        List of clusters with member IDs, sizes, and common tags
    """
    clusters = await _detect_clusters(min_cluster_size, min_score, algorithm)
    return {
        "count": len(clusters),
        "clusters": clusters,
    }


@mcp.tool()
async def memory_find_duplicates(
    min_similarity: float = 0.85,
    max_similarity: float = 1.0,
    limit: int = 10,
    use_llm: bool = True,
) -> Dict[str, Any]:
    """Find potential duplicate memory pairs with optional LLM-powered comparison.

    Scans cross-references to find memory pairs with similarity >= threshold,
    then optionally uses LLM to semantically compare them. Uses the same
    threshold (0.85) as the graph UI duplicate detection.

    Args:
        min_similarity: Minimum similarity score to consider (default: 0.85)
        max_similarity: Maximum similarity score (default: 1.0, kept for backward compatibility)
        limit: Maximum pairs to analyze (default: 10)
        use_llm: Whether to use LLM for semantic comparison (default: True)

    Returns:
        Dictionary with:
        - pairs: List of potential duplicate pairs with analysis
        - total_candidates: Total pairs found
        - analyzed: Number of pairs analyzed with LLM
        - llm_available: Whether LLM comparison was available

    Rate limited: 120s cooldown.
    """
    if msg := _check_tool_cooldown("memory_find_duplicates"):
        return {"error": "rate_limited", "message": msg}
    try:
        return await _find_duplicates_impl(min_similarity, max_similarity, limit, use_llm)
    finally:
        _finish_tool("memory_find_duplicates")


async def _find_duplicates_impl(
    min_similarity: float, max_similarity: float, limit: int, use_llm: bool
) -> Dict[str, Any]:
    def _run() -> Dict[str, Any]:
        return _find_duplicates_blocking(min_similarity, max_similarity, limit, use_llm)

    return await _in_worker(_run)


def _find_duplicates_blocking(
    min_similarity: float, max_similarity: float, limit: int, use_llm: bool
) -> Dict[str, Any]:
    from .storage import compare_memories_llm, connect, find_duplicate_pairs, get_memory

    with connect() as conn:
        duplicate_result = find_duplicate_pairs(conn, min_similarity, limit * 2)
        candidates = duplicate_result["pairs"]

        total_candidates = duplicate_result["total_pairs"]
        pairs = []
        llm_available = False

        for candidate in candidates[:limit]:
            mem_a = get_memory(conn, candidate["memory_a_id"])
            mem_b = get_memory(conn, candidate["memory_b_id"])
            if not mem_a or not mem_b:
                continue

            pair_result = {
                "memory_a": {
                    "id": mem_a["id"],
                    "preview": mem_a["content"][:150] + "..." if len(mem_a["content"]) > 150 else mem_a["content"],
                    "tags": mem_a.get("tags", []),
                },
                "memory_b": {
                    "id": mem_b["id"],
                    "preview": mem_b["content"][:150] + "..." if len(mem_b["content"]) > 150 else mem_b["content"],
                    "tags": mem_b.get("tags", []),
                },
                "similarity_score": round(candidate["similarity_score"], 3),
            }

            if use_llm:
                llm_result = compare_memories_llm(
                    mem_a["content"],
                    mem_b["content"],
                    mem_a.get("metadata"),
                    mem_b.get("metadata"),
                )
                if llm_result:
                    llm_available = True
                    pair_result["llm_verdict"] = llm_result.get("verdict", "review")
                    pair_result["llm_confidence"] = llm_result.get("confidence", 0)
                    pair_result["llm_reasoning"] = llm_result.get("reasoning", "")
                    pair_result["suggested_action"] = llm_result.get("suggested_action", "review")
                    if llm_result.get("merge_suggestion"):
                        pair_result["merge_suggestion"] = llm_result["merge_suggestion"]

            pairs.append(pair_result)

        return {
            "pairs": pairs,
            "total_candidates": total_candidates,
            "affected_node_count": duplicate_result["affected_node_count"],
            "analyzed": len(pairs),
            "llm_available": llm_available,
        }


@mcp.tool()
async def memory_detect_supersessions(
    min_similarity: float = 0.55,
    limit: int = 20,
    dry_run: bool = True,
    tags_any: Optional[List[str]] = None,
    min_confidence: float = 0.75,
) -> Dict[str, Any]:
    """Detect memories that supersede (update/replace) other memories.

    Scans existing memories for pairs where one is an evolved/updated version
    of another, then creates 'supersedes' edges between them. Complements
    memory_absorb which only catches supersessions at write time.

    Uses neutral LLM classification (not biased by timestamps) to determine
    both the relationship type and direction.

    Args:
        min_similarity: Minimum embedding similarity to consider (default: 0.55)
        limit: Maximum pairs to analyze with LLM (default: 20)
        dry_run: If True, preview detections without creating edges (default: True)
        tags_any: Only consider memories with any of these tags
        min_confidence: Minimum LLM confidence to accept (default: 0.75)

    Returns:
        Dictionary with candidates found, analyzed count, detected supersessions,
        and detailed results for each pair.

    Rate limited: 120s cooldown.
    """
    if msg := _check_tool_cooldown("memory_detect_supersessions"):
        return {"error": "rate_limited", "message": msg}
    try:
        result = await _detect_supersessions(
            min_similarity, limit, dry_run, tags_any, min_confidence,
        )
    except Exception as exc:
        logger.error("memory_detect_supersessions failed: %s", exc, exc_info=True)
        return _safe_error(exc, "memory_detect_supersessions")
    finally:
        _finish_tool("memory_detect_supersessions")

    if not dry_run and result.get("supersessions_created", 0) > 0:
        _schedule_cloud_graph_sync()

    return result


@mcp.tool()
async def memory_backfill_tags(
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Re-tag existing memories with project-prefixed tags.

    Uses deterministic normalization to prefix generic tags (e.g. "plan" → "memora/plan")
    when the memory content clearly belongs to a specific project. No LLM calls.

    Idempotent: re-running produces the same result.

    Args:
        dry_run: If True, preview changes without writing (default: True)

    Returns:
        Dictionary with processed count, changed count, and list of changes.

    Rate limited: 120s cooldown.
    """
    if msg := _check_tool_cooldown("memory_backfill_tags"):
        return {"error": "rate_limited", "message": msg}
    try:
        from .storage import backfill_tags

        def _run():
            with connect() as conn:
                return backfill_tags(conn, dry_run=dry_run)

        result = await _in_worker(_run)
    except Exception as exc:
        logger.error("memory_backfill_tags failed: %s", exc, exc_info=True)
        return _safe_error(exc, "memory_backfill_tags")
    finally:
        _finish_tool("memory_backfill_tags")

    if not dry_run and result.get("changed", 0) > 0:
        _schedule_cloud_graph_sync()

    return result


@mcp.tool()
async def memory_merge(
    source_id: int,
    target_id: int,
    merge_strategy: str = "append",
) -> Dict[str, Any]:
    """Merge source memory into target, then delete source.

    Combines two memories into one, preserving content and metadata.

    Args:
        source_id: Memory ID to merge from (will be deleted)
        target_id: Memory ID to merge into (will be updated)
        merge_strategy: How to combine content:
            - "append": Append source content to target (default)
            - "prepend": Prepend source content to target
            - "replace": Replace target content with source

    Returns:
        Updated target memory and deletion confirmation
    """
    from .storage import connect, delete_memory, update_memory

    source = await _get_memory(source_id)
    target = await _get_memory(target_id)

    if not source:
        return {"error": "not_found", "message": f"Source memory #{source_id} not found"}
    if not target:
        return {"error": "not_found", "message": f"Target memory #{target_id} not found"}

    # Guard: refuse to merge document fragments
    if _is_doc_memory(source.get("metadata")) or _is_doc_memory(target.get("metadata")):
        return {
            "error": "protected_fragment",
            "message": "Cannot merge document fragments or roots. "
                       "Modify the source document and re-store instead.",
        }

    # Combine content based on strategy
    if merge_strategy == "prepend":
        new_content = source["content"] + "\n\n---\n\n" + target["content"]
    elif merge_strategy == "replace":
        new_content = source["content"]
    else:  # append (default)
        new_content = target["content"] + "\n\n---\n\n" + source["content"]

    # Merge metadata (target takes precedence, but add source-specific fields)
    merged_metadata = dict(source.get("metadata") or {})
    merged_metadata.update(target.get("metadata") or {})
    merged_metadata["merged_from"] = source_id

    # Union tags
    source_tags = set(source.get("tags") or [])
    target_tags = set(target.get("tags") or [])
    merged_tags = list(source_tags | target_tags)

    def _merge_write():
        with connect() as conn:
            updated = update_memory(
                conn,
                target_id,
                content=new_content,
                metadata=merged_metadata,
                tags=merged_tags,
            )
            conn.commit()
            delete_memory(conn, source_id, reason="merged")
            from .storage import _log_action
            _log_action(conn, target_id, "merge", f"Merged #{source_id} into #{target_id}")
            conn.commit()
            return updated

    updated = await _in_worker(_merge_write)

    _schedule_cloud_graph_sync()
    return {
        "merged": True,
        "target_id": target_id,
        "source_id": source_id,
        "updated_memory": updated,
        "message": f"Memory #{source_id} merged into #{target_id} and deleted",
    }


@mcp.tool()
async def memory_export() -> Dict[str, Any]:
    """Export all memories to JSON format for backup or transfer. Rate limited: 60s cooldown."""
    if msg := _check_tool_cooldown("memory_export"):
        return {"error": "rate_limited", "message": msg}
    try:
        memories = await _export_memories()
        return {"count": len(memories), "memories": memories}
    finally:
        _finish_tool("memory_export")


@mcp.tool()
async def memory_upload_image(
    file_path: str,
    memory_id: int,
    image_index: int = 0,
    caption: Optional[str] = None,
) -> Dict[str, Any]:
    """Upload an image file directly to R2 storage.

    Uploads a local image file to R2 and returns the r2:// reference URL
    that can be used in memory metadata.

    Args:
        file_path: Absolute path to the image file to upload
        memory_id: Memory ID this image belongs to (used for organizing in R2)
        image_index: Index of image within the memory (default: 0)
        caption: Optional caption for the image

    Returns:
        Dictionary with r2_url (the r2:// reference) and image object ready for metadata
    """
    from pathlib import Path as _Path

    from PIL import Image as _PILImage

    from .image_storage import get_image_storage_instance

    image_storage = get_image_storage_instance()
    if not image_storage:
        return {
            "error": "r2_not_configured",
            "message": "R2 storage is not configured. Set MEMORA_STORAGE_URI to s3:// and configure AWS credentials.",
        }

    # --- Path validation (defense in depth) ---
    raw_path = _Path(file_path)

    # 1. Reject symlinks anywhere in the path chain
    for part in [raw_path] + list(raw_path.parents):
        if part.is_symlink():
            return {"error": "invalid_path", "message": "Symlinks are not supported"}

    try:
        resolved = raw_path.resolve(strict=True)
    except (OSError, ValueError):
        return {"error": "file_not_found", "message": "File not found"}

    # 2. Validate extension — aligned with image_storage.py ext_map
    _UPLOAD_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    if resolved.suffix.lower() not in _UPLOAD_EXTENSIONS:
        return {"error": "invalid_type", "message": "File must be an image (jpg, jpeg, png, gif, webp)"}

    # 3. Block known sensitive directories
    _BLOCKED_PATTERNS = [".ssh", ".gnupg", ".aws", ".config/gcloud", "id_rsa", "id_ed25519", ".env"]
    path_str = str(resolved).lower()
    for pattern in _BLOCKED_PATTERNS:
        if pattern in path_str:
            return {"error": "blocked_path", "message": "Cannot upload files from sensitive directories"}

    # 4. Verify file is actually an image and derive MIME from content
    _PILLOW_TO_MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif", "WEBP": "image/webp"}
    try:
        with _PILImage.open(str(resolved)) as img:
            img.verify()
            pillow_format = img.format
    except Exception:
        return {"error": "invalid_image", "message": "File is not a valid image"}

    content_type = _PILLOW_TO_MIME.get(pillow_format)
    if not content_type:
        return {"error": "unsupported_format", "message": f"Unsupported image format: {pillow_format}"}

    try:
        # Read file and upload
        with open(str(resolved), "rb") as f:
            image_data = f.read()

        r2_url = image_storage.upload_image(
            image_data=image_data,
            content_type=content_type,
            memory_id=memory_id,
            image_index=image_index,
        )

        # Build image object for metadata
        image_obj = {"src": r2_url}
        if caption:
            image_obj["caption"] = caption

        # Don't echo local file_path in response (path disclosure fix)
        return {
            "r2_url": r2_url,
            "image": image_obj,
            "content_type": content_type,
            "size_bytes": len(image_data),
        }

    except Exception as e:
        logger.error("Failed to upload image for memory %s: %s", memory_id, e)
        return {"error": "upload_failed", "message": "Image upload failed. Check server logs for details."}


@mcp.tool()
async def memory_migrate_images(dry_run: bool = False) -> Dict[str, Any]:
    """Migrate existing base64 images to R2 storage.

    Scans all memories and uploads any base64-encoded images to R2,
    replacing the data URIs with R2 URLs.

    Args:
        dry_run: If True, only report what would be migrated without making changes

    Returns:
        Dictionary with migration results including count of migrated images

    Rate limited: 300s cooldown.
    """
    if msg := _check_tool_cooldown("memory_migrate_images"):
        return {"error": "rate_limited", "message": msg}
    try:
        return await _migrate_images_to_r2(dry_run=dry_run)
    finally:
        _finish_tool("memory_migrate_images")


@_with_connection(writes=True)
def _migrate_images_to_r2(conn, dry_run: bool = False) -> Dict[str, Any]:
    """Migrate all base64 images to R2 storage."""
    import json as json_lib

    from .image_storage import get_image_storage_instance, parse_data_uri
    from .storage import update_memory

    image_storage = get_image_storage_instance()
    if not image_storage:
        return {
            "error": "r2_not_configured",
            "message": "R2 storage is not configured. Set MEMORA_STORAGE_URI to s3:// and configure AWS credentials.",
        }

    # Find memories with base64 images
    rows = conn.execute(
        "SELECT id, metadata FROM memories WHERE metadata LIKE '%data:image%'"
    ).fetchall()

    if not rows:
        return {"migrated_memories": 0, "migrated_images": 0, "message": "No base64 images found"}

    results = {
        "dry_run": dry_run,
        "memories_scanned": len(rows),
        "migrated_memories": 0,
        "migrated_images": 0,
        "errors": [],
    }

    for row in rows:
        memory_id = row["id"]
        try:
            metadata = json_lib.loads(row["metadata"]) if row["metadata"] else {}
        except json_lib.JSONDecodeError:
            continue

        images = metadata.get("images", [])
        if not isinstance(images, list):
            continue

        updated = False
        for idx, img in enumerate(images):
            if not isinstance(img, dict):
                continue
            src = img.get("src", "")
            if not src.startswith("data:image"):
                continue

            if dry_run:
                results["migrated_images"] += 1
                updated = True
                continue

            # Upload to R2
            try:
                image_bytes, content_type = parse_data_uri(src)
                new_url = image_storage.upload_image(
                    image_data=image_bytes,
                    content_type=content_type,
                    memory_id=memory_id,
                    image_index=idx,
                )
                img["src"] = new_url
                results["migrated_images"] += 1
                updated = True
            except Exception as e:
                logger.warning(
                    "Failed migrating image memory_id=%s image_index=%s: %s",
                    memory_id,
                    idx,
                    e,
                )
                results["errors"].append({
                    "memory_id": memory_id,
                    "image_index": idx,
                    "error": "migration_failed",
                })

        if updated:
            results["migrated_memories"] += 1
            if not dry_run:
                # Update the memory with new URLs
                update_memory(conn, memory_id, metadata=metadata)

    if dry_run:
        results["message"] = f"Would migrate {results['migrated_images']} images from {results['migrated_memories']} memories"
    else:
        results["message"] = f"Migrated {results['migrated_images']} images from {results['migrated_memories']} memories"

    return results


# NOTE: Graph visualization functions moved to memora/graph/ module
# See: graph/data.py, graph/templates.py, graph/issues.py, graph/server.py


@mcp.tool()
async def memory_export_graph(
    output_path: Optional[str] = None,
    min_score: float = 0.25,
) -> Dict[str, Any]:
    """Export memories as interactive HTML knowledge graph.

    Args:
        output_path: Path to save HTML file (default: ~/memories_graph.html)
        min_score: Minimum similarity score for edges (default: 0.25)

    Returns:
        Dictionary with path, node count, edge count, and tags
    """
    import os
    if output_path is None:
        output_path = os.path.expanduser("~/memories_graph.html")

    return await _in_worker(export_graph_html, output_path, min_score)


# Removed ~400 lines of old _export_graph_html code - now in graph/data.py





@mcp.tool()
async def memory_import(
    data: List[Dict[str, Any]],
    strategy: str = "append",
) -> Dict[str, Any]:
    """Import memories from JSON format. Rate limited: 60s cooldown.

    Args:
        data: List of memory dictionaries with content, metadata, tags, created_at
        strategy: "replace" (clear all first), "merge" (skip duplicates), or "append" (add all)
    """
    # Validate inputs before consuming cooldown
    if strategy not in ("replace", "merge", "append"):
        return {"error": "invalid_input", "message": f"Unknown strategy: {strategy}"}
    if not data:
        return {"error": "invalid_input", "message": "No data provided"}

    if msg := _check_tool_cooldown("memory_import"):
        return {"error": "rate_limited", "message": msg}
    try:
        result = await _import_memories(data, strategy)
        _schedule_cloud_graph_sync()
        return result
    except ValueError as exc:
        return {"error": "invalid_input", "message": str(exc)}
    finally:
        _finish_tool("memory_import")


@_with_connection
def _poll_events(
    conn,
    since_timestamp: Optional[str],
    tags_filter: Optional[List[str]],
    unconsumed_only: bool,
):
    return poll_events(conn, since_timestamp, tags_filter, unconsumed_only)


@_with_connection(writes=True)
def _clear_events(conn, event_ids: List[int]):
    return clear_events(conn, event_ids)


@mcp.tool()
async def memory_events_poll(
    since_timestamp: Optional[str] = None,
    tags_filter: Optional[List[str]] = None,
    unconsumed_only: bool = True,
) -> Dict[str, Any]:
    """Poll for memory events (e.g., shared-cache notifications).

    Args:
        since_timestamp: Only return events after this timestamp (ISO format)
        tags_filter: Only return events with these tags (e.g., ["shared-cache"])
        unconsumed_only: Only return unconsumed events (default: True)

    Returns:
        Dictionary with count and list of events
    """
    events = await _poll_events(since_timestamp, tags_filter, unconsumed_only)
    return {"count": len(events), "events": events}


@mcp.tool()
async def memory_events_clear(event_ids: List[int]) -> Dict[str, Any]:
    """Mark events as consumed.

    Args:
        event_ids: List of event IDs to mark as consumed

    Returns:
        Dictionary with count of cleared events
    """
    cleared = await _clear_events(event_ids)
    return {"cleared": cleared}


# Graph functions moved to memora/graph/ module


def main(argv: Optional[list[str]] = None) -> None:
    from . import __version__

    parser = argparse.ArgumentParser(description="Memory MCP Server")
    parser.add_argument("--version", action="version", version=f"memora {__version__}")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Default: start server (make it the default if no subcommand)
    parser.add_argument(
        "--transport",
        choices=sorted(VALID_TRANSPORTS),
        default=DEFAULT_TRANSPORT,
        help="MCP transport to use (defaults to env MEMORA_TRANSPORT or 'stdio')",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help="Host interface for HTTP transports (defaults to env MEMORA_HOST or 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="Port for HTTP transports (defaults to env MEMORA_PORT or 8000)",
    )
    parser.add_argument(
        "--graph-port",
        type=int,
        default=DEFAULT_GRAPH_PORT,
        help="Port for graph visualization server (defaults to env MEMORA_GRAPH_PORT or 8765)",
    )
    parser.add_argument(
        "--no-graph",
        action="store_true",
        help="Disable the graph visualization server",
    )

    # Subcommand: sync-pull
    subparsers.add_parser(
        "sync-pull",
        help="Force pull database from cloud storage (ignore local cache)"
    )

    # Subcommand: sync-push
    subparsers.add_parser(
        "sync-push",
        help="Force push database to cloud storage"
    )

    # Subcommand: sync-status
    subparsers.add_parser(
        "sync-status",
        help="Show sync status and backend information"
    )

    # Subcommand: info
    subparsers.add_parser(
        "info",
        help="Show storage backend information"
    )

    # Subcommand: migrate-images
    migrate_parser = subparsers.add_parser(
        "migrate-images",
        help="Migrate base64 images to R2 storage"
    )
    migrate_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be migrated without making changes"
    )

    args = parser.parse_args(argv)

    # Handle subcommands
    if args.command == "sync-pull":
        _handle_sync_pull()
    elif args.command == "sync-push":
        _handle_sync_push()
    elif args.command == "sync-status":
        _handle_sync_status()
    elif args.command == "info":
        _handle_info()
    elif args.command == "migrate-images":
        _handle_migrate_images(dry_run=args.dry_run)
    else:
        # Default: start server
        mcp.settings.host = args.host
        mcp.settings.port = args.port

        # Pre-warm database connection (triggers cloud sync if needed)
        # This prevents "connection failed" on first MCP connection
        try:
            import sys
            print("Initializing database...", file=sys.stderr)
            conn = connect()
            conn.close()
            print("Database ready.", file=sys.stderr)
        except Exception as e:
            logger.warning("Database pre-warm failed: %s", e)
            print(f"Warning: Database pre-warm failed: {e}", file=sys.stderr)

        # Start graph visualization server unless disabled
        if not args.no_graph:
            start_graph_server(args.host, args.graph_port)

        # Anthropic's tool input_schema validator rejects `anyOf` /
        # `oneOf` / `allOf` combinators — including the property-level
        # `{anyOf:[{type:X},{type:"null"}]}` Pydantic emits for
        # `Optional[X]`. Collapse those nullable unions to the
        # non-null branch in every registered tool's schema before
        # we start serving, so Claude Code (and any other strict
        # client) accepts the tools/list response.
        _sanitize_tool_schemas(mcp)

        mcp.run(transport=args.transport)


def _handle_sync_pull() -> None:
    """Handle sync-pull command."""
    from .backends import CloudSQLiteBackend
    from .storage import STORAGE_BACKEND

    if not isinstance(STORAGE_BACKEND, CloudSQLiteBackend):
        print("Error: sync-pull only works with cloud storage backends")
        print(f"Current backend: {STORAGE_BACKEND.__class__.__name__}")
        exit(1)

    print(f"Pulling database from {STORAGE_BACKEND.cloud_url}...")
    try:
        STORAGE_BACKEND.force_sync_pull()
        info = STORAGE_BACKEND.get_info()
        print("✓ Sync completed successfully")
        print(f"  Cache path: {info['cache_path']}")
        print(f"  Size: {info['cache_size_bytes'] / 1024 / 1024:.2f} MB")
        print(f"  Last sync: {info.get('last_sync', 'N/A')}")
    except Exception as e:
        print(f"✗ Sync failed: {e}")
        exit(1)


def _handle_sync_push() -> None:
    """Handle sync-push command."""
    from .backends import CloudSQLiteBackend
    from .storage import STORAGE_BACKEND

    if not isinstance(STORAGE_BACKEND, CloudSQLiteBackend):
        print("Error: sync-push only works with cloud storage backends")
        print(f"Current backend: {STORAGE_BACKEND.__class__.__name__}")
        exit(1)

    print(f"Pushing database to {STORAGE_BACKEND.cloud_url}...")
    try:
        STORAGE_BACKEND.force_sync_push()
        info = STORAGE_BACKEND.get_info()
        print("✓ Push completed successfully")
        print(f"  Cloud URL: {info['cloud_url']}")
        print(f"  Size: {info['cache_size_bytes'] / 1024 / 1024:.2f} MB")
        print(f"  Last sync: {info.get('last_sync', 'N/A')}")
    except Exception as e:
        print(f"✗ Push failed: {e}")
        exit(1)


def _handle_sync_status() -> None:
    """Handle sync-status command."""
    import json

    from .storage import STORAGE_BACKEND

    info = STORAGE_BACKEND.get_info()
    backend_type = info.get('backend_type', 'unknown')

    print(f"Storage Backend: {backend_type}")
    print()

    if backend_type == "cloud_sqlite":
        print(f"Cloud URL: {info.get('cloud_url', 'N/A')}")
        print(f"Bucket: {info.get('bucket', 'N/A')}")
        print(f"Key: {info.get('key', 'N/A')}")
        print()
        print(f"Cache Path: {info.get('cache_path', 'N/A')}")
        print(f"Cache Exists: {info.get('cache_exists', False)}")
        print(f"Cache Size: {info.get('cache_size_bytes', 0) / 1024 / 1024:.2f} MB")
        print()
        print(f"Is Dirty: {info.get('is_dirty', False)}")
        print(f"Last ETag: {info.get('last_etag', 'N/A')}")
        print(f"Last Sync: {info.get('last_sync', 'N/A')}")
        print(f"Auto Sync: {info.get('auto_sync', True)}")
        print(f"Encryption: {info.get('encrypt', False)}")
    elif backend_type == "local_sqlite":
        print(f"Database Path: {info.get('db_path', 'N/A')}")
        print(f"Exists: {info.get('exists', False)}")
        print(f"Size: {info.get('size_bytes', 0) / 1024 / 1024:.2f} MB")
    else:
        print(json.dumps(info, indent=2))


def _handle_info() -> None:
    """Handle info command."""
    import json

    from .storage import STORAGE_BACKEND

    info = STORAGE_BACKEND.get_info()
    print(json.dumps(info, indent=2, default=str))


def _handle_migrate_images(dry_run: bool = False) -> None:
    """Handle migrate-images command."""
    import json

    print(f"{'[DRY RUN] ' if dry_run else ''}Migrating base64 images to R2 storage...")

    result = asyncio.run(_migrate_images_to_r2(dry_run=dry_run))

    if "error" in result:
        print(f"Error: {result['message']}")
        return

    print(json.dumps(result, indent=2))

    if result.get("errors"):
        print(f"\nWarning: {len(result['errors'])} errors occurred during migration")


if __name__ == "__main__":
    main()
