"""Memory MCP server package."""

import json
import os
from importlib.metadata import version as _meta_version, PackageNotFoundError as _PNF
from pathlib import Path as _Path


def _bootstrap_mcp_env() -> None:
    """Walk up from cwd to find .mcp.json and set memora env vars (if not already set).

    Allows the clmux daemon (and any non-MCP caller) to spawn `python3 -m memora.cli`
    without inheriting the MCP env from its launch context. Runs at package import
    time so MEMORA_STORAGE_URI / MEMORA_ALLOW_ANY_TAG / etc. are visible to the
    module-level TAG_WHITELIST computation below.
    """
    cwd = _Path.cwd()
    for parent in [cwd, *cwd.parents]:
        mcp_path = parent / ".mcp.json"
        if not mcp_path.is_file():
            continue
        try:
            data = json.loads(mcp_path.read_text())
        except Exception:
            return
        env = data.get("mcpServers", {}).get("memora", {}).get("env", {})
        for key, value in env.items():
            os.environ.setdefault(key, str(value))
        return


_bootstrap_mcp_env()


def _get_version() -> str:
    """Read version from package metadata or pyproject.toml fallback."""
    try:
        return _meta_version("memora-mcp")
    except _PNF:
        pass
    toml = _Path(__file__).resolve().parent.parent / "pyproject.toml"
    if toml.exists():
        import re
        m = re.search(r'^version\s*=\s*"([^"]+)"', toml.read_text(), re.M)
        if m:
            return m.group(1)
    return "unknown"


__version__ = _get_version()

DEFAULT_TAGS = {
    "general",
    "status",
    "plan",
    "task",
    "note",
    "reference",
    "experiment",
    "dataset",
    "model",
    "analysis",
    # API2 (plan §6.3/§13 Q4): the landing records use ["landing", "<repo_id>"];
    # the deploy sets MEMORA_ALLOW_ANY_TAG=1 today, so these matter for a
    # container that does not -- see docs/api-v1-writes.md.
    "landing",
    "clmux",
    "memora",
}


def _load_tag_whitelist() -> set[str]:
    """Load tag allowlist from env or file (fallback to defaults).

    Set MEMORA_ALLOW_ANY_TAG=1 to disable tag restrictions entirely.
    """

    # Check if tag restrictions should be disabled
    if os.getenv("MEMORA_ALLOW_ANY_TAG") == "1":
        return set()  # Empty set disables validation

    file_path = os.getenv("MEMORA_TAG_FILE")
    env_list = os.getenv("MEMORA_TAGS")

    if not file_path:
        default_file = _Path(__file__).resolve().parent.parent / 'config' / 'allowed_tags.json'
        file_path = str(default_file) if default_file.exists() else None

    if file_path:
        try:
            data = _Path(file_path).read_text(encoding="utf-8")
            loaded = json.loads(data)
            if isinstance(loaded, list):
                whitelist = {str(tag).strip() for tag in loaded if str(tag).strip()}
                if whitelist:
                    return whitelist
        except FileNotFoundError:
            pass
        except Exception:
            return set(DEFAULT_TAGS)

    if env_list:
        parsed = {part.strip() for part in env_list.split(',') if part.strip()}
        if parsed:
            return parsed

    return set(DEFAULT_TAGS)


TAG_WHITELIST = _load_tag_whitelist()


def list_allowed_tags() -> list[str]:
    """Return a sorted list of allowed tags."""

    return sorted(TAG_WHITELIST)


__all__ = ["__version__", "server", "storage", "TAG_WHITELIST", "list_allowed_tags"]
