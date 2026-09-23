#!/usr/bin/env python3
"""Repoint a memora MCP client from a direct-D1 stdio server to memora-all's
HTTP endpoint (docs/local-primary-implementation.md §6 F4/F5, slice L8;
procedure: docs/local-primary-credentials.md).

  scripts/repoint_mcp_config.py FILE --url http://nuc8:8920/mcp/<store>            # dry run
  scripts/repoint_mcp_config.py FILE --url … --apply                                 # write it
  scripts/repoint_mcp_config.py FILE --url … --apply \\
      --check-health-token-file H --check-admin-token-file A                         # verify first

FILE is a JSON MCP configuration with an "mcpServers" object: a workspace or
Claude/Codex `.mcp.json`, `~/.claude.json`, or memora's
`~/.config/memora/credentials*.mcp.json`. Every server entry that reaches D1
directly -- a stdio entry whose env or args carry a d1:// URI, or
CLOUDFLARE_API_TOKEN / CF_API_TOKEN -- is replaced by

    {"type": "http", "url": "<--url>"}

Every other entry and key is kept as it was, in order. With --server NAME
only that entry is considered.

  * dry run (default): prints the change with every secret masked; writes
    nothing.
  * --apply: first writes a backup FILE.bak-repoint-<UTC timestamp> with mode
    0600 (the original holds the token), then replaces FILE atomically (temp
    file + rename) with FILE's own mode. The new file holds no D1 URI and no
    Cloudflare token.
  * --check-health-token-file / --check-admin-token-file: run
    `local_primary.py check-endpoint` against the URL's SERVER, through the
    scratch local store --check-store (default "scratch"), BEFORE writing; a
    failed check refuses the repoint. (The URL's own store is usually
    D1-backed; check-endpoint never writes such a store.)

The backup still holds the old token: delete it once the old token is
revoked (the credential doc's step 6).

Exit: 0 repointed or dry run with changes shown, 3 nothing to repoint,
2 refused (bad input, failed check).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memora.config_audit import TOKEN, mask  # noqa: E402

D1 = re.compile(r"d1://")
TOKEN_KEYS = ("CLOUDFLARE_API_TOKEN", "CF_API_TOKEN")


class Refused(RuntimeError):
    pass


def reaches_d1(entry: Any) -> bool:
    """A server entry that talks to D1 itself."""
    if not isinstance(entry, dict):
        return False
    env = entry.get("env") or {}
    args = entry.get("args") or []
    blob = json.dumps({"env": env, "args": args, "command": entry.get("command")})
    if D1.search(blob):
        return True
    return isinstance(env, dict) and any(env.get(k) for k in TOKEN_KEYS)


def _masked(obj: Any) -> Any:
    text = json.dumps(obj, indent=2)
    text = re.sub(r"d1://[^\"\s]+", lambda m: mask(m.group(0)), text)
    return TOKEN.sub(lambda m: m.group(0).replace(m.group(2), mask(m.group(2))) if m.group(2) else m.group(0), text)


def plan(doc: Dict[str, Any], url: str, server: str = None) -> List[str]:
    servers = doc.get("mcpServers")
    if not isinstance(servers, dict):
        raise Refused('no "mcpServers" object in the file')
    names = [server] if server else list(servers)
    if server and server not in servers:
        raise Refused(f"no server {server!r} in mcpServers")
    return [n for n in names if reaches_d1(servers[n])]


def rewrite(doc: Dict[str, Any], names: List[str], url: str) -> Dict[str, Any]:
    out = json.loads(json.dumps(doc))  # deep copy, key order kept
    for n in names:
        out["mcpServers"][n] = {"type": "http", "url": url}
    return out


def _check(url: str, health_file: str, admin_file: str, store: str) -> Dict[str, Any]:
    from memora import local_primary as lp
    from memora.endpoint_check import EndpointCheckFailed, check_endpoint

    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    try:
        return check_endpoint(base, lp.load_credential_file(health_file), lp.load_credential_file(admin_file),
                              store)
    except (EndpointCheckFailed, lp.L5Refused) as exc:
        raise Refused(f"check-endpoint failed, not repointing: {exc}") from exc


def apply(path: Path, new_doc: Dict[str, Any], *, now: float = None) -> Path:
    st = path.stat()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now if now is not None else time.time()))
    backup = path.with_name(f"{path.name}.bak-repoint-{stamp}")
    if backup.exists():
        raise Refused(f"backup {backup} already exists")
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out, open(path, "rb") as src:
        shutil.copyfileobj(src, out)
        out.flush()
        os.fsync(out.fileno())
    os.chmod(backup, 0o600)
    tmp_fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(tmp_fd, "w") as out:
            json.dump(new_doc, out, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, st.st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return backup


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file")
    ap.add_argument("--url", required=True, help="memora-all's MCP URL, e.g. http://nuc8:8920/mcp/memora")
    ap.add_argument("--server", help="only this mcpServers entry")
    ap.add_argument("--apply", action="store_true", help="write the change (default: dry run)")
    ap.add_argument("--check-health-token-file")
    ap.add_argument("--check-admin-token-file")
    ap.add_argument("--check-store", default="scratch", help="the scratch LOCAL store check-endpoint writes")
    args = ap.parse_args(argv)
    path = Path(args.file).expanduser()
    try:
        if not re.match(r"^https?://[^/]+/mcp(/[A-Za-z0-9._-]+)?/?$", args.url):
            raise Refused(f"--url must be http(s)://host:port/mcp[/<store>], not {args.url!r}")
        if bool(args.check_health_token_file) != bool(args.check_admin_token_file):
            raise Refused("give both --check-health-token-file and --check-admin-token-file, or neither")
        try:
            doc = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise Refused(f"cannot read {path} as JSON: {exc}") from exc
        names = plan(doc, args.url, args.server)
        if not names:
            print(json.dumps({"ok": True, "file": str(path), "repointed": [], "note": "no direct-D1 server entry"}))
            return 3
        new_doc = rewrite(doc, names, args.url)
        for n in names:
            print(f"--- {path} mcpServers.{n} (masked)\n{_masked(doc['mcpServers'][n])}\n"
                  f"+++ {path} mcpServers.{n}\n{json.dumps(new_doc['mcpServers'][n], indent=2)}", file=sys.stderr)
        check = None
        if args.check_health_token_file:
            check = _check(args.url, args.check_health_token_file, args.check_admin_token_file, args.check_store)
        if not args.apply:
            print(json.dumps({"ok": True, "dry_run": True, "file": str(path), "repointed": names,
                              "checked": bool(check)}))
            return 0
        backup = apply(path, new_doc)
    except Refused as exc:
        print(json.dumps({"ok": False, "refused": str(exc)}))
        return 2
    print(json.dumps({"ok": True, "file": str(path), "repointed": names, "backup": str(backup),
                      "checked": bool(check)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
