"""Memory MCP server package."""

import json
import os
from pathlib import Path as _Path

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


__all__ = ["server", "storage", "TAG_WHITELIST", "list_allowed_tags"]
