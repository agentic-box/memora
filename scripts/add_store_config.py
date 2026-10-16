#!/usr/bin/env python3
"""The operator-config edits of scripts/add_store.sh (Item 95), as a tool of its own.

  add_store_config.py check          --all-env F --deploy-env G --db D --uri d1://A/I   (new, or resumable)
  add_store_config.py verify-applied --all-env F --deploy-env G --db D --uri d1://A/I
  add_store_config.py apply          --all-env F --deploy-env G --db D --uri d1://A/I --backup-dir DIR
  add_store_config.py remove         --all-env F --deploy-env G --db D --backup-dir DIR

check    refuses unless the store is NEW everywhere (MEMORA_DATABASES, MEMORA_REPLICAS, MEMORA_PROJECTS), or is already
         present with EXACTLY the values this command would write (a resume after a crash between the two files), its
         D1 id is used by no other store, and the replication settings the finished store needs are in place.
verify-applied  succeeds only when both files already hold exactly those values and MEMORA_REPLICAS does not name the
         store (the precondition of the deploy and export steps: nothing is edited or deployed on a guess).
apply    under ONE shared operator lock for the pair (below), re-reads both files, adds ONLY
           instances/all.env      MEMORA_DATABASES[<db>] = d1://<account>/<id>   (the interim, pre-frozen store)
           instances/deploy.env   MEMORA_PROJECTS[<db>]  = ["<db>"]
         after dated 0600 backups of the files it changes, replaces each file only if its bytes are still exactly the
         ones the edit was computed from (otherwise it refuses and changes nothing further), reads both back, and checks
         that every other line and mapping is byte-identical. A crash between the two files is resumed by running it again.
remove   the rollback of the interim stage, same lock and checks: deletes ONLY the <db> keys from the CURRENT files, and
         only while MEMORA_DATABASES[<db>] is still the interim d1:// entry.

THE LOCK. Both files are guarded by one flock on <instances dir>/.operator-config.lock, held across read, check, backup,
write and read-back. It is honoured by the participating tools (this one); scripts/cutover_store.sh and a person with an
editor do not take it, so the byte-for-byte version check right before each replacement is the second line of defence, not
a guarantee against a non-cooperating writer in that last instant. Backups are evidence only: nothing here ever restores a
whole file over later edits.

Exit 0 done, 2 refused (nothing further was changed). Prints one JSON line.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

DB_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
URI_RE = re.compile(r"d1://([^/\s]+)/([^/\s]+)")
LOCK_NAME = ".operator-config.lock"

# Test seam only: called as hook(stage, path) at named points; a no-op in production.
_hook: Callable[[str, Path], None] = lambda stage, path: None


class Refused(Exception):
    pass


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class EnvFile:
    """A KEY=VALUE operator file read as lines; one JSON key edited in place."""

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self.raw_bytes = self.path.read_bytes()
        except OSError as exc:
            raise Refused(f"cannot read {path}: {exc}")
        self.sha = _sha(self.raw_bytes)
        self.text = self.raw_bytes.decode()
        self.lines = self.text.split("\n")

    def _index(self, key: str, *, required: bool) -> Optional[int]:
        hits = [i for i, ln in enumerate(self.lines) if re.match(rf"{re.escape(key)}=", ln)]
        if len(hits) > 1:
            raise Refused(f"{self.path} has {key} {len(hits)} times: fix it by hand first")
        if not hits:
            if required:
                raise Refused(f"{self.path} has no {key}")
            return None
        return hits[0]

    def raw(self, key: str) -> Optional[str]:
        i = self._index(key, required=False)
        if i is None:
            return None
        value = self.lines[i].split("=", 1)[1]
        if len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        return value

    def json_object(self, key: str, *, required: bool = True) -> Optional[Dict[str, Any]]:
        raw = self.raw(key)
        if raw is None or raw == "":
            if required:
                raise Refused(f"{self.path} has no {key}")
            return None
        try:
            value = json.loads(raw)
        except ValueError as exc:
            raise Refused(f"{self.path}: {key} is not JSON ({exc})")
        if not isinstance(value, dict):
            raise Refused(f"{self.path}: {key} is not a JSON object")
        return value

    def set_json(self, key: str, value: Dict[str, Any]) -> None:
        i = self._index(key, required=True)
        self.lines[i] = f"{key}='{json.dumps(value)}'"

    def render(self) -> str:
        return "\n".join(self.lines)


class OperatorLock:
    """flock on <dir of all.env>/.operator-config.lock for the whole operation."""

    def __init__(self, all_env: Path, timeout_s: float = 15.0):
        self.path = Path(all_env).parent / LOCK_NAME
        self.timeout_s = timeout_s
        self.fd: Optional[int] = None

    def __enter__(self):
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + self.timeout_s
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(self.fd)
                    self.fd = None
                    raise Refused(f"another add-store/operator-config run holds {self.path}; nothing was changed")
                time.sleep(0.1)

    def __exit__(self, *exc):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


def _ids(*maps: Optional[Dict[str, Any]], skip: Optional[str] = None) -> set:
    out = set()
    for m in maps:
        for name, uri in (m or {}).items():
            if name == skip:
                continue
            hit = URI_RE.fullmatch(str(uri))
            if hit:
                out.add(hit.group(2))
    return out


def _state(a: EnvFile, d: EnvFile, db: str, uri: str) -> Tuple[Dict[str, Any], Dict[str, Any], str, str]:
    """(databases, projects, all.env need, deploy.env need): need is 'add' or 'present'; anything else refuses."""
    if not DB_RE.fullmatch(db or ""):
        raise Refused(f"store name {db!r} must match {DB_RE.pattern}")
    hit = URI_RE.fullmatch(uri or "")
    if not hit:
        raise Refused("--uri must be d1://<account>/<database-id>")
    databases = a.json_object("MEMORA_DATABASES")
    replicas = a.json_object("MEMORA_REPLICAS", required=False) or {}
    projects = d.json_object("MEMORA_PROJECTS")
    if db in replicas:
        raise Refused(f"MEMORA_REPLICAS names {db!r}: the cutover has started; this tool does not apply")
    need_all = "add"
    if db in databases:
        if databases[db] != uri:
            raise Refused(f"store {db!r} is already configured in MEMORA_DATABASES with another value")
        need_all = "present"
    need_dep = "add"
    if db in projects:
        if projects[db] != [db]:
            raise Refused(f"store {db!r} is already configured in MEMORA_PROJECTS with another value")
        need_dep = "present"
    if hit.group(2) in _ids(databases, replicas, skip=db):
        raise Refused("the D1 database id is already used by another store: refusing")
    if a.raw("MEMORA_REPLICATION") != "write":
        raise Refused(f"{a.path}: MEMORA_REPLICATION must be 'write' (the other stores replicate in write mode)")
    if not a.raw("MEMORA_REPLICATION_INTERVAL_S"):
        raise Refused(f"{a.path}: MEMORA_REPLICATION_INTERVAL_S is not set")
    return databases, projects, need_all, need_dep


def check(all_env: Path, deploy_env: Path, db: str, uri: str) -> Dict[str, Any]:
    a, d = EnvFile(all_env), EnvFile(deploy_env)
    databases, projects, na, nd = _state(a, d, db, uri)
    return {"stores": sorted(databases), "projects": sorted(projects), "all_env": na, "deploy_env": nd,
            "replicated": sorted(a.json_object("MEMORA_REPLICAS", required=False) or {})}


def verify_applied(all_env: Path, deploy_env: Path, db: str, uri: str) -> Dict[str, Any]:
    out = check(all_env, deploy_env, db, uri)
    if out["all_env"] != "present" or out["deploy_env"] != "present":
        raise Refused(f"the configuration does not hold the interim entry for {db!r} yet "
                      f"(all.env: {out['all_env']}, deploy.env: {out['deploy_env']}): run the env step first; "
                      "nothing was deployed or exported")
    return out


def _backup(path: Path, backup_dir: Path, db: str, stamp: str) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    dest = backup_dir / f"{path.name}.bak-addstore-{db}-{stamp}"
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, path.read_bytes())
        os.fsync(fd)
    finally:
        os.close(fd)
    return dest


def _write_if_unchanged(path: Path, expected_sha: str, text: str) -> None:
    """Replace `path` only if its bytes are still exactly the ones the edit was computed from."""
    now = path.read_bytes()
    if _sha(now) != expected_sha:
        raise Refused(f"{path} changed while the edit was being prepared (another writer); nothing was written to it")
    tmp = path.with_name(path.name + ".addstore-tmp")
    mode = path.stat().st_mode & 0o777
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, text.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _backup_dir_outside(repo_hint: Path, backup_dir: Path) -> Path:
    backup_dir = Path(os.path.realpath(os.path.expanduser(str(backup_dir))))
    root = Path(os.path.realpath(repo_hint))
    if str(backup_dir) == str(root) or str(backup_dir).startswith(str(root) + os.sep):
        raise Refused(f"the backup directory {backup_dir} is inside the repo {root}; set it outside the checkout")
    return backup_dir


def _other_lines_unchanged(before: EnvFile, after: EnvFile, key: str) -> bool:
    i = before._index(key, required=True)
    return before.lines[:i] == after.lines[:i] and before.lines[i + 1:] == after.lines[i + 1:]


def apply(all_env: Path, deploy_env: Path, db: str, uri: str, backup_dir: Path, *, repo: Path,
          lock_timeout_s: float = 15.0) -> Dict[str, Any]:
    backup_dir = _backup_dir_outside(repo, backup_dir)
    with OperatorLock(all_env, lock_timeout_s):
        a, d = EnvFile(all_env), EnvFile(deploy_env)            # re-read under the lock
        a0, d0 = EnvFile(all_env), EnvFile(deploy_env)
        databases, projects, na, nd = _state(a, d, db, uri)
        if na == "present" and nd == "present":
            return {"added": {}, "already_applied": True, "unchanged_databases": sorted(databases), "backups": []}
        work: List[Tuple[Path, str, str]] = []                   # (path, expected sha, new text)
        if na == "add":
            a.set_json("MEMORA_DATABASES", dict(databases, **{db: uri}))
            work.append((Path(all_env), a0.sha, a.render()))
        if nd == "add":
            d.set_json("MEMORA_PROJECTS", dict(projects, **{db: [db]}))
            work.append((Path(deploy_env), d0.sha, d.render()))
        stamp = time.strftime("%Y%m%dT%H%M%S") + f"-{time.time_ns() % 1_000_000:06d}"
        backups = [str(_backup(path, backup_dir, db, stamp)) for path, _sha_, _t in work]
        _hook("before-write", Path(all_env))
        for path, expected, text in work:                         # all.env first, then deploy.env
            _write_if_unchanged(path, expected, text)
            _hook("after-write", path)
        ra, rd = EnvFile(all_env), EnvFile(deploy_env)
        if ra.json_object("MEMORA_DATABASES") != dict(databases, **{db: uri}) \
                or rd.json_object("MEMORA_PROJECTS") != dict(projects, **{db: [db]}) \
                or not _other_lines_unchanged(a0, ra, "MEMORA_DATABASES") \
                or not _other_lines_unchanged(d0, rd, "MEMORA_PROJECTS") \
                or ra.json_object("MEMORA_REPLICAS", required=False) != a0.json_object("MEMORA_REPLICAS", required=False):
            raise Refused("the files do not read back as edited; inspect them and the backups by hand")
        return {"added": {"MEMORA_DATABASES": {db: uri}, "MEMORA_PROJECTS": {db: [db]}},
                "wrote": [str(p) for p, _s, _t in work],
                "unchanged_databases": sorted(databases), "backups": backups}


def remove(all_env: Path, deploy_env: Path, db: str, backup_dir: Path, *, repo: Path,
           lock_timeout_s: float = 15.0) -> Dict[str, Any]:
    backup_dir = _backup_dir_outside(repo, backup_dir)
    if not DB_RE.fullmatch(db or ""):
        raise Refused(f"store name {db!r} must match {DB_RE.pattern}")
    with OperatorLock(all_env, lock_timeout_s):
        a, d = EnvFile(all_env), EnvFile(deploy_env)
        a0, d0 = EnvFile(all_env), EnvFile(deploy_env)
        databases = a.json_object("MEMORA_DATABASES")
        replicas = a.json_object("MEMORA_REPLICAS", required=False)
        projects = d.json_object("MEMORA_PROJECTS")
        entry = databases.get(db)
        if entry is not None and not URI_RE.fullmatch(str(entry)):
            raise Refused(f"MEMORA_DATABASES[{db!r}] is {entry!r}, not the interim d1:// entry: the store is past the "
                          "interim stage; use docs/cutover-runbook.md \"Rollback\" and the L6 runbook")
        if replicas and db in replicas:
            raise Refused(f"MEMORA_REPLICAS names {db!r}: the cutover has started; use the cutover rollback runbook")
        removed: Dict[str, Any] = {}
        work: List[Tuple[Path, str, str]] = []
        if db in databases:
            removed["MEMORA_DATABASES"] = databases[db]
            a.set_json("MEMORA_DATABASES", {k: v for k, v in databases.items() if k != db})
            work.append((Path(all_env), a0.sha, a.render()))
        if db in projects:
            removed["MEMORA_PROJECTS"] = projects[db]
            d.set_json("MEMORA_PROJECTS", {k: v for k, v in projects.items() if k != db})
            work.append((Path(deploy_env), d0.sha, d.render()))
        if not removed:
            return {"removed": {}, "backups": []}
        stamp = time.strftime("%Y%m%dT%H%M%S") + f"-{time.time_ns() % 1_000_000:06d}"
        backups = [str(_backup(path, backup_dir, db, stamp)) for path, _s, _t in work]
        _hook("before-write", Path(all_env))
        for path, expected, text in work:
            _write_if_unchanged(path, expected, text)
            _hook("after-write", path)
        ra, rd = EnvFile(all_env), EnvFile(deploy_env)
        if db in ra.json_object("MEMORA_DATABASES") or db in rd.json_object("MEMORA_PROJECTS") \
                or not _other_lines_unchanged(a0, ra, "MEMORA_DATABASES") \
                or not _other_lines_unchanged(d0, rd, "MEMORA_PROJECTS"):
            raise Refused("the files do not read back as edited; inspect them and the backups by hand")
        return {"removed": removed, "backups": backups}


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="add_store_config.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=("check", "verify-applied", "apply", "remove"))
    p.add_argument("--all-env", required=True)
    p.add_argument("--deploy-env", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--uri")
    p.add_argument("--backup-dir")
    p.add_argument("--lock-timeout", type=float, default=15.0)
    p.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent))
    args = p.parse_args(argv)
    try:
        if args.cmd == "check":
            out = check(Path(args.all_env), Path(args.deploy_env), args.db, args.uri or "")
        elif args.cmd == "verify-applied":
            out = verify_applied(Path(args.all_env), Path(args.deploy_env), args.db, args.uri or "")
        else:
            if not args.backup_dir:
                raise Refused("--backup-dir is required")
            if args.cmd == "apply":
                out = apply(Path(args.all_env), Path(args.deploy_env), args.db, args.uri or "",
                            Path(args.backup_dir), repo=Path(args.repo), lock_timeout_s=args.lock_timeout)
            else:
                out = remove(Path(args.all_env), Path(args.deploy_env), args.db, Path(args.backup_dir),
                             repo=Path(args.repo), lock_timeout_s=args.lock_timeout)
    except Refused as exc:
        print(json.dumps({"ok": False, "refused": str(exc)}))
        return 2
    print(json.dumps({"ok": True, **out}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
