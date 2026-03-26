"""MCP-compatible memory server backed by SQLite."""
from __future__ import annotations

import argparse
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
    export_sessions,
    find_invalid_tag_entries,
    generate_insights,
    get_crossrefs,
    get_memory,
    get_statistics,
    hybrid_search,
    import_memories,
    list_memories,
    poll_events,
    rebuild_crossrefs,
    rebuild_embeddings,
    remove_link,
    semantic_search,
    sync_to_cloud,
    update_crossrefs,
    update_memory,
)

logger = logging.getLogger(__name__)


def _safe_error(e: Exception, context: str = "operation") -> Dict[str, str]:
    """Return sanitized error for unexpected exceptions. Log full details internally."""
    logger.error("Failed %s: %s", context, e, exc_info=True)
    return {"error": f"{context}_failed", "message": f"The {context} failed. Check server logs for details."}


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


def _with_connection(func=None, *, writes=False):
    """Decorator that manages database connections and cloud sync.

    Opens a connection, runs the function, closes the connection,
    and syncs to cloud storage only after write operations.

    Args:
        writes: If True, syncs to cloud after operation. If False, skips sync (read-only).
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            conn = connect()
            try:
                result = func(conn, *args, **kwargs)
                # Only sync to cloud after write operations
                if writes:
                    sync_to_cloud()
                return result
            finally:
                conn.close()

        return wrapper

    # Allow using as @_with_connection or @_with_connection(writes=True)
    if func is not None:
        # Called as @_with_connection (default: read-only, no sync)
        return decorator(func)
    else:
        # Called as @_with_connection(writes=True)
        return decorator


@_with_connection(writes=True)
def _create_memory(
    conn,
    content: str,
    metadata: Optional[Dict[str, Any]],
    tags: Optional[list[str]],
):
    return add_memory(conn, content=content.strip(), metadata=metadata, tags=tags or [])


@_with_connection
def _get_memory(conn, memory_id: int):
    return get_memory(conn, memory_id)


@_with_connection(writes=True)
def _update_memory(
    conn,
    memory_id: int,
    content: Optional[str],
    metadata: Optional[Dict[str, Any]],
    tags: Optional[list[str]],
):
    return update_memory(conn, memory_id, content=content, metadata=metadata, tags=tags)


@_with_connection(writes=True)
def _delete_memory(conn, memory_id: int):
    return delete_memory(conn, memory_id)


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
):
    return list_memories(
        conn, query, metadata_filters, limit, offset,
        date_from, date_to, tags_any, tags_all, tags_none,
        sort_by_importance=sort_by_importance,
    )


@_with_connection(writes=True)
def _boost_memory(conn, memory_id: int, boost_amount: float):
    return boost_memory(conn, memory_id, boost_amount)


@_with_connection(writes=True)
def _create_memories(conn, entries: List[Dict[str, Any]]):
    return add_memories(conn, entries)


@_with_connection(writes=True)
def _delete_memories(conn, ids: List[int]):
    return delete_memories(conn, ids)


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
):
    return semantic_search(
        conn,
        query,
        metadata_filters=metadata_filters,
        top_k=top_k,
        min_score=min_score,
    )


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
    )


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
        get_existing_hierarchy_paths(
            _list_memories(None, None, None, 0, None, None, None, None, None)
        )
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
        record = _create_memory(content=redacted_content, metadata=metadata, tags=tags or [])
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
            hierarchy_suggestions = suggest_hierarchy_from_similar(
                related_memories,
                get_memory_by_id=_get_memory,
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
                            _update_memory(memory_id, None, auto_meta, None)
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
        record = _create_memory(content.strip(), metadata, tags)
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
        record = _create_memory(content.strip(), metadata, tags)
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
        record = _create_memory(content.strip(), metadata, tags)
    except ValueError as exc:
        return {"error": "invalid_input", "message": str(exc)}

    _schedule_cloud_graph_sync()
    return {"memory": record}


@mcp.tool()
async def memory_list(
    query: Optional[str] = None,
    metadata_filters: Optional[Dict[str, Any]] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = 0,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    tags_any: Optional[List[str]] = None,
    tags_all: Optional[List[str]] = None,
    tags_none: Optional[List[str]] = None,
    sort_by_importance: bool = False,
) -> Dict[str, Any]:
    """List memories, optionally filtering by substring query or metadata.

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
        sort_by_importance: Sort results by importance score (default: False, sorts by date)
    """
    try:
        items = _list_memories(
            query, metadata_filters, limit, offset,
            date_from, date_to, tags_any, tags_all, tags_none,
            sort_by_importance,
        )
    except ValueError as exc:
        return {"error": "invalid_filters", "message": str(exc)}
    return {"count": len(items), "memories": items}


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
    """List memories in compact format (id, preview, tags only) to reduce context usage.

    Returns minimal fields: id, content preview (first 80 chars), tags, and created_at.
    This tool is useful for browsing memories without loading full content and metadata.

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
        items = _list_memories(query, metadata_filters, limit, offset, date_from, date_to, tags_any, tags_all, tags_none)
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
        records = _create_memories(entries)
    except ValueError as exc:
        return {"error": "invalid_batch", "message": str(exc)}
    _schedule_cloud_graph_sync()
    return {"count": len(records), "memories": records}


@mcp.tool()
async def memory_delete_batch(ids: List[int]) -> Dict[str, Any]:
    """Delete multiple memories by id."""
    deleted = _delete_memories(ids)
    _schedule_cloud_graph_sync()
    return {"deleted": deleted}


@mcp.tool()
async def memory_get(memory_id: int, include_images: bool = False) -> Dict[str, Any]:
    """Retrieve a single memory by id.

    Args:
        memory_id: ID of the memory to retrieve
        include_images: If False, strip image data from metadata to reduce response size
    """
    record = _get_memory(memory_id)
    if not record:
        return {"error": "not_found", "id": memory_id}

    metadata = record.get("metadata") or {}
    if not include_images and metadata.get("images"):
        record["metadata"]["images"] = [
            {"caption": img.get("caption", "")} for img in metadata["images"]
        ]

    return {"memory": record}


@mcp.tool()
async def memory_update(
    memory_id: int,
    content: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    tags: Optional[list[str]] = None,
) -> Dict[str, Any]:
    """Update an existing memory. Only provided fields are updated."""
    try:
        record = _update_memory(memory_id, content, metadata, tags)
    except ValueError as exc:
        return {"error": "invalid_input", "message": str(exc)}
    if not record:
        return {"error": "not_found", "id": memory_id}
    _schedule_cloud_graph_sync()
    return {"memory": record}


@mcp.tool()
async def memory_delete(memory_id: int) -> Dict[str, Any]:
    """Delete a memory by id."""
    if _delete_memory(memory_id):
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

    tags = _collect_tags()
    tree = build_tag_hierarchy(tags)
    if not include_root and isinstance(tree, dict):
        tree = tree.get("children", [])
    return {"count": len(tags), "hierarchy": tree}


@mcp.tool()
async def memory_validate_tags(include_memories: bool = True) -> Dict[str, Any]:
    """Validate stored tags against the allowlist and report invalid entries."""
    from . import list_allowed_tags

    invalid_full = _find_invalid_tags()
    allowed = list_allowed_tags()
    existing = _collect_tags()
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
        items = _list_memories(query, metadata_filters, None, 0, date_from, date_to, tags_any, tags_all, tags_none)
    except ValueError as exc:
        return {"error": "invalid_filters", "message": str(exc)}

    hierarchy = build_hierarchy_tree(items, include_root=include_root, compact=compact)
    return {"count": len(items), "hierarchy": hierarchy}


@mcp.tool()
async def memory_semantic_search(
    query: str,
    top_k: int = 5,
    metadata_filters: Optional[Dict[str, Any]] = None,
    min_score: Optional[float] = None,
) -> Dict[str, Any]:
    """Perform a semantic search using vector embeddings."""

    try:
        results = _semantic_search(
            query,
            metadata_filters,
            top_k,
            min_score,
        )
    except ValueError as exc:
        return {"error": "invalid_filters", "message": str(exc)}
    return {"count": len(results), "results": results}


@mcp.tool()
async def memory_hybrid_search(
    query: str,
    semantic_weight: float = 0.6,
    top_k: int = 10,
    min_score: float = 0.0,
    metadata_filters: Optional[Dict[str, Any]] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    tags_any: Optional[List[str]] = None,
    tags_all: Optional[List[str]] = None,
    tags_none: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Perform a hybrid search combining keyword (FTS) and semantic (vector) search.

    Uses Reciprocal Rank Fusion (RRF) to merge results from both search methods,
    providing better results than either method alone.

    Args:
        query: Search query text
        semantic_weight: Weight for semantic results (0-1). Higher values favor semantic similarity.
                        Keyword weight = 1 - semantic_weight. Default: 0.6 (60% semantic, 40% keyword)
        top_k: Maximum number of results to return (default: 10)
        min_score: Minimum combined score threshold (default: 0.0)
        metadata_filters: Optional metadata filters
        date_from: Optional date filter (ISO format or relative like "7d", "1m", "1y")
        date_to: Optional date filter (ISO format or relative)
        tags_any: Match memories with ANY of these tags (OR logic)
        tags_all: Match memories with ALL of these tags (AND logic)
        tags_none: Exclude memories with ANY of these tags (NOT logic)

    Returns:
        Dictionary with count and list of results, each containing score and memory
    """
    try:
        results = _hybrid_search(
            query,
            semantic_weight,
            top_k,
            min_score,
            metadata_filters,
            date_from,
            date_to,
            tags_any,
            tags_all,
            tags_none,
        )
    except ValueError as exc:
        return {"error": "invalid_filters", "message": str(exc)}
    return {"count": len(results), "results": results}


@mcp.tool()
async def memory_rebuild_embeddings() -> Dict[str, Any]:
    """Recompute embeddings for all memories. Rate limited: 300s cooldown."""
    if msg := _check_tool_cooldown("memory_rebuild_embeddings"):
        return {"error": "rate_limited", "message": msg}
    try:
        updated = _rebuild_embeddings()
        return {"updated": updated}
    finally:
        _finish_tool("memory_rebuild_embeddings")


@mcp.tool()
async def memory_related(memory_id: int, refresh: bool = False) -> Dict[str, Any]:
    """Return cross-referenced memories for a given entry."""

    related = _get_related(memory_id, refresh)
    return {"id": memory_id, "related": related}


@mcp.tool()
async def memory_rebuild_crossrefs() -> Dict[str, Any]:
    """Recompute cross-reference links for all memories. Rate limited: 300s cooldown."""
    if msg := _check_tool_cooldown("memory_rebuild_crossrefs"):
        return {"error": "rate_limited", "message": msg}
    try:
        updated = _rebuild_crossrefs()
        return {"updated": updated}
    finally:
        _finish_tool("memory_rebuild_crossrefs")


@mcp.tool()
async def memory_stats() -> Dict[str, Any]:
    """Get statistics and analytics about stored memories."""

    return _get_statistics()


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
        return _generate_insights(period, stale_days, include_llm_analysis)
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
    record = _boost_memory(memory_id, boost_amount)
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
        result = _add_link(from_id, to_id, edge_type, bidirectional)
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
    result = _remove_link(from_id, to_id, bidirectional)
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
    clusters = _detect_clusters(min_cluster_size, min_score, algorithm)
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
    from .storage import compare_memories_llm, connect, find_duplicate_candidates

    with connect() as conn:
        candidates = find_duplicate_candidates(conn, min_similarity, limit * 2)

    total_candidates = len(candidates)
    pairs = []
    llm_available = False

    for candidate in candidates[:limit]:
        mem_a = _get_memory(candidate["memory_a_id"])
        mem_b = _get_memory(candidate["memory_b_id"])

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

        # Run LLM comparison if enabled
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
        "analyzed": len(pairs),
        "llm_available": llm_available,
    }


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

    source = _get_memory(source_id)
    target = _get_memory(target_id)

    if not source:
        return {"error": "not_found", "message": f"Source memory #{source_id} not found"}
    if not target:
        return {"error": "not_found", "message": f"Target memory #{target_id} not found"}

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

    # Update target memory
    with connect() as conn:
        updated = update_memory(
            conn,
            target_id,
            content=new_content,
            metadata=merged_metadata,
            tags=merged_tags,
        )
        conn.commit()

        # Delete source memory
        delete_memory(conn, source_id)

        from .storage import _log_action
        _log_action(conn, target_id, "merge", f"Merged #{source_id} into #{target_id}")
        conn.commit()

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
        memories = _export_memories()
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
        return _migrate_images_to_r2(dry_run=dry_run)
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

    return export_graph_html(output_path, min_score)


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
        result = _import_memories(data, strategy)
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
    events = _poll_events(since_timestamp, tags_filter, unconsumed_only)
    return {"count": len(events), "events": events}


@mcp.tool()
async def memory_events_clear(event_ids: List[int]) -> Dict[str, Any]:
    """Mark events as consumed.

    Args:
        event_ids: List of event IDs to mark as consumed

    Returns:
        Dictionary with count of cleared events
    """
    cleared = _clear_events(event_ids)
    return {"cleared": cleared}


# ── Session memory tools ────────────────────────────────────────────────
# These bypass FTS/embeddings/crossrefs/sync. Local SQLite only.

from .sessions import (
    SessionBackendError,
    SessionError,
    branch_state_get as _branch_state_get,
    scoped_search as _scoped_search_impl,
    session_close as _session_close_impl,
    session_delta as _session_delta_impl,
    session_get as _session_get_impl,
    session_list as _session_list_impl,
    session_start as _session_start_impl,
)


def _session_error(msg: str) -> Dict[str, str]:
    return {"error": "session_error", "message": msg}


def _require_session_backend() -> Optional[Dict[str, str]]:
    """Check if the backend supports sessions. Returns error dict or None."""
    if not STORAGE_BACKEND.supports_sessions:
        return _session_error(
            "Session memory requires local SQLite backend. "
            "Current backend does not support sessions."
        )
    return None


@mcp.tool()
async def session_start(
    repo_identity: str,
    branch: str,
    head_commit: Optional[str] = None,
    objective: Optional[str] = None,
    claude_session_id: Optional[str] = None,
    pane_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    transcript_path: Optional[str] = None,
    transcript_start_line: Optional[int] = None,
    start_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Start a new memory session.

    Args:
        repo_identity: Canonical repo ID (git remote hash)
        branch: Git branch name
        head_commit: Commit hash at session start
        objective: Session objective (auto-extracted from first prompt)
        claude_session_id: Claude Code conversation UUID
        pane_id: tmux pane ID
        workspace_id: clmux workspace ID
        transcript_path: Path to conversation JSONL
        transcript_start_line: First line in transcript for this session
        start_key: Replay-idempotency key (pane:inode:gen:offset)
    """
    err = _require_session_backend()
    if err:
        return err
    try:
        conn = connect()
        try:
            return _session_start_impl(
                conn,
                repo_identity=repo_identity,
                branch=branch,
                head_commit=head_commit,
                objective=objective,
                claude_session_id=claude_session_id,
                pane_id=pane_id,
                workspace_id=workspace_id,
                transcript_path=transcript_path,
                transcript_start_line=transcript_start_line,
                start_key=start_key,
            )
        finally:
            conn.close()
    except SessionError as e:
        return _session_error(str(e))
    except Exception as e:
        return _safe_error(e, "session_start")


@mcp.tool()
async def session_delta(
    session_id: str,
    structured_facts: Dict[str, Any],
    delta_id: str,
) -> Dict[str, Any]:
    """Append a delta to a session. Fast path — no FTS/embeddings/sync.

    Args:
        session_id: Session UUID
        structured_facts: JSON object with structured data (branch, files, todos, etc.)
        delta_id: Transport-level idempotency key (pane:inode:gen:offset)
    """
    err = _require_session_backend()
    if err:
        return err
    try:
        conn = connect()
        try:
            return _session_delta_impl(
                conn,
                session_id=session_id,
                structured_facts=structured_facts,
                delta_id=delta_id,
            )
        finally:
            conn.close()
    except SessionError as e:
        return _session_error(str(e))
    except Exception as e:
        return _safe_error(e, "session_delta")


@mcp.tool()
async def session_close(
    session_id: str,
    summary: Optional[str] = None,
    outcome: Optional[str] = None,
    snapshot: Optional[Dict[str, Any]] = None,
    head_commit_end: Optional[str] = None,
) -> Dict[str, Any]:
    """Close a session. Triggers exact-once close state machine.

    Args:
        session_id: Session UUID
        summary: One-liner session summary
        outcome: What actually happened
        snapshot: Branch state snapshot (JSON object)
        head_commit_end: Commit hash at session end
    """
    err = _require_session_backend()
    if err:
        return err
    try:
        conn = connect()
        try:
            return _session_close_impl(
                conn,
                session_id=session_id,
                summary=summary,
                outcome=outcome,
                snapshot=snapshot,
                head_commit_end=head_commit_end,
            )
        finally:
            conn.close()
    except SessionError as e:
        return _session_error(str(e))
    except Exception as e:
        return _safe_error(e, "session_close")


@mcp.tool()
async def session_get(session_id: str) -> Dict[str, Any]:
    """Get a session with all its deltas.

    Args:
        session_id: Session UUID
    """
    err = _require_session_backend()
    if err:
        return err
    try:
        conn = connect()
        try:
            result = _session_get_impl(conn, session_id)
            if result is None:
                return _session_error(f"Session {session_id} not found")
            return result
        finally:
            conn.close()
    except Exception as e:
        return _safe_error(e, "session_get")


@mcp.tool()
async def session_list(
    repo_identity: Optional[str] = None,
    branch: Optional[str] = None,
    state: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """List sessions with optional filters.

    Args:
        repo_identity: Filter by repo identity
        branch: Filter by branch name
        state: Filter by state (open, closing, closed)
        limit: Maximum results (default 50)
    """
    err = _require_session_backend()
    if err:
        return err
    try:
        conn = connect()
        try:
            sessions = _session_list_impl(
                conn,
                repo_identity=repo_identity,
                branch=branch,
                state=state,
                limit=limit,
            )
            return {"sessions": sessions, "count": len(sessions)}
        finally:
            conn.close()
    except Exception as e:
        return _safe_error(e, "session_list")


@mcp.tool()
async def memory_scoped_search(
    query: str,
    scope: Literal["auto", "project", "global"] = "auto",
    repo_identity: Optional[str] = None,
    branch: Optional[str] = None,
    session_id: Optional[str] = None,
    top_k: int = 10,
) -> Dict[str, Any]:
    """Search memories with scoped retrieval across session, branch, and global tiers.

    Args:
        query: Search query text
        scope: "auto" (tiered fallback), "project" (repo-wide), or "global"
        repo_identity: Canonical repo ID (required for auto/project scope)
        branch: Git branch name (used for auto scope)
        session_id: Current session UUID (used for auto scope to search deltas)
        top_k: Maximum results per tier
    """
    err = _require_session_backend()
    if err:
        return err
    try:
        conn = connect()
        try:
            return _scoped_search_impl(
                conn,
                query,
                scope=scope,
                repo_identity=repo_identity,
                branch=branch,
                session_id=session_id,
                top_k=top_k,
            )
        finally:
            conn.close()
    except Exception as e:
        return _safe_error(e, "memory_scoped_search")


@mcp.tool()
async def branch_state_get(
    repo_identity: str,
    branch: str,
) -> Dict[str, Any]:
    """Get the current branch state snapshot.

    Args:
        repo_identity: Canonical repo ID
        branch: Git branch name
    """
    err = _require_session_backend()
    if err:
        return err
    try:
        conn = connect()
        try:
            result = _branch_state_get(conn, repo_identity, branch)
            if result is None:
                return {"snapshot": None, "message": "No state for this branch"}
            return result
        finally:
            conn.close()
    except Exception as e:
        return _safe_error(e, "branch_state_get")


# Graph functions moved to memora/graph/ module


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Memory MCP Server")
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

    result = _migrate_images_to_r2(dry_run=dry_run)

    if "error" in result:
        print(f"Error: {result['message']}")
        return

    print(json.dumps(result, indent=2))

    if result.get("errors"):
        print(f"\nWarning: {len(result['errors'])} errors occurred during migration")


if __name__ == "__main__":
    main()
