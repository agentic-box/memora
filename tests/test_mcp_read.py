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
    from tests.l3_fakes import local_store
    local_store(tmp / "gamma.db")          # a REAL initialised sync_outbox + triggers
    mp.setenv("MEMORA_DATABASES", json.dumps({"alpha": str(tmp / "alpha.db"),
                                              "beta": str(tmp / "beta.db"),
                                              "gamma": str(tmp / "gamma.db")}))
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
    seed = Client("/mcp/beta").open()
    for text in ("seed one stored", "seed two stored"):
        assert not seed.call("memory_create", {"content": text})["result"].get("isError")
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
    assert resp["result"]["content"] and not resp["result"].get("isError"), resp


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


# ---------------------------------------------------------------- memory_related is a pure read

_TABLES = ("memories", "memories_crossrefs", "memories_embeddings", "memories_meta",
           "sync_outbox", "sync_state")


def _snapshot(db):
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='sync_outbox'").fetchone(), \
            "the store must have a REAL sync_outbox"
        return {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in _TABLES}
    finally:
        conn.close()


@pytest.fixture
def replicated(live):
    """gamma: replicated store with three memories whose crossrefs are stored."""
    w = Client("/mcp/gamma").open()
    for text in ("alpha topic notes", "alpha topic more notes", "alpha topic third notes"):
        assert not w.call("memory_create", {"content": text})["result"].get("isError")
    return live / "gamma.db"


def _related(client, memory_id, **kw):
    resp = client.call("memory_related", {"memory_id": memory_id, **kw})
    assert "error" not in resp and not resp["result"].get("isError"), resp
    return json.loads(resp["result"]["content"][0]["text"])


def _drop_crossref_row(db, memory_id):
    conn = sqlite3.connect(str(db))
    conn.execute("DELETE FROM memories_crossrefs WHERE memory_id = ?", (memory_id,))
    conn.commit()
    conn.close()


def test_memory_related_on_mcp_read_never_writes_or_syncs(replicated, monkeypatch):
    calls = []
    import memora.server as srv
    monkeypatch.setattr(srv, "sync_to_cloud", lambda *a, **k: calls.append(1))
    r = Client("/mcp-read/gamma").open()
    ids = [row[0] for row in _snapshot(replicated)["memories"]]
    # (a) stored crossrefs, refresh false; (b) stored crossrefs, refresh true
    before = _snapshot(replicated)
    a = _related(r, ids[-1], refresh=False)     # the last memory has stored neighbours
    b = _related(r, ids[-1], refresh=True)
    assert a["read_only"] and b["read_only"] and a["computed"] and b["computed"]
    assert a["related"] == b["related"] and a["related"], a
    assert _snapshot(replicated) == before
    # (c) never computed: no row -> empty, computed False, and STILL nothing written
    _drop_crossref_row(replicated, ids[0])
    before = _snapshot(replicated)
    for refresh in (False, True):
        c = _related(r, ids[0], refresh=refresh)
        assert c["computed"] is False and c["related"] == []
    assert _snapshot(replicated) == before
    assert calls == [], "the read path must not invoke sync_to_cloud"


def test_memory_related_on_mcp_still_computes_and_syncs(replicated, monkeypatch):
    """Control: the writable path is unchanged -- a never-computed memory is
    computed on read and the write path syncs (the spy that stays empty on /mcp-read)."""
    import memora.server as srv
    calls = []
    monkeypatch.setattr(srv, "sync_to_cloud", lambda *a, **k: calls.append(1))
    ids = [row[0] for row in _snapshot(replicated)["memories"]]
    _drop_crossref_row(replicated, ids[0])
    w = Client("/mcp/gamma").open()
    out = _related(w, ids[0])
    assert "read_only" not in out and "computed" not in out and out["related"]
    assert calls, "the writable path must run the write/sync path"
    assert _related(w, ids[-1], refresh=True)["related"]


def test_every_reader_tool_leaves_a_replicated_store_untouched(replicated):
    r = Client("/mcp-read/gamma").open()
    before = _snapshot(replicated)
    for name, args in (("memory_stats", {}), ("memory_list_compact", {}),
                       ("memory_semantic_search", {"query": "topic"}),
                       ("memory_hybrid_search", {"query": "topic"}),
                       ("memory_get", {"memory_id": 1}), ("memory_related", {"memory_id": 1})):
        resp = r.call(name, args)
        assert "error" not in resp and not resp["result"].get("isError"), (name, resp)
    assert _snapshot(replicated) == before


def test_under_the_leader_profile_the_list_and_refusals_hold(live):
    """Deployed shape: MEMORA_TOOL_PROFILE=leader prunes the global registry."""
    from memora.tool_profile import apply_tool_profile
    snap = dict(server.mcp._tool_manager._tools)
    try:
        apply_tool_profile(server.mcp, "leader")
        r = Client("/mcp-read/alpha").open()
        assert r.tools() == set(READER_TOOLS)
        assert _refused(r.call("memory_create", {"content": "x"}))
        assert _refused(r.call("memory_context", {"query": "x"}))
        w = Client("/mcp/alpha").open()
        assert w.tools() == set(LEADER_TOOLS)
    finally:
        server.mcp._tool_manager._tools.clear()
        server.mcp._tool_manager._tools.update(snap)


def test_read_session_ids_are_pruned_to_live_sessions(live):
    from memora import db_routing
    db_routing._read_sessions.update(f"stale-{i}" for i in range(300))
    r = Client("/mcp-read/alpha").open()
    assert r.sid in db_routing._read_sessions
    assert not any(sid.startswith("stale-") for sid in db_routing._read_sessions)
    # the pruned-away ids are refused, and the live one still works
    assert "error" not in r.call("memory_stats")
