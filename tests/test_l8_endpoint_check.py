"""F4a check-endpoint (plan §6 F4a, slice L8) against a local HTTP fake of
memora-all: real sockets, real JSON-RPC over streamable HTTP (SSE framing),
no memora server and no D1."""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from memora.endpoint_check import EndpointCheckFailed, check_endpoint

REPO = Path(__file__).resolve().parent.parent
HEALTH, ADMIN = "H" * 48, "A" * 48


class FakeMemoraAll:
    """Answers like memora-all. Knobs change one behaviour each."""

    def __init__(self, **knobs):
        self.knobs = dict(kind="sqlite", admin_enforced=True, health_detail=True, readback=True,
                          delete_ok=True, stats_db=None, liveness=200)
        self.knobs.update(knobs)
        self.calls = []
        self.memories = {}
        self.next_id = 100
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body, headers=None, sse=False):
                raw = (f"event: message\ndata: {json.dumps(body)}\n\n" if sse else json.dumps(body)).encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream" if sse else "application/json")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                auth = self.headers.get("Authorization", "")
                fake.calls.append(("GET", self.path, auth[:12]))
                if self.path == "/health":
                    return self._send(fake.knobs["liveness"], {"status": "ok", "version": "0.4.6"})
                if self.path == "/admin/data-volume":
                    if auth != f"Bearer {ADMIN}" and fake.knobs["admin_enforced"]:
                        return self._send(401, {"error": "unauthorized"})
                    entry = {"needs_data_volume": True, "refused": None}
                    if fake.knobs["kind"] is not None:
                        entry["kind"] = fake.knobs["kind"]
                    return self._send(200, {"stores": {"scratch": entry,
                                                       "memora": {"kind": "d1", "needs_data_volume": True,
                                                                  "refused": None}}})
                if self.path.startswith("/health/db/"):
                    ok = auth == f"Bearer {HEALTH}" and fake.knobs["health_detail"]
                    return self._send(200, {"status": "ok", "latency_ms": 1.5} if ok else {"status": "ok"})
                return self._send(404, {})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                msg = json.loads(self.rfile.read(length) or b"{}")
                fake.calls.append(("POST", self.path, msg.get("method"),
                                   (msg.get("params") or {}).get("name")))
                store = self.path.rsplit("/", 1)[-1]
                if msg.get("method") == "initialize":
                    return self._send(200, {"jsonrpc": "2.0", "id": msg["id"], "result": {}},
                                      {"mcp-session-id": "sid-1"}, sse=True)
                if "id" not in msg:
                    return self._send(202, {})
                name = msg["params"]["name"]
                args = msg["params"].get("arguments", {})
                if name == "memory_stats":
                    out = {"database": fake.knobs["stats_db"] or store, "total_memories": len(fake.memories)}
                elif name == "memory_create":
                    fake.next_id += 1
                    fake.memories[fake.next_id] = args["content"]
                    out = {"memory": {"id": fake.next_id, "content": args["content"]}}
                elif name == "memory_get":
                    mid = args["memory_id"]
                    if mid not in fake.memories:
                        out = {"error": "not_found", "id": mid}
                    else:
                        content = fake.memories[mid] if fake.knobs["readback"] else "something else"
                        out = {"memory": {"id": mid, "content": content}}
                elif name == "memory_delete":
                    if fake.knobs["delete_ok"]:
                        fake.memories.pop(args["memory_id"], None)
                        out = {"status": "deleted", "id": args["memory_id"]}
                    else:
                        out = {"error": "not_found"}
                else:
                    out = {"error": "unknown tool"}
                result = {"content": [{"type": "text", "text": json.dumps(out)}]}
                return self._send(200, {"jsonrpc": "2.0", "id": msg["id"], "result": result}, sse=True)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tools(self):
        return [c[3] for c in self.calls if c[0] == "POST" and c[2] == "tools/call"]

    def close(self):
        self.server.shutdown()


@pytest.fixture
def fake_factory():
    made = []

    def make(**knobs):
        f = FakeMemoraAll(**knobs)
        made.append(f)
        return f
    yield make
    for f in made:
        f.close()


def test_full_round_trip_creates_and_deletes_one_memory(fake_factory):
    fake = fake_factory()
    report = check_endpoint(fake.url, HEALTH, ADMIN, "scratch")
    assert report["ok"] and [s["step"] for s in report["steps"]] == \
        ["liveness", "admin auth", "store kind", "health auth", "mcp round trip"]
    assert fake.tools() == ["memory_stats", "memory_create", "memory_get", "memory_delete", "memory_get"]
    assert fake.memories == {}, "the throwaway memory is gone"
    assert report["steps"][-1]["wrote"] is True


def test_no_write_mode_only_reads(fake_factory):
    fake = fake_factory()
    check_endpoint(fake.url, HEALTH, ADMIN, "scratch", write=False)
    assert fake.tools() == ["memory_stats"]


@pytest.mark.parametrize("knobs,store,step", [
    ({"kind": "d1"}, "scratch", "store kind"),
    ({"kind": "s3"}, "scratch", "store kind"),
    ({"kind": None}, "scratch", "store kind"),          # an older memora-all: no kinds reported
    ({}, "memora", "store kind"),                        # a D1-backed store named explicitly
    ({}, "nope", "store kind"),                          # not in the registry
    ({"admin_enforced": False}, "scratch", "admin auth"),
    ({"health_detail": False}, "scratch", "health auth"),
    ({"liveness": 503}, "scratch", "liveness"),
])
def test_refuses_before_any_write(fake_factory, knobs, store, step):
    fake = fake_factory(**knobs)
    with pytest.raises(EndpointCheckFailed) as exc:
        check_endpoint(fake.url, HEALTH, ADMIN, store)
    assert exc.value.step == step
    assert "memory_create" not in fake.tools(), "nothing may be written before every check passed"


def test_a_wrong_database_binding_refuses_before_writing(fake_factory):
    fake = fake_factory(stats_db="memora")
    with pytest.raises(EndpointCheckFailed, match="bound to 'memora'"):
        check_endpoint(fake.url, HEALTH, ADMIN, "scratch")
    assert "memory_create" not in fake.tools()


def test_a_failed_read_back_still_deletes(fake_factory):
    fake = fake_factory(readback=False)
    with pytest.raises(EndpointCheckFailed, match="does not read back"):
        check_endpoint(fake.url, HEALTH, ADMIN, "scratch")
    assert "memory_delete" in fake.tools() and fake.memories == {}


def test_a_failed_delete_is_reported(fake_factory):
    fake = fake_factory(delete_ok=False)
    with pytest.raises(EndpointCheckFailed, match="not deleted"):
        check_endpoint(fake.url, HEALTH, ADMIN, "scratch")


def test_equal_tokens_are_refused_without_a_request(fake_factory):
    fake = fake_factory()
    with pytest.raises(EndpointCheckFailed, match="same value"):
        check_endpoint(fake.url, ADMIN, ADMIN, "scratch")
    assert fake.calls == []


def _token_file(tmp_path, name, value, mode=0o600):
    p = tmp_path / name
    p.write_text(value)
    os.chmod(p, mode)
    return str(p)


def test_cli(fake_factory, tmp_path):
    fake = fake_factory()
    h, a = _token_file(tmp_path, "h", HEALTH), _token_file(tmp_path, "a", ADMIN)
    cmd = [sys.executable, str(REPO / "scripts" / "local_primary.py"), "check-endpoint",
           "--memora-url", fake.url, "--health-token-file", h, "--admin-token-file", a]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(r.stdout)["ok"] is True
    assert HEALTH not in r.stdout and ADMIN not in r.stdout
    r = subprocess.run(cmd + ["--store", "memora"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and json.loads(r.stdout)["step"] == "store kind"
    loose = _token_file(tmp_path, "loose", ADMIN, 0o644)
    r = subprocess.run(cmd[:-1] + [loose], capture_output=True, text=True, timeout=60)
    assert r.returncode == 2, "a world-readable token file is refused"


def test_admin_data_volume_reports_store_kinds(monkeypatch):
    """The server side: /admin/data-volume says which stores are local."""
    from memora import admin_auth, storage

    monkeypatch.setenv("MEMORA_DATABASES", json.dumps(
        {"s": "/data/s.db", "f": "file:///data/f.db", "d": "d" + "1://a/b", "o": "s3://b/k"}))
    monkeypatch.setenv("MEMORA_DEFAULT_DB", "s")
    storage.set_store_refusals({})
    status, body = admin_auth.data_volume_status()
    assert status == 200
    assert {n: e["kind"] for n, e in body["stores"].items()} == {"s": "sqlite", "f": "sqlite", "d": "d1", "o": "s3"}
