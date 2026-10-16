#!/usr/bin/env python3
"""The operator-config edits of scripts/add_store.sh (Item 95), as a tool of its own.

  add_store_config.py check  --all-env F --deploy-env G --db D --uri d1://A/I
  add_store_config.py apply  --all-env F --deploy-env G --db D --uri d1://A/I --backup-dir DIR
  add_store_config.py remove --all-env F --deploy-env G --db D --backup-dir DIR

check   refuses unless the store is NEW everywhere (not in MEMORA_DATABASES,
        MEMORA_REPLICAS or MEMORA_PROJECTS), its D1 id is configured nowhere,
        and the replication settings the finished store needs are in place.
apply   re-reads both files at the moment of writing, adds ONLY
          instances/all.env      MEMORA_DATABASES[<db>] = d1://<account>/<id>   (the interim, pre-frozen store)
          instances/deploy.env   MEMORA_PROJECTS[<db>]  = ["<db>"]
        keeps every other line and every other mapping byte for byte (verified
        by reading the files back), after a dated 0600 backup of each. Backups
        are evidence only: nothing here ever restores a whole file over later edits.
remove  the rollback of the interim stage: deletes ONLY the <db> keys from the
        CURRENT files, and only while MEMORA_DATABASES[<db>] is still the interim
        d1:// entry (after the cutover's env edits, use docs/cutover-runbook.md
        "Rollback" and the L6 runbook instead).

Exit 0 done, 2 refused (nothing was changed). Prints one JSON line.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DB_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
URI_RE = re.compile(r"d1://([^/\s]+)/([^/\s]+)")


class Refused(Exception):
    pass


class EnvFile:
    """A KEY=VALUE operator file read as lines; one JSON key edited in place."""

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self.text = self.path.read_text()
        except OSError as exc:
            raise Refused(f"cannot read {path}: {exc}")
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


def _ids(*maps: Optional[Dict[str, Any]]) -> set:
    out = set()
    for m in maps:
        for uri in (m or {}).values():
            hit = URI_RE.fullmatch(str(uri))
            if hit:
                out.add(hit.group(2))
    return out


def check(all_env: Path, deploy_env: Path, db: str, uri: str) -> Dict[str, Any]:
    if not DB_RE.fullmatch(db or ""):
        raise Refused(f"store name {db!r} must match {DB_RE.pattern}")
    hit = URI_RE.fullmatch(uri or "")
    if not hit:
        raise Refused("--uri must be d1://<account>/<database-id>")
    new_id = hit.group(2)
    a, d = EnvFile(all_env), EnvFile(deploy_env)
    databases = a.json_object("MEMORA_DATABASES")
    replicas = a.json_object("MEMORA_REPLICAS", required=False) or {}
    projects = d.json_object("MEMORA_PROJECTS")
    if db in databases or db in replicas or db in projects:
        raise Refused(f"store {db!r} is already configured (MEMORA_DATABASES / MEMORA_REPLICAS / MEMORA_PROJECTS)")
    if new_id in _ids(databases, replicas):
        raise Refused("the D1 database id is already used by another store: refusing")
    if a.raw("MEMORA_REPLICATION") != "write":
        raise Refused(f"{all_env}: MEMORA_REPLICATION must be 'write' (the other stores replicate in write mode)")
    if not a.raw("MEMORA_REPLICATION_INTERVAL_S"):
        raise Refused(f"{all_env}: MEMORA_REPLICATION_INTERVAL_S is not set")
    return {"stores": sorted(databases), "replicated": sorted(replicas), "projects": sorted(projects)}


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


def _write(path: Path, text: str) -> None:
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


def _backup_dir_outside(repo_hint: Path, backup_dir: Path) -> Path:
    backup_dir = Path(os.path.realpath(os.path.expanduser(str(backup_dir))))
    root = Path(os.path.realpath(repo_hint))
    if str(backup_dir) == str(root) or str(backup_dir).startswith(str(root) + os.sep):
        raise Refused(f"the backup directory {backup_dir} is inside the repo {root}; set it outside the checkout")
    return backup_dir


def _other_lines_unchanged(before: EnvFile, after: EnvFile, key: str) -> bool:
    i = before._index(key, required=True)
    return before.lines[:i] == after.lines[:i] and before.lines[i + 1:] == after.lines[i + 1:]


def apply(all_env: Path, deploy_env: Path, db: str, uri: str, backup_dir: Path, *, repo: Path) -> Dict[str, Any]:
    backup_dir = _backup_dir_outside(repo, backup_dir)
    check(all_env, deploy_env, db, uri)                      # at the moment of writing
    a, d = EnvFile(all_env), EnvFile(deploy_env)
    a0, d0 = EnvFile(all_env), EnvFile(deploy_env)
    databases = a.json_object("MEMORA_DATABASES")
    projects = d.json_object("MEMORA_PROJECTS")
    a.set_json("MEMORA_DATABASES", dict(databases, **{db: uri}))
    d.set_json("MEMORA_PROJECTS", dict(projects, **{db: [db]}))
    stamp = time.strftime("%Y%m%dT%H%M%S") + f"-{time.time_ns() % 1_000_000:06d}"
    backups = [str(_backup(Path(all_env), backup_dir, db, stamp)), str(_backup(Path(deploy_env), backup_dir, db, stamp))]
    _write(Path(all_env), a.render())
    try:
        _write(Path(deploy_env), d.render())
    except BaseException:
        # undo only OUR edit of the first file, and only if it is still ours
        cur = EnvFile(all_env)
        if cur.render() == a.render():
            _write(Path(all_env), a0.render())
        raise
    # read both back: the new entries are there, everything else is as it was
    ra, rd = EnvFile(all_env), EnvFile(deploy_env)
    if ra.json_object("MEMORA_DATABASES") != dict(databases, **{db: uri}) \
            or rd.json_object("MEMORA_PROJECTS") != dict(projects, **{db: [db]}) \
            or not _other_lines_unchanged(a0, ra, "MEMORA_DATABASES") \
            or not _other_lines_unchanged(d0, rd, "MEMORA_PROJECTS") \
            or ra.json_object("MEMORA_REPLICAS", required=False) != a0.json_object("MEMORA_REPLICAS", required=False):
        raise Refused("the files do not read back as edited; inspect them and the backups by hand")
    return {"added": {"MEMORA_DATABASES": {db: uri}, "MEMORA_PROJECTS": {db: [db]}},
            "unchanged_databases": sorted(databases), "backups": backups}


def remove(all_env: Path, deploy_env: Path, db: str, backup_dir: Path, *, repo: Path) -> Dict[str, Any]:
    backup_dir = _backup_dir_outside(repo, backup_dir)
    if not DB_RE.fullmatch(db or ""):
        raise Refused(f"store name {db!r} must match {DB_RE.pattern}")
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
    removed = {}
    if db in databases:
        removed["MEMORA_DATABASES"] = databases[db]
        a.set_json("MEMORA_DATABASES", {k: v for k, v in databases.items() if k != db})
    if db in projects:
        removed["MEMORA_PROJECTS"] = projects[db]
        d.set_json("MEMORA_PROJECTS", {k: v for k, v in projects.items() if k != db})
    if not removed:
        return {"removed": {}, "backups": []}
    stamp = time.strftime("%Y%m%dT%H%M%S") + f"-{time.time_ns() % 1_000_000:06d}"
    backups = [str(_backup(Path(all_env), backup_dir, db, stamp)), str(_backup(Path(deploy_env), backup_dir, db, stamp))]
    _write(Path(all_env), a.render())
    _write(Path(deploy_env), d.render())
    ra, rd = EnvFile(all_env), EnvFile(deploy_env)
    if db in ra.json_object("MEMORA_DATABASES") or db in rd.json_object("MEMORA_PROJECTS") \
            or not _other_lines_unchanged(a0, ra, "MEMORA_DATABASES") \
            or not _other_lines_unchanged(d0, rd, "MEMORA_PROJECTS"):
        raise Refused("the files do not read back as edited; inspect them and the backups by hand")
    return {"removed": removed, "backups": backups}


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="add_store_config.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=("check", "apply", "remove"))
    p.add_argument("--all-env", required=True)
    p.add_argument("--deploy-env", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--uri")
    p.add_argument("--backup-dir")
    p.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent))
    args = p.parse_args(argv)
    try:
        if args.cmd == "check":
            out = check(Path(args.all_env), Path(args.deploy_env), args.db, args.uri or "")
        else:
            if not args.backup_dir:
                raise Refused("--backup-dir is required")
            if args.cmd == "apply":
                out = apply(Path(args.all_env), Path(args.deploy_env), args.db, args.uri or "",
                            Path(args.backup_dir), repo=Path(args.repo))
            else:
                out = remove(Path(args.all_env), Path(args.deploy_env), args.db, Path(args.backup_dir),
                             repo=Path(args.repo))
    except Refused as exc:
        print(json.dumps({"ok": False, "refused": str(exc)}))
        return 2
    print(json.dumps({"ok": True, **out}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
