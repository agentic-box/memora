"""The local-primary operator tool (`scripts/local_primary.py`).

docs/local-primary-implementation.md §0 P1, §4, §6.1, §8 L5. Every step
that reads D1 does it under the store's freeze (memora-all's §1 barrier,
re-checked at every step boundary), with the D1 READ token only
(MEMORA_D1_READ_TOKEN). The only D1 writes -- the sequence high-water
UPDATE and operator-selected R2-restore rows -- use an operator credential
read from a 0600 file, never the service environment, through a writer
whose statement allow-list is fixed here.

Commands: export, recheck (piece a); seed, sequence-highwater, snapshot,
volume-check (piece b); restore, reconcile, resume (piece c).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

POINTER = "docs/local-primary-implementation.md §0 P1, §4"
EPOCH_SQL = "SELECT value FROM memories_meta WHERE key = 'embedding_change_epoch'"
RECEIPT_MAX_AGE_S = 24 * 3600
PAGE_ROWS = 500
EXPORT_ATTEMPTS = 3


class L5Refused(RuntimeError):
    """A step refused to run (a precondition failed). Nothing was changed by
    the refusing step."""


class L5Halt(RuntimeError):
    """A D1 write step halted (for example D1 rejected the sequence UPDATE).
    The surrounding procedure must not continue."""


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ credentials (§6.1)

def read_token(token_file: Optional[str] = None) -> str:
    """The D1 READ token: from a file when given, else MEMORA_D1_READ_TOKEN."""
    if token_file:
        return load_credential_file(token_file)
    token = os.getenv("MEMORA_D1_READ_TOKEN", "").strip()
    if not token:
        raise L5Refused("no D1 read token: set MEMORA_D1_READ_TOKEN or pass --read-token-file")
    return token


def load_credential_file(path: str) -> str:
    """A secret from a file that only its owner can read (mode 0600 or
    stricter, owned by this user). Never from the environment."""
    p = Path(path)
    try:
        st = p.stat()
    except OSError as exc:
        raise L5Refused(f"credential file {path}: {exc}")
    if not stat.S_ISREG(st.st_mode):
        raise L5Refused(f"credential file {path} is not a regular file")
    if st.st_mode & 0o077:
        raise L5Refused(f"credential file {path} must be mode 0600 (it is {oct(st.st_mode & 0o777)})")
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise L5Refused(f"credential file {path} is not owned by this user")
    token = p.read_text().strip()
    if not token:
        raise L5Refused(f"credential file {path} is empty")
    return token


# ------------------------------------------------------------------ D1 reads

class D1Reader:
    """Read-only helpers over a SELECT-only connection (the read token)."""

    def __init__(self, conn):
        self.conn = conn  # D1SelectOnlyConnection or a test double with .execute -> (rows, meta)

    def rows(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        rows, _meta = self.conn.execute(sql, tuple(params))
        return rows

    def epoch(self) -> int:
        rows = self.rows(EPOCH_SQL)
        if not rows:
            raise L5Refused("D1 has no embedding_change_epoch row")
        return int(rows[0]["value"])

    def tables(self) -> List[Tuple[str, str]]:
        """(name, CREATE sql) of every user table, in creation order."""
        rows = self.rows("SELECT name, sql FROM sqlite_master WHERE type = 'table' AND sql IS NOT NULL "
                         "ORDER BY rowid")
        return [(r["name"], r["sql"]) for r in rows if _user_table(r["name"])]

    def indexes_and_triggers(self) -> List[str]:
        rows = self.rows("SELECT name, tbl_name, sql FROM sqlite_master WHERE type IN ('index', 'trigger') "
                         "AND sql IS NOT NULL ORDER BY rowid")
        return [r["sql"] for r in rows if _user_table(r["tbl_name"])]

    def columns(self, table: str) -> List[Tuple[str, int]]:
        rows = self.rows("SELECT name, pk FROM pragma_table_info(?) ORDER BY cid", (table,))
        return [(r["name"], int(r["pk"] or 0)) for r in rows]

    def all_rows(self, table: str) -> Iterable[Dict[str, Any]]:
        order = _order_by(self.columns(table))
        offset = 0
        while True:
            page = self.rows(f'SELECT * FROM "{table}" ORDER BY {order} LIMIT ? OFFSET ?', (PAGE_ROWS, offset))
            yield from page
            if len(page) < PAGE_ROWS:
                return
            offset += PAGE_ROWS

    def sequences(self) -> Dict[str, int]:
        try:
            rows = self.rows("SELECT name, seq FROM sqlite_sequence")
        except Exception:
            return {}
        return {r["name"]: int(r["seq"]) for r in rows}


def _user_table(name: Optional[str]) -> bool:
    return bool(name) and not name.startswith(("sqlite_", "_cf_", "memories_fts"))


def _order_by(cols: List[Tuple[str, int]]) -> str:
    pk = [name for name, p in sorted(cols, key=lambda c: c[1]) if p]
    return ", ".join(f'"{c}"' for c in pk) if pk else "rowid"


# ------------------------------------------------------------------ hashing

def _norm(v: Any) -> Any:
    """One value, normalised so a D1 JSON value and the same SQLite value
    hash alike (D1 may return 1 for 1.0)."""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, (bytes, bytearray)):
        return {"$hex": bytes(v).hex()}
    if isinstance(v, list):  # a BLOB as D1 returns it
        return {"$hex": bytes(v).hex()}
    return v


def table_stats(rows: Iterable[Dict[str, Any]], columns: List[str]) -> Dict[str, Any]:
    """{count, sha256} over rows already in primary-key order."""
    h = hashlib.sha256()
    n = 0
    cols = sorted(columns)
    for row in rows:
        h.update(json.dumps([_norm(row.get(c)) for c in cols], separators=(",", ":"), sort_keys=True,
                            ensure_ascii=False).encode("utf-8"))
        h.update(b"\n")
        n += 1
    return {"count": n, "sha256": h.hexdigest()}


def remote_stats(reader: D1Reader, tables: List[str]) -> Dict[str, Dict[str, Any]]:
    out = {}
    for t in tables:
        cols = [c for c, _ in reader.columns(t)]
        out[t] = table_stats(reader.all_rows(t), cols)
    return out


def _scratch_connect(db_path: Path) -> sqlite3.Connection:
    """The one sqlite3.connect in this module: scratch files (an export
    being verified, a file being seeded before it is a store). A live store
    is only ever opened through its backend (primary lock, write gate)."""
    return sqlite3.connect(str(db_path))


def local_stats(db_path: Path, tables: List[str]) -> Dict[str, Dict[str, Any]]:
    db = _scratch_connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        out = {}
        for t in tables:
            info = [(r[1], int(r[5] or 0)) for r in db.execute(f'PRAGMA table_info("{t}")')]
            cols = [c for c, _ in info]
            rows = (dict(r) for r in db.execute(f'SELECT * FROM "{t}" ORDER BY {_order_by(info)}'))
            out[t] = table_stats(rows, cols)
        return out
    finally:
        db.close()


# ------------------------------------------------------------------ export writers

def _sql_literal(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, (bytes, bytearray)):
        return "X'" + bytes(v).hex() + "'"
    if isinstance(v, list):
        return "X'" + bytes(v).hex() + "'"
    return "'" + str(v).replace("'", "''") + "'"


def export_select(reader: D1Reader, out: Path) -> List[str]:
    """The paged-SELECT export (only under the freeze, §4): the schema from
    sqlite_master, then one INSERT per row. Returns the exported tables."""
    tables = reader.tables()
    names = [n for n, _ in tables]
    tmp = out.with_suffix(out.suffix + ".partial")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("PRAGMA foreign_keys=OFF;\nBEGIN TRANSACTION;\n")
        for _name, ddl in tables:
            fh.write(ddl.rstrip(";") + ";\n")
        for name in names:
            cols = [c for c, _ in reader.columns(name)]
            collist = ", ".join(f'"{c}"' for c in cols)
            for row in reader.all_rows(name):
                vals = ", ".join(_sql_literal(row.get(c)) for c in cols)
                fh.write(f'INSERT INTO "{name}" ({collist}) VALUES ({vals});\n')
        seqs = reader.sequences()
        for name, seq in sorted(seqs.items()):
            fh.write(f"INSERT INTO sqlite_sequence (name, seq) VALUES ({_sql_literal(name)}, {seq});\n")
        for ddl in reader.indexes_and_triggers():
            fh.write(ddl.rstrip(";") + ";\n")
        fh.write("COMMIT;\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, out)
    return names


def export_native(account_id: str, d1_name: str, token: str, out: Path) -> None:
    """`wrangler d1 export --remote` with an environment built from scratch
    (§4): PATH, HOME, the account, and CLOUDFLARE_API_TOKEN = the READ token.
    Env-gated by the caller (MEMORA_L5_NATIVE_EXPORT=1)."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"),
           "CLOUDFLARE_ACCOUNT_ID": account_id, "CLOUDFLARE_API_TOKEN": token}
    r = subprocess.run(["npx", "wrangler", "d1", "export", d1_name, "--remote", "--output", str(out)],
                       env=env, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0 or not out.exists():
        raise L5Refused(f"native export failed (exit {r.returncode}): {r.stderr[-500:]}")


def load_sql(sql_path: Path, db_path: Path) -> None:
    db = _scratch_connect(db_path)
    try:
        db.executescript(sql_path.read_text(encoding="utf-8"))
        db.commit()
    finally:
        db.close()


# ------------------------------------------------------------------ R2

class FsR2:
    """A filesystem R2 (tests, rehearsals): keys are relative paths."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def put(self, key: str, path: Path) -> None:
        dest = self.root / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)

    def get(self, key: str) -> bytes:
        return (self.root / key).read_bytes()

    def list(self, prefix: str) -> List[str]:
        base = self.root / prefix
        if not base.exists():
            return []
        return sorted(str(p.relative_to(self.root)) for p in base.rglob("*") if p.is_file())

    def delete(self, key: str) -> None:
        (self.root / key).unlink()


class S3R2:
    """R2 through its S3 API (boto3, AWS_* / MEMORA_R2_* settings)."""

    def __init__(self, bucket: str):
        import boto3

        self.bucket = bucket
        self.client = boto3.client("s3", endpoint_url=os.getenv("MEMORA_R2_ENDPOINT") or os.getenv("AWS_ENDPOINT_URL"))

    def put(self, key: str, path: Path) -> None:
        self.client.upload_file(str(path), self.bucket, key)

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def list(self, prefix: str) -> List[str]:
        keys, token = [], None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            resp = self.client.list_objects_v2(**kw)
            keys += [o["Key"] for o in resp.get("Contents", [])]
            if not resp.get("IsTruncated"):
                return sorted(keys)
            token = resp.get("NextContinuationToken")

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


# ------------------------------------------------------------------ the freeze (§1)

class FreezeClient:
    """memora-all's freeze barrier over HTTP: POST /admin/freeze/<db>, and
    GET /health/db/<db> re-checked at every step boundary (§9 p): the step
    continues only while the store reports `frozen` with 0 in flight and no
    open intent (`frozen-unsafe` is a refusal)."""

    def __init__(self, base_url: str, admin_token: str, db: str, *, health_token: Optional[str] = None,
                 timeout: float = 60.0):
        self.base = base_url.rstrip("/")
        self.admin_token = admin_token
        # /health/db/<db> shows the freeze fields only to an authorised caller
        # (MEMORA_HEALTH_TOKEN, or a loopback peer -- not one behind docker's
        # port mapping), so it may need its own token.
        self.health_token = health_token or admin_token
        self.db = db
        self.timeout = timeout
        self.placed = False

    def _request(self, method: str, path: str) -> Tuple[int, Dict[str, Any]]:
        token = self.health_token if path.startswith("/health/") else self.admin_token
        req = urllib.request.Request(self.base + path, method=method,
                                     headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"{}")
            except ValueError:
                return exc.code, {}
        except (urllib.error.URLError, OSError) as exc:
            raise L5Refused(f"memora-all is not reachable at {self.base}: {exc}")

    def freeze(self) -> None:
        status, body = self._request("GET", f"/health/db/{self.db}")
        already = (body.get("freeze") or {}).get("state") in ("frozen", "frozen-unsafe")
        if not already:
            status, body = self._request("POST", f"/admin/freeze/{self.db}?timeout_s=30")
            if status != 200:
                raise L5Refused(f"freeze of {self.db} refused ({status}): {body}")
            self.placed = True
        self.check("after placing the freeze")

    def check(self, where: str) -> None:
        status, body = self._request("GET", f"/health/db/{self.db}")
        if "freeze" not in body:
            raise L5Refused(f"/health/db/{self.db} ({status}) shows no freeze state {where}: "
                            "unknown store, no readiness result yet, or the health token is not accepted")
        fr = body.get("freeze") or {}
        if fr.get("state") != "frozen" or fr.get("in_flight") != 0 or fr.get("open_intents"):
            raise L5Refused(f"{self.db} is not frozen with 0 in flight {where}: "
                            f"state={fr.get('state')} in_flight={fr.get('in_flight')} "
                            f"open_intents={fr.get('open_intents')}")

    def thaw_if_placed(self) -> None:
        if self.placed:
            self.placed = False
            status, body = self._request("DELETE", f"/admin/freeze/{self.db}")
            if status != 200:
                raise L5Refused(f"the freeze this step placed on {self.db} was NOT lifted ({status}): {body}; "
                                f"lift it with DELETE /admin/freeze/{self.db}")


class ServiceStopped:
    """The barrier when memora-all is stopped (rollback steps): the
    container must report State.Running=false at every step boundary."""

    def __init__(self, container: str = "memora-all", runner: Callable[..., Any] = subprocess.run):
        self.container = container
        self.runner = runner
        self.placed = False

    def _running(self) -> str:
        r = self.runner(["docker", "inspect", "-f", "{{.State.Running}}", self.container],
                        capture_output=True, text=True, timeout=30)
        return (r.stdout or "").strip() if r.returncode == 0 else f"inspect failed: {r.stderr.strip()}"

    def freeze(self) -> None:
        self.check("at the start")

    def check(self, where: str) -> None:
        state = self._running()
        if state != "false":
            raise L5Refused(f"{self.container} must be stopped {where} (State.Running={state})")

    def thaw_if_placed(self) -> None:
        return None


# ------------------------------------------------------------------ receipts (P1)

@dataclass
class Deps:
    """What a command touches, injectable for tests."""
    reader: D1Reader
    freeze: Any
    r2: Any
    account_id: str
    database_id: str
    d1_name: Optional[str] = None
    read_token: Optional[str] = None
    native_export: bool = False
    writer_factory: Optional[Callable[[], Any]] = None
    clock: Callable[[], float] = time.time
    log: Callable[[str], None] = lambda msg: print(msg, file=sys.stderr)


def _verify_export(deps: Deps, sql_path: Path, tables: List[str], work: Path) -> Dict[str, Dict[str, Any]]:
    scratch = work / "verify.db"
    if scratch.exists():
        scratch.unlink()
    load_sql(sql_path, scratch)
    local = local_stats(scratch, tables)
    scratch.unlink()
    remote = remote_stats(deps.reader, tables)
    if local != remote:
        bad = sorted(t for t in tables if local.get(t) != remote.get(t))
        raise _Mismatch(f"export does not match D1 in {bad}")
    return local


class _Mismatch(Exception):
    pass


def export(db: str, deps: Deps, out_dir: Path) -> Path:
    """P1: a verified export under the freeze, uploaded to R2 and read back,
    with a receipt. Returns the receipt path."""
    out_dir = Path(out_dir) / db
    out_dir.mkdir(parents=True, exist_ok=True)
    deps.freeze.freeze()
    try:
        return _export_frozen(db, deps, out_dir)
    finally:
        deps.freeze.thaw_if_placed()


def _export_frozen(db: str, deps: Deps, out_dir: Path) -> Path:
    base = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(deps.clock()))
    stamp, n = base, 1
    while any(out_dir.glob(f"{stamp}.*")):  # never overwrite an earlier export or receipt
        n += 1
        stamp = f"{base}-{n}"
    sql_path = out_dir / f"{stamp}.sql"
    last_error = None
    for _attempt in range(EXPORT_ATTEMPTS):
        deps.freeze.check("before the export")
        before = deps.reader.epoch()
        method = "select"
        if deps.native_export:
            try:
                export_native(deps.account_id, deps.d1_name or db, deps.read_token or "", sql_path)
                method = "native"
            except (L5Refused, OSError, subprocess.SubprocessError) as exc:
                # §4: if the read token may not export, fail closed to the
                # paged SELECT -- still under this freeze.
                deps.log(f"native export refused, using the paged SELECT: {exc}")
        if method == "native":
            tables = [n for n, _ in deps.reader.tables()]
        else:
            tables = export_select(deps.reader, sql_path)
        after = deps.reader.epoch()
        deps.freeze.check("after the export")
        if before != after:
            last_error = f"D1 epoch moved during the export ({before} -> {after})"
            continue
        try:
            stats = _verify_export(deps, sql_path, tables, out_dir)
        except _Mismatch as exc:
            last_error = str(exc)
            continue
        break
    else:
        raise L5Refused(f"no verified export after {EXPORT_ATTEMPTS} attempts: {last_error}")
    sql_sha = _sha256_file(sql_path)
    r2_key = f"exports/{db}/{sql_path.name}"
    deps.r2.put(r2_key, sql_path)
    r2_sha = hashlib.sha256(deps.r2.get(r2_key)).hexdigest()
    if r2_sha != sql_sha:
        raise L5Refused(f"R2 read-back of {r2_key} does not match the export ({r2_sha} != {sql_sha})")
    deps.freeze.check("before the receipt")
    receipt = {
        "version": 1, "db": db, "account_id": deps.account_id, "database_id": deps.database_id,
        "epoch": before, "tables": stats, "sql_path": str(sql_path), "sql_sha256": sql_sha,
        "r2_key": r2_key, "r2_sha256": r2_sha, "method": method,
        "verified_at": _now_iso(), "verified_at_epoch": deps.clock(),
    }
    rpath = out_dir / f"{stamp}.receipt.json"
    tmp = rpath.with_suffix(".tmp")
    tmp.write_text(json.dumps(receipt, indent=2, sort_keys=True))
    os.replace(tmp, rpath)
    return rpath


def load_receipt(path: str, db: str, *, now: Optional[float] = None, check_sql: bool = True) -> Dict[str, Any]:
    """A usable receipt (P1): verified, for this database, younger than 24 h,
    with the R2 read-back matched and (by default) its SQL file intact."""
    p = Path(path)
    try:
        r = json.loads(p.read_text())
    except (OSError, ValueError) as exc:
        raise L5Refused(f"receipt {path} is unreadable: {exc}")
    if not isinstance(r, dict) or r.get("version") != 1:
        raise L5Refused(f"receipt {path} is not a version-1 export receipt")
    if r.get("db") != db:
        raise L5Refused(f"receipt {path} is for {r.get('db')!r}, not {db!r}")
    if not r.get("verified_at") or r.get("r2_sha256") != r.get("sql_sha256"):
        raise L5Refused(f"receipt {path} is not verified (R2 read-back unmatched)")
    age = (now if now is not None else time.time()) - float(r.get("verified_at_epoch") or 0)
    if age > RECEIPT_MAX_AGE_S:
        raise L5Refused(f"receipt {path} is older than 24 h ({int(age)} s)")
    if check_sql:
        sql = Path(r["sql_path"])
        if not sql.exists() or _sha256_file(sql) != r["sql_sha256"]:
            raise L5Refused(f"receipt {path}: the export file {sql} is missing or changed")
    return r


def recheck(db: str, receipt_path: str, deps: Deps, out_dir: Path, *, hold: bool = False) -> Path:
    """P1 freeze-recheck (review 7524 P1-4, 7531 P1-3): under the freeze,
    compare D1's epoch, per-table counts AND full content hashes with the
    receipt; if anything changed, take a fresh export while still frozen.
    Returns the receipt to use (the same one, or the fresh one). With
    hold=True the freeze stays in place (the caller's step continues)."""
    receipt = load_receipt(receipt_path, db)
    deps.freeze.freeze()
    try:
        deps.freeze.check("before the recheck")
        tables = sorted(receipt["tables"])
        same = (deps.reader.epoch() == receipt["epoch"]
                and sorted(n for n, _ in deps.reader.tables()) == tables  # a table added or dropped
                and remote_stats(deps.reader, tables) == receipt["tables"])
        deps.freeze.check("after the recheck")
        if same:
            return Path(receipt_path)
        return _export_frozen(db, deps, Path(out_dir) / db)
    finally:
        if not hold:
            deps.freeze.thaw_if_placed()
