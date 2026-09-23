"""Fail-closed identity check for the live D1 test module (review 7599 P1-3).

Before any setup or write, the live module asks Cloudflare for the database's
name by id (read token) and requires that it contains "throwaway" AND equals
MEMORA_D1_TEST_DATABASE_NAME. A lookup failure refuses too.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Callable, Dict, Tuple


def fetch_database(account: str, database: str, token: str) -> Dict:
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/accounts/{account}/d1/database/{database}",
        headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())["result"]


def verify_throwaway(account: str, database: str, read_token: str, expected_name: str,
                     *, fetch: Callable[[str, str, str], Dict] = fetch_database) -> Tuple[bool, str]:
    if "throwaway" not in (expected_name or ""):
        return False, "MEMORA_D1_TEST_DATABASE_NAME must contain 'throwaway'"
    try:
        info = fetch(account, database, read_token)
    except Exception as exc:
        return False, f"database lookup failed: {type(exc).__name__}: {exc}"
    name = (info or {}).get("name")
    if name != expected_name:
        return False, f"database {database} is named {name!r}, not {expected_name!r}"
    if "throwaway" not in name:
        return False, f"database {name!r} is not a throwaway"
    return True, name
