#!/usr/bin/env python3
"""Memora SessionStart hook - inject relevant memories into Claude Code context.

This script:
1. Reads session info from stdin (cwd, session_id)
2. Extracts project context from working directory
3. Searches memora for relevant memories
4. Returns additionalContext for Claude's system prompt
"""

import json
import os
import sys
from pathlib import Path


def load_memora_env():
    """Load memora environment variables from plugin .mcp.json or global settings."""
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    search_paths = []

    if plugin_root:
        search_paths.append(Path(plugin_root) / ".mcp.json")

    search_paths.extend([
        Path.home() / ".claude" / "settings.json",
        Path.home() / ".mcp.json",
        Path.cwd() / ".mcp.json",
    ])

    for mcp_path in search_paths:
        if mcp_path.exists():
            try:
                with open(mcp_path) as f:
                    config = json.load(f)
                servers = config.get("mcpServers", {})
                memora_config = servers.get("memora", {})
                env_vars = memora_config.get("env", {})
                for key, value in env_vars.items():
                    if key not in os.environ:
                        if isinstance(value, str) and value.startswith("~"):
                            value = os.path.expanduser(value)
                        os.environ[key] = str(value)
                return True
            except Exception:
                pass
    return False


def extract_project_context(cwd: str) -> dict:
    """Extract project identifiers from working directory."""
    path = Path(cwd)
    project_name = path.name

    queries = [project_name]
    for parent in list(path.parents)[:2]:
        if parent.name and parent.name not in ("", "Users", "home", "repos", "src"):
            queries.append(parent.name)

    return {
        "project_name": project_name,
        "cwd": cwd,
        "search_query": " ".join(queries[:3]),
    }


def search_memora(query: str, top_k: int = 5) -> list:
    """Search memora for relevant memories using direct storage import."""
    try:
        from memora import storage

        conn = storage.connect()
        results = storage.hybrid_search(
            conn,
            query=query,
            top_k=top_k,
            min_score=0.02,
        )
        conn.close()
        return results
    except ImportError:
        return []
    except Exception:
        return []


def format_memories(memories: list, max_chars: int = 1500) -> str:
    """Format memories concisely for context injection."""
    if not memories:
        return ""

    lines = ["## Relevant Memories (Memora)\n"]
    total_chars = len(lines[0])

    for item in memories:
        memory = item.get("memory", item)
        mid = memory.get("id", "?")
        content = memory.get("content", "")
        tags = memory.get("tags", [])

        if len(content) > 150:
            content = content[:150] + "..."

        tags_str = ", ".join(tags[:3]) if tags else ""
        entry = f"- [#{mid}] {content}"
        if tags_str:
            entry += f" ({tags_str})"
        entry += "\n"

        if total_chars + len(entry) > max_chars:
            lines.append("- ... more available via `memory_hybrid_search`\n")
            break

        lines.append(entry)
        total_chars += len(entry)

    lines.append("\nUse memora tools to search for more context.\n")
    return "".join(lines)


def main():
    """Main entry point for SessionStart hook."""
    try:
        load_memora_env()
        input_data = json.load(sys.stdin)
        cwd = input_data.get("cwd", os.getcwd())
        context = extract_project_context(cwd)
        memories = search_memora(context["search_query"], top_k=5)

        if memories:
            additional_context = format_memories(memories)
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": additional_context
                }
            }
        else:
            output = {}

        print(json.dumps(output))

    except Exception:
        print(json.dumps({}))

    sys.exit(0)


if __name__ == "__main__":
    main()
