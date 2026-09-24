"""memora /api/v1 handlers (memora/api_v1.py): real storage on SQLite and on
the FakeD1 double, auth, token-file checks, fail-closed registration,
admission, and isolation from the MCP session lifecycle."""

import asyncio
import hashlib
import json
import os
import time

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

import memora
import memora.storage as storage
from memora import api_v1, health
from tests.conftest import FakeD1Backend

TOKEN = "memora-api-v1-test-token"
OTHER_TOKEN = "a-token-for-ob1-only"


def _clear_health():
    health._snapshot = None
    health._snapshot_at = 0.0
    health._refresh_owner = 0
    health.stop_refresher()


@pytest.fixture(autouse=True)
def _reset_health():
    _clear_health()
    yield
    _clear_health()


def _write_tokens(tmp_path, table, mode=0o600):
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700, exist_ok=True)
    path = secrets / "tokens.json"
    path.write_text(json.dumps(table))
    path.chmod(mode)
    return path


def _digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


@pytest.fixture(params=["sqlite", "fake_d1"])
def api(request, tmp_path, monkeypatch):
    """A server with stores memora (the backend under test) and broken (a
    probe that always fails), tokens for memora+nostore and for ob1 only."""
    monkeypatch.setenv("HOME", str(tmp_path))
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({
        "memora": str(tmp_path / "memora.db"),
        "broken": str(blocker / "broken.db"),
    }))
    monkeypatch.setenv("MEMORA_DEFAULT_DB", "memora")
    monkeypatch.setenv("MEMORA_PROJECTS", json.dumps({"memora": ["clmux", "memora"]}))
    monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    storage._corpus_cache.clear()
    if request.param == "fake_d1":
        fake = FakeD1Backend(tmp_path / "memora-d1.db")
        real = storage.backend_for
        monkeypatch.setattr(storage, "backend_for", lambda name: fake if name == "memora" else real(name))
    tokens = _write_tokens(tmp_path, {
        _digest(TOKEN): ["memora", "nostore"],
        _digest(OTHER_TOKEN): ["ob1"],
    })
    _connect_store().close()  # the store exists (created by a writing path)
    mcp = FastMCP("api-test")
    assert api_v1.register_api_routes(
        mcp, bind_host="127.0.0.1", env={"MEMORA_API_TOKENS_FILE": str(tokens)},
    )
    app = mcp.streamable_http_app()
    with TestClient(app) as client:
        client.mcp = mcp
        yield client
    storage._corpus_cache.clear()


AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _seed(conn):
    ids = {}
    ids["a"] = storage.add_memory(conn, content="clmux daemon owns memora access for agents",
                                  tags=["clmux/architecture"], project="clmux")["id"]
    ids["b"] = storage.add_memory(conn, content="memora absorb gate uses cosine per leaf",
                                  tags=["memora/absorb"], project="memora")["id"]
    ids["c"] = storage.add_memory(conn, content="unrelated cooking recipe for bread", tags=["food"])["id"]
    old = storage.add_memory(conn, content="clmux daemon memora access old version", project="clmux")
    new = storage.add_memory(conn, content="clmux daemon memora access new version", project="clmux")
    storage.add_link(conn, new["id"], old["id"], edge_type="supersedes")
    ids["old"], ids["new"] = old["id"], new["id"]
    # One normal (MCP-path) search records the store's embedding model, as
    # on any store in use; the API's read-only search never writes it.
    storage.semantic_search(conn, "init")
    conn.commit()
    storage._corpus_cache.clear()
    return ids


def _connect_store(name="memora"):
    token = storage.CURRENT_DB.set(name)
    try:
        return storage.connect()
    finally:
        storage.CURRENT_DB.reset(token)


# --- health -------------------------------------------------------------------

def test_health_ok_reports_capability_and_projects(api):
    r = api.get("/api/v1/memora/health", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok" and body["writes"] == "unsupported"
    assert body["projects"] == ["clmux", "memora"] and body["supervisor"] is None
    assert body["api_version"] == "v1" and body["contract_version"] == api_v1.CONTRACT_VERSION
    assert body["mode"] == "hybrid-v1" and body["version"] == memora.__version__
    assert "reason" not in body and r.headers["cache-control"] == "no-store"


def test_health_503_for_a_failing_probe(tmp_path, monkeypatch, api):
    tokens = _write_tokens(tmp_path, {_digest("broken-token"): ["broken"]})
    mcp = FastMCP("api-test-broken")
    api_v1.register_api_routes(mcp, bind_host="127.0.0.1", env={"MEMORA_API_TOKENS_FILE": str(tokens)})
    with TestClient(mcp.streamable_http_app()) as client:
        r = client.get("/api/v1/broken/health", headers={"Authorization": "Bearer broken-token"})
    assert r.status_code == 503
    body = r.json()
    # The read-only probe never creates a database: "no database there".
    assert body["status"] == "down" and body["reason"] == "store_missing" and body["store"] == "broken"
    assert body["detail"] == "no_database"


# --- search -------------------------------------------------------------------

def test_search_returns_cosine_fused_and_rank(api):
    with _connect_store() as conn:
        ids = _seed(conn)
    r = api.post("/api/v1/memora/search", headers=AUTH,
                 json={"query": "clmux daemon memora access", "top_k": 3, "preview_chars": 12})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "hybrid-v1" and body["embedding_model"] and body["count"] == len(body["results"]) <= 3
    fused = [h["fused"] for h in body["results"]]
    assert fused == sorted(fused, reverse=True)
    assert [h["rank"] for h in body["results"]] == list(range(1, len(fused) + 1))
    assert all(len(h["preview"]) <= 12 for h in body["results"])
    assert all(h["cosine"] is None or -1 <= h["cosine"] <= 1 for h in body["results"])
    returned = {h["id"] for h in body["results"]}
    assert ids["old"] not in returned  # follow=active: superseded never returned
    assert ids["a"] in returned


def test_search_project_and_tag_filters(api):
    with _connect_store() as conn:
        ids = _seed(conn)
    r = api.post("/api/v1/memora/search", headers=AUTH,
                 json={"query": "memora access absorb daemon", "top_k": 10, "project": "memora"})
    assert r.status_code == 200
    assert {h["id"] for h in r.json()["results"]} == {ids["b"]}
    r = api.post("/api/v1/memora/search", headers=AUTH,
                 json={"query": "bread recipe", "tags_any": ["food"]})
    assert [h["id"] for h in r.json()["results"]] == [ids["c"]]
    r = api.post("/api/v1/memora/search", headers=AUTH,
                 json={"query": "anything", "tags_any": ["no/such-tag"]})
    assert r.json() == {**r.json(), "count": 0, "results": []}


def test_search_never_returns_an_unfinished_import_row(api):
    """A row an interrupted D1 import left marked (#47) is not a memory yet."""
    import json as _json
    import memora.storage as storage

    with _connect_store() as conn:
        ids = _seed(conn)
        raw = conn.execute("SELECT metadata FROM memories WHERE id = ?", (ids["a"],)).fetchone()[0]
        meta = _json.loads(raw) if raw else {}
        meta["import_attempt"] = "0" * 32 + ":1:0"
        conn.execute("UPDATE memories SET metadata = ? WHERE id = ?", (_json.dumps(meta), ids["a"]))
        conn.commit()
        storage.invalidate_corpus_cache(conn)
    r = api.post("/api/v1/memora/search", headers=AUTH,
                 json={"query": "clmux daemon memora access", "top_k": 10})
    assert r.status_code == 200 and ids["a"] not in {h["id"] for h in r.json()["results"]}


@pytest.mark.parametrize("body,fragment", [
    ({}, "query is required"),
    ({"query": "   "}, "blank"),
    ({"query": "x", "top_k": 21}, "top_k"),
    ({"query": "x", "top_k": True}, "top_k"),
    ({"query": "x", "preview_chars": 401}, "preview_chars"),
    ({"query": "x", "tags_any": []}, "tags_any"),
    ({"query": "x", "extra": 1}, "unknown field"),
    ({"query": "x", "project": "Bad Name"}, "project"),
    ({"query": "x", "project": "trader"}, "unknown_project"),
    ([], "JSON object"),
])
def test_search_rejects_bad_requests(api, body, fragment):
    r = api.post("/api/v1/memora/search", headers=AUTH, json=body)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"
    assert fragment in r.json()["message"]


def test_search_rejects_invalid_json(api):
    r = api.post("/api/v1/memora/search", headers={**AUTH, "Content-Type": "application/json"},
                 content=b"{not json")
    assert r.status_code == 400 and "not valid JSON" in r.json()["message"]


# --- absorb -------------------------------------------------------------------

ABSORB = {"idempotency_key": "landing:clmux:dev:abc123", "project": "clmux",
          "facts": ["a landed fact"], "source": "landing"}


def test_absorb_is_501_and_writes_nothing(api):
    with _connect_store() as conn:
        before = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    r = api.post("/api/v1/memora/absorb", headers=AUTH, json=ABSORB)
    assert r.status_code == 501
    assert r.json()["error"] == "writes_unsupported" and r.json()["writes"] == "unsupported"
    with _connect_store() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == before


@pytest.mark.parametrize("patch", [
    {"project": None}, {"idempotency_key": "has spaces"}, {"facts": []},
    {"source": ""}, {"project": "trader"}, {"extra": True},
])
def test_absorb_validates_before_the_capability_check(api, patch):
    body = {k: v for k, v in {**ABSORB, **patch}.items() if v is not None}
    r = api.post("/api/v1/memora/absorb", headers=AUTH, json=body)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"


def test_absorb_executor_seam_passes_status_and_body(api, monkeypatch):
    seen = {}

    def executor(store, req):
        seen["store"], seen["req"] = store, req
        return 409, {"error": "in_progress", "message": "retry later"}

    monkeypatch.setattr(api_v1, "writes_capability", lambda store: "transactional")
    monkeypatch.setattr(api_v1, "absorb_executor", executor)
    r = api.post("/api/v1/memora/absorb", headers=AUTH, json=ABSORB)
    assert r.status_code == 409 and r.json() == {"error": "in_progress", "message": "retry later"}
    assert seen["store"] == "memora" and seen["req"]["project"] == "clmux"


def test_canonical_request_sha256_is_order_independent():
    a = api_v1.validate_absorb_request(dict(ABSORB))
    b = api_v1.validate_absorb_request(dict(reversed(list(ABSORB.items()))))
    assert api_v1.canonical_request_sha256(a) == api_v1.canonical_request_sha256(b)
    c = api_v1.validate_absorb_request({**ABSORB, "facts": ["another fact"]})
    assert api_v1.canonical_request_sha256(a) != api_v1.canonical_request_sha256(c)


# --- auth ---------------------------------------------------------------------

@pytest.mark.parametrize("headers,path,status,code", [
    ({}, "/api/v1/memora/health", 401, "bad_token"),
    ({"Authorization": "Bearer wrong"}, "/api/v1/memora/health", 401, "bad_token"),
    ({"Authorization": f"Basic {TOKEN}"}, "/api/v1/memora/health", 401, "bad_token"),
    (AUTH, "/api/v1/ob1/health", 403, "store_forbidden"),
    ({"Authorization": f"Bearer {OTHER_TOKEN}"}, "/api/v1/memora/health", 403, "store_forbidden"),
    (AUTH, "/api/v1/nostore/health", 404, "unknown_store"),
    (AUTH, "/api/v1/Bad.Name/health", 404, "unknown_store"),
    ({}, "/api/v1/nostore/health", 401, "bad_token"),  # auth before existence
])
def test_auth_and_store_order(api, headers, path, status, code):
    r = api.get(path, headers=headers)
    assert r.status_code == status and r.json()["error"] == code


def test_search_and_absorb_need_the_token_too(api):
    assert api.post("/api/v1/memora/search", json={"query": "x"}).status_code == 401
    assert api.post("/api/v1/memora/absorb", json=ABSORB).status_code == 401


# --- registration (fail closed) ---------------------------------------------------

def _registered(bind_host, env):
    mcp = FastMCP("reg-test")
    ok = api_v1.register_api_routes(mcp, bind_host=bind_host, env=env)
    return ok, mcp


@pytest.mark.parametrize("bind", ["0.0.0.0", "127.0.0.1", "::1", "localhost"])
def test_without_a_tokens_file_the_api_is_never_registered(bind, caplog, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"memora": str(tmp_path / "m.db")}))
    monkeypatch.setenv("MEMORA_DEFAULT_DB", "memora")
    ok, mcp = _registered(bind, {})
    assert ok is False and "MEMORA_API_TOKENS_FILE is unset" in caplog.text
    with TestClient(mcp.streamable_http_app()) as client:
        assert client.get("/api/v1/memora/health").status_code == 404  # no route at all


def test_unsafe_tokens_file_is_not_registered(home, caplog):
    path = _write_tokens(home, {_digest(TOKEN): ["memora"]}, mode=0o644)
    ok, _ = _registered("127.0.0.1", {"MEMORA_API_TOKENS_FILE": str(path)})
    assert ok is False and "NOT registered" in caplog.text


def test_malformed_projects_config_is_not_registered(monkeypatch, home, caplog):
    path = _write_tokens(home, {_digest(TOKEN): ["memora"]})
    monkeypatch.setenv("MEMORA_PROJECTS", '{"memora": ["Bad Name"]}')
    ok, _ = _registered("127.0.0.1", {"MEMORA_API_TOKENS_FILE": str(path)})
    assert ok is False and "NOT registered" in caplog.text


# --- token file checks (§7.1) ---------------------------------------------------

@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _load(path):
    return api_v1.load_token_table(str(path))


def test_token_file_accepts_a_safe_file(home):
    path = _write_tokens(home, {_digest(TOKEN): ["memora"]})
    assert _load(path) == {_digest(TOKEN): frozenset({"memora"})}


def test_token_file_refuses_a_symlink(home):
    target = _write_tokens(home, {_digest(TOKEN): ["memora"]})
    link = home / "secrets" / "link.json"
    link.symlink_to(target)
    with pytest.raises(api_v1.ApiConfigError, match="cannot open"):
        _load(link)


def test_token_file_refuses_a_symlinked_parent(home):
    _write_tokens(home, {_digest(TOKEN): ["memora"]})
    (home / "alias").symlink_to(home / "secrets")
    # O_NOFOLLOW refuses the final-component symlink (ELOOP).
    with pytest.raises(api_v1.ApiConfigError, match="symlink"):
        _load(home / "alias" / "tokens.json")


def test_token_file_refuses_group_or_other_permissions(home):
    path = _write_tokens(home, {_digest(TOKEN): ["memora"]}, mode=0o640)
    with pytest.raises(api_v1.ApiConfigError, match="mode"):
        _load(path)


def test_token_file_refuses_a_group_writable_parent(home):
    path = _write_tokens(home, {_digest(TOKEN): ["memora"]})
    (home / "secrets").chmod(0o770)
    try:
        with pytest.raises(api_v1.ApiConfigError, match="writable"):
            _load(path)
    finally:
        (home / "secrets").chmod(0o700)


def test_token_file_refuses_another_owner(home, monkeypatch):
    path = _write_tokens(home, {_digest(TOKEN): ["memora"]})
    real = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: real + 1)
    with pytest.raises(api_v1.ApiConfigError, match="owner"):
        _load(path)


def test_token_file_refuses_non_regular_oversized_and_relative(home):
    (home / "secrets").mkdir(mode=0o700)
    (home / "secrets" / "dir.json").mkdir(mode=0o700)
    with pytest.raises(api_v1.ApiConfigError):
        _load(home / "secrets" / "dir.json")
    big = home / "secrets" / "big.json"
    big.write_text(json.dumps({_digest(str(i)): ["memora"] for i in range(80)}))
    big.chmod(0o600)
    with pytest.raises(api_v1.ApiConfigError, match="larger"):
        _load(big)
    with pytest.raises(api_v1.ApiConfigError, match="absolute"):
        api_v1.load_token_table("secrets/tokens.json")


@pytest.mark.parametrize("table", [
    {}, [], {"NOT-HEX": ["memora"]}, {_digest(TOKEN): []}, {_digest(TOKEN): ["Bad Store"]},
])
def test_token_file_content_is_validated(home, table):
    path = _write_tokens(home, table)
    with pytest.raises(api_v1.ApiConfigError):
        _load(path)


def test_token_value_never_logged(api, caplog):
    api.get("/api/v1/memora/health", headers=AUTH)
    api.get("/api/v1/memora/health", headers={"Authorization": "Bearer secret-guess"})
    assert TOKEN not in caplog.text and "secret-guess" not in caplog.text


# --- admission, isolation, responsiveness ---------------------------------------------

def test_admission_limit_returns_429(api):
    state = api.mcp._memora_api_state
    state.inflight = state.max_inflight
    try:
        r = api.post("/api/v1/memora/search", headers=AUTH, json={"query": "x"})
        assert r.status_code == 429 and r.json()["error"] == "admission"
    finally:
        state.inflight = 0


def test_api_calls_never_create_mcp_sessions(api):
    for _ in range(5):
        api.get("/api/v1/memora/health", headers=AUTH)
        api.post("/api/v1/memora/search", headers=AUTH, json={"query": "x"})
    assert api.mcp.session_manager._server_instances == {}


def test_health_stays_responsive_during_a_slow_search(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    tokens = _write_tokens(tmp_path, {_digest(TOKEN): ["memora"]})
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"memora": str(tmp_path / "m.db")}))
    monkeypatch.setenv("MEMORA_DEFAULT_DB", "memora")
    _connect_store().close()  # the store exists
    monkeypatch.setattr(api_v1, "run_search", lambda store, req: (time.sleep(1.0), {
        "count": 0, "mode": "hybrid-v1", "embedding_model": "x", "results": [], "took_ms": 1000,
    })[1])
    mcp = FastMCP("slow")
    assert api_v1.register_api_routes(mcp, bind_host="127.0.0.1", env={"MEMORA_API_TOKENS_FILE": str(tokens)})
    app = mcp.streamable_http_app()

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            search = asyncio.create_task(client.post("/api/v1/memora/search", headers=AUTH, json={"query": "x"}))
            await asyncio.sleep(0.1)
            t0 = time.perf_counter()
            health_resp = await client.get("/api/v1/memora/health", headers=AUTH)
            health_s = time.perf_counter() - t0
            search_resp = await search
            return health_resp.status_code, health_s, search_resp.status_code

    health_status, health_s, search_status = asyncio.run(scenario())
    assert search_status == 200 and health_status == 200
    assert health_s < 0.8  # answered while the search was still running


# --- token rotation (leader decision, msg 7057) -------------------------------------

def _rotate(path, table, mode=0o600):
    path.chmod(0o600)
    path.write_text(json.dumps(table))
    path.chmod(mode)
    os.utime(path, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))  # a distinct mtime


def test_rotated_tokens_file_takes_effect_without_a_restart(api, tmp_path, monkeypatch):
    monkeypatch.setattr(api_v1, "TOKEN_RELOAD_INTERVAL_S", 0.0)
    path = tmp_path / "secrets" / "tokens.json"
    assert api.get("/api/v1/memora/health", headers=AUTH).status_code == 200
    _rotate(path, {_digest("rotated-token"): ["memora"]})
    assert api.get("/api/v1/memora/health", headers=AUTH).status_code == 401
    assert api.get("/api/v1/memora/health",
                   headers={"Authorization": "Bearer rotated-token"}).status_code == 200


@pytest.mark.parametrize("bad", ["invalid-json", "unsafe-mode"])
def test_a_bad_reload_keeps_the_last_good_tokens(api, tmp_path, monkeypatch, caplog, bad):
    monkeypatch.setattr(api_v1, "TOKEN_RELOAD_INTERVAL_S", 0.0)
    path = tmp_path / "secrets" / "tokens.json"
    if bad == "invalid-json":
        path.write_text("{not json")
        os.utime(path, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
    else:
        _rotate(path, {_digest("rotated-token"): ["memora"]}, mode=0o644)
    assert api.get("/api/v1/memora/health", headers=AUTH).status_code == 200  # last good set
    assert "reload failed; keeping the last good set" in caplog.text


def test_reload_is_checked_at_most_once_per_interval(api, tmp_path):
    path = tmp_path / "secrets" / "tokens.json"
    _rotate(path, {_digest("rotated-token"): ["memora"]})
    # Default interval: within the same second the old table still answers.
    assert api.get("/api/v1/memora/health", headers=AUTH).status_code == 200


# --- Phase 0 round 2: the search is strictly read-only (review 7151) -----------------

from tests.conftest import FakeD1Connection  # noqa: E402

WRITE_VERBS = ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER")


@pytest.fixture
def statements(monkeypatch):
    """Every statement FakeD1 executes (the API's D1 store in these tests)."""
    seen = []
    real = FakeD1Connection.execute

    def execute(self, sql, params=None):
        seen.append(" ".join(sql.split()))
        return real(self, sql, params)

    monkeypatch.setattr(FakeD1Connection, "execute", execute)
    return seen


def _writes(statements):
    return [s for s in statements if s.split()[0].upper() in WRITE_VERBS]


def _fresh_caches():
    from memora.embeddings import _integrity_check_cache

    storage._corpus_cache.clear()
    _integrity_check_cache.clear()


def _search_bodies():
    return [{"query": "clmux daemon memora access"},
            {"query": "memora absorb", "project": "memora", "tags_any": ["memora/absorb"]}]


@pytest.mark.parametrize("api", ["fake_d1"], indirect=True)
def test_search_with_a_missing_vector_writes_nothing_and_reports_it_unscored(api, statements):
    with _connect_store() as conn:
        ids = _seed(conn)
        conn.execute("DELETE FROM memories_embeddings WHERE memory_id = ?", (ids["b"],))
        conn.commit()
    _fresh_caches()
    statements.clear()
    for body in _search_bodies():
        r = api.post("/api/v1/memora/search", headers=AUTH, json=body)
        assert r.status_code == 200, r.text
        assert r.json()["unscored"] == 1
    assert _writes(statements) == []
    with _connect_store() as conn:  # still missing: never repaired by the API
        assert conn.execute("SELECT COUNT(*) FROM memories_embeddings WHERE memory_id = ?",
                            (ids["b"],)).fetchone()[0] == 0


@pytest.mark.parametrize("api", ["fake_d1"], indirect=True)
def test_search_on_a_model_mismatch_is_503_and_writes_nothing(api, statements):
    with _connect_store() as conn:
        _seed(conn)
        conn.execute("UPDATE memories_meta SET value = ? WHERE key = 'embedding_model'",
                     ("openai|text-embedding-3-small|api.openai.com|dense:1536",))
        conn.commit()
    _fresh_caches()
    statements.clear()
    for body in _search_bodies():
        r = api.post("/api/v1/memora/search", headers=AUTH, json=body)
        assert r.status_code == 503, r.text
        assert r.json() == {
            "error": "store_degraded", "status": "degraded", "reason": "model_mismatch",
            "detail": "model_or_representation_mismatch",
            "message": "search cannot score this store (model_or_representation_mismatch); nothing was written",
        }
    assert _writes(statements) == []


@pytest.mark.parametrize("api", ["fake_d1"], indirect=True)
def test_search_on_a_store_with_no_recorded_model_is_503_and_writes_nothing(api, statements):
    with _connect_store() as conn:
        storage.add_memory(conn, content="a memory on a store never searched", tags=["x"])
        # A store written before E1: the write path now records the model, so remove it.
        conn.execute("DELETE FROM memories_meta WHERE key = 'embedding_model'")
        conn.commit()
    _fresh_caches()
    statements.clear()
    r = api.post("/api/v1/memora/search", headers=AUTH, json={"query": "memory"})
    assert r.status_code == 503 and r.json()["detail"] == "embedding_model_unrecorded"
    assert _writes(statements) == []


@pytest.mark.parametrize("api", ["fake_d1"], indirect=True)
def test_search_on_an_empty_store_is_200_and_writes_nothing(api, statements):
    _connect_store().close()  # the schema exists (set up by a writing path); no rows
    _fresh_caches()
    statements.clear()
    r = api.post("/api/v1/memora/search", headers=AUTH, json={"query": "anything"})
    assert r.status_code == 200 and r.json()["count"] == 0 and r.json()["unscored"] == 0
    assert _writes(statements) == []


@pytest.mark.parametrize("api", ["fake_d1"], indirect=True)
def test_a_read_only_load_never_seeds_the_shared_corpus_cache(api):
    with _connect_store() as conn:
        ids = _seed(conn)
        conn.execute("DELETE FROM memories_embeddings WHERE memory_id = ?", (ids["b"],))
        conn.commit()
    _fresh_caches()
    assert api.post("/api/v1/memora/search", headers=AUTH, json={"query": "memora"}).status_code == 200
    # The incomplete read-only load lives only in the read-only slot.
    keys = list(storage._corpus_cache)
    assert len(keys) == 1 and keys[0].endswith("|ro")
    # The normal (MCP) path still repairs, and the API then reuses its snapshot.
    with _connect_store() as conn:
        storage.semantic_search(conn, "memora")
    r = api.post("/api/v1/memora/search", headers=AUTH, json={"query": "memora"})
    assert r.status_code == 200 and r.json()["unscored"] == 0


# --- body cap and admission before body work ---------------------------------------

def test_body_over_the_cap_is_413_from_content_length(api):
    r = api.post("/api/v1/memora/search", headers={**AUTH, "Content-Type": "application/json"},
                 content=b"{" + b" " * (api_v1.MAX_BODY_BYTES + 10) + b"}")
    assert r.status_code == 413
    assert r.json() == {"error": "payload_too_large",
                        "message": f"request body exceeds {api_v1.MAX_BODY_BYTES} bytes"}


def test_body_over_the_cap_is_413_while_streaming_without_content_length(api):
    def chunks():
        for _ in range(20):
            yield b" " * 8192

    r = api.post("/api/v1/memora/absorb", headers={**AUTH, "Content-Type": "application/json"},
                 content=chunks())
    assert r.status_code == 413 and r.json()["error"] == "payload_too_large"


def test_body_at_the_cap_is_read(api):
    _connect_store().close()  # the schema exists
    body = json.dumps({"query": "x"}).encode()
    body = body[:-1] + b" " * (api_v1.MAX_BODY_BYTES - len(body)) + b"}"
    assert len(body) == api_v1.MAX_BODY_BYTES
    r = api.post("/api/v1/memora/search", headers={**AUTH, "Content-Type": "application/json"}, content=body)
    assert r.status_code == 200


def test_413_and_400_do_not_depend_on_load_and_admission_gates_execution(api):
    state = api.mcp._memora_api_state
    state.inflight = state.max_inflight
    try:
        r = api.post("/api/v1/memora/search", headers=AUTH, content=b"x" * (api_v1.MAX_BODY_BYTES * 4))
        assert r.status_code == 413
        r = api.post("/api/v1/memora/absorb", headers=AUTH, content=b"not json")
        assert r.status_code == 400
        r = api.post("/api/v1/memora/search", headers=AUTH, json={"query": "x", "top_k": 99})
        assert r.status_code == 400
        r = api.post("/api/v1/memora/search", headers=AUTH, json={"query": "x"})
        assert r.status_code == 429
    finally:
        state.inflight = 0


def test_concurrent_oversized_bodies_are_bounded_and_health_stays_responsive(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    tokens = _write_tokens(tmp_path, {_digest(TOKEN): ["memora"]})
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"memora": str(tmp_path / "m.db")}))
    monkeypatch.setenv("MEMORA_DEFAULT_DB", "memora")
    mcp = FastMCP("oversized")
    assert api_v1.register_api_routes(mcp, bind_host="127.0.0.1", env={
        "MEMORA_API_TOKENS_FILE": str(tokens), "MEMORA_API_MAX_INFLIGHT": "2"})
    state = mcp._memora_api_state
    app = mcp.streamable_http_app()
    peak = {"n": 0}

    async def slow_chunks():
        for _ in range(40):
            peak["n"] = max(peak["n"], state.inflight)
            await asyncio.sleep(0.005)
            yield b" " * 4096  # 160 KiB in all: over the cap part-way through

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            posts = [asyncio.create_task(client.post(
                "/api/v1/memora/search", headers={**AUTH, "Content-Type": "application/json"},
                content=slow_chunks())) for _ in range(12)]
            await asyncio.sleep(0.02)
            t0 = time.perf_counter()
            health_resp = await client.get("/api/v1/memora/health", headers=AUTH)
            health_s = time.perf_counter() - t0
            codes = sorted({(await p).status_code for p in posts})
            return health_resp.status_code, health_s, codes

    health_status, health_s, codes = asyncio.run(scenario())
    # Every oversized body is refused after at most MAX_BODY_BYTES read,
    # before admission: none ever holds an execution slot.
    assert codes == [413]
    assert peak["n"] == 0 and state.inflight == 0
    assert health_status in (200, 503) and health_s < 0.5


def test_smoke_script_does_not_resolve_a_symlinked_token_file(tmp_path, monkeypatch):
    import scripts.memora_api_smoke as smoke

    monkeypatch.setenv("HOME", str(tmp_path))
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    real = secrets / "token"
    real.write_text(TOKEN)
    real.chmod(0o600)
    link = secrets / "token-link"
    link.symlink_to(real)
    monkeypatch.setattr(smoke, "_call", lambda *a, **k: pytest.fail("no request may be sent"))
    # O_NOFOLLOW refuses the final-component symlink (ELOOP).
    with pytest.raises(api_v1.ApiConfigError, match="symbolic link"):
        smoke.main(["--base", "http://127.0.0.1:9", "--store", "memora", "--token-file", str(link)])



@pytest.mark.parametrize("api", ["fake_d1"], indirect=True)
def test_search_on_a_store_without_a_schema_is_503_and_creates_nothing(api, statements):
    with _connect_store() as conn:
        conn.execute("DROP TABLE memories")
        conn.commit()
    _fresh_caches()
    statements.clear()
    r = api.post("/api/v1/memora/search", headers=AUTH, json={"query": "anything"})
    assert r.status_code == 503 and r.json()["error"] == "store_unavailable"
    assert r.json()["status"] == "down" and r.json()["reason"] == "integrity_fault"
    assert r.json()["detail"] == "schema_missing"
    assert _writes(statements) == []


# --- round 3: missing local store, shared read-only snapshot ----------------------

def test_a_missing_local_store_is_503_and_nothing_is_created(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    ghost = tmp_path / "not" / "yet" / "ghost.db"
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"ghost": str(ghost)}))
    monkeypatch.setenv("MEMORA_DEFAULT_DB", "ghost")
    storage._registry_cache = None
    _clear_health()
    tokens = _write_tokens(tmp_path, {_digest(TOKEN): ["ghost"]})
    mcp = FastMCP("ghost")
    assert api_v1.register_api_routes(mcp, bind_host="127.0.0.1", env={"MEMORA_API_TOKENS_FILE": str(tokens)})
    with TestClient(mcp.streamable_http_app()) as client:
        r = client.post("/api/v1/ghost/search", headers=AUTH, json={"query": "x"})
        assert r.status_code == 503
        assert r.json() == {"error": "store_unavailable", "status": "down", "reason": "store_missing",
                            "detail": "no_database",
                            "message": "this store cannot serve reads (no_database); nothing was written or created"}
        h = client.get("/api/v1/ghost/health", headers=AUTH)
        for _ in range(50):  # the refresher probes in the background
            if h.json().get("reason") != "unproven":
                break
            time.sleep(0.05)
            h = client.get("/api/v1/ghost/health", headers=AUTH)
        assert h.status_code == 503 and h.json()["reason"] == "store_missing"
    assert not ghost.exists() and not (tmp_path / "not").exists()
    stop = getattr(health, "stop_refresher", None)
    if stop:
        stop()


@pytest.mark.parametrize("api", ["fake_d1"], indirect=True)
def test_concurrent_cold_searches_share_one_read_only_load(api, monkeypatch):
    with _connect_store() as conn:
        ids = _seed(conn)
        conn.execute("DELETE FROM memories_embeddings WHERE memory_id = ?", (ids["b"],))
        conn.commit()
    _fresh_caches()
    real, loads = storage._load_corpus_snapshot, []

    def slow_load(conn, **kw):
        loads.append(kw.get("repair_missing", True))
        time.sleep(0.2)  # every concurrent search arrives while this runs
        return real(conn, **kw)

    monkeypatch.setattr(storage, "_load_corpus_snapshot", slow_load)
    app = api.app

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await asyncio.gather(*[
                client.post("/api/v1/memora/search", headers=AUTH, json={"query": "memora access"})
                for _ in range(6)
            ])

    responses = asyncio.run(scenario())
    assert [r.status_code for r in responses] == [200] * 6
    assert all(r.json()["unscored"] == 1 for r in responses)
    assert loads == [False]  # ONE read-only load for six cold searches
    # Bounded: one snapshot for the store, in its read-only slot, under the budget.
    entries = list(storage._corpus_cache.items())
    assert len(entries) == 1 and entries[0][0].endswith("|ro")
    assert entries[0][1].nbytes <= storage._corpus_cache_budget_bytes()


@pytest.mark.parametrize("api", ["fake_d1"], indirect=True)
def test_a_complete_read_only_load_is_shared_with_the_mcp_tools(api, monkeypatch):
    with _connect_store() as conn:
        _seed(conn)
    _fresh_caches()
    assert api.post("/api/v1/memora/search", headers=AUTH, json={"query": "memora"}).status_code == 200
    keys = list(storage._corpus_cache)
    assert len(keys) == 1 and not keys[0].endswith("|ro")
    loads = []
    real = storage._load_corpus_snapshot
    monkeypatch.setattr(storage, "_load_corpus_snapshot", lambda conn, **kw: (loads.append(1), real(conn, **kw))[1])
    with _connect_store() as conn:
        storage.semantic_search(conn, "memora")
    assert loads == []  # the MCP path reused the API's complete snapshot


# --- round 4: reads never create files (WAL policy); body bound at the receive boundary ---

import sqlite3  # noqa: E402

from memora.backends import LocalSQLiteBackend, StoreLockedError  # noqa: E402


def _wal_db(path):
    w = sqlite3.connect(path)
    w.execute("PRAGMA journal_mode=WAL")
    w.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT)")
    w.execute("INSERT INTO memories (content) VALUES ('first')")
    w.commit()
    w.close()
    assert not os.path.exists(f"{path}-wal") and not os.path.exists(f"{path}-shm")


def _listing(d):
    return sorted(os.listdir(d))


def test_wal_without_sidecars_reads_immutably_and_creates_nothing(tmp_path):
    db = tmp_path / "w.db"
    _wal_db(db)
    before = _listing(tmp_path)
    conn = LocalSQLiteBackend(db).connect_read_only()
    assert [tuple(r) for r in conn.execute("SELECT content FROM memories")] == [("first",)]
    conn.close()
    assert _listing(tmp_path) == before == ["w.db"]


def test_wal_with_an_active_writer_reads_committed_data_and_creates_nothing(tmp_path):
    db = tmp_path / "w.db"
    _wal_db(db)
    writer = sqlite3.connect(db)
    writer.execute("INSERT INTO memories (content) VALUES ('second')")
    writer.commit()  # committed into -wal, not checkpointed; the writer stays open
    try:
        before = _listing(tmp_path)
        assert before == ["w.db", "w.db-shm", "w.db-wal"]
        conn = LocalSQLiteBackend(db).connect_read_only()
        rows = [r[0] for r in conn.execute("SELECT content FROM memories ORDER BY id")]
        conn.close()
        assert rows == ["first", "second"]
        assert _listing(tmp_path) == before
    finally:
        writer.close()


def test_wal_in_an_unwritable_directory_without_sidecars_still_reads(tmp_path):
    d = tmp_path / "ro"
    d.mkdir()
    db = d / "w.db"
    _wal_db(db)
    d.chmod(0o555)
    try:
        conn = LocalSQLiteBackend(db).connect_read_only()
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
        conn.close()
        assert _listing(d) == ["w.db"]
    finally:
        d.chmod(0o755)


def test_wal_with_unusable_sidecars_is_refused_not_repaired(tmp_path):
    db = tmp_path / "w.db"
    _wal_db(db)
    writer = sqlite3.connect(db)
    writer.execute("INSERT INTO memories (content) VALUES ('second')")
    writer.commit()
    shm = tmp_path / "w.db-shm"
    try:
        shm.chmod(0o000)  # a -shm this user cannot use (as if owned elsewhere)
        before = _listing(tmp_path)
        with pytest.raises(StoreLockedError):
            LocalSQLiteBackend(db).connect_read_only()
        assert _listing(tmp_path) == before
    finally:
        shm.chmod(0o644)
        writer.close()


def test_wal_with_only_one_sidecar_is_refused(tmp_path):
    db = tmp_path / "w.db"
    _wal_db(db)
    (tmp_path / "w.db-wal").write_bytes(b"")
    with pytest.raises(StoreLockedError, match="wal_sidecars_incomplete"):
        LocalSQLiteBackend(db).connect_read_only()
    assert _listing(tmp_path) == ["w.db", "w.db-wal"]


def test_api_search_on_a_locked_local_store_is_503_store_locked(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    db = tmp_path / "locked.db"
    _wal_db(db)
    (tmp_path / "locked.db-shm").write_bytes(b"")  # only one sidecar
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"locked": str(db)}))
    monkeypatch.setenv("MEMORA_DEFAULT_DB", "locked")
    storage._registry_cache = None
    tokens = _write_tokens(tmp_path, {_digest(TOKEN): ["locked"]})
    mcp = FastMCP("locked")
    assert api_v1.register_api_routes(mcp, bind_host="127.0.0.1", env={"MEMORA_API_TOKENS_FILE": str(tokens)})
    with TestClient(mcp.streamable_http_app()) as client:
        r = client.post("/api/v1/locked/search", headers=AUTH, json={"query": "x"})
    assert r.status_code == 503 and r.json()["error"] == "store_unavailable"
    assert r.json()["reason"] == "store_locked" and r.json()["detail"] == "wal_sidecars_incomplete"
    assert sorted(os.listdir(tmp_path)) == sorted(["locked.db", "locked.db-shm", "secrets"])
    health.stop_refresher()


def _asgi_post(app, messages):
    """POST /api/v1/memora/search straight into the ASGI app with a fake
    receive that delivers `messages` (no Content-Length). Returns
    (status, receive calls)."""
    calls = {"n": 0}
    queue = list(messages)

    async def receive():
        calls["n"] += 1
        if queue:
            return queue.pop(0)
        return {"type": "http.disconnect"}

    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": "/api/v1/memora/search", "raw_path": b"/api/v1/memora/search",
        "root_path": "", "query_string": b"", "client": ("127.0.0.1", 5000), "server": ("t", 80),
        "headers": [(b"authorization", f"Bearer {TOKEN}".encode()), (b"content-type", b"application/json")],
    }
    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status, calls["n"]


def test_one_huge_chunk_is_refused_without_being_kept(api):
    import tracemalloc

    huge = {"type": "http.request", "body": b" " * (1024 * 1024), "more_body": False}
    tracemalloc.start()
    try:
        status, calls = _asgi_post(api.app, [huge])
        _cur, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert status == 413 and calls == 1
    # The 1 MiB message was never copied or kept: well under cap + message.
    assert peak < api_v1.MAX_BODY_BYTES + 1024 * 1024
    assert peak < 512 * 1024


def test_many_small_chunks_stop_right_after_the_cap(api):
    piece = 16 * 1024
    messages = [{"type": "http.request", "body": b" " * piece, "more_body": True} for _ in range(100)]
    status, calls = _asgi_post(api.app, messages)
    assert status == 413
    assert calls == api_v1.MAX_BODY_BYTES // piece + 1  # stopped at the first message past the cap
