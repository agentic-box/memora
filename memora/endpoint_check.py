"""F4a: prove a client can reach memora-all over HTTP, authenticated, before
it is repointed there and the old D1 token is revoked
(docs/local-primary-implementation.md §6 F4a, slice L8).

check_endpoint() runs, in order, and stops at the first failure:

  1. liveness      GET /health answers 200 status ok.
  2. admin auth    GET /admin/data-volume with no token and with the HEALTH
                   token is refused (401/403): the admin token is enforced.
  3. store kind    GET /admin/data-volume with the ADMIN token lists the
                   scratch store as a local SQLite store ("kind": "sqlite")
                   that is not refused. A d1:// or s3:// store, an unknown
                   store, or a route without "kind" (a memora-all older than
                   L8) refuses: the round trip below writes, and it must
                   never write a live or D1-backed store.
  4. health auth   GET /health/db/<store> with the HEALTH token answers 200
                   with the authorised detail body.
  5. round trip    over MCP (streamable HTTP, /mcp/<store>): initialize,
                   memory_stats (bound to <store>), then -- unless write is
                   False -- memory_create of one tagged throwaway memory,
                   memory_get of it, memory_delete, and memory_get again
                   (must be not_found).

Nothing here talks to D1; every write lands in the scratch local store and is
deleted in the same run.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple


class EndpointCheckFailed(RuntimeError):
    def __init__(self, step: str, detail: str):
        super().__init__(f"{step}: {detail}")
        self.step = step
        self.detail = detail


Fetch = Callable[[str, str, Dict[str, str], Optional[bytes]], Tuple[int, Dict[str, str], bytes]]


def urllib_fetch(url: str, method: str, headers: Dict[str, str], body: Optional[bytes],
                 timeout: float = 30.0) -> Tuple[int, Dict[str, str], bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


def _json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8") or "null")
    except (ValueError, UnicodeDecodeError):
        return None


def _parse_sse_or_json(raw: bytes) -> Any:
    text = raw.decode("utf-8", "replace")
    for line in text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[len("data: "):])
    return json.loads(text)


class _Mcp:
    def __init__(self, fetch: Fetch, url: str):
        self.fetch, self.url, self.sid, self.next_id = fetch, url, None, 1

    def _post(self, payload: Dict[str, Any]) -> Tuple[int, Dict[str, str], bytes]:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.sid:
            headers["mcp-session-id"] = self.sid
        return self.fetch(self.url, "POST", headers, json.dumps(payload).encode())

    def initialize(self) -> None:
        status, headers, raw = self._post({
            "jsonrpc": "2.0", "id": self._id(), "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "memora-check-endpoint", "version": "1"}}})
        if status != 200:
            raise EndpointCheckFailed("mcp initialize", f"HTTP {status}")
        result = _parse_sse_or_json(raw)
        if "error" in result:
            raise EndpointCheckFailed("mcp initialize", f"JSON-RPC error {result['error']}")
        self.sid = headers.get("mcp-session-id")
        if not self.sid:
            raise EndpointCheckFailed("mcp initialize", "no mcp-session-id header")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    def call(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        status, _h, raw = self._post({"jsonrpc": "2.0", "id": self._id(), "method": "tools/call",
                                      "params": {"name": name, "arguments": arguments}})
        if status != 200:
            raise EndpointCheckFailed(f"mcp {name}", f"HTTP {status}")
        result = _parse_sse_or_json(raw)
        if "error" in result:
            raise EndpointCheckFailed(f"mcp {name}", f"JSON-RPC error {result['error']}")
        tool = result.get("result") or {}
        if tool.get("isError"):
            raise EndpointCheckFailed(f"mcp {name}", f"tool error {json.dumps(tool)[:300]}")
        for item in tool.get("content") or []:
            if item.get("type") == "text":
                try:
                    return json.loads(item["text"])
                except ValueError:
                    break
        out = (tool.get("structuredContent") or {}).get("result")
        if not isinstance(out, dict):
            raise EndpointCheckFailed(f"mcp {name}", "no result object")
        return out


def check_endpoint(base_url: str, health_token: str, admin_token: str, store: str, *,
                   write: bool = True, fetch: Fetch = urllib_fetch) -> Dict[str, Any]:
    base = base_url.rstrip("/")
    steps: List[Dict[str, Any]] = []
    if health_token == admin_token:
        raise EndpointCheckFailed("tokens", "the health and admin tokens are the same value")

    # 1. liveness
    status, _h, raw = fetch(f"{base}/health", "GET", {}, None)
    body = _json(raw) or {}
    if status != 200 or body.get("status") != "ok":
        raise EndpointCheckFailed("liveness", f"GET /health answered {status} {str(body)[:200]}")
    steps.append({"step": "liveness", "version": body.get("version")})

    # 2. the admin token is enforced
    for label, headers in (("no token", {}), ("health token", {"Authorization": f"Bearer {health_token}"})):
        status, _h, raw = fetch(f"{base}/admin/data-volume", "GET", headers, None)
        if status not in (401, 403):
            raise EndpointCheckFailed("admin auth", f"/admin/data-volume with {label} answered {status}, not 401/403")
    steps.append({"step": "admin auth", "enforced": True})

    # 3. the scratch store is local and served
    status, _h, raw = fetch(f"{base}/admin/data-volume", "GET", {"Authorization": f"Bearer {admin_token}"}, None)
    body = _json(raw)
    if status != 200 or not isinstance(body, dict):
        raise EndpointCheckFailed("store kind", f"/admin/data-volume with the admin token answered {status}")
    entry = (body.get("stores") or {}).get(store)
    if not isinstance(entry, dict):
        raise EndpointCheckFailed("store kind", f"store {store!r} is not in memora-all's registry")
    if "kind" not in entry:
        raise EndpointCheckFailed("store kind", "this memora-all does not report store kinds; refusing to write")
    if entry["kind"] != "sqlite":
        raise EndpointCheckFailed("store kind", f"store {store!r} is {entry['kind']!r}, not a local SQLite scratch store")
    if entry.get("refused"):
        raise EndpointCheckFailed("store kind", f"store {store!r} is refused: {entry['refused']}")
    steps.append({"step": "store kind", "store": store, "kind": "sqlite"})

    # 4. per-store health with the health token
    status, _h, raw = fetch(f"{base}/health/db/{store}", "GET", {"Authorization": f"Bearer {health_token}"}, None)
    body = _json(raw) or {}
    if status != 200 or body.get("status") != "ok" or "latency_ms" not in body:
        raise EndpointCheckFailed("health auth", f"/health/db/{store} answered {status} {str(body)[:200]} "
                                  "(an unauthorised caller gets no latency_ms)")
    steps.append({"step": "health auth", "latency_ms": body.get("latency_ms")})

    # 5. MCP round trip
    mcp = _Mcp(fetch, f"{base}/mcp/{store}")
    mcp.initialize()
    stats = mcp.call("memory_stats", {})
    if stats.get("database") != store:
        raise EndpointCheckFailed("mcp memory_stats", f"bound to {stats.get('database')!r}, not {store!r}")
    step: Dict[str, Any] = {"step": "mcp round trip", "database": store, "wrote": False}
    if write:
        marker = f"memora check-endpoint {uuid.uuid4().hex} {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}"
        created = mcp.call("memory_create", {"content": marker, "tags": ["check-endpoint"],
                                             "suggest_similar": False})
        mid = (created.get("memory") or {}).get("id")
        if not isinstance(mid, int):
            raise EndpointCheckFailed("mcp memory_create", f"no memory id in {str(created)[:200]}")
        try:
            got = mcp.call("memory_get", {"memory_id": mid})
            if (got.get("memory") or {}).get("content") != marker:
                raise EndpointCheckFailed("mcp memory_get", f"memory {mid} does not read back")
        finally:
            deleted = mcp.call("memory_delete", {"memory_id": mid})
        if deleted.get("status") != "deleted":
            raise EndpointCheckFailed("mcp memory_delete", f"memory {mid} not deleted: {str(deleted)[:200]}")
        gone = mcp.call("memory_get", {"memory_id": mid})
        if gone.get("error") != "not_found":
            raise EndpointCheckFailed("mcp memory_get", f"memory {mid} still readable after delete")
        step.update(wrote=True, created_and_deleted=mid)
    steps.append(step)
    return {"ok": True, "endpoint": base, "store": store, "steps": steps}
