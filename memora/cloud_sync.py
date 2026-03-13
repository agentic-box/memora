"""Cloud graph sync helper for real-time updates.

This module provides functions to sync memora data to Cloudflare D1
and notify connected WebSocket clients of updates.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

# Auto-detect sync script location (sibling memora-graph directory)
_THIS_DIR = Path(__file__).parent
_DEFAULT_SYNC_SCRIPT = _THIS_DIR.parent / "memora-graph" / "scripts" / "sync.sh"

# Configuration from environment (evaluated at runtime via functions)
def _is_cloud_graph_enabled() -> bool:
    return os.getenv("MEMORA_CLOUD_GRAPH_ENABLED", "").lower() in ("true", "1", "yes")

def _get_worker_url() -> str:
    return os.getenv("MEMORA_CLOUD_GRAPH_WORKER_URL", "").strip()

# Keep for backward compatibility
CLOUD_GRAPH_ENABLED = _is_cloud_graph_enabled()
CLOUD_GRAPH_WORKER_URL = _get_worker_url()
CLOUD_GRAPH_SYNC_SCRIPT = os.getenv("MEMORA_CLOUD_GRAPH_SYNC_SCRIPT", "") or (
    str(_DEFAULT_SYNC_SCRIPT) if _DEFAULT_SYNC_SCRIPT.exists() else ""
)

# Debounce settings - batch rapid writes
_sync_timer: Optional[threading.Timer] = None
_sync_lock = threading.Lock()
SYNC_DEBOUNCE_SECONDS = float(os.getenv("MEMORA_CLOUD_GRAPH_DEBOUNCE", "1.0"))


def _do_sync() -> None:
    """Perform the actual sync operation."""
    global _sync_timer
    _sync_timer = None

    if not _is_cloud_graph_enabled():
        return

    try:
        # Skip sync script when using D1 backend - D1 is the source of truth
        # The sync script was designed for R2->D1 sync which would overwrite D1 changes
        # Now we just broadcast to notify clients to fetch fresh data from D1

        # Always notify WebSocket clients
        _broadcast_update()

    except Exception:
        # Don't fail the main operation if sync fails
        logger.exception("Cloud graph sync failed")


def _broadcast_update() -> None:
    """Notify connected WebSocket clients of an update."""
    worker_url = _get_worker_url()
    if not worker_url:
        logger.debug("Skipping cloud graph broadcast; MEMORA_CLOUD_GRAPH_WORKER_URL is not set")
        return

    url = f"{worker_url}/broadcast"
    try:
        req = Request(
            url,
            data=json.dumps({}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "memora-sync/1.0",
            },
            method="POST",
        )
        with urlopen(req, timeout=5) as resp:
            logger.debug("Cloud graph broadcast OK (%s)", resp.status)
    except URLError as e:
        logger.warning("Cloud graph broadcast failed for %s: %s", url, e)
    except Exception as e:
        logger.exception("Unexpected cloud graph broadcast error for %s: %s", url, e)


def schedule_sync() -> None:
    """Schedule a sync operation with debouncing.

    Multiple rapid writes will be batched into a single sync
    after SYNC_DEBOUNCE_SECONDS of inactivity.
    """
    global _sync_timer

    if not _is_cloud_graph_enabled():
        return

    with _sync_lock:
        # Cancel any pending sync
        if _sync_timer is not None:
            _sync_timer.cancel()

        # Schedule new sync after debounce period
        _sync_timer = threading.Timer(SYNC_DEBOUNCE_SECONDS, _do_sync)
        _sync_timer.daemon = True
        _sync_timer.start()


def sync_now() -> None:
    """Perform sync immediately without debouncing."""
    global _sync_timer

    if not _is_cloud_graph_enabled():
        return

    with _sync_lock:
        # Cancel any pending sync
        if _sync_timer is not None:
            _sync_timer.cancel()
            _sync_timer = None

    # Run sync in background thread
    thread = threading.Thread(target=_do_sync, daemon=True)
    thread.start()
