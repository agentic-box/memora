#!/usr/bin/env python3
"""Check the memora API v1 contract (contracts/memora-api/v1).

  validate   schemas are valid JSON Schema (2020-12); every fixture's request
             and response bodies validate against their schemas; every named
             fixture the design requires exists; manifest.json matches.
  manifest   rewrite manifest.json (sha256 of every contract file).
  live       start a real memora server on a scratch store (tfidf embeddings,
             no LLM, a safe tokens file, declared projects, and a deliberately
             broken store), replay every fixture whose "live" block is set, and
             compare: status + schema always, and the body too for
             "compare": "exact" (volatile fields: presence and type only).
             Also checks the absorb 501 path wrote nothing. Never touches a
             live store: everything is under a fresh temporary directory.

Exit status: 0 when everything checks, 1 otherwise.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts" / "memora-api" / "v1"
SCHEMAS = CONTRACT / "schemas"
FIXTURES = CONTRACT / "fixtures"
MANIFEST = CONTRACT / "manifest.json"

TEST_TOKEN = "memora-api-v1-test-token"
REQUIRED_FIXTURES = {
    # clmux docs/PLAN_MEMORA_DAEMON.md §3.5, plus the project addendum.
    "search_ok", "search_empty", "search_keyword_only", "absorb_done",
    "absorb_in_progress", "absorb_writes_unsupported", "absorb_key_conflict",
    "unknown_store", "store_forbidden", "bad_token",
    "health_transactional", "health_writes_unsupported", "health_unavailable",
    "search_project_filter", "bad_request_unknown_project",
    # Phase 0 rounds 2-3: body cap, read-only search refusals.
    "payload_too_large", "search_model_mismatch", "search_store_unavailable",
}


def load_schema(name: str) -> Dict[str, Any]:
    return json.loads((SCHEMAS / name).read_text())


def load_fixtures() -> List[Dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted(FIXTURES.glob("*.json"))]


def contract_files() -> List[Path]:
    return sorted(p for p in CONTRACT.rglob("*") if p.is_file() and p != MANIFEST)


def build_manifest() -> Dict[str, Any]:
    return {
        "contract": "memora-api/v1",
        "contract_version": (CONTRACT / "VERSION").read_text().strip(),
        "files": {
            str(p.relative_to(CONTRACT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in contract_files()
        },
    }


def _validator(schema_name: str):
    from jsonschema import Draft202012Validator

    return Draft202012Validator(load_schema(schema_name))


def schema_errors(schema_name: str, instance: Any) -> List[str]:
    return [f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}"
            for e in _validator(schema_name).iter_errors(instance)]


def validate() -> List[str]:
    from jsonschema import Draft202012Validator

    problems: List[str] = []
    for path in sorted(SCHEMAS.glob("*.json")):
        try:
            Draft202012Validator.check_schema(json.loads(path.read_text()))
        except Exception as exc:
            problems.append(f"schema {path.name}: {exc}")
    fixtures = load_fixtures()
    names = {f["name"] for f in fixtures}
    for missing in sorted(REQUIRED_FIXTURES - names):
        problems.append(f"missing required fixture {missing}")
    for f in fixtures:
        if f"{f['name']}.json" not in {p.name for p in FIXTURES.glob('*.json')}:
            problems.append(f"fixture name {f['name']} does not match its file")
        body = f["request"].get("body")
        if body is not None and f.get("request_schema"):
            problems += [f"{f['name']} request {e}" for e in schema_errors(f["request_schema"], body)]
        problems += [f"{f['name']} response {e}"
                     for e in schema_errors(f["response_schema"], f["response"]["body"])]
        if f["response_schema"] == "health_response.json":
            ok = f["response"]["body"].get("status") == "ok"
            if (f["response"]["status"] == 200) != ok:
                problems.append(f"{f['name']}: health status {f['response']['status']} vs body status")
    if not MANIFEST.exists():
        problems.append("manifest.json missing (run: manifest)")
    elif json.loads(MANIFEST.read_text()) != build_manifest():
        problems.append("manifest.json does not match the contract files (run: manifest)")
    return problems


def compare(fixture: Dict[str, Any], status: int, body: Any, *, mode: str,
            store: Optional[str] = None) -> List[str]:
    """Compare a response with a fixture. mode: exact | schema."""
    name = fixture["name"]
    problems: List[str] = []
    if status != fixture["response"]["status"]:
        problems.append(f"{name}: status {status} != {fixture['response']['status']}")
    problems += [f"{name}: {e}" for e in schema_errors(fixture["response_schema"], body)]
    if mode == "exact" and isinstance(body, dict):
        expected = dict(fixture["response"]["body"])
        if store is not None and "store" in expected:
            expected["store"] = store
        for key in fixture.get("volatile", []):
            if key in expected:
                if key not in body or type(body[key]) is not type(expected[key]):
                    problems.append(f"{name}: volatile {key} missing or of another type")
                expected.pop(key)
                body = {k: v for k, v in body.items() if k != key}
        if body != expected:
            problems.append(f"{name}: body {json.dumps(body, sort_keys=True)} != "
                            f"{json.dumps(expected, sort_keys=True)}")
    return problems


# --------------------------------------------------------------------------
# live
# --------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http(base: str, method: str, path: str, headers: Dict[str, str], body: Any = None) -> Tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        return exc.code, json.loads(raw) if raw else None


def scratch_env(root: Path) -> Dict[str, str]:
    """Environment for a scratch server: every path under `root`."""
    secrets = root / "secrets"
    secrets.mkdir(mode=0o700)
    tokens = secrets / "tokens.json"
    tokens.write_text(json.dumps({
        hashlib.sha256(TEST_TOKEN.encode()).hexdigest(): ["memora", "nostore", "broken"],
    }))
    tokens.chmod(0o600)
    blocker = root / "not-a-directory"
    blocker.write_text("x")
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("MEMORA_", "CLOUDFLARE", "CF_", "AWS_", "OPENAI_"))
    }
    env.update({
        "HOME": str(root),  # the tokens file is "under $HOME": parents checked up to root only
        "MEMORA_DATABASES": json.dumps({
            "memora": str(root / "memora.db"),
            "broken": str(blocker / "broken.db"),  # parent is a file: no database (store_missing)
        }),
        "MEMORA_DEFAULT_DB": "memora",
        "MEMORA_API_TOKENS_FILE": str(tokens),
        # "broken" stands in for the fixtures' "memora" store, so it declares the same projects.
        "MEMORA_PROJECTS": json.dumps({"memora": ["project-a", "clmux", "memora"],
                                       "broken": ["project-a", "clmux", "memora"]}),
        "MEMORA_EMBEDDING_MODEL": "tfidf",
        "MEMORA_LLM_ENABLED": "false",
        "MEMORA_ALLOW_ANY_TAG": "1",
        "MEMORA_TRANSPORT": "streamable-http",
        "MEMORA_HOST": "127.0.0.1",
        "PYTHONPATH": str(ROOT),
    })
    return env


SEED = [
    ("clmuxd owns memora access: agents never call memora directly; reads happen at dispatch.",
     ["clmux/architecture", "landing"], "clmux"),
    ("memora absorb gates every supersession per leaf with a similarity floor and an LLM check.",
     ["memora/absorb"], "memora"),
    ("The daemon's sidebar shows memora health from the per-store health route.",
     ["clmux/research"], "clmux"),
]


def _seed(env: Dict[str, str]) -> None:
    code = (
        "import json,sys\n"
        "from memora import storage\n"
        "for content, tags, project in json.loads(sys.argv[1]):\n"
        "    with storage.connect() as c:\n"
        "        storage.add_memory(c, content=content, tags=tags, project=project)\n"
        # One normal (MCP-path) search records the store's embedding model,
        # as on any store in use; the API's read-only search never writes it.
        "with storage.connect() as c:\n"
        "    storage.semantic_search(c, 'init')\n"
        "    c.commit()\n"
    )
    subprocess.run([sys.executable, "-c", code, json.dumps(SEED)], env=env, check=True,
                   cwd=str(ROOT), capture_output=True)


def _count(env: Dict[str, str]) -> int:
    code = ("from memora import storage\n"
            "c = storage.connect()\n"
            "print(c.execute('SELECT COUNT(*) FROM memories').fetchone()[0])\n")
    out = subprocess.run([sys.executable, "-c", code], env=env, check=True, cwd=str(ROOT),
                         capture_output=True, text=True)
    return int(out.stdout.strip())


def live() -> List[str]:
    problems: List[str] = []
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="memora-api-contract-")))
    env = scratch_env(root)
    _seed(env)
    port = _free_port()
    log = open(root / "server.log", "wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "memora.server", "--no-graph", "--port", str(port)],
        env=env, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    auth = {"Authorization": f"Bearer {TEST_TOKEN}"}
    try:
        deadline = time.time() + 30
        while True:  # wait until both stores have a readiness verdict
            try:
                s1, _ = _http(base, "GET", "/api/v1/memora/health", auth)
                s2, b2 = _http(base, "GET", "/api/v1/broken/health", auth)
                if s1 == 200 and s2 == 503 and b2.get("reason") == "store_missing":
                    break
            except (urllib.error.URLError, ConnectionError):
                pass
            if time.time() > deadline:
                return [f"server did not become ready; log: {root / 'server.log'}"]
            time.sleep(0.3)
        before = _count(env)
        for f in load_fixtures():
            spec = f.get("live")
            if not spec:
                continue
            path = f["request"]["path"]
            if spec.get("store"):
                parts = path.split("/")
                parts[3] = spec["store"]
                path = "/".join(parts)
            status, body = _http(base, f["request"]["method"], path,
                                 dict(f["request"]["headers"]), f["request"].get("body"))
            problems += compare(f, status, body, mode=spec["compare"], store=spec.get("store"))
        if _count(env) != before:
            problems.append("the store changed during the replay (search and absorb must write nothing)")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("validate", "manifest", "live"))
    args = ap.parse_args(argv)
    if args.command == "manifest":
        MANIFEST.write_text(json.dumps(build_manifest(), indent=2, sort_keys=True) + "\n")
        print(f"wrote {MANIFEST.relative_to(ROOT)}")
        return 0
    problems = validate() if args.command == "validate" else live()
    for p in problems:
        print(f"FAIL {p}")
    if not problems:
        print(f"{args.command}: ok")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
