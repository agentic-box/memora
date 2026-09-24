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

import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
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
    """A secret from a file, never from the environment. The same rule as
    the L2a admin tokens (cross-finding 7633): lstat (a symlink is refused,
    not followed), a regular file, owned by this user, mode exactly 0600.
    Refused otherwise; the file is never chmod-ed."""
    p = Path(path)
    try:
        st = os.lstat(p)
    except OSError as exc:
        raise L5Refused(f"credential file {path}: {exc}")
    if stat.S_ISLNK(st.st_mode):
        raise L5Refused(f"credential file {path} is a symlink")
    if not stat.S_ISREG(st.st_mode):
        raise L5Refused(f"credential file {path} is not a regular file")
    if stat.S_IMODE(st.st_mode) != 0o600:
        raise L5Refused(f"credential file {path} must be mode 0600 (it is {oct(stat.S_IMODE(st.st_mode))})")
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
        order = _order_for(table, self.columns(table))
        offset = 0
        while True:
            page = self.rows(f'SELECT * FROM "{table}" ORDER BY {order} LIMIT ? OFFSET ?', (PAGE_ROWS, offset))
            yield from page
            if len(page) < PAGE_ROWS:
                return
            offset += PAGE_ROWS

    def has_sequence_table(self) -> bool:
        return bool(self.rows("SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                              (SEQUENCE_TABLE,)))

    def sequences(self) -> Dict[str, int]:
        """D1's sqlite_sequence ({} when D1 has none; a failed read raises)."""
        if not self.has_sequence_table():
            return {}
        rows = self.rows("SELECT name, seq FROM sqlite_sequence")
        return {r["name"]: int(r["seq"]) for r in rows}

    def hashed_tables(self) -> List[str]:
        """What a receipt covers: every user table, plus sqlite_sequence
        (the AUTOINCREMENT counters; review 7621 P1-1)."""
        names = sorted(n for n, _ in self.tables())
        return names + [SEQUENCE_TABLE] if self.has_sequence_table() else names


def _user_table(name: Optional[str]) -> bool:
    return bool(name) and not name.startswith(("sqlite_", "_cf_", "memories_fts"))


SEQUENCE_TABLE = "sqlite_sequence"


def _order_for(table: str, cols: List[Tuple[str, int]]) -> str:
    # sqlite_sequence has no key and its rowids differ after a load (the
    # dump rewrites its rows): order it by name on both sides.
    return '"name"' if table == SEQUENCE_TABLE else _order_by(cols)


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
            rows = (dict(r) for r in db.execute(f'SELECT * FROM "{t}" ORDER BY {_order_for(t, info)}'))
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
    sqlite_master, then one INSERT per row, then D1's AUTOINCREMENT
    counters. Returns the tables the receipt covers."""
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
        # Loading the rows already made SQLite create a sequence row (max id);
        # replace it, or a second row for the same name would be ignored and
        # the next id would be max(id)+1 instead of D1's counter+1 (7621 P1-1).
        for name, seq in sorted(reader.sequences().items()):
            fh.write(f"DELETE FROM sqlite_sequence WHERE name = {_sql_literal(name)};\n")
            fh.write(f"INSERT INTO sqlite_sequence (name, seq) VALUES ({_sql_literal(name)}, {int(seq)});\n")
        for ddl in reader.indexes_and_triggers():
            fh.write(ddl.rstrip(";") + ";\n")
        fh.write("COMMIT;\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, out)
    return reader.hashed_tables()


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
    open intent (`frozen-unsafe` is a refusal). No step lifts the freeze:
    only the explicit `thaw` command does (review 7621 P1-2), so the window
    between a recheck and the step that relies on it stays closed."""

    def __init__(self, base_url: str, admin_token: str, db: str, *, health_token: str,
                 timeout: float = 60.0):
        self.base = base_url.rstrip("/")
        self.admin_token = admin_token
        # /health/db/<db> shows the freeze fields only to an authorised caller
        # (MEMORA_HEALTH_TOKEN, or a loopback peer -- not one behind docker's
        # port mapping). It is its own token: memora-all refuses an admin
        # token equal to the health token, so it never defaults to it (7633).
        if not health_token:
            raise L5Refused("the freeze client needs the health token (--health-token-file)")
        if health_token == admin_token:
            raise L5Refused("the health token must differ from the admin token")
        self.health_token = health_token
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
        """Place the freeze unless it is already in place, then check it."""
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

    def require(self, where: str) -> None:
        """The freeze must ALREADY be in place (a step that relies on an
        earlier one's result never places its own)."""
        try:
            self.check(where)
        except L5Refused as exc:
            raise L5Refused(f"{exc} -- this step needs the freeze already in place: run "
                            f"`local_primary.py freeze {self.db}` (POST /admin/freeze/{self.db}) first")

    def thaw(self) -> None:
        """The explicit `thaw` command: the only way this tool lifts a freeze."""
        status, body = self._request("DELETE", f"/admin/freeze/{self.db}")
        if status != 200:
            raise L5Refused(f"the freeze on {self.db} was NOT lifted ({status}): {body}")
        self.placed = False


class ServiceStopped:
    """The barrier when memora-all is stopped (rollback steps): the
    container must report State.Running=false at every step boundary."""

    stopped_service = True  # steps that need the store's primary lock require this barrier

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

    def require(self, where: str) -> None:
        self.check(where)

    def check(self, where: str) -> None:
        state = self._running()
        if state != "false":
            raise L5Refused(f"{self.container} must be stopped {where} (State.Running={state})")

    def thaw(self) -> None:
        return None  # nothing to lift: memora-all is started by the operator


def check_service_route(db: str, store: Path) -> str:
    """X3 round 2 (review 7778 P1-1): the primary lock proves only that no
    process serves THIS local file. For a step that needs memora-all
    stopped, its registry must route <db> to exactly this file: memora-all
    serving <db> from d1:// (or another path) never takes this lock. The
    registry is memora-all's own MEMORA_DATABASES, which
    scripts/lp_container.sh passes into the container. Returns the route."""
    from .storage import DatabaseRegistryError, database_registry

    if not os.getenv("MEMORA_DATABASES", "").strip():
        raise L5Refused("--lock-barrier needs memora-all's MEMORA_DATABASES (scripts/lp_container.sh passes "
                        "it): without memora-all's routing the lock cannot prove the store is not served")
    try:
        routes = database_registry()
    except DatabaseRegistryError as exc:
        raise L5Refused(f"memora-all's MEMORA_DATABASES is unusable: {exc}")
    spec = routes.get(db)
    if spec is None:
        raise L5Refused(f"memora-all does not route {db!r} (known: {sorted(routes)}); refusing --lock-barrier")
    if "://" in spec and not spec.startswith("file://"):
        raise L5Refused(f"memora-all serves {db!r} from {spec.split('://', 1)[0]}://, not the local file "
                        f"{store}: its primary lock proves nothing about that service")
    path = spec[len("file://"):] if spec.startswith("file://") else spec
    if os.path.realpath(path) != os.path.realpath(str(store)):
        raise L5Refused(f"memora-all routes {db!r} to {path}, not {store}: the lock on {store} proves nothing")
    return spec


class LockBarrier:
    """The barrier inside a one-off maintenance container, where docker is
    not available (X3, scripts/lp_container.sh): the store's primary lock
    (flock on <db>.primary-lock, the canonical path of L2). memora-all holds
    it for as long as it serves the store, so holding it proves memora-all
    is not serving it. Taken at the first check (before any D1 call: the CLI
    checks at once) and held for the WHOLE run: steps inside that take and
    release the same lock do not release it (backends.pin_primary_lock).
    Every boundary re-verifies that the lock is still ours, on the lock file
    that is at the path now."""

    stopped_service = True  # the steps that need memora-all stopped accept it

    def __init__(self, store: Path, service_data_dir: Optional[Path] = None):
        self.store = Path(store)
        # Stopped-required runs (X3 round 3): the data volume's service lock,
        # which memora-all holds for as long as it runs -- the proof that it
        # does not run, held for the whole run. None for rollback finish.
        self.service_data_dir = Path(service_data_dir) if service_data_dir is not None else None
        self.service_placed = False
        self.placed = False

    def take_service_lock(self, where: str) -> None:
        from .backends import StoreLockedError, acquire_service_lock, service_lock_problem

        if self.service_data_dir is None:
            return
        if not self.service_placed:
            try:
                acquire_service_lock(self.service_data_dir)
            except StoreLockedError as exc:
                raise L5Refused(f"--lock-barrier {where}: maintenance lock: {exc} -- stop memora-all first")
            self.service_placed = True
        problem = service_lock_problem(self.service_data_dir)
        if problem:
            raise L5Refused(f"--lock-barrier lost {where}: the service lock: {problem}")

    def freeze(self) -> None:
        self.check("at the start")

    def require(self, where: str) -> None:
        self.check(where)

    def check(self, where: str) -> None:
        from .backends import StoreLockedError, pin_primary_lock, primary_lock_problem

        self.take_service_lock(where)
        if not self.placed:
            try:
                pin_primary_lock(self.store)
            except StoreLockedError as exc:
                raise L5Refused(f"--lock-barrier {where}: {exc} -- memora-all (or another run) is serving "
                                f"{self.store}; stop it first")
            self.placed = True
        problem = primary_lock_problem(self.store)
        if problem:
            raise L5Refused(f"--lock-barrier lost {where}: {problem}")

    def thaw(self) -> None:
        return None  # held for the whole run; release() when the process is done

    def release(self) -> None:
        from .backends import release_service_lock, unpin_primary_lock

        if self.placed:
            unpin_primary_lock(self.store)
            self.placed = False
        if self.service_placed:
            release_service_lock(self.service_data_dir)
            self.service_placed = False


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
    with a receipt. Returns the receipt path. Places the freeze if it is not
    in place, and leaves it in place (success or not): only `thaw` lifts it."""
    out_dir = Path(out_dir) / db
    out_dir.mkdir(parents=True, exist_ok=True)
    deps.freeze.freeze()
    return _export_frozen(db, deps, out_dir)


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
            tables = deps.reader.hashed_tables()
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
        "d1_uri": d1_uri(deps.account_id, deps.database_id),
        "epoch": before, "tables": stats, "sql_path": str(sql_path), "sql_sha256": sql_sha,
        "r2_key": r2_key, "r2_sha256": r2_sha, "method": method,
        "verified_at": _now_iso(), "verified_at_epoch": deps.clock(),
    }
    rpath = out_dir / f"{stamp}.receipt.json"
    tmp = rpath.with_suffix(".tmp")
    tmp.write_text(json.dumps(receipt, indent=2, sort_keys=True))
    os.replace(tmp, rpath)
    return rpath


def d1_uri(account_id: str, database_id: str) -> str:
    return f"d1://{account_id}/{database_id}"


def load_receipt(path: str, db: str, *, account_id: str, database_id: str, now: Optional[float] = None,
                 check_sql: bool = True, max_age_s: Optional[float] = RECEIPT_MAX_AGE_S) -> Dict[str, Any]:
    """A usable receipt (P1): verified, for this store name AND this D1
    database (account id, database id and URI; review 7621 P1-3), younger
    than 24 h, with the R2 read-back matched and (by default) its SQL file
    intact."""
    p = Path(path)
    try:
        r = json.loads(p.read_text())
    except (OSError, ValueError) as exc:
        raise L5Refused(f"receipt {path} is unreadable: {exc}")
    if not isinstance(r, dict) or r.get("version") != 1:
        raise L5Refused(f"receipt {path} is not a version-1 export receipt")
    if r.get("db") != db:
        raise L5Refused(f"receipt {path} is for {r.get('db')!r}, not {db!r}")
    want = {"account_id": account_id, "database_id": database_id, "d1_uri": d1_uri(account_id, database_id)}
    got = {k: r.get(k) for k in want}
    if got != want:
        raise L5Refused(f"receipt {path} is for another D1 database ({got}), not {want}")
    if not r.get("verified_at") or r.get("r2_sha256") != r.get("sql_sha256"):
        raise L5Refused(f"receipt {path} is not verified (R2 read-back unmatched)")
    age = (now if now is not None else time.time()) - float(r.get("verified_at_epoch") or 0)
    if max_age_s is not None and age > max_age_s:
        raise L5Refused(f"receipt {path} is older than 24 h ({int(age)} s)")
    if check_sql:
        sql = Path(r["sql_path"])
        if not sql.exists() or _sha256_file(sql) != r["sql_sha256"]:
            raise L5Refused(f"receipt {path}: the export file {sql} is missing or changed")
    return r


def recheck(db: str, receipt_path: str, deps: Deps, out_dir: Path) -> Path:
    """P1 freeze-recheck (review 7524 P1-4, 7531 P1-3): under a freeze that
    is ALREADY in place (refused otherwise), compare D1's epoch, table set,
    per-table counts AND full content hashes (sqlite_sequence included) with
    the receipt; if anything changed, take a fresh export, still frozen.
    Returns the receipt to use (the same one, or the fresh one). Never lifts
    the freeze: the step that relies on the recheck runs under the same one
    (review 7621 P1-2)."""
    receipt = load_receipt(receipt_path, db, account_id=deps.account_id, database_id=deps.database_id)
    deps.freeze.require("before the recheck")
    tables = sorted(receipt["tables"])
    same = (deps.reader.epoch() == receipt["epoch"]
            and sorted(deps.reader.hashed_tables()) == tables  # a table added or dropped
            and remote_stats(deps.reader, tables) == receipt["tables"])
    deps.freeze.check("after the recheck")
    if same:
        return Path(receipt_path)
    return _export_frozen(db, deps, Path(out_dir) / db)


# ------------------------------------------------------------------ seed (§4)

SEQ_TABLES = ("memories", "memories_actions")
FTS_REBUILD = (
    "DELETE FROM memories_fts",
    # The same values _fts_upsert writes: NULL metadata/tags become ''.
    "INSERT INTO memories_fts(rowid, content, metadata, tags) "
    "SELECT id, content, COALESCE(metadata, ''), COALESCE(tags, '') FROM memories",
)


def rebuild_fts(conn) -> int:
    for sql in FTS_REBUILD:
        conn.execute(sql)
    return int(conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0])


def _local_sequences(conn) -> Dict[str, Dict[str, int]]:
    """{table: {seq, max_id}} for the AUTOINCREMENT tables the plan names."""
    out = {}
    for t in SEQ_TABLES:
        row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (t,)).fetchone()
        max_id = conn.execute(f'SELECT COALESCE(MAX(id), 0) FROM "{t}"').fetchone()[0]
        out[t] = {"seq": int(row[0]) if row else 0, "max_id": int(max_id)}
    return out


def _read_d1_sequences(deps: Deps) -> Dict[str, int]:
    """D1's sqlite_sequence, read under the freeze (re-checked around it)."""
    deps.freeze.check("before reading D1's sequences")
    seqs = deps.reader.sequences()
    deps.freeze.check("after reading D1's sequences")
    return seqs


def _file_sequences(db_path: Path) -> Dict[str, int]:
    db = _scratch_connect(db_path)
    try:
        return {n: int(v) for n, v in db.execute("SELECT name, seq FROM sqlite_sequence")}
    finally:
        db.close()


def _sql_sequences(sql_path: Path) -> Dict[str, int]:
    """The counters an export carries: load it into memory and read them."""
    db = _scratch_connect(Path(":memory:"))
    try:
        db.executescript(sql_path.read_text(encoding="utf-8"))
        return {n: int(v) for n, v in db.execute("SELECT name, seq FROM sqlite_sequence")}
    finally:
        db.close()


def _with_sidecars(path: Path) -> Tuple[Path, ...]:
    return (path, Path(f"{path}-wal"), Path(f"{path}-shm"), Path(f"{path}-journal"))


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def seed(db: str, receipt_path: str, out: Path, deps: Deps, out_dir: Path, *,
         replica_uri: Optional[str] = None, rehearse: bool = False, lock_held: bool = False) -> Dict[str, Any]:
    """§4 seed: a new local store from a verified export. Built beside the
    target and linked into place only when it verifies; an existing target
    is never touched. Holds the target's primary lock while it works, so a
    memora-all already serving that path refuses it (and vice versa). Runs
    under the freeze the export left in place (required, never placed or
    lifted here; review 7621 P1-2), and seeds from what `recheck` returns
    under it -- the same receipt, or a fresh export if D1 changed (review
    7642 P1-1). The replica URI is the verified D1 identity's; a supplied
    one must equal it (7642 P1-2)."""
    from .backends import LocalSQLiteBackend, StoreLockedError, acquire_primary_lock, release_primary_lock
    from . import schema

    derived = d1_uri(deps.account_id, deps.database_id)
    if replica_uri is not None and replica_uri != derived:
        raise L5Refused(f"--replica-uri {replica_uri!r} is not the verified D1 database {derived!r}")
    replica_uri = derived
    if rehearse:
        out = Path(tempfile.mkdtemp(prefix=f"l5-rehearse-{db}-")) / Path(out).name
    out = Path(out)
    for p in _with_sidecars(out):
        if p.exists():
            raise L5Refused(f"{p} already exists: seed never overwrites a store (restore handles an existing one)")
    used = recheck(db, receipt_path, deps, out_dir)  # requires the freeze; a fresh export if D1 changed
    receipt = load_receipt(str(used), db, account_id=deps.account_id, database_id=deps.database_id)
    deps.freeze.check("before the seed")
    out.parent.mkdir(parents=True, exist_ok=True)  # §9 (k): before the primary lock
    if not lock_held:  # restore holds it across its move-aside and this seed
        try:
            acquire_primary_lock(out)
        except StoreLockedError as exc:
            raise L5Refused(f"cannot seed {out}: {exc}")
    tmp = out.with_name(out.name + ".seed-partial")
    try:
        for p in _with_sidecars(tmp):
            if p.exists():
                p.unlink()  # our own leftovers from an interrupted seed, sidecars included (7642 P2)
        load_sql(Path(receipt["sql_path"]), tmp)
        d1_seq = _read_d1_sequences(deps)
        conn = LocalSQLiteBackend(tmp).connect()
        try:
            schema.ensure_schema(conn)
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            fts_rows = rebuild_fts(conn)
            local = _local_sequences(conn)
            sequences = {}
            for t in SEQ_TABLES:
                hw = max(local[t]["seq"], local[t]["max_id"], int(d1_seq.get(t, 0)))
                if conn.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = ?", (hw, t)).rowcount == 0:
                    conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (t, hw))
                sequences[t] = {**local[t], "d1_seq": d1_seq.get(t), "set": hw}
            conn.commit()
            schema.install_sync(conn, replica_uri, receipt["epoch"])
            state = dict(conn.execute("SELECT last_acked_seq, d1_epoch_expected FROM sync_state").fetchone())
        finally:
            conn.close()
        # Every table as exported; sqlite_sequence is raised on purpose (the
        # high-water above), so it is checked as "no counter went down".
        data = sorted(t for t in receipt["tables"] if t != SEQUENCE_TABLE)
        stats = local_stats(tmp, data)
        want = {t: receipt["tables"][t] for t in data}
        if stats != want:
            bad = sorted(t for t in data if stats.get(t) != want[t])
            raise L5Refused(f"the seeded file does not match the receipt in {bad}; nothing was placed")
        exported = _sql_sequences(Path(receipt["sql_path"]))
        seeded = _file_sequences(tmp)
        lower = {n: (seeded.get(n), v) for n, v in exported.items() if (seeded.get(n) or 0) < v}
        if lower:
            raise L5Refused(f"the seeded sequences are below the export's: {lower}; nothing was placed")
        deps.freeze.check("before placing the seeded store")
        with open(tmp, "rb") as fh:
            os.fsync(fh.fileno())
        try:
            os.link(tmp, out)  # fails if the target appeared meanwhile
        except FileExistsError:
            raise L5Refused(f"{out} appeared during the seed; nothing was placed")
        _fsync_dir(out.parent)
    finally:
        for p in _with_sidecars(tmp):
            if p.exists():
                p.unlink()
        if not lock_held:
            release_primary_lock(out)
    return {"out": str(out), "receipt": str(used), "replica_uri": replica_uri, "epoch": receipt["epoch"],
            "tables": stats,
            "fts_rows": fts_rows, "sequences": sequences, "sync_state": state, "rehearse": rehearse}


# ------------------------------------------------------------------ sequence high-water on D1 (§4, H7)

SEQ_UPDATE_SQL = "UPDATE sqlite_sequence SET seq = ? WHERE name = ? AND seq < ?"
# `restamp` (write path 4, §5.3/§8): one memories_meta row, embedding_integrity.
RESTAMP_SQL = ("INSERT INTO memories_meta (key, value) VALUES (?, ?) "
               "ON CONFLICT(key) DO UPDATE SET value = excluded.value")
RESTAMP_KEY = "embedding_integrity"


class OperatorD1Writer:
    """The operator's D1 writer (§6.1): a D1Connection from memora/backends.py
    with the credential from a 0600 file, restricted to a fixed statement
    allow-list. It sends raw (no write gate, no journal): the store is
    frozen while it runs, and each call is one statement the operator ran."""

    ALLOWED = frozenset({SEQ_UPDATE_SQL})
    # R2 restore (§4): per-key statements built by the replicator's
    # _build_statements and accepted by its P2 _check_statement, nothing else.
    RESTORE_SHAPES = frozenset({"upsert", "insert", "delete"})

    def __init__(self, conn, *, allow_restore: bool = False, allow_restamp: bool = False):
        self.conn = conn  # backends.D1Connection (or a test double with _send)
        self.allow_restore = allow_restore
        self.allow_restamp = allow_restamp
        self.sent: List[Tuple[str, tuple]] = []

    @classmethod
    def from_credential_file(cls, account_id: str, database_id: str, path: str, *,
                             allow_restore: bool = False, allow_restamp: bool = False) -> "OperatorD1Writer":
        from .backends import D1Connection

        return cls(D1Connection(account_id, database_id, load_credential_file(path)), allow_restore=allow_restore,
                   allow_restamp=allow_restamp)

    def _allowed(self, sql: str, params: tuple = ()) -> bool:
        if sql in self.ALLOWED:
            return True
        if self.allow_restamp and sql == RESTAMP_SQL:
            return len(params) == 2 and params[0] == RESTAMP_KEY
        if not self.allow_restore:
            return False
        from .replicator import ReplicatorStatementError, _check_statement

        try:
            return _check_statement(sql) in self.RESTORE_SHAPES
        except ReplicatorStatementError:
            return False

    def send(self, sql: str, params: tuple) -> Dict[str, Any]:
        if not self._allowed(sql, tuple(params)):
            raise L5Refused(f"statement not on the operator allow-list: {sql[:80]}")
        self.sent.append((sql, tuple(params)))
        return self.conn._send(sql, tuple(params))


def sequence_highwater(db: str, receipt_path: str, local_path: Path, deps: Deps, out_dir: Path, *,
                       dry_run: bool = False) -> Dict[str, Any]:
    """H7: before a rollback or a restore, raise D1's sqlite_sequence to the
    local high-water so D1 never re-issues an id the local store used. Needs
    a receipt and a passing recheck, under the freeze already in place
    (checked at every boundary, left in place for the next step). One
    UPDATE per table, only where D1 is behind; a rejected or unapplied
    UPDATE HALTS the procedure -- there is no fallback."""
    from .backends import LocalSQLiteBackend

    used = recheck(db, receipt_path, deps, out_dir)  # requires the freeze; leaves it in place
    conn = LocalSQLiteBackend(Path(local_path)).connect_read_only()
    try:
        local = _local_sequences(conn)
    finally:
        conn.close()
    deps.freeze.check("before reading D1's sequences")
    d1 = deps.reader.sequences()
    plan = []
    for t in SEQ_TABLES:
        hw = max(local[t]["seq"], local[t]["max_id"])
        if hw == 0:
            continue
        if t not in d1:
            raise L5Halt(f"D1 has no sqlite_sequence row for {t} (local high-water {hw}); the plan "
                         "allows only the UPDATE -- stop and report")
        if d1[t] < hw:
            plan.append((SEQ_UPDATE_SQL, (hw, t, hw)))
    report = {"receipt": str(used), "local": local, "d1_before": d1, "statements": plan, "dry_run": dry_run}
    if dry_run or not plan:
        return report
    if deps.writer_factory is None:
        raise L5Refused("no operator writer: pass --credential-file")
    writer = deps.writer_factory()
    for sql, params in plan:
        deps.freeze.check("before the sequence UPDATE")
        try:
            res = writer.send(sql, params)
        except L5Refused:
            raise
        except Exception as exc:
            raise L5Halt(f"D1 rejected {sql!r} {params}: {type(exc).__name__}: {exc}")
        if isinstance(res, dict) and res.get("success") is False:
            raise L5Halt(f"D1 rejected {sql!r} {params}: {res}")
    deps.freeze.check("after the sequence UPDATE")
    after = deps.reader.sequences()
    short = {t: (after.get(t), params[0]) for _sql, params in plan for t in [params[1]]
             if (after.get(t) or 0) < params[0]}
    if short:
        raise L5Halt(f"D1 accepted the sequence UPDATE but did not apply it: {short}")
    report["d1_after"] = after
    return report


# ------------------------------------------------------------------ snapshot (§4)

SNAPSHOT_KEEP = 14
_SNAPSHOT_KEY = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{6}Z\.db\.gz$")


def backup_store(store_path: Path, dest: Path) -> Path:
    """One consistent copy of a (possibly live) store: `sqlite3 .backup`
    through its read-only connection, checked with integrity_check."""
    from .backends import LocalSQLiteBackend

    src = LocalSQLiteBackend(Path(store_path)).connect_read_only()
    try:
        dst = _scratch_connect(Path(dest))
        try:
            src.backup(dst)
            ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            dst.close()
    finally:
        src.close()
    if ok != "ok":
        raise L5Refused(f"the copy of {store_path} fails integrity_check: {ok}")
    return Path(dest)


def _store_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in (path, Path(f"{path}-wal")) if p.exists())


def snapshot(db: str, store_path: Path, r2, work_dir: Path, *, keep: int = SNAPSHOT_KEEP,
             now: Optional[float] = None, disk_free: Callable[[Path], int] = lambda p: shutil.disk_usage(p).free
             ) -> Dict[str, Any]:
    """`sqlite3 .backup` of a live store through its read-only connection,
    gzip, R2 `<db>/<ts>.db.gz` with a read-back hash, then retention: keep
    the newest `keep` snapshots (only keys this command writes are ever
    deleted). Refused when free space is below 2x the store's size."""
    from .backends import LocalSQLiteBackend

    store_path, work_dir = Path(store_path), Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    size = _store_bytes(store_path)
    free = disk_free(work_dir)
    if free < 2 * size:
        raise L5Refused(f"free space {free} B in {work_dir} is below 2x the store size ({size} B)")
    ts = time.strftime("%Y-%m-%dT%H%M%SZ", time.gmtime(now if now is not None else time.time()))
    key = f"{db}/{ts}.db.gz"
    tmpdir = Path(tempfile.mkdtemp(prefix=f"snapshot-{db}-", dir=str(work_dir)))
    try:
        copy, gz = tmpdir / "copy.db", tmpdir / f"{ts}.db.gz"
        backup_store(store_path, copy)
        with open(copy, "rb") as fin, gzip.open(gz, "wb") as fout:
            shutil.copyfileobj(fin, fout)
        sha = _sha256_file(gz)
        r2.put(key, gz)
        back = hashlib.sha256(r2.get(key)).hexdigest()
        if back != sha:
            raise L5Refused(f"R2 read-back of {key} does not match the snapshot ({back} != {sha})")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    ours = sorted(k for k in r2.list(f"{db}/") if _SNAPSHOT_KEY.match(k[len(db) + 1:]))
    removed = [k for k in ours[:-keep] if k != key] if keep > 0 else []
    for k in removed:
        r2.delete(k)
    return {"key": key, "sha256": sha, "store_bytes": size, "kept": len(ours) - len(removed), "removed": removed}


# ------------------------------------------------------------------ volume alert (§4)

def volume_check(stores: List[Path], *, min_free_pct: float = 10.0,
                 usage: Callable[[Path], Any] = shutil.disk_usage) -> Dict[str, Any]:
    """For each store's volume: free space must be at least 2x the store (the
    snapshot needs it) and at least `min_free_pct` of the volume. Returns
    {ok, alerts, volumes}; the CLI exits 4 when an alert is raised."""
    alerts, volumes = [], []
    for store in stores:
        store = Path(store)
        u = usage(store.parent)
        size = _store_bytes(store) if store.exists() else 0
        pct = 100.0 * u.free / u.total if u.total else 0.0
        volumes.append({"store": str(store), "store_bytes": size, "free": u.free, "total": u.total,
                        "free_pct": round(pct, 2)})
        if u.free < 2 * size:
            alerts.append(f"{store}: free {u.free} B is below 2x the store ({size} B)")
        if pct < min_free_pct:
            alerts.append(f"{store}: only {pct:.1f}% of the volume is free (minimum {min_free_pct}%)")
    return {"ok": not alerts, "alerts": alerts, "volumes": volumes}


# ------------------------------------------------------------------ restore (§4, H5)

def _move_aside(out: Path, clock: Callable[[], float]) -> Optional[str]:
    """Move an existing store and its sidecars into <out>.pre-restore-<ts>/
    (never deleted). The caller holds the primary lock."""
    parts = [p for p in (out, Path(f"{out}-wal"), Path(f"{out}-shm"), Path(f"{out}-journal")) if p.exists()]
    if not parts:
        return None
    base = f"{out.name}.pre-restore-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(clock()))}"
    dest, n = out.with_name(base), 1
    while dest.exists():
        n += 1
        dest = out.with_name(f"{base}-{n}")
    dest.mkdir()
    for p in parts:
        os.replace(p, dest / p.name)
    _fsync_dir(out.parent)
    return str(dest)


def _restore_into(db: str, receipt_path: str, out: Path, deps: Deps, out_dir: Path, *,
                  replica_uri: Optional[str], rehearse: bool, lock_held: bool = False) -> Dict[str, Any]:
    """Move the old store aside (not on a rehearsal) and seed -- which
    rechecks the receipt under the freeze -- holding the target's primary
    lock across both (taken here, or already held by the caller). If the
    seed fails, the old store is put back."""
    from .backends import StoreLockedError, acquire_primary_lock, release_primary_lock

    if rehearse:
        return {**seed(db, receipt_path, out, deps, out_dir, replica_uri=replica_uri, rehearse=True),
                "moved_aside": None}
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not lock_held:
        _take_store_lock(out)
    try:
        deps.freeze.check("before moving the old store aside")
        moved = _move_aside(out, deps.clock)
        try:
            rep = seed(db, receipt_path, out, deps, out_dir, replica_uri=replica_uri, lock_held=True)
        except BaseException:
            if moved and not out.exists():  # put the old store back: nothing was placed
                for p in Path(moved).iterdir():
                    os.replace(p, out.parent / p.name)
                Path(moved).rmdir()
                moved = None
            raise
        return {**rep, "moved_aside": moved}
    finally:
        if not lock_held:
            release_primary_lock(out)


def _take_store_lock(out: Path) -> None:
    from .backends import StoreLockedError, acquire_primary_lock

    try:
        acquire_primary_lock(out)
    except StoreLockedError as exc:
        raise L5Refused(f"cannot restore {out}: {exc} (stop memora-all first)")


def restore(db: str, receipt_path: str, out: Path, deps: Deps, out_dir: Path, *,
            replica_uri: Optional[str] = None, rehearse: bool = False) -> Dict[str, Any]:
    """The default restore (H5): a FULL re-seed from a verified export. The
    old store is moved aside (kept, never deleted) and a new one seeded; the
    seed rechecks the receipt under the freeze already in place (a fresh
    export if D1 changed). Unacked local writes are lost (accepted, 7507)."""
    return _restore_into(db, receipt_path, Path(out), deps, out_dir, replica_uri=replica_uri, rehearse=rehearse)


# ------------------------------------------------------------------ restore --from-r2: conflicts (§4)

def _compare_tables() -> Dict[str, Tuple[str, ...]]:
    from .schema import SYNC_TABLES

    return dict(SYNC_TABLES)


def _meta_excluded() -> Tuple[str, ...]:
    from .schema import SYNC_META_EXCLUDED

    return tuple(SYNC_META_EXCLUDED)


def _group_of(table: str, row: Dict[str, Any]) -> str:
    """Conflict groups (§4): per memory id (the memories row and its
    embeddings, crossrefs, tombstones, tombstone_components and actions),
    one per memories_meta key; an action with no memory is its own group."""
    if table == "memories":
        return f"memory:{row['id']}"
    if table == "memories_meta":
        return f"meta:{row['key']}"
    if table == "memories_actions" and row.get("memory_id") is None:
        return f"action:{row['id']}"
    return f"memory:{row['memory_id']}"


def _file_rows(db_path: Path, table: str, columns: List[str]) -> Dict[Tuple, Dict[str, Any]]:
    pk = _compare_tables()[table]
    db = _scratch_connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        have = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
        if not have:
            return {}
        cols = [c for c in columns if c in have]
        out = {}
        for r in db.execute(f'SELECT {", ".join(chr(34) + c + chr(34) for c in cols)} FROM "{table}"'):
            row = {c: r[c] for c in cols}
            if table == "memories_meta" and row["key"] in _meta_excluded():
                continue
            out[tuple(row[c] for c in pk)] = row
        return out
    finally:
        db.close()


def _norm_row(row: Optional[Dict[str, Any]]) -> Any:
    return None if row is None else {c: _norm(v) for c, v in sorted(row.items())}


def _group_digest(rows: List[Dict[str, Any]]) -> str:
    """sha256 of a group's rows as {table, pk, row}, sorted: the preimage
    the apply step re-reads and compares."""
    canon = sorted(json.dumps(r, sort_keys=True, default=str, separators=(",", ":")) for r in rows)
    return hashlib.sha256("\n".join(canon).encode("utf-8")).hexdigest()


def _group_rows(side: Dict[str, Dict[Tuple, Dict[str, Any]]], table_pk: Dict[str, Tuple[str, ...]],
                keys: List[Tuple[str, Tuple]]) -> List[Dict[str, Any]]:
    out = []
    for table, pk in keys:
        row = side[table].get(pk)
        if row is not None:
            out.append({"table": table, "pk": list(pk), "row": _norm_row(row)})
    return out


def build_conflicts(snapshot_db: Path, d1_db: Path, columns: Dict[str, List[str]]) -> List[Dict[str, Any]]:
    """Every difference between the snapshot and D1 (a verified export of
    it) over the §5.2 tables, keys on one side only included, grouped; each
    group carries both versions of every row it holds and D1's preimage."""
    tables = _compare_tables()
    snap = {t: _file_rows(snapshot_db, t, columns[t]) for t in tables}
    d1 = {t: _file_rows(d1_db, t, columns[t]) for t in tables}
    members: Dict[str, List[Tuple[str, Tuple]]] = {}
    for t in tables:
        for side in (snap, d1):
            for pk, row in side[t].items():
                members.setdefault(_group_of(t, row), [])
                if (t, pk) not in members[_group_of(t, row)]:
                    members[_group_of(t, row)].append((t, pk))
    groups = []
    def order(gid: str):
        kind, _, ident = gid.partition(":")
        return (kind, int(ident) if ident.lstrip("-").isdigit() else 0, ident)

    for gid in sorted(members, key=order):
        keys = sorted(members[gid], key=lambda k: (list(tables).index(k[0]), [str(v) for v in k[1]]))
        if all(_norm_row(snap[t].get(pk)) == _norm_row(d1[t].get(pk)) for t, pk in keys):
            continue
        d1_rows = _group_rows(d1, tables, keys)
        groups.append({"group": gid, "keys": [{"table": t, "pk": list(pk)} for t, pk in keys],
                       "snapshot_rows": _group_rows(snap, tables, keys), "d1_rows": d1_rows,
                       "d1_preimage_sha256": _group_digest(d1_rows)})
    _annotate_inbound_refs(groups, snap, d1)
    return groups


def _crossref_targets(related: Any) -> List[int]:
    """The memory ids a memories_crossrefs.related value points at (a JSON
    list of ids or of {"id": ...} objects)."""
    try:
        items = json.loads(related) if isinstance(related, str) else (related or [])
    except ValueError:
        return []
    out = []
    for it in items if isinstance(items, list) else []:
        mid = it.get("id") if isinstance(it, dict) else it
        if isinstance(mid, int) and not isinstance(mid, bool):
            out.append(mid)
    return out


def _annotate_inbound_refs(groups: List[Dict[str, Any]], snap: Dict[str, Dict], d1: Dict[str, Dict]) -> None:
    """§9 (w): per memory group, the memories whose crossrefs point at it on
    either side, so the operator sees the dependencies between groups
    (conflicting choices can leave a stale reference)."""
    refs: Dict[int, set] = {}
    for side_name, side in (("snapshot", snap), ("d1", d1)):
        for row in side.get("memories_crossrefs", {}).values():
            for target in _crossref_targets(row.get("related")):
                if target != row.get("memory_id"):
                    refs.setdefault(target, set()).add((int(row["memory_id"]), side_name))
    gids = {g["group"] for g in groups}
    for g in groups:
        kind, _, ident = g["group"].partition(":")
        if kind != "memory":
            continue
        g["inbound_refs"] = [{"from": f"memory:{src}", "side": side, "from_is_conflict": f"memory:{src}" in gids}
                             for src, side in sorted(refs.get(int(ident), ()))]


def dangling_references(conflicts: Dict[str, Any], sel: Dict[str, str]) -> List[Dict[str, Any]]:
    """With these choices, the crossrefs that would point at a memory the
    chosen side does not have (a warning for the operator, §9 (w))."""
    groups = {g["group"]: g for g in conflicts["groups"]}

    def chosen_rows(g):
        return g["snapshot_rows"] if sel[g["group"]] == "snapshot" else g["d1_rows"]

    out = []
    for gid, g in groups.items():
        if not gid.startswith("memory:") or any(r["table"] == "memories" for r in chosen_rows(g)):
            continue
        target = int(gid.split(":", 1)[1])
        for ref in g.get("inbound_refs", []):
            src = groups.get(ref["from"])
            if src is None:  # not a conflict: the same crossref on both sides, it stays
                out.append({"from": ref["from"], "to": gid})
                continue
            rows = [r for r in chosen_rows(src) if r["table"] == "memories_crossrefs"]
            if any(target in _crossref_targets(r["row"].get("related")) for r in rows):
                out.append({"from": ref["from"], "to": gid})
    return sorted({(d["from"], d["to"]): d for d in out}.values(), key=lambda d: (d["to"], d["from"]))


def _fetch_snapshot(r2, key: str, work: Path) -> Tuple[Path, str]:
    raw = r2.get(key)
    sha = hashlib.sha256(raw).hexdigest()
    path = work / "snapshot.db"
    try:
        path.write_bytes(gzip.decompress(raw) if key.endswith(".gz") else raw)
    except OSError as exc:
        raise L5Refused(f"snapshot {key} is not a readable gzip: {exc}")
    db = _scratch_connect(path)
    try:
        ok = db.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        ok = str(exc)
    finally:
        db.close()
    if ok != "ok":
        raise L5Refused(f"snapshot {key} fails integrity_check: {ok}")
    return path, sha


def restore_prepare(db: str, snapshot_key: str, receipt_path: str, deps: Deps, out_dir: Path) -> Dict[str, Any]:
    """`restore --from-r2 KEY` step 1-3: under the freeze, recheck the
    receipt, compare the snapshot with D1 (the verified export of it) and
    write conflicts-<ts>.json for the operator. Writes nothing else."""
    used = recheck(db, receipt_path, deps, out_dir)
    receipt = load_receipt(str(used), db, account_id=deps.account_id, database_id=deps.database_id)
    work = Path(tempfile.mkdtemp(prefix=f"restore-{db}-", dir=str(Path(out_dir))))
    try:
        # §4 step 1 loads the snapshot; its FTS is not needed here: the
        # restored local store is re-seeded from D1 (FTS rebuilt there).
        snap, snap_sha = _fetch_snapshot(deps.r2, snapshot_key, work)
        d1 = work / "d1.db"
        load_sql(Path(receipt["sql_path"]), d1)
        columns = {t: [c for c, _ in deps.reader.columns(t)] for t in _compare_tables()}
        groups = build_conflicts(snap, d1, columns)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    deps.freeze.check("before writing the conflicts file")
    doc = {"version": 1, "db": db, "account_id": deps.account_id, "database_id": deps.database_id,
           "receipt": str(used), "receipt_sha256": _sha256_file(Path(used)), "snapshot_key": snapshot_key,
           "snapshot_sha256": snap_sha, "columns": columns, "created_at": _now_iso(), "groups": groups}
    base = Path(out_dir) / db
    base.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(deps.clock()))
    path, n = base / f"conflicts-{stamp}.json", 1
    while path.exists():
        n += 1
        path = base / f"conflicts-{stamp}-{n}.json"
    path.write_text(json.dumps(doc, indent=2, sort_keys=True))
    return {"conflicts": str(path), "conflicts_sha256": _sha256_file(path), "groups": len(groups),
            "receipt": str(used)}


# ------------------------------------------------------------------ restore --from-r2: apply (§4)

RESTORE_CHOICES = ("d1", "snapshot")
_FK_ORDER = ("memories", "memories_embeddings", "memories_crossrefs", "tombstones", "tombstone_components",
             "memories_actions", "memories_meta")


def load_approval(conflicts_path: str, approve_path: str, db: str, deps: Deps) -> Tuple[Dict[str, Any], Dict[str, str], str]:
    """The conflicts file and the operator's --approve file, which must quote
    the conflicts file's sha256 and choose d1|snapshot for EVERY group."""
    try:
        conflicts = json.loads(Path(conflicts_path).read_text())
        approve = json.loads(Path(approve_path).read_text())
    except (OSError, ValueError) as exc:
        raise L5Refused(f"conflicts/approve file unreadable: {exc}")
    if conflicts.get("version") != 1 or conflicts.get("db") != db:
        raise L5Refused(f"{conflicts_path} is not a version-1 conflicts file for {db!r}")
    if (conflicts.get("account_id"), conflicts.get("database_id")) != (deps.account_id, deps.database_id):
        raise L5Refused(f"{conflicts_path} is for another D1 database")
    sha = _sha256_file(Path(conflicts_path))
    if not isinstance(approve, dict) or approve.get("conflicts_sha256") != sha:
        raise L5Refused(f"{approve_path} does not quote this conflicts file (sha256 {sha})")
    sel = approve.get("selections")
    groups = [g["group"] for g in conflicts["groups"]]
    if not isinstance(sel, dict):
        raise L5Refused(f"{approve_path} has no selections")
    missing = sorted(set(groups) - set(sel))
    extra = sorted(set(sel) - set(groups))
    bad = sorted(g for g, v in sel.items() if v not in RESTORE_CHOICES)
    if missing or extra or bad:
        raise L5Refused(f"{approve_path} must choose d1|snapshot for every group and nothing else: "
                        f"missing={missing[:20]} unknown={extra[:20]} invalid={bad[:20]}")
    return conflicts, sel, sha


def _group_statements(group: Dict[str, Any], columns: Dict[str, List[str]]) -> List[Tuple[str, tuple]]:
    """The per-key statements that make D1's rows of one group equal the
    snapshot's: UPSERTs parents first, DELETEs children first; rows already
    equal send nothing. Each one passes the replicator's P2 check."""
    from .replicator import _build_statements, _check_statement

    snap = {(r["table"], tuple(r["pk"])): r["row"] for r in group["snapshot_rows"]}
    d1 = {(r["table"], tuple(r["pk"])): r["row"] for r in group["d1_rows"]}
    ups, dels = [], []
    for k in group["keys"]:
        key = (k["table"], tuple(k["pk"]))
        if snap.get(key) == d1.get(key):
            continue
        row = snap.get(key)
        if row is not None:
            row = {c: (bytes.fromhex(v["$hex"]) if isinstance(v, dict) and "$hex" in v else v) for c, v in row.items()}
        stmts = _build_statements(k["table"], list(k["pk"]), row, columns[k["table"]])
        (ups if row is not None else dels).append((k["table"], stmts))
    ups.sort(key=lambda x: _FK_ORDER.index(x[0]))
    dels.sort(key=lambda x: -_FK_ORDER.index(x[0]))
    out = [st for _t, stmts in ups + dels for st in stmts]
    for sql, _params in out:
        _check_statement(sql)
    return out


_BY_MEMORY = (("memories", "id"), ("memories_embeddings", "memory_id"), ("memories_crossrefs", "memory_id"),
              ("tombstones", "memory_id"), ("tombstone_components", "memory_id"),
              ("memories_actions", "memory_id"))


def _enumerate_group(fetch: Callable[[str, tuple], List[Dict[str, Any]]], gid: str,
                     columns: Dict[str, List[str]]) -> List[Dict[str, Any]]:
    """EVERY current row of a group -- the memories row and all its child
    rows by memory id, the meta key, or the memory-less action -- not just
    the keys recorded at prepare, so a row added since is seen (review 7674
    P1-3). Normalised like the conflicts file's rows."""
    tables = _compare_tables()
    kind, _, ident = gid.partition(":")
    if kind == "memory":
        queries = [(t, f'"{col}" = ?', (int(ident),)) for t, col in _BY_MEMORY]
    elif kind == "meta":
        queries = [("memories_meta", '"key" = ?', (ident,))]
    elif kind == "action":
        queries = [("memories_actions", '"id" = ? AND "memory_id" IS NULL', (int(ident),))]
    else:
        raise L5Refused(f"unknown conflict group {gid!r}")
    out = []
    for t, where, params in queries:
        cols = columns[t]
        for r in fetch(f'SELECT {", ".join(chr(34) + c + chr(34) for c in cols)} FROM "{t}" WHERE {where}', params):
            row = {c: r.get(c) for c in cols}
            out.append({"table": t, "pk": [row[c] for c in tables[t]], "row": _norm_row(row)})
    return out


def _d1_group_rows(reader: D1Reader, group: Dict[str, Any], columns: Dict[str, List[str]]) -> List[Dict[str, Any]]:
    return _enumerate_group(lambda sql, params: reader.rows(sql, params), group["group"], columns)


def _delete_guard(plan: Dict[str, List[Tuple[str, tuple]]], receipt: Dict[str, Any], attempt: str,
                  allow_deletes: Optional[str]) -> Optional[str]:
    """P3 for restore replay: per-table DELETEs against D1's row counts."""
    from .replicator import DELETE_GUARD_FRACTION, DELETE_GUARD_ROWS

    n: Dict[str, int] = {}
    for stmts in plan.values():
        for sql, _p in stmts:
            if sql.startswith("DELETE FROM "):
                t = sql.split()[2]
                # the embeddings DELETE+INSERT pair is an update, not a delete
                if not any(s.startswith(f"INSERT INTO {t} ") for s, _ in stmts):
                    n[t] = n.get(t, 0) + 1
    for t, k in sorted(n.items()):
        total = int((receipt["tables"].get(t) or {}).get("count", 0))
        if (k > DELETE_GUARD_ROWS or k > DELETE_GUARD_FRACTION * total) and allow_deletes != attempt:
            return f"delete_guard: {t} {k}/{total} attempt={attempt}"
    return None


def restore_apply(db: str, conflicts_path: str, approve_path: str, receipt_path: str, deps: Deps,
                  out: Path, out_dir: Path, *, replica_uri: Optional[str] = None, dry_run: bool = False,
                  allow_deletes: Optional[str] = None) -> Dict[str, Any]:
    """`restore --from-r2` steps 4-7:
    - `d1` groups never write D1;
    - `snapshot` groups send per-key UPSERT/DELETE (P2-checked, P3-guarded)
      after the group's CURRENT rows on D1 (all of them, enumerated) hash to
      the recorded preimage (a changed group is aborted and reported, the
      others continue); afterwards the group's current rows must equal the
      snapshot's exactly (an extra row HALTS);
    - when every group is resolved, D1 holds the chosen state everywhere, so
      the local store is rebuilt from a fresh verified export of it (the old
      store moved aside, kept), after checking that export's groups.
    It needs memora-all STOPPED (--service-stopped) and holds the target's
    primary lock from before the first D1 send through the rebuild (review
    7674 P1-1). --dry-run writes nothing: the statements per group, the
    delete-guard result and the rebuild plan (7674 P1-2). A failed send
    HALTS: the group's outcome on D1 is unknown."""
    from .backends import release_primary_lock
    from .replicator import ReplicatorStatementError

    conflicts, sel, csha = load_approval(conflicts_path, approve_path, db, deps)
    used = recheck(db, receipt_path, deps, out_dir)
    receipt = load_receipt(str(used), db, account_id=deps.account_id, database_id=deps.database_id)
    columns = conflicts["columns"]
    chosen = [g for g in conflicts["groups"] if sel[g["group"]] == "snapshot"]
    plan = {}
    for g in chosen:
        try:
            plan[g["group"]] = _group_statements(g, columns)
        except ReplicatorStatementError as exc:
            raise L5Refused(f"group {g['group']}: no allowed statement can restore it: {exc}")
    attempt = hashlib.sha256((csha + _sha256_file(Path(approve_path))).encode()).hexdigest()[:16]
    halt = _delete_guard(plan, receipt, attempt, allow_deletes)
    report: Dict[str, Any] = {"conflicts_sha256": csha, "attempt": attempt, "receipt": str(used),
                              "d1_groups": sorted(g for g, v in sel.items() if v == "d1"),
                              "statements": {g: plan[g] for g in sorted(plan)}, "dry_run": dry_run,
                              "delete_guard": halt or "within bounds",
                              "dangling_references": dangling_references(conflicts, sel)}
    if dry_run:
        report["local"] = (f"would be rebuilt from a fresh verified export of D1 into {out} "
                           f"(the old store moved aside)")
        return report
    if halt:
        raise L5Refused(f"{halt}: pass --allow-deletes {attempt} to allow this one attempt")
    if not getattr(deps.freeze, "stopped_service", False):
        raise L5Refused("restore --from-r2 writes D1 and then replaces the local store: it needs memora-all "
                        "stopped (--service-stopped); nothing was sent")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    _take_store_lock(out)  # before the first D1 send, through the rebuild
    try:
        applied, aborted = [], []
        writer = None
        if any(plan.values()):
            if deps.writer_factory is None:
                raise L5Refused("no operator writer: pass --credential-file")
            writer = deps.writer_factory()
        for g in chosen:
            gid = g["group"]
            deps.freeze.check(f"before group {gid}")
            if _group_digest(_d1_group_rows(deps.reader, g, columns)) != g["d1_preimage_sha256"]:
                aborted.append(gid)  # D1 changed since the conflicts file (a new row too): not written
                continue
            for sql, params in plan[gid]:
                try:
                    res = writer.send(sql, params)
                except L5Refused:
                    raise
                except Exception as exc:
                    raise L5Halt(f"group {gid}: D1 send failed ({type(exc).__name__}: {exc}); its outcome is "
                                 f"unknown. Applied so far: {applied}. Re-run the prepare step.")
                if isinstance(res, dict) and res.get("success") is False:
                    raise L5Halt(f"group {gid}: D1 rejected {sql[:80]!r}: {res}. Applied so far: {applied}")
            if _group_digest(_d1_group_rows(deps.reader, g, columns)) != _group_digest(g["snapshot_rows"]):
                raise L5Halt(f"group {gid}: D1's rows for it are not exactly the snapshot's. "
                             f"Applied so far: {applied}")
            applied.append(gid)
        report.update({"applied": applied, "aborted": aborted})
        if aborted:
            report["local"] = "not rebuilt: some groups were aborted; re-run the prepare step"
            raise L5Refused(json.dumps(report, default=str))
        deps.freeze.check("before the fresh export")
        fresh = _export_frozen(db, deps, Path(out_dir) / db)
        _check_rebuild_source(fresh, conflicts, sel, columns, deps)
        report["local"] = _restore_into(db, str(fresh), out, deps, out_dir, replica_uri=replica_uri,
                                        rehearse=False, lock_held=True)
        return report
    finally:
        release_primary_lock(out)


def _check_rebuild_source(fresh_receipt: Path, conflicts: Dict[str, Any], sel: Dict[str, str],
                          columns: Dict[str, List[str]], deps: Deps) -> None:
    """Before the local rebuild: in the fresh export, every snapshot group
    is exactly the snapshot's rows and every d1 group exactly its recorded
    D1 rows (7674 P1-3, the rebuild's expectations)."""
    r = load_receipt(str(fresh_receipt), conflicts["db"], account_id=deps.account_id,
                     database_id=deps.database_id)
    work = Path(tempfile.mkdtemp(prefix="rebuild-check-", dir=str(Path(fresh_receipt).parent)))
    try:
        scratch = work / "fresh.db"
        load_sql(Path(r["sql_path"]), scratch)
        db = _scratch_connect(scratch)
        db.row_factory = sqlite3.Row
        try:
            def fetch(sql, params):
                return [dict(x) for x in db.execute(sql, params)]

            for g in conflicts["groups"]:
                want = g["snapshot_rows"] if sel[g["group"]] == "snapshot" else g["d1_rows"]
                if _group_digest(_enumerate_group(fetch, g["group"], columns)) != _group_digest(want):
                    raise L5Halt(f"group {g['group']}: the fresh export does not hold the chosen "
                                 f"{sel[g['group']]} rows; the local store was not rebuilt")
        finally:
            db.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ------------------------------------------------------------------ reconcile (§1) and resume (§2.6, P3)

class AdminClient(FreezeClient):
    """GET /admin/intents/<db> and POST /admin/reconcile/<db>/<id>."""

    def _post_json(self, path: str, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        req = urllib.request.Request(self.base + path, method="POST", data=json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {self.admin_token}",
                                              "Content-Type": "application/json"})
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

    def post_compare(self, fields: Dict[str, Any]) -> Dict[str, Any]:
        status, out = self._post_json(f"/admin/compare/{self.db}", fields)
        if status != 200:
            raise L5Refused(f"memora-all did not record the compare ({status}): {out}")
        return out

    def intents(self) -> Dict[str, Any]:
        status, body = self._request("GET", f"/admin/intents/{self.db}")
        if status != 200:
            raise L5Refused(f"GET /admin/intents/{self.db} failed ({status}): {body}")
        return body

    def accept(self, intent_id: int, body: Dict[str, Any]) -> Dict[str, Any]:
        status, out = self._post_json(f"/admin/reconcile/{self.db}/{intent_id}", body)
        if status != 200:
            raise L5Refused(f"reconcile of intent {intent_id} refused ({status}): {out}")
        return out


RECONCILE_DECISIONS = ("applied", "not-applied")


def reconcile_accept(db: str, client: AdminClient, *, intent_id: int, receipt_path: str, operator: str,
                     decision: str, evidence_sha256: str, account_id: str, database_id: str) -> Dict[str, Any]:
    """`reconcile --accept` (§1, L2's accept body): the receipt must be a
    usable one for this D1 database, and the evidence digest must be the one
    GET /admin/intents shows now for this intent (the server re-checks)."""
    load_receipt(receipt_path, db, account_id=account_id, database_id=database_id)
    if decision not in RECONCILE_DECISIONS:
        raise L5Refused(f"decision must be one of {RECONCILE_DECISIONS}")
    if not operator.strip():
        raise L5Refused("--operator is required")
    shown = {int(i["id"]): i for i in client.intents().get("open_intents", [])}
    if intent_id not in shown:
        raise L5Refused(f"intent {intent_id} is not open on {db}")
    if shown[intent_id].get("evidence_sha256") != evidence_sha256:
        raise L5Refused(f"intent {intent_id}: the evidence changed since you read it "
                        f"(now {shown[intent_id].get('evidence_sha256')}); show it again and decide on that")
    body = {"receipt": str(receipt_path), "operator": operator.strip(), "intent_id": intent_id,
            "decision": decision, "evidence_sha256": evidence_sha256}
    return {"request": body, "response": client.accept(intent_id, body)}


def resume_store(db_path: Path, reader: D1Reader, *, accept_d1_epoch: Optional[int] = None,
                 allow_deletes: Optional[str] = None, barrier: Any = None) -> Dict[str, Any]:
    """`resume` (§2.6, P3): clear a replicator halt on a local store. Holds
    the store's primary lock (memora-all must be stopped). An accepted D1
    epoch must be D1's epoch now."""
    from .backends import LocalSQLiteBackend, StoreLockedError, acquire_primary_lock, release_primary_lock
    from .replicator import resume

    db_path = Path(db_path)
    if not db_path.is_file():
        raise L5Refused(f"no store at {db_path}")
    if barrier is not None:
        barrier.check("before reading D1's epoch")
    if accept_d1_epoch is not None:
        now = reader.epoch()
        if int(accept_d1_epoch) != now:
            raise L5Refused(f"--accept-d1-epoch {accept_d1_epoch} is not D1's epoch now ({now})")
    try:
        acquire_primary_lock(db_path)
    except StoreLockedError as exc:
        raise L5Refused(f"cannot resume {db_path}: {exc} (stop memora-all first)")
    try:
        if barrier is not None:
            barrier.check("before clearing the halt")
        conn = LocalSQLiteBackend(db_path).connect()
        try:
            cleared = resume(conn, accept_d1_epoch=accept_d1_epoch, allow_deletes=allow_deletes)
        except ValueError as exc:
            raise L5Refused(str(exc))
        finally:
            conn.close()
    finally:
        release_primary_lock(db_path)
    return {"store": str(db_path), "cleared": cleared, "lock_barrier": barrier is not None}
