"""G1 (leader 7918): memora-all's graph UI reads and edits the store its
?db= selects, through the normal registry backend, behind the graph token.
Offline: local SQLite stores; a d1:// store is only resolved, never called."""
from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from memora import storage, write_gate
from memora.graph import server as gs

TOKEN = "g" * 48


def _store(path, name, contents):
    reset = storage.CURRENT_DB.set(name)
    try:
        conn = storage.connect()
        try:
            for c in contents:
                storage.add_memory(conn, content=c, tags=["g1"])
            conn.commit()
        finally:
            conn.close()
    finally:
        storage.CURRENT_DB.reset(reset)


@pytest.fixture
def stores(tmp_path, monkeypatch):
    reg = {"alpha": str(tmp_path / "alpha.db"), "beta": str(tmp_path / "beta.db")}
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps(reg))
    monkeypatch.setenv("MEMORA_DEFAULT_DB", "alpha")
    import memora

    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
    monkeypatch.delenv("MEMORA_GRAPH_TOKEN", raising=False)
    monkeypatch.delenv("MEMORA_GRAPH_TOKEN_FILE", raising=False)
    monkeypatch.setenv("MEMORA_HEALTH_TOKEN", TOKEN)
    _store(tmp_path / "alpha.db", "alpha", ["alpha one", "alpha two"])
    _store(tmp_path / "beta.db", "beta", ["beta only"])
    return reg


def _client(host="0.0.0.0", auth=True):
    c = TestClient(gs.build_graph_app(host))
    if auth:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
    return c


def _contents(resp):
    assert resp.status_code == 200, resp.text
    return sorted(m["content"] for m in resp.json()["memories"])


# ---------------------------------------------------------------- store selection

def test_each_store_serves_its_own_memories(stores):
    c = _client()
    assert _contents(c.get("/api/memories?db=alpha")) == ["alpha one", "alpha two"]
    assert _contents(c.get("/api/memories?db=beta")) == ["beta only"]


def test_no_db_is_the_default_store(stores):
    assert _contents(_client().get("/api/memories")) == ["alpha one", "alpha two"]


def test_the_graph_and_one_memory_follow_the_store(stores):
    c = _client()
    labels = {n["label"] for n in c.get("/api/graph?db=beta").json()["nodes"]}
    assert any("beta only" in label for label in labels) and not any("alpha" in label for label in labels)
    beta_id = c.get("/api/memories?db=beta").json()["memories"][0]["id"]
    assert c.get(f"/api/memories/{beta_id}?db=beta").json()["content"] == "beta only"


@pytest.mark.parametrize("db", ["nope", "..", "alpha/../beta", "ALPHA"])
def test_an_unknown_store_is_refused(stores, db):
    r = _client().get("/api/memories", params={"db": db})
    assert r.status_code == 400 and r.json() == {"error": "unknown_db", "known": ["alpha", "beta"]}


def test_the_store_list_and_the_spa_selector(stores):
    c = _client()
    assert c.get("/api/databases").json() == {"databases": ["alpha", "beta"], "default": "alpha"}
    html = c.get("/graph").text
    assert '"dbSelector": true' in html


def test_without_a_registry_there_is_one_store_and_no_selector(local_db, monkeypatch):
    monkeypatch.delenv("MEMORA_DATABASES", raising=False)
    monkeypatch.setenv("MEMORA_HEALTH_TOKEN", TOKEN)
    c = _client()
    assert c.get("/api/memories").status_code == 200
    assert c.get("/api/memories?db=alpha").status_code == 400
    assert '"dbSelector": false' in c.get("/graph").text


def test_the_request_runs_bound_to_the_selected_store(stores, monkeypatch, tmp_path):
    """Local primary vs d1://: the handler's store is the registry's backend
    for the selected name (a d1:// store is resolved, not called)."""
    reg = dict(stores, remote="d1://acct/dbid")
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps(reg))
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "unused-offline")
    seen = []

    def fake_graph(*a, **k):
        name = storage.CURRENT_DB.get()
        seen.append((name, type(storage.backend_for(name)).__name__))
        return {"nodes": [], "edges": []}

    monkeypatch.setattr(gs, "get_graph_data", fake_graph)
    c = _client()
    assert c.get("/api/graph?db=alpha").status_code == 200
    assert c.get("/api/graph?db=remote").status_code == 200
    assert seen == [("alpha", "LocalSQLiteBackend"), ("remote", "D1Backend")]
    assert storage.CURRENT_DB.get() is None  # reset after the request


def test_a_refused_store_is_503(stores, monkeypatch):
    monkeypatch.setattr(storage, "_store_refusals", {"beta": "data volume unfit"})
    r = _client().get("/api/memories?db=beta")
    assert r.status_code == 503 and r.json()["error"] == "store_refused"


def test_executor_and_thread_work_keep_the_store(stores):
    import threading

    reset = storage.CURRENT_DB.set("beta")
    try:
        bound = gs._in_context(lambda: storage.CURRENT_DB.get())
    finally:
        storage.CURRENT_DB.reset(reset)
    out = []
    t = threading.Thread(target=lambda: out.append(bound()))
    t.start()
    t.join()
    assert out == ["beta"]


# ---------------------------------------------------------------- edits

def test_an_edit_lands_in_the_selected_store_only(stores):
    c = _client()
    mid = c.get("/api/memories?db=beta").json()["memories"][0]["id"]
    r = c.patch(f"/api/memories/{mid}?db=beta", json={"tags": ["edited"]})
    assert r.status_code == 200 and r.json()["tags"] == ["edited"]
    assert c.get(f"/api/memories/{mid}?db=beta").json()["tags"] == ["edited"]
    same_id_alpha = c.get(f"/api/memories/{mid}?db=alpha").json()
    assert same_id_alpha.get("tags") != ["edited"]


def test_a_frozen_store_refuses_edits_cleanly_and_says_read_only(stores):
    write_gate.persist_freeze("beta")
    write_gate._reset_for_tests()  # a fresh process: beta starts frozen
    c = _client()
    mid = c.get("/api/memories?db=beta").json()["memories"][0]["id"]  # X2: reads still served
    r = c.patch(f"/api/memories/{mid}?db=beta", json={"tags": ["x"]})
    assert r.status_code == 409 and r.json()["error"] == "store_read_only" and r.json()["db"] == "beta"
    assert c.get(f"/api/memories/{mid}?db=beta").json()["tags"] == ["g1"]
    assert c.get("/api/capabilities?db=beta").json()["read_only"] is True
    assert c.get("/api/capabilities?db=alpha").json()["read_only"] is False


def test_a_cross_origin_edit_is_refused(stores):
    c = _client()
    mid = c.get("/api/memories?db=alpha").json()["memories"][0]["id"]
    r = c.patch(f"/api/memories/{mid}?db=alpha", json={"tags": ["x"]}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


# ---------------------------------------------------------------- the token

@pytest.mark.parametrize("path", ["/api/graph", "/api/memories", "/api/memories/1", "/api/databases",
                                  "/api/capabilities", "/api/actions", "/r2/images/x.png", "/_graph_limit.mjs"])
def test_every_route_needs_the_token(stores, path):
    c = _client(auth=False)
    r = c.get(path)
    assert r.status_code == 401 and r.json()["memora_graph"] is True
    r = c.get(path, headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_patch_and_chat_need_the_token(stores):
    c = _client(auth=False)
    assert c.patch("/api/memories/1", json={"tags": []}).status_code == 401
    assert c.post("/api/chat", json={"message": "hi"}).status_code == 401


def test_the_page_offers_a_login_and_the_cookie_opens_it(stores):
    c = _client(auth=False)
    r = c.get("/graph?db=beta")
    assert r.status_code == 401 and 'action="/login"' in r.text and TOKEN not in r.text
    assert 'value="/graph?db=beta"' in r.text
    bad = c.post("/login", data={"token": "wrong", "next": "/graph"}, follow_redirects=False)
    assert bad.status_code == 401 and "memora_graph" not in bad.cookies
    ok = c.post("/login", data={"token": TOKEN, "next": "/graph?db=beta"}, follow_redirects=False)
    assert ok.status_code == 303 and ok.headers["location"] == "/graph?db=beta"
    cookie = ok.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert c.get("/api/memories?db=beta").status_code == 200  # the cookie now authorises


def test_login_never_redirects_off_the_graph(stores):
    c = _client(auth=False)
    r = c.post("/login", data={"token": TOKEN, "next": "https://evil.example/"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/graph"


def test_a_dedicated_graph_token_replaces_the_health_token(stores, monkeypatch, tmp_path):
    f = tmp_path / "graph.token"
    f.write_text("dedicated-" + "x" * 30)
    f.chmod(0o600)
    monkeypatch.setenv("MEMORA_GRAPH_TOKEN_FILE", str(f))
    c = _client(auth=False)
    assert c.get("/api/graph", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 401
    assert c.get("/api/memories", headers={"Authorization": "Bearer dedicated-" + "x" * 30}).status_code == 200


def test_a_non_loopback_bind_without_any_token_fails_closed(stores, monkeypatch):
    monkeypatch.delenv("MEMORA_HEALTH_TOKEN", raising=False)
    r = _client(host="0.0.0.0", auth=False).get("/api/memories")
    assert r.status_code == 503 and r.json()["error"] == "graph_token_not_configured"


def test_loopback_without_a_token_stays_open_as_before(stores, monkeypatch):
    monkeypatch.delenv("MEMORA_HEALTH_TOKEN", raising=False)
    assert _client(host="127.0.0.1", auth=False).get("/api/memories").status_code == 200


def test_the_port_probe_recognises_a_guarded_graph_server(stores):
    """start_graph_server's probe must not mistake memora's own guarded
    server (401) for another service."""
    import socket
    import threading

    import uvicorn

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(gs.build_graph_app("0.0.0.0"), host="127.0.0.1", port=port,
                                        log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    import time

    for _ in range(50):
        if gs._check_port_status("127.0.0.1", port) != "free":
            break
        time.sleep(0.1)
    try:
        assert gs._check_port_status("127.0.0.1", port) == "memora"
    finally:
        srv.should_exit = True


# ---------------------------------------------------------------- review 7922: the exact origin

SELF = "http://testserver"  # TestClient's scheme://host (port 80)


@pytest.mark.parametrize("origin", ["http://testserver:8080", "http://testserver:9999", "https://testserver",
                                    "http://localhost", "http://127.0.0.1:8766", "null", "http://evil.example"])
def test_a_cross_origin_browser_request_is_refused_on_every_mutating_route(stores, origin):
    """SameSite cookies are scoped by site, not port: another service on the
    same host (a different port) must not drive the graph with the cookie."""
    c = _client()
    mid = c.get("/api/memories?db=alpha").json()["memories"][0]["id"]
    h = {"Origin": origin}
    assert c.patch(f"/api/memories/{mid}?db=alpha", json={"tags": ["x"]}, headers=h).status_code == 403
    assert c.post("/api/chat?db=alpha", content='{"message": "delete memory 1"}',
                  headers={**h, "Content-Type": "text/plain"}).status_code == 403
    assert c.get("/api/events?db=alpha", headers=h).status_code == 403
    login = _client(auth=False).post("/login", data={"token": TOKEN}, headers=h, follow_redirects=False)
    assert login.status_code == 403 and "memora_graph" not in login.cookies
    assert c.get(f"/api/memories/{mid}?db=alpha").json()["tags"] == ["g1"]


def test_the_exact_origin_is_accepted(stores):
    c = _client()
    mid = c.get("/api/memories?db=alpha").json()["memories"][0]["id"]
    r = c.patch(f"/api/memories/{mid}?db=alpha", json={"tags": ["same-origin"]}, headers={"Origin": SELF})
    assert r.status_code == 200
    login = _client(auth=False).post("/login", data={"token": TOKEN}, headers={"Origin": SELF + ":80"},
                                     follow_redirects=False)
    assert login.status_code == 303


def test_origin_ok_compares_scheme_host_and_port():
    from starlette.requests import Request as R

    def req(origin, host="100.104.19.74:8766"):
        headers = [(b"host", host.encode())] + ([(b"origin", origin.encode())] if origin is not None else [])
        return R({"type": "http", "scheme": "http", "path": "/", "headers": headers, "query_string": b"",
                  "server": ("100.104.19.74", 8766)})

    assert gs._origin_ok(req(None))
    assert gs._origin_ok(req("http://100.104.19.74:8766"))
    assert not gs._origin_ok(req("http://100.104.19.74:8765"))
    assert not gs._origin_ok(req("http://100.104.19.74"))
    assert not gs._origin_ok(req("https://100.104.19.74:8766"))
    assert not gs._origin_ok(req("http://127.0.0.1:8766"))
    assert gs._origin_ok(req("http://localhost:8765", host="localhost:8765"))
    assert not gs._origin_ok(req("http://localhost:3000", host="localhost:8765"))


def test_a_refused_single_store_is_503(local_db, monkeypatch):
    monkeypatch.delenv("MEMORA_DATABASES", raising=False)
    monkeypatch.setenv("MEMORA_HEALTH_TOKEN", TOKEN)
    monkeypatch.setattr(storage, "_store_refusals", {None: "data volume unfit"})
    r = _client().get("/api/memories")
    assert r.status_code == 503 and r.json()["error"] == "store_refused"
