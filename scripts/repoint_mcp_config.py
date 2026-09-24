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
`~/.config/memora/credentials*.mcp.json`. For every server entry that reaches
D1 directly -- a stdio entry whose env or args carry a d1:// URI, or
CLOUDFLARE_API_TOKEN / CF_API_TOKEN -- only the ROUTING is rewritten
(review 7667 P1-2b):

  * "command" and "args" are replaced by "type": "http", "url": <--url>;
  * from "env", CLOUDFLARE_API_TOKEN and CF_API_TOKEN are removed, a d1://
    MEMORA_STORAGE_URI is removed, and the d1:// entries of a
    MEMORA_DATABASES registry are removed (the key goes when none remain);
  * every other env key (LLM, embedding, AWS, tuning) is KEPT, because
    memora-instance.sh's cred_args reads them from
    credentials*.mcp.json. --drop-env removes the whole env instead, for a
    client that rejects env on an http entry.

Every other entry and key is kept as it was, in order. With --server NAME
only that entry is considered.

Output never contains a value from an entry (review 7667 P1-1): the preview
prints keys and routing fields only, each value as <redacted:LENGTH>, and
every args element that is not a flag name likewise.

  * dry run (default): prints the change, redacted; writes nothing.
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
revoked (the credential doc's step 8).

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
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


def safe_url(value: Any) -> str:
    """A URL as scheme://host[:port] only (review 7689 P1-1): a path segment,
    userinfo, a query or a fragment can each carry a credential, so none is
    printed -- a path is shown as its segment count."""
    if not isinstance(value, str):
        return _redacted(value)
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return _redacted(value)
    if not parts.scheme or not host:
        return _redacted(value)
    out = f"{parts.scheme}://{host}{port}"
    segments = [p for p in parts.path.split("/") if p]
    if segments:
        out += f"/<redacted path:{len(segments)} segment{'s' if len(segments) != 1 else ''}>"
    withheld = [w for w, present in (("userinfo", parts.username or parts.password), ("query", parts.query),
                                     ("fragment", parts.fragment)) if present]
    if withheld:
        out += f" ({'/'.join(withheld)} withheld)"
    return out


ROUTING_FIELDS = ("type",)  # printed as they are; url through safe_url; everything else is redacted


def _redacted(value: Any) -> str:
    return f"<redacted:{len(value) if isinstance(value, str) else len(json.dumps(value))}>"


def redact_entry(entry: Any) -> Any:
    """An MCP server entry with every value redacted: env values, args that
    are not flag names, command, url, headers. Keys and the "type" field
    remain, so an operator can see WHAT changes, never a secret."""
    if not isinstance(entry, dict):
        return _redacted(entry)
    out: Dict[str, Any] = {}
    for key, value in entry.items():
        if key in ROUTING_FIELDS and isinstance(value, str):
            out[key] = value
        elif key == "url":
            out[key] = safe_url(value)
        elif key == "env" and isinstance(value, dict):
            out[key] = {k: _redacted(v) for k, v in value.items()}
        elif key == "args" and isinstance(value, list):
            out[key] = [a if isinstance(a, str) and re.fullmatch(r"--?[A-Za-z][\w-]*", a) else _redacted(a)
                        for a in value]
        elif isinstance(value, dict):
            out[key] = {k: _redacted(v) for k, v in value.items()}
        else:
            out[key] = _redacted(value)
    return out


def _without_d1_registry(raw: str) -> Optional[str]:
    """MEMORA_DATABASES minus its d1:// entries; None when nothing is left
    or the value is not a JSON object (then the whole key goes)."""
    try:
        reg = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(reg, dict):
        return None
    kept = {k: v for k, v in reg.items() if not (isinstance(v, str) and v.startswith("d1://"))}
    return json.dumps(kept) if kept else None


def repoint_entry(entry: Dict[str, Any], url: str, *, drop_env: bool = False) -> Dict[str, Any]:
    """Only the routing changes: command/args -> type/url; the D1 URI and
    the Cloudflare token leave env; every other env key stays."""
    out: Dict[str, Any] = {"type": "http", "url": url}
    for key, value in entry.items():
        if key in ("command", "args", "type", "url", "env", "cwd", "transport"):
            continue
        out[key] = value
    env = entry.get("env")
    if isinstance(env, dict) and not drop_env:
        kept: Dict[str, Any] = {}
        for k, v in env.items():
            if k in TOKEN_KEYS:
                continue
            if k == "MEMORA_STORAGE_URI" and isinstance(v, str) and D1.search(v):
                continue
            if k == "MEMORA_DATABASES" and isinstance(v, str) and D1.search(v):
                reduced = _without_d1_registry(v)
                if reduced is not None:
                    kept[k] = reduced
                continue
            if isinstance(v, str) and D1.search(v):
                continue  # any other d1:// value is routing too
            kept[k] = v
        if kept:
            out["env"] = kept
    return out


def plan(doc: Dict[str, Any], url: str, server: str = None) -> List[str]:
    servers = doc.get("mcpServers")
    if not isinstance(servers, dict):
        raise Refused('no "mcpServers" object in the file')
    names = [server] if server else list(servers)
    if server and server not in servers:
        raise Refused(f"no server {server!r} in mcpServers")
    return [n for n in names if reaches_d1(servers[n])]


def rewrite(doc: Dict[str, Any], names: List[str], url: str, *, drop_env: bool = False) -> Dict[str, Any]:
    out = json.loads(json.dumps(doc))  # deep copy, key order kept
    for n in names:
        out["mcpServers"][n] = repoint_entry(out["mcpServers"][n], url, drop_env=drop_env)
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
    ap.add_argument("--drop-env", action="store_true",
                    help="remove the whole env of a repointed entry (for clients that reject env on http)")
    ap.add_argument("--check-health-token-file")
    ap.add_argument("--check-admin-token-file")
    ap.add_argument("--check-store", default="scratch", help="the scratch LOCAL store check-endpoint writes")
    args = ap.parse_args(argv)
    path = Path(args.file).expanduser()
    try:
        if not re.match(r"^https?://[^/]+/mcp(/[A-Za-z0-9._-]+)?/?$", args.url):
            raise Refused(f"--url must be http(s)://host:port/mcp[/<store>], not {safe_url(args.url)!r}")
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
        new_doc = rewrite(doc, names, args.url, drop_env=args.drop_env)
        for n in names:
            print(f"--- {path} mcpServers.{n} (values redacted)\n"
                  f"{json.dumps(redact_entry(doc['mcpServers'][n]), indent=2)}\n"
                  f"+++ {path} mcpServers.{n} (values redacted)\n"
                  f"{json.dumps(redact_entry(new_doc['mcpServers'][n]), indent=2)}", file=sys.stderr)
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
