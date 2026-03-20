"""Cloud graph sync helper for real-time updates.

This module provides functions to sync memora data to Cloudflare D1
and notify connected WebSocket clients of updates.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
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

# Track whether anyone is listening — skip broadcasts when no clients connected
_known_connections: int = 0  # connection count from last broadcast response
_last_check: float = 0.0  # monotonic time of last broadcast
_NO_CLIENTS_RECHECK_SECONDS = 10.0  # re-check every 10s when no clients


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
    """Notify connected WebSocket clients of an update.

    Skips the broadcast when no clients were connected on the last check,
    re-checking every _NO_CLIENTS_RECHECK_SECONDS to detect new viewers.
    """
    global _known_connections, _last_check

    worker_url = _get_worker_url()
    if not worker_url:
        logger.debug("Skipping cloud graph broadcast; MEMORA_CLOUD_GRAPH_WORKER_URL is not set")
        return

    with _sync_lock:
        now = time.monotonic()
        if _known_connections == 0 and (now - _last_check) < _NO_CLIENTS_RECHECK_SECONDS:
            logger.debug("Skipping broadcast — no clients connected (next check in %.0fs)",
                          _NO_CLIENTS_RECHECK_SECONDS - (now - _last_check))
            return

    url = f"{worker_url}/broadcast"
    data: dict = {}
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
            body = resp.read()
            try:
                data = json.loads(body)
            except (json.JSONDecodeError, AttributeError):
                pass
            with _sync_lock:
                _last_check = time.monotonic()
                _known_connections = data.get("remaining", 0)
            logger.debug("Cloud graph broadcast OK (%s), sent=%s, remaining=%s",
                         resp.status, data.get("sent", "?"), _known_connections)
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
