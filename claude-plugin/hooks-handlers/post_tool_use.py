#!/usr/bin/env python3
"""Memora PostToolUse hook - auto-capture significant actions.

This script captures actions that have INHERENT CONTEXT:
- Git commits (commit message provides context)
- Test results (test output provides context)
- WebFetch research (URL and content provide context)
- Documentation edits (README, CLAUDE.md - content IS context)

It does NOT capture raw code edits (Edit/Write to source files) because:
- The hook only sees tool inputs/outputs, not conversation context
- Without knowing WHY a change was made, the capture is low-value noise
- Use manual memory_create for meaningful code change documentation
"""

import json
import os
import sys
import hashlib
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Tuple, List

# --- Configuration ---
SIGNIFICANCE_THRESHOLD = 0.6
CACHE_TTL_MINUTES = 30
MAX_CONTENT_LENGTH = 500

# --- Research Detection ---
RESEARCH_KEYWORDS = ["compare", "comparison", "difference", "vs", "versus", "alternative",
                     "features", "pros", "cons", "overview", "review", "analyze", "analysis"]
RESEARCH_URL_PATTERNS = [
    "github.com", "gitlab.com", "docs.", "documentation", "readme",
    "wiki", "blog", "medium.com", "dev.to", "stackoverflow", "arxiv.org",
]
MAX_RESEARCH_CONTENT_LENGTH = 1500

# --- Excluded Tool Prefixes ---
EXCLUDED_PREFIXES = ["mcp__memora__"]


def load_memora_env() -> dict:
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

    env_vars = {}
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
                return env_vars
            except Exception:
                pass
    return env_vars


def is_enabled(env_vars: dict) -> bool:
    """Check if auto-capture is enabled."""
    flag = env_vars.get("MEMORA_AUTO_CAPTURE", os.environ.get("MEMORA_AUTO_CAPTURE", "false"))
    return flag.lower() in ("true", "1", "yes")


def get_memora_storage():
    """Import and return memora storage module."""
    try:
        from memora import storage
        return storage
    except ImportError:
        return None


def is_excluded_tool(tool_name: str) -> bool:
    """Check if tool should be excluded from capture."""
    return any(tool_name.startswith(prefix) for prefix in EXCLUDED_PREFIXES)


def extract_content(tool_name: str, tool_input: dict, tool_result: dict) -> str:
    """Extract relevant content for analysis."""
    if tool_name == "Write":
        return tool_input.get("content", "")[:2000]
    elif tool_name == "Edit":
        old = tool_input.get("old_string", "")
        new = tool_input.get("new_string", "")
        return f"{old} -> {new}"[:2000]
    elif tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if isinstance(tool_result, dict):
            output = str(tool_result.get("output", tool_result.get("stdout", "")))[:1000]
        else:
            output = str(tool_result)[:1000]
        return f"{cmd}\n{output}"
    elif tool_name == "WebFetch":
        url = tool_input.get("url", "")
        prompt = tool_input.get("prompt", "")
        if isinstance(tool_result, dict):
            content = str(tool_result.get("output", tool_result.get("content", "")))[:3000]
        else:
            content = str(tool_result)[:3000]
        return f"URL: {url}\nPrompt: {prompt}\n{content}"
    return ""


def is_research_url(url: str) -> bool:
    """Check if URL matches research patterns."""
    url_lower = url.lower()
    return any(pattern in url_lower for pattern in RESEARCH_URL_PATTERNS)


def count_keyword_matches(content: str, keywords: List[str]) -> int:
    """Count keyword matches in content (case-insensitive)."""
    content_lower = content.lower()
    return sum(1 for kw in keywords if kw.lower() in content_lower)


def detect_webfetch_research(tool_input: dict, tool_result: dict) -> Tuple[Optional[str], float]:
    """Detect if WebFetch is research-worthy and calculate significance."""
    url = tool_input.get("url", "")
    prompt = tool_input.get("prompt", "")

    if isinstance(tool_result, dict):
        content = str(tool_result.get("output", tool_result.get("content", "")))
    else:
        content = str(tool_result)

    combined_text = f"{prompt} {content}".lower()
    keyword_matches = count_keyword_matches(combined_text, RESEARCH_KEYWORDS)

    score = 0.0

    if "github.com" in url.lower() and "/blob/" not in url.lower():
        score += 0.5
        if url.rstrip("/").count("/") <= 4:
            score += 0.2
    elif any(p in url.lower() for p in ["docs.", "documentation", "wiki"]):
        score += 0.4
    elif is_research_url(url):
        score += 0.3

    if keyword_matches > 0:
        score += min(keyword_matches * 0.1, 0.3)

    if len(content) > 500:
        score += 0.1

    if "github.com" in url.lower():
        capture_type = "research-github"
    elif any(p in url.lower() for p in ["docs.", "documentation"]):
        capture_type = "research-docs"
    elif keyword_matches >= 2:
        capture_type = "research-comparison"
    else:
        capture_type = "research-general"

    if score >= SIGNIFICANCE_THRESHOLD:
        return capture_type, min(score, 1.0)

    return None, 0.0


def summarize_research_content(url: str, prompt: str, content: str, max_length: int = MAX_RESEARCH_CONTENT_LENGTH) -> str:
    """Summarize WebFetch research content for storage."""
    lines = []

    if "github.com" in url.lower():
        parts = url.rstrip("/").split("/")
        if len(parts) >= 5:
            owner, repo = parts[3], parts[4]
            lines.append(f"**Repository:** {owner}/{repo}")

    lines.append(f"**URL:** {url}")

    if prompt:
        lines.append(f"**Query:** {prompt}")

    lines.append("")
    lines.append("**Key Findings:**")

    content_lines = content.split("\n")
    extracted = []

    for line in content_lines:
        line = line.strip()
        if not line:
            continue

        if line.startswith("#"):
            current_section = line.lstrip("#").strip()
            if len(extracted) < 20:
                extracted.append(f"\n**{current_section}**")
        elif line.startswith(("-", "*", "\u2022")) or ":" in line[:50]:
            if len(extracted) < 30:
                extracted.append(line[:200])
        elif any(kw in line.lower() for kw in ["feature", "support", "provide", "include", "enable"]):
            if len(extracted) < 30:
                extracted.append(f"- {line[:200]}")

    if not extracted:
        extracted = [content[:max_length]]

    lines.extend(extracted)
    result = "\n".join(lines)

    if len(result) > max_length:
        result = result[:max_length] + "\n\n[... truncated]"

    return result


def detect_capture_type(tool_name: str, tool_input: dict, tool_result: dict) -> Tuple[Optional[str], float]:
    """Detect capture type and calculate significance score."""
    if tool_name == "WebFetch":
        return detect_webfetch_research(tool_input, tool_result)

    content = extract_content(tool_name, tool_input, tool_result)
    command = tool_input.get("command", "")

    if tool_name == "Bash" and "git commit" in command:
        return "git-commit", 0.8

    test_patterns = ["pytest", "npm test", "cargo test", "go test", "jest", "vitest", "make test"]
    if tool_name == "Bash" and any(p in command for p in test_patterns):
        if isinstance(tool_result, dict):
            output = str(tool_result.get("output", tool_result.get("stdout", "")))
        else:
            output = str(tool_result)
        if any(kw in output for kw in ["passed", "failed", "PASSED", "FAILED", "error", "Error"]):
            return "test-result", 0.7

    if tool_name in ("Edit", "Write"):
        file_path = tool_input.get("file_path", "")
        file_name = Path(file_path).name if file_path else ""
        doc_patterns = ["README", "CLAUDE.md", "CONTRIBUTING", "CHANGELOG", "LICENSE"]
        if any(p in file_name.upper() for p in doc_patterns):
            return "documentation", 0.7
        return None, 0.0

    return None, 0.0


def compute_content_hash(capture_type: str, tool_name: str, tool_input: dict) -> str:
    """Generate hash for deduplication."""
    if tool_name == "WebFetch":
        key_parts = [capture_type, tool_name, tool_input.get("url", "")]
    else:
        key_parts = [
            capture_type, tool_name,
            tool_input.get("file_path", ""),
            tool_input.get("command", "")[:100],
        ]
    return hashlib.md5("|".join(key_parts).encode()).hexdigest()[:16]


def load_cache(session_id: str) -> dict:
    """Load capture cache for session."""
    cache_file = Path(f"/tmp/memora_capture_cache_{session_id}.json")
    if cache_file.exists():
        try:
            with open(cache_file) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_cache(session_id: str, cache: dict):
    """Save capture cache for session."""
    cache_file = Path(f"/tmp/memora_capture_cache_{session_id}.json")
    try:
        with open(cache_file, "w") as f:
            json.dump(cache, f)
    except Exception:
        pass


def is_duplicate(content_hash: str, session_id: str) -> bool:
    """Check if action was recently captured."""
    cache = load_cache(session_id)
    now = datetime.now()

    cache = {
        k: v for k, v in cache.items()
        if now - datetime.fromisoformat(v) < timedelta(minutes=CACHE_TTL_MINUTES)
    }

    if content_hash in cache:
        return True

    cache[content_hash] = now.isoformat()
    save_cache(session_id, cache)
    return False


def format_memory_content(
    capture_type: str,
    tool_name: str,
    tool_input: dict,
    tool_result: dict,
    cwd: str,
) -> str:
    """Format memory content for storage."""
    if tool_name == "WebFetch":
        url = tool_input.get("url", "")
        prompt = tool_input.get("prompt", "")
        if isinstance(tool_result, dict):
            content = str(tool_result.get("output", tool_result.get("content", "")))
        else:
            content = str(tool_result)

        titles = {
            "research-github": "GitHub Repository Research",
            "research-docs": "Documentation Research",
            "research-comparison": "Comparison Research",
            "research-general": "Web Research",
        }
        title = titles.get(capture_type, "Research")
        project = Path(cwd).name if cwd else "unknown"
        header = f"{title}\n\n**Project:** {project}\n"
        summary = summarize_research_content(url, prompt, content)
        return header + summary

    file_path = tool_input.get("file_path", "")
    command = tool_input.get("command", "")
    project = Path(cwd).name if cwd else "unknown"

    if capture_type == "git-commit":
        import subprocess

        commit_hash = ""
        commit_msg = ""

        try:
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, cwd=cwd, timeout=5
            )
            if result.returncode == 0:
                commit_hash = result.stdout.strip()

            result = subprocess.run(
                ["git", "log", "-1", "--pretty=%s"],
                capture_output=True, text=True, cwd=cwd, timeout=5
            )
            if result.returncode == 0:
                commit_msg = result.stdout.strip()
        except Exception:
            pass

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

        if commit_hash and commit_msg:
            return f"- `{commit_hash}` [{timestamp}] {commit_msg}"
        elif commit_msg:
            return f"- [{timestamp}] {commit_msg}"
        elif commit_hash:
            return f"- `{commit_hash}` [{timestamp}] (message not captured)"
        else:
            return f"- [{timestamp}] (commit not captured)"

    if capture_type == "test-result":
        if isinstance(tool_result, dict):
            output = str(tool_result.get("output", tool_result.get("stdout", "")))[:MAX_CONTENT_LENGTH]
        else:
            output = str(tool_result)[:MAX_CONTENT_LENGTH]

        lines = ["Test Results", ""]
        lines.append(f"**Project:** {project}")
        lines.append(f"**Command:** `{command[:150]}`")
        if output:
            lines.append(f"\n**Output:**\n```\n{output}\n```")
        return "\n".join(lines)

    if capture_type == "documentation":
        file_name = Path(file_path).name if file_path else "unknown"
        content = tool_input.get("content", "") or tool_input.get("new_string", "")

        lines = [f"Documentation Update: {file_name}", ""]
        lines.append(f"**Project:** {project}")
        lines.append(f"**File:** {file_path}")

        if content:
            preview = content[:1000]
            lines.append(f"\n**Content:**\n```\n{preview}\n```")
            if len(content) > 1000:
                lines.append("\n[... truncated]")

        return "\n".join(lines)

    return f"Auto-captured: {capture_type}\n\n**Project:** {project}"


def find_existing_memory(storage, conn, content: str, capture_type: str, project: str, file_path: str = "") -> Optional[dict]:
    """Search for existing memory that could be updated instead of creating new."""
    try:
        if file_path:
            results = storage.list_memories(
                conn,
                metadata_filters={"file_path": file_path, "capture_type": capture_type},
                limit=1,
            )
            if results:
                return results[0]

        results = storage.hybrid_search(
            conn,
            query=f"{project} {capture_type} {content[:100]}",
            top_k=5,
            min_score=0.15,
            tags_any=[f"memora/auto-capture/{capture_type}"],
        )

        for result in results:
            memory = result.get("memory", {})
            mem_metadata = memory.get("metadata", {}) or {}
            if mem_metadata.get("project") == project:
                return memory

        return None
    except Exception:
        return None


def find_hierarchy_placement(storage, conn, capture_type: str, project: str) -> dict:
    """Find appropriate hierarchy placement based on existing memories."""
    try:
        category_mapping = {
            "git-commit": "commits",
            "test-result": "testing",
            "documentation": "docs",
            "research-github": "research",
            "research-docs": "research",
            "research-comparison": "research",
            "research-general": "research",
        }
        category = category_mapping.get(capture_type, "auto-capture")

        project_memories = storage.hybrid_search(
            conn, query=project, top_k=5, min_score=0.1,
        )

        for result in project_memories:
            memory = result.get("memory", {})
            mem_metadata = memory.get("metadata", {}) or {}
            section = mem_metadata.get("section", "")
            if section == project or section.startswith(f"{project}/"):
                return {"section": project, "subsection": category}

        results = storage.hybrid_search(
            conn,
            query=f"{project} {capture_type}",
            top_k=3, min_score=0.1,
            tags_any=["memora/auto-capture"],
        )

        for result in results:
            memory = result.get("memory", {})
            mem_metadata = memory.get("metadata", {}) or {}
            if mem_metadata.get("section"):
                return {
                    "section": mem_metadata.get("section"),
                    "subsection": mem_metadata.get("subsection", category),
                }

        return {"section": project, "subsection": category}
    except Exception:
        return {"section": project, "subsection": "auto-capture"}


def get_memory_type_config(capture_type: str, tool_result: dict) -> dict:
    """Determine memory type and metadata based on capture type."""
    if capture_type == "git-commit":
        return {
            "memory_type": "regular",
            "tags": ["memora/auto-capture", "memora/auto-capture/git-commit"],
            "metadata": {"type": "auto-capture", "capture_type": "git-commit"}
        }

    if capture_type == "test-result":
        if isinstance(tool_result, dict):
            output = str(tool_result.get("output", tool_result.get("stdout", "")))
        else:
            output = str(tool_result)

        has_failures = any(kw in output.lower() for kw in ["failed", "error", "failure"])

        if has_failures:
            return {
                "memory_type": "issue",
                "tags": ["memora/issues", "memora/auto-capture"],
                "metadata": {
                    "type": "issue", "status": "open",
                    "severity": "major", "category": "testing",
                }
            }
        else:
            return {
                "memory_type": "regular",
                "tags": ["memora/auto-capture", "memora/auto-capture/test-result"],
                "metadata": {"type": "auto-capture", "capture_type": "test-result"}
            }

    if capture_type == "documentation":
        return {
            "memory_type": "regular",
            "tags": ["memora/knowledge", "memora/auto-capture"],
            "metadata": {"type": "auto-capture", "capture_type": "documentation"}
        }

    if capture_type.startswith("research-"):
        return {
            "memory_type": "regular",
            "tags": ["memora/auto-capture", "memora/auto-capture/research"],
            "metadata": {"type": "auto-capture", "capture_type": capture_type}
        }

    return {
        "memory_type": "regular",
        "tags": ["memora/auto-capture"],
        "metadata": {"type": "auto-capture", "capture_type": capture_type}
    }


def _handle_git_commit_log(storage, conn, commit_entry: str, project: str, cwd: str, session_id: str):
    """Handle git commits by maintaining a single log memory per project."""
    try:
        results = storage.list_memories(
            conn,
            metadata_filters={"capture_type": "git-commits-log", "project": project},
            limit=1,
        )

        if results:
            existing = results[0]
            existing_content = existing.get("content", "")
            updated_content = f"{existing_content}\n{commit_entry}"

            storage.update_memory(conn, memory_id=existing["id"], content=updated_content)
            conn.close()

            try:
                storage.sync_to_cloud()
            except Exception:
                pass

            return existing, "updated"

        content = f"## Git Commits: {project}\n\n{commit_entry}"
        metadata = {
            "type": "auto-capture",
            "capture_type": "git-commits-log",
            "project": project,
            "cwd": cwd,
            "session_id": session_id,
            "section": project,
            "subsection": "commits",
        }
        tags = ["memora/auto-capture", "memora/auto-capture/git-commits"]

        memory = storage.add_memory(conn, content=content, metadata=metadata, tags=tags)
        conn.close()

        try:
            storage.sync_to_cloud()
        except Exception:
            pass

        return memory, "created"

    except Exception:
        return None, "error"


def find_or_create_memory(
    storage, content: str, capture_type: str, tool_name: str,
    tool_input: dict, tool_result: dict, cwd: str,
    session_id: str, significance_score: float,
):
    """Find existing memory to update, or create new one."""
    try:
        conn = storage.connect()
        project = Path(cwd).name if cwd else "unknown"

        type_config = get_memory_type_config(capture_type, tool_result)

        if capture_type == "git-commit":
            return _handle_git_commit_log(storage, conn, content, project, cwd, session_id)

        file_path = tool_input.get("file_path", "")
        existing = find_existing_memory(storage, conn, content, capture_type, project, file_path)

        if existing:
            existing_content = existing.get("content", "")
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            updated_content = f"{existing_content}\n\n---\n**[{timestamp}]**\n{content}"

            storage.update_memory(conn, memory_id=existing["id"], content=updated_content)
            conn.close()

            try:
                storage.sync_to_cloud()
            except Exception:
                pass

            return existing, "updated"

        hierarchy = find_hierarchy_placement(storage, conn, capture_type, project)

        tags = type_config["tags"]
        metadata = type_config["metadata"].copy()

        metadata.update({
            "tool_name": tool_name,
            "project": project,
            "cwd": cwd,
            "session_id": session_id,
            "significance_score": significance_score,
            "section": hierarchy["section"],
            "subsection": hierarchy["subsection"],
        })

        if file_path:
            metadata["file_path"] = file_path

        url = tool_input.get("url", "")
        if url:
            metadata["url"] = url

        if type_config["memory_type"] == "issue" and file_path:
            path_parts = Path(file_path).parts
            if len(path_parts) > 1:
                metadata["component"] = path_parts[-2] if path_parts[-2] != "src" else path_parts[-1].replace(".py", "")

        memory = storage.add_memory(conn, content=content, metadata=metadata, tags=tags)
        conn.close()

        try:
            storage.sync_to_cloud()
        except Exception:
            pass

        return memory, "created"
    except Exception:
        return None, "error"


def main():
    """Main entry point for PostToolUse hook."""
    try:
        env_vars = load_memora_env()

        if not is_enabled(env_vars):
            print(json.dumps({}))
            sys.exit(0)

        input_data = json.load(sys.stdin)

        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {})
        tool_result = input_data.get("tool_result", {})
        session_id = input_data.get("session_id", "unknown")
        cwd = input_data.get("cwd", "")

        if is_excluded_tool(tool_name):
            print(json.dumps({}))
            sys.exit(0)

        capture_type, significance = detect_capture_type(tool_name, tool_input, tool_result)

        if not capture_type or significance < SIGNIFICANCE_THRESHOLD:
            print(json.dumps({}))
            sys.exit(0)

        content_hash = compute_content_hash(capture_type, tool_name, tool_input)
        if is_duplicate(content_hash, session_id):
            print(json.dumps({}))
            sys.exit(0)

        storage = get_memora_storage()
        if not storage:
            print(json.dumps({}))
            sys.exit(0)

        content = format_memory_content(capture_type, tool_name, tool_input, tool_result, cwd)
        memory, action = find_or_create_memory(
            storage, content, capture_type, tool_name, tool_input, tool_result,
            cwd, session_id, significance
        )

        if memory:
            if action == "updated":
                output = {"systemMessage": f"[Memora] Updated: {capture_type} (#{memory.get('id', '?')})"}
            else:
                output = {"systemMessage": f"[Memora] Captured: {capture_type} (#{memory.get('id', '?')})"}
            print(json.dumps(output))
        else:
            print(json.dumps({}))

    except Exception:
        print(json.dumps({}))

    sys.exit(0)


if __name__ == "__main__":
    main()
