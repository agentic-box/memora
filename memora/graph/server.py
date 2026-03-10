"""HTTP server and routes for graph visualization."""

import asyncio
import functools
import logging
import os
import socket
import sys
import threading
from copy import deepcopy
from importlib.metadata import version as get_version

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from sse_starlette.sse import EventSourceResponse

import json
from importlib.resources import files as _pkg_files

from .data import get_graph_data, get_memory_for_api
from ..storage import connect, add_memory, update_memory, delete_memory, get_memory, hybrid_search, rewrite_query, multi_query_hybrid_search, _get_llm_client, LLM_MODEL


CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_memory",
            "description": "Create a new memory in the knowledge base. Use when the user asks to save, create, add, or remember something.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "The full text content of the memory."},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags to categorize the memory."},
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_memory",
            "description": "Update an existing memory by ID. Use when the user asks to modify, edit, or change a specific memory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "integer", "description": "The ID of the memory to update."},
                    "content": {"type": "string", "description": "New full text content. Replaces existing content."},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "New tags. Replaces all existing tags."},
                },
                "required": ["memory_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_memory",
            "description": "Delete a memory by ID. Use when the user asks to remove or delete a specific memory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "integer", "description": "The ID of the memory to delete."},
                },
                "required": ["memory_id"],
            },
        },
    },
]


def _execute_chat_tool(tool_name: str, arguments: dict) -> str:
    """Execute a chat tool call and return result as JSON string."""
    conn = connect()
    try:
        if tool_name == "create_memory":
            result = add_memory(conn, content=arguments["content"], tags=arguments.get("tags"))
            return json.dumps({"success": True, "action": "created", "memory_id": result["id"], "preview": result["content"][:100]})

        elif tool_name == "update_memory":
            mid = arguments["memory_id"]
            result = update_memory(conn, mid, content=arguments.get("content"), tags=arguments.get("tags"))
            if result is None:
                return json.dumps({"success": False, "error": f"Memory #{mid} not found."})
            return json.dumps({"success": True, "action": "updated", "memory_id": mid, "preview": result["content"][:100]})

        elif tool_name == "delete_memory":
            mid = arguments["memory_id"]
            existing = get_memory(conn, mid)
            if not existing:
                return json.dumps({"success": False, "error": f"Memory #{mid} not found."})
            delete_memory(conn, mid)
            return json.dumps({"success": True, "action": "deleted", "memory_id": mid, "preview": existing["content"][:100]})

        return json.dumps({"success": False, "error": f"Unknown tool: {tool_name}"})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)[:200]})
    finally:
        conn.close()


logger = logging.getLogger(__name__)


def _get_memora_version() -> str:
    try:
        return get_version("memora")
    except Exception as exc:
        logger.debug("Unable to read memora package version: %s", exc)
        return ""


def _serialize_memory_api_result(memory: dict) -> dict:
    """Normalize a memory record to the graph API shape."""
    meta = memory.get("metadata") or {}
    return {
        "id": memory["id"],
        "content": memory["content"],
        "tags": memory.get("tags", []),
        "created": memory.get("created_at", ""),
        "updated": memory.get("updated_at"),
        "metadata": meta,
    }


def _normalize_host_for_connect(host: str) -> str:
    """Convert wildcard bind addresses to connectable localhost."""
    if host in ("0.0.0.0", "::", ""):
        return "127.0.0.1"
    return host


def _check_port_status(host: str, port: int) -> str:
    """Check port status and identify what's running.

    Returns:
        "free" - port is available
        "memora" - our graph server is running
        "other" - something else is using the port
    """
    connect_host = _normalize_host_for_connect(host)

    # First, quick check if port is in use
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        try:
            s.connect((connect_host, port))
        except (OSError, socket.timeout):
            return "free"

    # Port is in use - verify it's our graph server
    try:
        import urllib.request
        url = f"http://{connect_host}:{port}/api/graph"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = resp.read().decode()
            # Check for our specific response structure
            if '"nodes"' in data or '"count"' in data:
                return "memora"
    except Exception as exc:
        logger.debug("Port %s probe could not verify memora server: %s", port, exc)

    return "other"


def start_graph_server(host: str, port: int) -> None:
    """Start background HTTP server for graph visualization.

    This server provides:
    - /graph: SPA HTML page
    - /api/graph: Graph data API
    - /api/memories/{id}: Individual memory API
    - /r2/{path}: R2 image proxy

    Args:
        host: Host to bind to
        port: Port to bind to
    """
    port_status = _check_port_status(host, port)
    if port_status == "memora":
        print(f"Graph server already running on port {port}, reusing existing", file=sys.stderr)
        return
    elif port_status == "other":
        print(f"Port {port} is in use by another service, skipping graph server", file=sys.stderr)
        return

    from starlette.applications import Starlette
    from starlette.routing import Route

    def _load_spa_html(version: str) -> str:
        html = _pkg_files("memora.graph").joinpath("index.html").read_text("utf-8")
        config = json.dumps({
            "version": version,
            "r2Prefix": "/r2/",
            "dbSelector": False,
            "wsUrl": None,
            "sseUrl": "/api/events",
        })
        return html.replace(
            "</head>",
            f"<script>window.MEMORA_CONFIG={config};</script>\n</head>",
        )

    GRAPH_HTML = _load_spa_html(version=_get_memora_version())

    async def graph_handler(request: Request):
        """Serve the static graph SPA."""
        return HTMLResponse(GRAPH_HTML)

    async def api_graph(request: Request):
        """API endpoint: Get graph nodes and edges."""
        try:
            min_score = float(request.query_params.get("min_score", 0.25))
            rebuild = request.query_params.get("rebuild", "").lower() == "true"
            result = get_graph_data(min_score, rebuild=rebuild)
            return JSONResponse(result)
        except Exception as e:
            logger.exception("Graph API request failed: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def api_memory(request: Request):
        """API endpoint: Get a single memory by ID."""
        try:
            memory_id = int(request.path_params.get("id"))
            result = get_memory_for_api(memory_id)
            if result.get("error") == "not_found":
                return JSONResponse(result, status_code=404)
            return JSONResponse(result)
        except Exception as e:
            logger.exception("Graph memory API request failed: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def api_memories_list(request: Request):
        """API endpoint: Get memories for timeline with pagination."""
        try:
            limit = max(1, min(int(request.query_params.get("limit", "50")), 200))
            offset = max(0, int(request.query_params.get("offset", "0")))

            conn = connect()
            total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            rows = conn.execute(
                """SELECT id, content, created_at, updated_at, tags, metadata
                   FROM memories ORDER BY created_at DESC
                   LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
            conn.close()
            memories = []
            for row in rows:
                memories.append({
                    "id": row["id"],
                    "content": row["content"],
                    "created": row["created_at"].split(" ")[0] if row["created_at"] else "",
                    "updated": row["updated_at"].split(" ")[0] if row["updated_at"] else None,
                    "tags": json.loads(row["tags"]) if row["tags"] else [],
                    "metadata": json.loads(row["metadata"]) if row["metadata"] else {},
                })
            return JSONResponse({
                "memories": memories,
                "total": total,
                "limit": limit,
                "offset": offset,
            })
        except Exception as e:
            logger.exception("Graph memories list API request failed: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def api_actions(request: Request):
        """API endpoint: Get action history."""
        try:
            from ..storage import get_action_history
            limit = int(request.query_params.get("limit", "200"))
            conn = connect()
            actions = get_action_history(conn, limit=limit)
            conn.close()
            return JSONResponse({"actions": actions})
        except Exception as e:
            logger.exception("Graph actions API request failed: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def graph_events(request: Request):
        """SSE endpoint for graph update notifications."""
        async def event_generator():
            last_count = None
            last_modified = None
            while True:
                try:
                    conn = connect()
                    row = conn.execute(
                        """SELECT COUNT(*) as cnt,
                           MAX(COALESCE(updated_at, created_at)) as latest
                           FROM memories"""
                    ).fetchone()
                    conn.close()

                    current_count = row["cnt"] if row else 0
                    current_modified = row["latest"] if row else None

                    # Detect changes (create, update, or delete)
                    if last_count is not None and (
                        current_count != last_count or current_modified != last_modified
                    ):
                        yield {"event": "graph-updated", "data": "refresh"}

                    last_count = current_count
                    last_modified = current_modified
                except Exception:
                    logger.debug("SSE graph change poll failed", exc_info=True)

                await asyncio.sleep(2)  # Check every 2 seconds

        return EventSourceResponse(event_generator())

    async def api_memory_patch(request: Request):
        """API endpoint: Update tags and/or metadata for a memory."""
        try:
            memory_id = int(request.path_params.get("id"))
            body = await request.json()
            tags = body.get("tags")
            metadata = body.get("metadata")
            favorite = body.get("favorite")

            conn = connect()
            existing = get_memory(conn, memory_id)
            if not existing:
                conn.close()
                return JSONResponse({"error": "not_found"}, status_code=404)

            merged_metadata = deepcopy(existing.get("metadata") or {})
            if metadata is not None:
                if not isinstance(metadata, dict):
                    conn.close()
                    return JSONResponse({"error": "invalid_metadata"}, status_code=400)
                merged_metadata = metadata
            if favorite is not None:
                if bool(favorite):
                    merged_metadata["favorite"] = True
                else:
                    merged_metadata.pop("favorite", None)

            if tags is not None and not isinstance(tags, list):
                conn.close()
                return JSONResponse({"error": "invalid_tags"}, status_code=400)

            result = update_memory(
                conn,
                memory_id,
                metadata=merged_metadata if metadata is not None or favorite is not None else None,
                tags=tags,
            )
            conn.close()
            if result is None:
                return JSONResponse({"error": "not_found"}, status_code=404)
            return JSONResponse(_serialize_memory_api_result(result))
        except ValueError as e:
            logger.exception("Graph memory patch validation failed: %s", e)
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:
            logger.exception("Graph memory patch API request failed: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def r2_image_proxy(request: Request):
        """Proxy images from R2 storage."""
        try:
            from ..image_storage import get_image_storage_instance

            image_storage = get_image_storage_instance()
            if not image_storage:
                return JSONResponse({"error": "R2 not configured"}, status_code=503)

            key = request.path_params.get("path", "")
            if not key:
                return JSONResponse({"error": "No path provided"}, status_code=400)

            try:
                response = image_storage.s3_client.get_object(
                    Bucket=image_storage.bucket,
                    Key=key,
                )
                image_data = response["Body"].read()
                content_type = response.get("ContentType", "image/jpeg")

                return Response(
                    content=image_data,
                    media_type=content_type,
                    headers={"Cache-Control": "public, max-age=86400"},
                )
            except Exception as e:
                logger.debug("R2 image proxy could not load key '%s': %s", key, e)
                return JSONResponse({"error": f"Image not found: {e}"}, status_code=404)

        except Exception as e:
            logger.exception("R2 image proxy request failed: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def api_chat(request: Request):
        """API endpoint: Chat about memories using LLM with RAG."""
        try:
            body = await request.json()
            message = body.get("message", "").strip()
            history = body.get("history", [])

            if not message:
                return JSONResponse({"error": "empty_message"}, status_code=400)

            client = _get_llm_client()
            if not client:
                return JSONResponse(
                    {"error": "llm_not_configured",
                     "message": "LLM not configured. Set OPENAI_API_KEY and OPENAI_BASE_URL environment variables."},
                    status_code=503,
                )

            # Rewrite query for improved retrieval, then multi-query search
            loop = asyncio.get_event_loop()

            rewrite_result = await loop.run_in_executor(
                None, functools.partial(rewrite_query, message, max_queries=3)
            )
            queries = rewrite_result["queries"]
            filters = rewrite_result.get("filters", {})

            conn = connect()
            try:
                results = await loop.run_in_executor(
                    None,
                    functools.partial(
                        multi_query_hybrid_search,
                        conn,
                        queries,
                        top_k=8,
                        date_from=filters.get("date_from"),
                        date_to=filters.get("date_to"),
                        tags_any=filters.get("tags_any"),
                    ),
                )
            finally:
                conn.close()

            # Build context from search results
            references = []
            context_parts = []
            for r in results:
                mem = r.get("memory", r)
                score = r.get("score", 0.0)
                references.append({
                    "id": mem["id"],
                    "score": round(score, 3),
                    "preview": mem["content"][:100].replace("\n", " "),
                })
                tags_str = ", ".join(mem.get("tags", []))
                content_truncated = mem["content"][:500]
                context_parts.append(
                    f"Memory #{mem['id']} (tags: {tags_str}):\n{content_truncated}"
                )

            context_block = "\n\n---\n\n".join(context_parts) if context_parts else "No relevant memories found."

            system_msg = {
                "role": "system",
                "content": (
                    "You are a helpful assistant for the user's personal knowledge base (Memora).\n"
                    "Use the following memories as context. When referencing a memory, cite it as [Memory #<id>].\n"
                    "If the memories don't contain relevant information, say so honestly.\n\n"
                    "## Tool Use — IMPORTANT\n\n"
                    "You have tools to create, update, and delete memories. You MUST call the appropriate tool when the user asks to:\n"
                    "- Create/save/add/remember something → call create_memory\n"
                    "- Update/edit/modify a memory → call update_memory\n"
                    "- Delete/remove a memory → call delete_memory\n\n"
                    "ALWAYS call the tool directly. Do NOT ask for confirmation, do NOT say you can't find the memory, "
                    "do NOT suggest content without calling the tool.\n"
                    "The memory database has many more entries than what's shown in context below — "
                    "if the user references a memory ID, trust them and call the tool.\n"
                    "When creating a memory, write substantive, well-structured content.\n"
                    "When updating, apply the user's requested changes to the existing content.\n\n"
                    f"## Relevant Memories\n\n{context_block}"
                ),
            }

            # Build messages: system + last 20 history messages + current
            trimmed_history = history[-20:]
            messages = [system_msg] + trimmed_history + [{"role": "user", "content": message}]

            async def event_generator():
                # Emit references first
                yield {"event": "references", "data": json.dumps(references)}

                # Stream LLM response via thread bridge
                queue: asyncio.Queue = asyncio.Queue()

                def run_llm():
                    try:
                        chat_model = os.getenv("CHAT_MODEL", "") or LLM_MODEL

                        # First LLM call — may produce tool_calls
                        stream = client.chat.completions.create(
                            model=chat_model,
                            messages=messages,
                            tools=CHAT_TOOLS,
                            stream=True,
                            temperature=0.7,
                            max_tokens=2000,
                        )

                        content_text = ""
                        tool_calls_by_index = {}

                        for chunk in stream:
                            delta = chunk.choices[0].delta

                            # Content tokens — stream immediately
                            if delta.content:
                                content_text += delta.content
                                loop.call_soon_threadsafe(queue.put_nowait, ("token", delta.content))

                            # Tool call deltas — accumulate
                            if delta.tool_calls:
                                for tc in delta.tool_calls:
                                    idx = tc.index
                                    if idx not in tool_calls_by_index:
                                        tool_calls_by_index[idx] = {"id": "", "name": "", "arguments": ""}
                                    entry = tool_calls_by_index[idx]
                                    if tc.id:
                                        entry["id"] = tc.id
                                    if tc.function and tc.function.name:
                                        entry["name"] = tc.function.name
                                    if tc.function and tc.function.arguments:
                                        entry["arguments"] += tc.function.arguments

                        # No tool calls — done
                        if not tool_calls_by_index:
                            loop.call_soon_threadsafe(queue.put_nowait, ("done", ""))
                            return

                        # Execute tool calls
                        tool_results = []
                        for idx in sorted(tool_calls_by_index.keys()):
                            tc = tool_calls_by_index[idx]
                            try:
                                args = json.loads(tc["arguments"])
                            except json.JSONDecodeError:
                                args = {}

                            result_str = _execute_chat_tool(tc["name"], args)

                            # Emit action event to frontend
                            action_data = json.loads(result_str)
                            action_data["tool"] = tc["name"]
                            loop.call_soon_threadsafe(queue.put_nowait, ("action", json.dumps(action_data)))

                            tool_results.append({"role": "tool", "tool_call_id": tc["id"], "content": result_str})

                        # Second LLM call with tool results (no tools — prevent loops)
                        assistant_msg = {
                            "role": "assistant",
                            "content": content_text or None,
                            "tool_calls": [
                                {
                                    "id": tool_calls_by_index[i]["id"],
                                    "type": "function",
                                    "function": {
                                        "name": tool_calls_by_index[i]["name"],
                                        "arguments": tool_calls_by_index[i]["arguments"],
                                    },
                                }
                                for i in sorted(tool_calls_by_index.keys())
                            ],
                        }

                        stream2 = client.chat.completions.create(
                            model=chat_model,
                            messages=messages + [assistant_msg] + tool_results,
                            stream=True,
                            temperature=0.7,
                            max_tokens=2000,
                        )
                        for chunk in stream2:
                            delta = chunk.choices[0].delta
                            if delta.content:
                                loop.call_soon_threadsafe(queue.put_nowait, ("token", delta.content))

                        loop.call_soon_threadsafe(queue.put_nowait, ("done", ""))
                    except Exception as e:
                        loop.call_soon_threadsafe(queue.put_nowait, ("error", str(e)[:200]))

                llm_thread = threading.Thread(target=run_llm, daemon=True)
                llm_thread.start()

                while True:
                    event_type, data = await queue.get()
                    if event_type == "token":
                        yield {"event": "token", "data": json.dumps(data)}
                    elif event_type == "action":
                        yield {"event": "action", "data": data}
                    elif event_type in ("done", "error"):
                        yield {"event": event_type, "data": data}
                        break

            return EventSourceResponse(event_generator())

        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    app = Starlette(
        routes=[
            Route("/graph", graph_handler),
            Route("/api/graph", api_graph),
            Route("/api/events", graph_events),
            Route("/api/chat", api_chat, methods=["POST"]),
            Route("/api/memories", api_memories_list),
            Route("/api/memories/{id:int}", api_memory),
            Route("/api/memories/{id:int}", api_memory_patch, methods=["PATCH"]),
            Route("/api/memories/{id:int}/favorite", api_memory_patch, methods=["PATCH"]),
            Route("/api/actions", api_actions),
            Route("/r2/{path:path}", r2_image_proxy),
        ]
    )

    def run_server():
        import uvicorn

        config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        server = uvicorn.Server(config)
        # SO_REUSEADDR is set by default in uvicorn, but we ensure quick restart
        server.run()

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()

    # Get bucket name for unique URL
    bucket_name = ""
    try:
        from ..storage import STORAGE_BACKEND
        if hasattr(STORAGE_BACKEND, 'bucket'):
            bucket_name = STORAGE_BACKEND.bucket
    except Exception:
        logger.debug("Unable to include bucket param in graph URL", exc_info=True)

    bucket_param = f"?bucket={bucket_name}" if bucket_name else ""
    print(f"Graph visualization available at http://{host}:{port}/graph{bucket_param}", file=sys.stderr)
