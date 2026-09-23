"""Authorisation for the /admin/* operator routes (local-primary plan §9
item (a), slice L2a).

The routes themselves live in memora/admin.py (L2: freeze, intents,
reconcile). They place and lift write freezes and accept open intents, so
they need more than the health token, which every health prober holds. They
take ONE credential: MEMORA_ADMIN_TOKEN, presented as
"Authorization: Bearer <token>". install_admin_auth() installs the check
through admin.set_admin_auth().

- No loopback exemption. /health/db trusts a loopback peer; /admin/* does
  not, so a process that merely shares the network namespace gets nothing.
- Unset token: every /admin/* route answers 403 admin_disabled. Fail closed.
- The token must be at least ADMIN_TOKEN_MIN_LEN ASCII characters and must
  differ from MEMORA_HEALTH_TOKEN; otherwise the server refuses to start.

Operators reach the routes through docker exec, so the token never leaves
the container's environment:

  docker exec memora-all python -c 'import os, urllib.request as u; \
    r = u.Request("http://127.0.0.1:8000/admin/data-volume", \
      headers={"Authorization": "Bearer " + os.environ["MEMORA_ADMIN_TOKEN"]}); \
    print(u.urlopen(r).read().decode())'

The launchers keep a copy on the host (~/.config/memora/<instance>.admin-token,
0600) for scripts that call through the published port.
"""
from __future__ import annotations

import hmac
import os
from typing import Any, Dict, Optional, Tuple

ADMIN_TOKEN_ENV = "MEMORA_ADMIN_TOKEN"
HEALTH_TOKEN_ENV = "MEMORA_HEALTH_TOKEN"
ADMIN_TOKEN_MIN_LEN = 32

Result = Tuple[int, Dict[str, Any]]


class AdminConfigError(RuntimeError):
    """MEMORA_ADMIN_TOKEN is unusable. Raised at startup, never defaulted."""


def admin_token() -> Optional[str]:
    """The configured admin token, or None when admin routes are disabled."""
    token = os.getenv(ADMIN_TOKEN_ENV, "")
    if not token:
        return None
    if len(token) < ADMIN_TOKEN_MIN_LEN:
        raise AdminConfigError(
            f"{ADMIN_TOKEN_ENV} must be at least {ADMIN_TOKEN_MIN_LEN} characters")
    try:
        token.encode("ascii")
    except UnicodeError as exc:
        raise AdminConfigError(f"{ADMIN_TOKEN_ENV} must be ASCII") from exc
    health = os.getenv(HEALTH_TOKEN_ENV, "")
    if health and hmac.compare_digest(token.encode(), health.encode("utf-8", "replace")):
        # The health token is held by every prober; an admin token equal to it
        # is no separation at all.
        raise AdminConfigError(f"{ADMIN_TOKEN_ENV} must differ from {HEALTH_TOKEN_ENV}")
    return token


def check_admin(request: Any) -> Optional[Result]:
    """None when the request may use an admin route, else (status, body):
    403 when admin routes are disabled, 401 otherwise. No detail in either."""
    try:
        token = admin_token()
    except AdminConfigError:
        # Validated at startup; a token changed underneath a running process
        # is treated as disabled rather than half-trusted.
        token = None
    if token is None:
        return 403, {"error": "admin_disabled"}
    header = request.headers.get("authorization", "") or ""
    prefix = "Bearer "
    if not header.startswith(prefix):
        return 401, {"error": "unauthorized"}
    presented = header[len(prefix):]
    try:
        ok = hmac.compare_digest(presented.encode("utf-8"), token.encode("utf-8"))
    except (TypeError, UnicodeError):
        return 401, {"error": "unauthorized"}
    return None if ok else (401, {"error": "unauthorized"})


def data_volume_status() -> Result:
    """GET /admin/data-volume: what the startup /data check decided per store."""
    from . import storage
    from .data_volume import MARKER_ENV, data_dir, uri_needs_data_volume

    try:
        registry = storage.database_registry()
    except storage.DatabaseRegistryError as exc:
        return 503, {"error": "registry_error", "message": str(exc)}
    stores = dict(registry) if registry else {None: storage.single_store_uri()}
    refusals = dict(storage._store_refusals)
    return 200, {
        "data_dir": str(data_dir()),
        "volume": os.getenv(MARKER_ENV) or None,
        "stores": {
            (name or "(default)"): {
                "needs_data_volume": uri_needs_data_volume(uri),
                "refused": refusals.get(name),
            }
            for name, uri in stores.items()
        },
    }


def install_admin_auth(mcp: Any) -> None:
    """Validate MEMORA_ADMIN_TOKEN, install check_admin as the /admin/* auth
    hook (admin.set_admin_auth), and add GET /admin/data-volume.

    Raises AdminConfigError on an unusable token (server main exits 2).
    """
    from starlette.responses import JSONResponse

    from .admin import require_admin, set_admin_auth

    admin_token()  # validate now, not on the first request
    set_admin_auth(check_admin)

    @mcp.custom_route("/admin/data-volume", methods=["GET"])
    async def _data_volume(request):
        denied = require_admin(request)
        if denied is not None:
            status, body = denied
        else:
            status, body = data_volume_status()
        return JSONResponse(body, status_code=status)
