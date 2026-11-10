"""Item 267: the read-only /mcp-read[/<db>] endpoint.

Driven end to end over real HTTP against the REAL memora.server.mcp (all
tools), with two stores, so a refusal is proven to leave the rows and the
sync_outbox untouched -- not inferred from a probe tool.
"""
import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request

import pytest

import memora.server as server
from memora import storage
from memora.db_routing import parse_route, NotAnMcpPath, routed_streamable_http_app
from memora.tool_profile import AGENT_TOOLS, LEADER_TOOLS, READER_TOOLS

PORT = 8881


class TestParseRoute:
    @pytest.mark.parametrize("path,expected", [
        ("/mcp", (False, None)), ("/mcp/", (False, None)), ("/mcp/a", (False, "a")),
        ("/mcp-read", (True, None)), ("/mcp-read/", (True, None)), ("/mcp-read/a", (True, "a")),
    ])
    def test_routes(self, path, expected):
        assert parse_route(path) == expected

    @pytest.mark.parametrize("path", ["/mcp-readx", "/mcp-read/a/b", "/mcp-other", "/health", "/mcpread"])
    def test_non_routes(self, path):
        with pytest.raises(NotAnMcpPath):
            parse_route(path)


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    import os
    import uvicorn
    tmp = tmp_path_factory.mktemp("mcpread")
    mp = pytest.MonkeyPatch()
    mp.setenv("MEMORA_DATABASES", json.dumps({"alpha": str(tmp / "alpha.db"),
                                              "beta": str(tmp / "beta.db")}))
    mp.setenv("MEMORA_DEFAULT_DB", "alpha")
    mp.setattr(storage, "EMBEDDING_MODEL", "tfidf")
    storage._registry_cache = None
    storage._registry_source = None
    app = routed_streamable_http_app(server.mcp)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        if getattr(srv, "started", False):
            break
        time.sleep(0.1)
    yield tmp
    srv.should_exit = True
    mp.undo()
    storage._registry_cache = None
    storage._registry_source = None


class Client:
    def __init__(self, path):
        self.path = path
        self.sid = None
        self.n = 0

    def rpc(self, method, params=None, notify=False, path=None):
        self.n += 1
        body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            body["id"] = self.n
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.sid:
            headers["Mcp-Session-Id"] = self.sid
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path or self.path}",
                                     data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self.sid = resp.headers.get("Mcp-Session-Id") or self.sid
                raw = resp.read().decode()
        except urllib.error.HTTPError as e:
            return {"http_status": e.code, "body": e.read().decode()}
        if notify or not raw.strip():
            return None
        for line in raw.splitlines():
            if line.startswith("data: "):
                raw = line[6:]
                break
        return json.loads(raw)

    def open(self):
        self.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                "clientInfo": {"name": "t", "version": "0"}})
        self.rpc("notifications/initialized", notify=True)
        return self

    def call(self, name, args=None, path=None):
        return self.rpc("tools/call", {"name": name, "arguments": args or {}}, path=path)

    def tools(self):
        return {t["name"] for t in self.rpc("tools/list")["result"]["tools"]}


def _counts(db):
    conn = sqlite3.connect(str(db))
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='memories'").fetchone():
            return 0, 0                      # store not initialised yet: nothing stored
        n = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        has = conn.execute("SELECT 1 FROM sqlite_master WHERE name='sync_outbox'").fetchone()
        o = conn.execute("SELECT COUNT(*) FROM sync_outbox").fetchone()[0] if has else 0
        return n, o
    finally:
        conn.close()


def _refused(resp):
    return "error" in resp and "read-only" in resp["error"]["message"]


def test_list_is_filtered_on_mcp_read_and_unchanged_on_mcp(live):
    r = Client("/mcp-read/alpha").open()
    assert r.tools() == set(READER_TOOLS)
    w = Client("/mcp/alpha").open()
    listed = w.tools()
    assert set(server.mcp._tool_manager._tools) == listed          # nothing pruned on /mcp
    assert "memory_create" in listed and "memory_context" in listed


def test_every_non_reader_tool_is_refused_before_any_side_effect(live):
    r = Client("/mcp-read/alpha").open()
    before = _counts(live / "alpha.db")
    for name in sorted(set(server.mcp._tool_manager._tools) - READER_TOOLS):
        resp = r.call(name, {})
        assert _refused(resp), (name, resp)
    # and a VALID write, which would otherwise succeed
    resp = r.call("memory_create", {"content": "must not be stored"})
    assert _refused(resp), resp
    assert _counts(live / "alpha.db") == before


def test_the_same_valid_write_succeeds_on_mcp(live):
    """Positive control: the refusal above is the path, not a broken call."""
    w = Client("/mcp/beta").open()
    before = _counts(live / "beta.db")
    resp = w.call("memory_create", {"content": "stored on the write path"})
    assert "error" not in resp and not resp["result"].get("isError"), resp
    assert _counts(live / "beta.db")[0] == before[0] + 1


@pytest.mark.parametrize("name,args", [
    ("memory_stats", {}), ("memory_list_compact", {}),
    ("memory_semantic_search", {"query": "stored"}), ("memory_hybrid_search", {"query": "stored"}),
    ("memory_get", {"memory_id": 1}), ("memory_related", {"memory_id": 1}),
])
def test_each_reader_tool_works(live, name, args):
    r = Client("/mcp-read/beta").open()
    resp = r.call(name, args)
    assert not _refused(resp) and "error" not in resp, resp
    assert resp["result"]["content"], resp


def test_db_routing_works_on_mcp_read(live):
    Client("/mcp/beta").open().call("memory_create", {"content": "only in beta zzqq"})
    stats_beta = Client("/mcp-read/beta").open().call("memory_stats")["result"]["content"][0]["text"]
    stats_alpha = Client("/mcp-read/alpha").open().call("memory_stats")["result"]["content"][0]["text"]
    assert json.loads(stats_beta)["total_memories"] > json.loads(stats_alpha)["total_memories"]
    unknown = Client("/mcp-read/nosuch").rpc("initialize", {})
    assert unknown["http_status"] == 404


def test_bare_mcp_read_is_the_default_db_read_only(live):
    r = Client("/mcp-read").open()
    assert r.tools() == set(READER_TOOLS)
    assert _refused(r.call("memory_create", {"content": "x"}))


def test_a_session_cannot_cross_paths_in_either_direction(live):
    before = _counts(live / "alpha.db")
    w = Client("/mcp/alpha").open()
    # write session replayed on the read path: refused by the router
    crossed = w.call("memory_stats", path="/mcp-read/alpha")
    assert crossed["http_status"] == 404
    r = Client("/mcp-read/alpha").open()
    crossed = r.call("memory_create", {"content": "smuggled"}, path="/mcp/alpha")
    assert crossed["http_status"] == 404
    assert _counts(live / "alpha.db") == before
    # and the read session still works on its own path
    assert "error" not in r.call("memory_stats")


def test_without_a_filterable_server_mcp_read_fails_closed(monkeypatch, tmp_path):
    """No lowlevel handlers to filter -> /mcp-read is 404, never served unfiltered."""
    import asyncio
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"alpha": str(tmp_path / "a.db")}))
    monkeypatch.setenv("MEMORA_DEFAULT_DB", "alpha")
    storage._registry_cache = None
    storage._registry_source = None
    reached = []

    async def inner(scope, receive, send):
        reached.append(scope["path"])

    class _M:
        session_manager = type("M", (), {"_server_instances": {}})()
        settings = type("S", (), {"transport_security": None})()

        def streamable_http_app(self):
            return inner

    app = routed_streamable_http_app(_M())
    sent = []

    async def send(msg):
        sent.append(msg)

    asyncio.run(app({"type": "http", "path": "/mcp-read/alpha", "method": "POST",
                     "headers": []}, None, send))
    assert sent[0]["status"] == 404 and not reached
