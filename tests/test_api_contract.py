"""The memora API v1 contract (contracts/memora-api/v1) against the real app.

clmux docs/PLAN_MEMORA_DAEMON.md §3.5: memora's pytest sends each fixture's
request to the real app with a stub storage layer and checks the response
against the schema and the fixture. The stubs are chosen by each fixture's
"replay" block; everything else -- auth, routing, validation, response
shaping -- is the production code path.
"""

import hashlib
import json

import pytest
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

import memora
import memora.storage as storage
from memora import api_v1, health
from scripts.memora_api_contract import (
    CONTRACT,
    MANIFEST,
    REQUIRED_FIXTURES,
    TEST_TOKEN,
    build_manifest,
    compare,
    load_fixtures,
    validate,
)

FIXTURES = load_fixtures()


def test_contract_validates_and_manifest_is_current():
    # validate() checks the schemas, every fixture, and the manifest.
    assert validate() == []
    assert json.loads(MANIFEST.read_text()) == build_manifest()


def test_every_required_fixture_exists_and_is_replayed():
    names = {f["name"] for f in FIXTURES}
    assert REQUIRED_FIXTURES <= names
    assert len(names) == len(FIXTURES)


def test_contract_version_matches_the_server():
    assert (CONTRACT / "VERSION").read_text().strip() == api_v1.CONTRACT_VERSION


@pytest.fixture
def app_factory(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    tokens = secrets / "tokens.json"
    tokens.write_text(json.dumps({hashlib.sha256(TEST_TOKEN.encode()).hexdigest(): ["memora", "nostore"]}))
    tokens.chmod(0o600)
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"memora": str(tmp_path / "memora.db")}))
    monkeypatch.setenv("MEMORA_DEFAULT_DB", "memora")
    monkeypatch.setenv("MEMORA_PROJECTS", json.dumps({"memora": ["project-a", "clmux", "memora"]}))
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    token = storage.CURRENT_DB.set("memora")  # the store exists (a writing path created it)
    try:
        storage.connect().close()
    finally:
        storage.CURRENT_DB.reset(token)

    def make(probe):
        async def readiness(**_kw):
            entry = {
                "ok": {"status": "ok"},
                "error": {"status": "error", "error": "Stub"},
                "store_missing": {"status": "error", "error": "StoreMissingError",
                                  "message": "no database file at /x"},
            }[probe]
            return {"databases": {"memora": entry}, "too_stale": False}

        monkeypatch.setattr(health, "readiness_payload_async", readiness)
        monkeypatch.setattr(health, "ensure_refresher", lambda: None)
        mcp = FastMCP("contract")
        assert api_v1.register_api_routes(mcp, bind_host="127.0.0.1",
                                          env={"MEMORA_API_TOKENS_FILE": str(tokens)})
        return mcp

    return make


def _install_stubs(fixture, monkeypatch, mcp):
    replay = fixture.get("replay") or {}
    if "capability" in replay:
        monkeypatch.setattr(api_v1, "writes_capability", lambda store, v=replay["capability"]: v)
    if replay.get("absorb") == "response":
        status, body = fixture["response"]["status"], fixture["response"]["body"]
        monkeypatch.setattr(api_v1, "absorb_executor", lambda store, req: (status, dict(body)))
    if replay.get("search_hits") == "response":
        resp = fixture["response"]["body"]
        req = fixture["request"]["body"]

        def search(conn, query, **kw):
            assert query == req["query"]
            assert kw["project"] == req.get("project") and kw["tags_any"] == req.get("tags_any")
            assert kw["top_k"] == req.get("top_k", api_v1.DEFAULT_TOP_K) and kw["follow"] == "active"
            assert kw["read_only"] is True and kw["auto_rebuild"] is False
            kw["coverage"]["unscored"] = resp["unscored"]
            return [
                {"score": h["fused"], "cosine": h["cosine"],
                 "memory": {"id": h["id"], "created_at": h["created_at"], "tags": h["tags"],
                            "content": h["preview"]}}
                for h in resp["results"]
            ]

        monkeypatch.setattr(storage, "hybrid_search_scored", search)
        monkeypatch.setattr(storage, "_read_meta_keys",
                            lambda conn, keys: {"embedding_model": resp["embedding_model"]})
    if "search_unavailable" in replay:
        spec = replay["search_unavailable"]

        def unavailable(conn, query, **kw):
            raise storage.SearchUnavailable(spec["reason"], spec["detail"])

        monkeypatch.setattr(storage, "hybrid_search_scored", unavailable)
    if replay.get("admission_full"):
        state = mcp._memora_api_state
        state.inflight = state.max_inflight


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f["name"] for f in FIXTURES])
def test_fixture_matches_the_live_handler(fixture, app_factory, monkeypatch):
    mcp = app_factory((fixture.get("replay") or {}).get("probe", "ok"))
    _install_stubs(fixture, monkeypatch, mcp)
    request = fixture["request"]
    with TestClient(mcp.streamable_http_app()) as client:
        response = client.request(
            request["method"], request["path"], headers=request["headers"],
            json=request.get("body"),
        )
    problems = compare(fixture, response.status_code, response.json(), mode="exact")
    assert problems == [], problems
