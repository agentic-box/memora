"""Storage backend abstraction for pluggable cloud and local storage.

This module provides a backend system that allows memora to transparently
use different storage backends (local SQLite, cloud-synced SQLite, etc.) while
keeping the same API surface.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from .intent_journal import IntentJournalError, journal_for
from .sql_classify import READ, TXN, classify_statement, derive_effect, is_select_only
from .write_gate import StoreReadOnlyError, _WriteGate, gate_for_key

try:
    import filelock
except ImportError:
    filelock = None

try:
    import boto3
    from botocore.config import Config as BotoConfig
    from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError
except ImportError:
    boto3 = None
    ClientError = None
    NoCredentialsError = None
    EndpointConnectionError = None
    BotoConfig = None

logger = logging.getLogger(__name__)

# Retry configuration
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0  # seconds
RETRY_MAX_DELAY = 30.0  # seconds
SYNC_CHECK_TTL = 30.0  # seconds - skip HEAD request if we checked recently

# Transient error codes that should trigger retry
TRANSIENT_ERROR_CODES = {
    "500", "502", "503", "504",  # Server errors
    "RequestTimeout", "RequestTimeoutException",
    "ThrottlingException", "Throttling",
    "SlowDown", "ServiceUnavailable",
    "InternalError",
}


def _is_transient_error(error: Exception) -> bool:
    """Check if an error is transient and should be retried."""
    if EndpointConnectionError and isinstance(error, EndpointConnectionError):
        return True
    if ClientError and isinstance(error, ClientError):
        error_code = error.response.get("Error", {}).get("Code", "")
        return error_code in TRANSIENT_ERROR_CODES
    # Also retry on connection errors
    if isinstance(error, (ConnectionError, TimeoutError, OSError)):
        return True
    return False


def _get_user_friendly_error(error: Exception, operation: str) -> str:
    """Convert S3/R2 errors to user-friendly messages with actionable advice."""
    if NoCredentialsError and isinstance(error, NoCredentialsError):
        return (
            f"AWS/R2 credentials not found while {operation}.\n"
            "Please configure credentials using one of:\n"
            "  - Environment variables: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY\n"
            "  - AWS credentials file: ~/.aws/credentials\n"
            "  - For Cloudflare R2: Set AWS_ENDPOINT_URL to your R2 endpoint"
        )

    if ClientError and isinstance(error, ClientError):
        error_code = error.response.get("Error", {}).get("Code", "")
        error_msg = error.response.get("Error", {}).get("Message", str(error))

        if error_code == "AccessDenied" or error_code == "403":
            return (
                f"Access denied while {operation}.\n"
                f"Error: {error_msg}\n"
                "Please check:\n"
                "  - Your API token has the correct permissions (read/write)\n"
                "  - The bucket name is correct\n"
                "  - For R2: Token needs 'Object Read & Write' permission"
            )
        elif error_code == "InvalidAccessKeyId":
            return (
                f"Invalid access key while {operation}.\n"
                "Your AWS_ACCESS_KEY_ID appears to be incorrect.\n"
                "Please verify your credentials are correct."
            )
        elif error_code == "SignatureDoesNotMatch":
            return (
                f"Signature mismatch while {operation}.\n"
                "Your AWS_SECRET_ACCESS_KEY appears to be incorrect.\n"
                "Please verify your credentials are correct."
            )
        elif error_code == "NoSuchBucket":
            return (
                f"Bucket not found while {operation}.\n"
                f"Error: {error_msg}\n"
                "Please check that the bucket exists and the name is correct."
            )
        elif error_code in TRANSIENT_ERROR_CODES:
            return (
                f"Temporary service error while {operation} (will retry).\n"
                f"Error: {error_code} - {error_msg}"
            )
        else:
            return f"S3/R2 error while {operation}: {error_code} - {error_msg}"

    if EndpointConnectionError and isinstance(error, EndpointConnectionError):
        return (
            f"Cannot connect to cloud storage while {operation}.\n"
            "Please check:\n"
            "  - Your internet connection\n"
            "  - The AWS_ENDPOINT_URL is correct (for R2/MinIO)\n"
            f"Error: {error}"
        )

    return f"Error while {operation}: {error}"


def _retry_with_backoff(func, operation: str, max_retries: int = MAX_RETRIES):
    """Execute a function with exponential backoff retry for transient errors.

    Args:
        func: Callable to execute
        operation: Description of operation for error messages
        max_retries: Maximum number of retry attempts

    Returns:
        Result of func()

    Raises:
        Original exception if non-transient or retries exhausted
    """
    last_error = None

    for attempt in range(max_retries + 1):
        try:
            return func()
        except Exception as e:
            last_error = e

            if not _is_transient_error(e):
                # Non-transient error, don't retry
                raise

            if attempt == max_retries:
                # Last attempt failed
                logger.error(
                    f"All {max_retries + 1} attempts failed for {operation}: {e}"
                )
                raise

            # Calculate delay with exponential backoff + jitter
            delay = min(
                RETRY_BASE_DELAY * (2 ** attempt) + (time.time() % 1),
                RETRY_MAX_DELAY
            )
            logger.warning(
                f"Transient error during {operation} (attempt {attempt + 1}/{max_retries + 1}), "
                f"retrying in {delay:.1f}s: {e}"
            )
            time.sleep(delay)

    # Should not reach here, but just in case
    raise last_error


class ConflictError(Exception):
    """Raised when a cloud sync conflict is detected (concurrent modification)."""
    pass


class StoreMissingError(RuntimeError):
    """A read-only open found no database (nothing was created)."""


class StoreLockedError(RuntimeError):
    """A read-only open was refused rather than create a file: WAL sidecar
    files that cannot be used for the read lock, an incomplete sidecar pair,
    or a hot journal needing recovery. str() is a short, stable detail."""


class StorageBackend(ABC):
    """Abstract base class for storage backends.

    Backends are responsible for:
    1. Providing a SQLite connection via connect()
    2. Syncing state before use (download from cloud, etc.)
    3. Syncing state after writes (upload to cloud, etc.)
    """

    @abstractmethod
    def connect(self, *, check_same_thread: bool = True) -> sqlite3.Connection:
        """Return a SQLite connection ready for use.

        For cloud backends, this may involve syncing from remote first.

        Args:
            check_same_thread: SQLite connection parameter

        Returns:
            sqlite3.Connection ready for queries
        """
        pass

    def connect_read_only(self, *, check_same_thread: bool = True) -> sqlite3.Connection:
        """A connection for READ-ONLY callers (the plain JSON API, readiness
        probes). Default: connect(). Local SQLite overrides it so a read never
        creates a file or directory."""
        return self.connect(check_same_thread=check_same_thread)

    @abstractmethod
    def sync_before_use(self) -> None:
        """Sync state before using the database (e.g., download from cloud)."""
        pass

    @abstractmethod
    def sync_after_write(self) -> None:
        """Sync state after modifying the database (e.g., upload to cloud)."""
        pass

    @abstractmethod
    def get_info(self) -> dict:
        """Return diagnostic information about the backend."""
        pass


class _StoreRWLock:
    """A process-wide reader-writer lock for ONE local store (see
    LocalSQLiteBackend.connect / connect_read_only). Writer-preferring, so a
    stream of reads cannot starve a writer's open or close. A thread that
    already holds the shared side may re-enter it; the exclusive side is
    re-entrant for its holder; opening a writer on the same store while
    holding a read raises instead of deadlocking.

    Closes by the GC never block (a finalizer may run on a thread holding
    unrelated locks): they take the exclusive side only if it is free now,
    else hand the connection to defer_close, and the lock closes it -- under
    the exclusive side -- as soon as the reads drain or the writer section
    ends."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._readers: Dict[int, int] = {}  # thread id -> shared holds
        self._writer = False
        self._writer_owner: Optional[int] = None
        self._waiting_writers = 0
        self.open_writers = 0  # in-process writer connections open (changed under exclusive)
        self._deferred: list = []  # writer connections the GC could not close yet
        self._draining = False

    def acquire_shared(self) -> None:
        me = threading.get_ident()
        with self._cond:
            if me not in self._readers:
                while self._writer or self._waiting_writers:
                    self._cond.wait()
            self._readers[me] = self._readers.get(me, 0) + 1

    def release_shared(self, holder: Optional[int] = None) -> None:
        me = threading.get_ident() if holder is None else holder
        with self._cond:
            count = self._readers.get(me, 0)
            if count <= 1:
                self._readers.pop(me, None)
            else:
                self._readers[me] = count - 1
            if not self._readers:
                self._cond.notify_all()
        self._drain_deferred()

    def _try_exclusive(self, ignore_waiting: bool = False) -> bool:
        me = threading.get_ident()
        with self._cond:
            if self._writer or self._readers or (self._waiting_writers and not ignore_waiting):
                return False
            self._writer, self._writer_owner = True, me
            return True

    def _release_exclusive(self) -> None:
        with self._cond:
            self._writer, self._writer_owner = False, None
            self._cond.notify_all()

    @contextlib.contextmanager
    def exclusive(self):
        me = threading.get_ident()
        with self._cond:
            nested = self._writer and self._writer_owner == me
        if nested:
            yield  # re-entered by its holder
            return
        with self._cond:
            if me in self._readers:
                raise RuntimeError("a writer connection was opened or closed on a local store while this "
                                   "thread holds a read-only connection to it (would deadlock)")
            self._waiting_writers += 1
            try:
                while self._writer or self._readers:
                    self._cond.wait()
            finally:
                self._waiting_writers -= 1
            self._writer, self._writer_owner = True, me
        try:
            self._drain_locked()  # pending GC closes run inside every writer section
            yield
        finally:
            try:
                self._drain_locked()
            finally:
                self._release_exclusive()

    def _drain_locked(self) -> None:
        """Close deferred connections; the caller holds the exclusive side."""
        while True:
            with self._cond:
                if not self._deferred:
                    return
                conn = self._deferred.pop(0)
            try:
                conn._memora_close_locked()
            except Exception:
                logger.exception("local store writer connection: deferred close failed")

    def close_from_gc(self, conn: Any) -> None:
        """Close a writer connection the GC is finalizing, never blocking."""
        with self._cond:
            nested = self._writer and self._writer_owner == threading.get_ident()
        if nested:
            conn._memora_close_locked()
            return
        if self._try_exclusive():
            try:
                conn._memora_close_locked()
            finally:
                self._release_exclusive()
            self._drain_deferred()
            return
        with self._cond:
            self._deferred.append(conn)  # keeps it alive until closed

    def _drain_deferred(self) -> None:
        with self._cond:
            if self._draining or not self._deferred:
                return
            self._draining = True
        try:
            while True:
                with self._cond:
                    if not self._deferred:
                        return
                # Deferred closes go before waiting writers (they are short and
                # must not starve); a busy exclusive side drains them itself.
                if not self._try_exclusive(ignore_waiting=True):
                    return
                try:
                    with self._cond:
                        conn = self._deferred.pop(0)
                    try:
                        conn._memora_close_locked()
                    except Exception:
                        logger.exception("local store writer connection: deferred close failed")
                finally:
                    self._release_exclusive()
        finally:
            with self._cond:
                self._draining = False


_STORE_LOCKS: Dict[str, _StoreRWLock] = {}
_STORE_LOCKS_GUARD = threading.Lock()


def _store_lock(path: Path) -> _StoreRWLock:
    """The lock for a local store, by its real path (so two names for one
    file share it). Never creates anything."""
    key = os.path.realpath(str(path))
    with _STORE_LOCKS_GUARD:
        lock = _STORE_LOCKS.get(key)
        if lock is None:
            lock = _STORE_LOCKS[key] = _StoreRWLock()
        return lock


# SQLite's own close, through one name (tests make it fail to prove the
# wrappers' bookkeeping only follows a SUCCESSFUL close).
def _sqlite_close(conn: sqlite3.Connection) -> None:
    sqlite3.Connection.close(conn)


# Thread checks that mirror stock sqlite3 with check_same_thread=True.
# The native connection is opened with check_same_thread=False (so a close by
# the GC, on any thread, can really close it under the store lock); the
# wrappers below re-impose the caller's check on EVERY public method of
# Connection and Cursor present on the running Python -- default-deny: a
# method a new Python adds is checked too -- except the ones stock sqlite3
# itself allows from any thread (verified by tests/test_local_store_lock.py
# against native sqlite3).
_CONNECTION_UNCHECKED = frozenset({"__enter__", "interrupt"})
_CURSOR_UNCHECKED = frozenset({"__iter__", "setinputsizes", "setoutputsize"})
_CHECKED_DUNDERS = frozenset({"__enter__", "__exit__", "__iter__", "__next__"})


def _public_callables(cls: type) -> list:
    return sorted(
        name for name in dir(cls)
        if (not name.startswith("_") or name in _CHECKED_DUNDERS) and callable(getattr(cls, name, None))
        and not isinstance(getattr(cls, name), type)
    )


def _checked(base: type, name: str):
    native = getattr(base, name)

    def method(self, *args, **kwargs):
        self._memora_check()
        return native(self, *args, **kwargs)

    method.__name__ = method.__qualname__ = name
    method.__doc__ = getattr(native, "__doc__", None)
    method._memora_checked = True
    return method


class _ThreadCheckedCursor(sqlite3.Cursor):
    """Every public Cursor method thread-checked like stock sqlite3."""

    def _memora_check(self) -> None:
        check = getattr(self.connection, "_memora_check", None)
        if check is not None:
            check()


for _name in _public_callables(sqlite3.Cursor):
    if _name not in _CURSOR_UNCHECKED:
        setattr(_ThreadCheckedCursor, _name, _checked(sqlite3.Cursor, _name))


class _ThreadCheckedConnection(sqlite3.Connection):
    """Every public Connection method (and the autocommit property, which
    stock sqlite3 checks) thread-checked like stock sqlite3; cursor() --
    also used by execute() -- returns thread-checked cursors."""

    _memora_owner: Optional[int] = None
    _memora_check_thread = True

    def _memora_check(self) -> None:
        if self._memora_check_thread and self._memora_owner is not None \
                and threading.get_ident() != self._memora_owner:
            raise sqlite3.ProgrammingError(
                "SQLite objects created in a thread can only be used in that same thread. "
                f"The object was created in thread id {self._memora_owner} and this is thread id "
                f"{threading.get_ident()}.")

    def cursor(self, factory=None):
        self._memora_check()
        if factory is None:
            factory = _ThreadCheckedCursor
        elif not issubclass(factory, _ThreadCheckedCursor):
            factory = type(f"_ThreadChecked{factory.__name__}", (_ThreadCheckedCursor, factory), {})
        return sqlite3.Connection.cursor(self, factory)

    # The C-level convenience methods create their cursor WITHOUT calling the
    # Python cursor() above, so they go through it here explicitly (same
    # arguments, return values and exceptions as native).
    def execute(self, sql, parameters=(), /):
        self._memora_check()
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, parameters, /):
        self._memora_check()
        return self.cursor().executemany(sql, parameters)

    def executescript(self, sql_script, /):
        self._memora_check()
        return self.cursor().executescript(sql_script)

    def blobopen(self, *args, **kwargs):
        self._memora_check()
        return _ThreadCheckedBlob(sqlite3.Connection.blobopen(self, *args, **kwargs), self)


# Connection methods returning a cursor or cursor-like object are overridden
# explicitly above; iterdump's generator builds its cursor through cursor().
_CONNECTION_EXPLICIT = frozenset({"cursor", "execute", "executemany", "executescript", "blobopen"})
for _name in _public_callables(sqlite3.Connection):
    if _name not in _CONNECTION_UNCHECKED and _name not in _CONNECTION_EXPLICIT:
        setattr(_ThreadCheckedConnection, _name, _checked(sqlite3.Connection, _name))


class _ThreadCheckedBlob:
    """sqlite3.Blob cannot be subclassed: a proxy that thread-checks every
    public Blob operation (stock sqlite3 checks them all) and delegates."""

    __slots__ = ("_blob", "_conn")

    def __init__(self, blob, conn) -> None:
        self._blob, self._conn = blob, conn

    def __enter__(self):
        self._conn._memora_check()
        self._blob.__enter__()
        return self

    def __exit__(self, *exc):
        self._conn._memora_check()
        return self._blob.__exit__(*exc)

    def __len__(self):
        self._conn._memora_check()
        return len(self._blob)

    def __getitem__(self, key):
        self._conn._memora_check()
        return self._blob[key]

    def __setitem__(self, key, value):
        self._conn._memora_check()
        self._blob[key] = value


_BLOB_DUNDERS = frozenset({"__enter__", "__exit__", "__len__", "__getitem__", "__setitem__"})
if hasattr(sqlite3, "Blob"):
    for _name in [n for n in dir(sqlite3.Blob) if not n.startswith("_") and callable(getattr(sqlite3.Blob, n))]:
        def _blob_method(self, *args, _name=_name, **kwargs):
            self._conn._memora_check()
            return getattr(self._blob, _name)(*args, **kwargs)

        _blob_method.__name__ = _name
        setattr(_ThreadCheckedBlob, _name, _blob_method)
if hasattr(sqlite3.Connection, "autocommit"):  # Python 3.12+
    _native_autocommit = sqlite3.Connection.autocommit

    def _get_autocommit(self):
        self._memora_check()
        return _native_autocommit.__get__(self)

    def _set_autocommit(self, value):
        self._memora_check()
        _native_autocommit.__set__(self, value)

    _ThreadCheckedConnection.autocommit = property(_get_autocommit, _set_autocommit)


class StoreWriteAborted(RuntimeError):
    """A helper called rollback() inside store_write: the transaction is gone."""


WRITER_BUSY_TIMEOUT_MS = 5000


def writer_setup_pragmas(live_primary: bool) -> tuple:
    """The ONLY statements connect() runs on a writer before arming its gate
    (plan §9 item b). Both are idempotent and change no row."""
    base = (f"PRAGMA busy_timeout = {WRITER_BUSY_TIMEOUT_MS}",)
    return base + (("PRAGMA journal_mode = WAL",) if live_primary else ())


_STORE_WRITE_LOCKS: Dict[str, threading.Lock] = {}
_STORE_WRITE_LOCKS_GUARD = threading.Lock()
_STORE_WRITE_TLS = threading.local()


def _store_write_lock(db_path) -> threading.Lock:
    key = os.path.realpath(str(db_path))
    with _STORE_WRITE_LOCKS_GUARD:
        lk = _STORE_WRITE_LOCKS.get(key)
        if lk is None:
            lk = _STORE_WRITE_LOCKS[key] = threading.Lock()
        return lk


def in_store_write() -> bool:
    """True while this thread is inside store_write (the no-network rule:
    nothing may call an LLM, an embedding service or R2 then)."""
    return getattr(_STORE_WRITE_TLS, "depth", 0) > 0


def after_store_write(callback) -> None:
    """Run callback() after the current store_write COMMITS, outside its lock
    (deferred image processing, plan §3). Dropped if it rolls back."""
    stack = getattr(_STORE_WRITE_TLS, "after", None)
    if not stack:
        raise RuntimeError("after_store_write outside store_write")
    stack[-1].append(callback)


@contextlib.contextmanager
def store_write(conn):
    """One BEGIN IMMEDIATE transaction on a local writer (plan §3), under a
    per-store process-wide lock (writer connections are thread-affine, so a
    lock replaces "one shared connection"). Inner commits are deferred to
    the end, an inner rollback aborts the whole transaction, and callbacks
    registered with after_store_write run after the commit, outside the
    lock. Re-entrant on the same connection (a nested call joins)."""
    if not getattr(conn, "supports_transactions", False):
        raise TypeError("store_write needs a transactional local writer connection")
    if conn._memora_store_write_depth:
        conn._memora_store_write_depth += 1
        try:
            yield conn
        finally:
            conn._memora_store_write_depth -= 1
        return
    lock = _store_write_lock(conn._memora_db_path)
    lock.acquire()
    callbacks: list = []
    entered = committed = False
    try:
        if conn.in_transaction:
            conn._memora_commit_now()  # earlier, independent writes of this caller
        conn.execute("BEGIN IMMEDIATE")
        conn._memora_store_write_depth = 1
        _STORE_WRITE_TLS.depth = getattr(_STORE_WRITE_TLS, "depth", 0) + 1
        stack = getattr(_STORE_WRITE_TLS, "after", None)
        if stack is None:
            stack = _STORE_WRITE_TLS.after = []
        stack.append(callbacks)
        entered = True
        try:
            yield conn
        except BaseException:
            conn._memora_store_write_depth = 0
            try:
                conn._memora_rollback_now()
            except sqlite3.Error:
                logger.exception("store_write: rollback failed")
            raise
        conn._memora_store_write_depth = 0
        try:
            conn._memora_commit_now()
        except BaseException:
            try:
                conn._memora_rollback_now()
            except sqlite3.Error:
                logger.exception("store_write: rollback after a failed commit failed")
            raise
        committed = True
    finally:
        conn._memora_store_write_depth = 0
        if entered:
            _STORE_WRITE_TLS.after.pop()
            _STORE_WRITE_TLS.depth -= 1
        lock.release()
    if committed:
        for cb in callbacks:
            try:
                cb()
            except Exception:
                logger.exception("after-commit callback failed")


_COMMIT_EVENTS: Dict[str, threading.Event] = {}
_COMMIT_EVENTS_GUARD = threading.Lock()


def commit_event(db_path) -> threading.Event:
    """The per-store event a commit sets (plan §2.2): the replicator waits
    on it instead of polling. Keyed by the store's real path."""
    key = os.path.realpath(str(db_path))
    with _COMMIT_EVENTS_GUARD:
        ev = _COMMIT_EVENTS.get(key)
        if ev is None:
            ev = _COMMIT_EVENTS[key] = threading.Event()
        return ev


def _notify_commit(conn) -> None:
    lock_owner = getattr(conn, "_memora_db_path", None)
    if lock_owner is None:
        return
    key = os.path.realpath(str(lock_owner))
    with _COMMIT_EVENTS_GUARD:
        ev = _COMMIT_EVENTS.get(key)
    if ev is not None:
        ev.set()


class _GatedCursor(_ThreadCheckedCursor):
    """Cursor of a local writer connection: every statement is classified,
    and a mutating one enters the store's write gate (the freeze barrier,
    plan §1) before it runs. The token is the CONNECTION's: it is taken by the
    first mutation of a transaction and released when no transaction is open
    any more (commit, rollback, close, or an autocommit statement finishing).
    Connection.execute/executemany/executescript go through cursor(), so the
    raw cursor is not a bypass (plan round-8 P2c)."""

    def execute(self, sql, parameters=(), /):
        conn = self.connection
        conn._memora_gate_before(sql)
        try:
            return super().execute(sql, parameters)
        finally:
            conn._memora_gate_after()

    def executemany(self, sql, parameters, /):
        conn = self.connection
        conn._memora_gate_before(sql)
        try:
            return super().executemany(sql, parameters)
        finally:
            conn._memora_gate_after()

    def executescript(self, sql_script, /):
        conn = self.connection
        conn._memora_gate_before(sql_script)
        try:
            return super().executescript(sql_script)
        finally:
            conn._memora_gate_after()


class _LockedWriterConnection(_ThreadCheckedConnection):
    """A local writer connection whose close (explicit or by the GC) takes
    the exclusive side of its store lock (see LocalSQLiteBackend.connect).

    The bookkeeping (closed flag, open_writers) changes only AFTER the
    underlying close succeeded: a close that raises (e.g. from the wrong
    thread) leaves the connection open, counted, and still closable only
    under the exclusive side; the exception propagates.

    Writes pass the store's write gate (see _GatedCursor)."""

    supports_transactions = True  # plan §1 M10: absorb phase 3 may use one transaction

    _memora_lock: Optional[_StoreRWLock] = None
    _memora_closed = False
    _memora_gate: Optional[_WriteGate] = None  # None until connect() arms it
    _memora_token = None

    def cursor(self, factory=None):
        if factory is None:
            factory = _GatedCursor
        elif not issubclass(factory, _GatedCursor):
            factory = type(f"_Gated{factory.__name__}", (_GatedCursor, factory), {})
        return _ThreadCheckedConnection.cursor(self, factory)

    def _memora_gate_before(self, sql) -> None:
        gate = self._memora_gate
        if gate is None or self._memora_token is not None:
            return
        c = classify_statement(sql if isinstance(sql, str) else str(sql))
        if c.kind == READ or (c.kind == TXN and c.txn_end):
            return
        self._memora_token = gate.enter(c.main or c.kind)

    def _memora_gate_after(self) -> None:
        tok = self._memora_token
        if tok is None:
            return
        try:
            idle = not self.in_transaction
        except sqlite3.ProgrammingError:  # closed
            idle = True
        if idle:
            self._memora_token = None
            self._memora_gate.leave(tok)

    # Inside store_write (depth > 0) every inner commit is DEFERRED to the
    # store_write's own commit, and an inner rollback aborts the whole
    # transaction: phase 3 of absorb, an import, a replicator ack are one
    # transaction however their helpers are written (plan §3).
    _memora_store_write_depth = 0

    def commit(self):
        if self._memora_store_write_depth:
            self._memora_check()
            return None  # deferred to store_write
        return self._memora_commit_now()

    def _memora_commit_now(self):
        try:
            result = _ThreadCheckedConnection.commit(self)
        finally:
            self._memora_gate_after()
        _notify_commit(self)
        return result

    def rollback(self):
        if self._memora_store_write_depth:
            self._memora_check()
            raise StoreWriteAborted("rollback() inside store_write: the whole transaction is rolled back")
        return self._memora_rollback_now()

    def _memora_rollback_now(self):
        try:
            return _ThreadCheckedConnection.rollback(self)
        finally:
            self._memora_gate_after()

    def __exit__(self, *exc):
        if self._memora_store_write_depth:
            # `with conn:` inside store_write: neither commit nor roll back
            # here; an exception propagates to store_write, which rolls back.
            self._memora_check()
            return False
        try:
            result = _ThreadCheckedConnection.__exit__(self, *exc)
        finally:
            self._memora_gate_after()
        if not exc or exc[0] is None:
            _notify_commit(self)
        return result

    def _memora_release_token(self) -> None:
        tok, self._memora_token = self._memora_token, None
        if tok is not None:
            self._memora_gate.leave(tok)

    def close(self) -> None:
        self._memora_check()
        lock = self._memora_lock
        if lock is None or self._memora_closed:
            try:
                return super().close()
            finally:
                if self._memora_closed or lock is None:
                    self._memora_release_token()
        with lock.exclusive():
            self._memora_close_locked()

    def _memora_close_locked(self) -> None:
        """Close under the exclusive side (held by the caller)."""
        if self._memora_closed:
            return
        _sqlite_close(self)  # raises -> nothing below runs
        self._memora_closed = True
        self._memora_lock.open_writers -= 1
        self._memora_release_token()  # a closed connection's transaction is gone

    def __del__(self) -> None:
        lock = self._memora_lock
        if lock is None or self._memora_closed:
            return
        try:
            lock.close_from_gc(self)  # never blocks; may defer (keeping self alive)
        except Exception:
            logger.exception("local store writer connection: close by the GC failed")


class _ReplicatorConnection(_LockedWriterConnection):
    """The replicator's writer (plan §1 H6): exempt from the write gate by
    construction, so it can drain the outbox while ingress is frozen. Only
    LocalSQLiteBackend.connect_replicator() creates it."""

    def _memora_gate_before(self, sql) -> None:
        return


class _LockedReaderConnection(_ThreadCheckedConnection):
    """A read-only connection that holds the shared side of its store lock
    until the underlying close SUCCEEDS (explicitly or by the GC): a close
    that raises (e.g. from the wrong thread) keeps the read protected."""

    supports_transactions = False

    _memora_release = None

    def close(self) -> None:
        self._memora_check()
        _sqlite_close(self)  # raises -> the shared hold is kept
        release, self._memora_release = self._memora_release, None
        if release is not None:
            release()

    def __del__(self) -> None:
        try:
            _sqlite_close(self)
        except Exception:
            logger.exception("local store read-only connection: close by the GC failed")
            return
        release, self._memora_release = self._memora_release, None
        if release is not None:
            release()


def _sqlite_header_is_wal(path: Path) -> bool:
    """WAL mode per the database header (file format write/read version bytes
    18-19 == 2), read without opening SQLite -- so nothing is created."""
    try:
        with open(path, "rb") as fh:
            header = fh.read(20)
    except OSError:
        return False
    return len(header) >= 20 and header[:16] == b"SQLite format 3\x00" and (header[18] == 2 or header[19] == 2)


class LocalSQLiteBackend(StorageBackend):
    """Local file-based SQLite backend (original behavior)."""

    def __init__(self, db_path: Path):
        """Initialize local SQLite backend.

        Args:
            db_path: Path to the SQLite database file
        """
        self.db_path = Path(db_path)
        # The registry name (set by storage.backend_for); None when the
        # backend was built directly.
        self.store_name: Optional[str] = None
        # Set when this process may not serve the store at all (a live
        # primary whose lock another process holds): every open raises.
        self.refused_reason: Optional[str] = None

    def write_gate(self) -> _WriteGate:
        """This store's write gate (plan §1), shared by every backend object
        for the same real file."""
        return gate_for_key("sqlite:" + os.path.realpath(str(self.db_path)), self.store_name)

    @property
    def live_primary(self) -> bool:
        """True when this store is named in MEMORA_REPLICAS (plan §1 M10):
        a local primary replicated to D1. Dark until configured."""
        if not self.store_name:
            return False
        raw = os.getenv("MEMORA_REPLICAS", "").strip()
        if not raw:
            return False
        try:
            replicas = json.loads(raw)
        except ValueError:
            return False
        return isinstance(replicas, dict) and self.store_name in replicas

    def _ensure_parent_dir(self) -> None:
        """Ensure parent directory exists."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self, *, check_same_thread: bool = True) -> sqlite3.Connection:
        """Return a WRITER connection to the local SQLite database (created if
        missing: this is the writing path).

        Every in-process writer connection to a local store is opened HERE,
        and its open and close take the EXCLUSIVE side of the store's
        process-wide lock (_store_lock): no writer opens or closes while a
        read-only connection is open (connect_read_only holds the shared side
        until it is closed). The open also touches the database once, so a
        WAL database's -wal/-shm exist for as long as this connection is open.
        """
        return self._open_writer(check_same_thread, _LockedWriterConnection, gated=True)

    def connect_replicator(self, *, check_same_thread: bool = True) -> sqlite3.Connection:
        """The replicator's writer: exempt from the write gate and the freeze
        (plan §1 H6). Nothing but memora/replicator.py may call this; a test
        asserts it."""
        return self._open_writer(check_same_thread, _ReplicatorConnection, gated=False)

    def fence(self) -> None:
        """A live primary is written only by the process holding its primary
        lock (plan §1 M10): take it, or refuse this store in this process."""
        if self.refused_reason is not None or not self.live_primary:
            return
        try:
            acquire_primary_lock(self.db_path)
        except StoreLockedError as exc:
            self.refused_reason = str(exc)
            raise

    def _open_writer(self, check_same_thread: bool, factory, *, gated: bool) -> sqlite3.Connection:
        if self.refused_reason is not None:
            raise StoreLockedError(f"store refused in this process: {self.refused_reason}")
        # Every writer open of a live primary goes through the lock holder:
        # the first one takes the process-lifetime lock, or the open fails
        # before anything (the file, its schema) is touched.
        self.fence()
        self._ensure_parent_dir()
        lock = _store_lock(self.db_path)
        with lock.exclusive():
            conn = sqlite3.connect(self.db_path, check_same_thread=False, factory=factory)
            conn._memora_owner = threading.get_ident()
            conn._memora_check_thread = check_same_thread
            try:
                conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            except sqlite3.Error:
                pass  # e.g. not a database yet; the caller's own statements report it
            conn._memora_lock = lock
            conn._memora_db_path = self.db_path
            lock.open_writers += 1
        conn.row_factory = sqlite3.Row
        # Setup PRAGMAs on the raw connection (plan §3, §9 item b): these and
        # nothing else. busy_timeout on every writer; WAL only for a live
        # primary (readers must not wait for a phase-3 transaction), so every
        # other local store keeps its journal mode.
        for pragma in writer_setup_pragmas(self.live_primary):
            conn.execute(pragma).fetchall()
        # Armed last: the setup above is not gated.
        if gated:
            conn._memora_gate = self.write_gate()
        return conn

    def connect_read_only(self, *, check_same_thread: bool = True) -> sqlite3.Connection:
        """A read that NEVER creates a file (no directory, database, -wal or
        -shm) -- for writers in THIS process; a read may be REFUSED.

        The whole read -- from this open to the returned connection's close
        -- holds the SHARED side of the store's process-wide lock, so no
        in-process writer connection opens or closes meanwhile (their open
        and close take the exclusive side). Under it:
        - No database file: StoreMissingError.
        - Rollback-journal database: mode=ro (memora's own local stores).
        - WAL database (header bytes 18-19 == 2) with an in-process writer
          open: its -wal and -shm exist (the writer's open touched them) and
          cannot go away during this read; mode=ro takes the WAL read lock
          through them and sees committed data. Refused (StoreLockedError)
          if they are incomplete or this user cannot use the -shm.
        - WAL database with NO in-process writer and no sidecars:
          mode=ro&immutable=1 (no locks, nothing created). SQLite's immutable
          mode turns off locking and change detection, so a file changing
          under it can give stale or torn results; it is used only while the
          shared lock guarantees no in-process writer can start.
        - WAL database with no in-process writer but sidecars present (an
          EXTERNAL writer, or a crash's leftovers): mode=ro through them if
          both exist and are usable, else refused.
        A first read runs at open; any other open/lock failure is refused as
        StoreLockedError, never repaired by creating anything.

        OUT OF SCOPE: writers in another process. memora is the single writer
        of its local stores (the design assumes it); an external writer can
        defeat both the no-create guarantee (it can close, deleting the
        sidecars, between our check and our open) and immutable correctness.
        """
        if self.refused_reason is not None:
            raise StoreLockedError(f"store refused in this process: {self.refused_reason}")
        path = self.db_path
        lock = _store_lock(path)
        lock.acquire_shared()
        try:
            if not path.is_file():
                raise StoreMissingError(f"no database file at {path}")
            from urllib.parse import quote

            wal, shm = Path(f"{path}-wal"), Path(f"{path}-shm")
            params = "mode=ro"
            if _sqlite_header_is_wal(path):
                has_wal, has_shm = wal.exists(), shm.exists()
                if lock.open_writers == 0 and not has_wal and not has_shm and self.live_primary:
                    # A live primary is never read immutable (plan §1 M10):
                    # the replicator's anchor writer keeps the sidecars.
                    raise StoreLockedError("wal_sidecars_missing_live_primary")
                if lock.open_writers == 0 and not has_wal and not has_shm:
                    params = "mode=ro&immutable=1"
                elif has_wal != has_shm or not has_wal:
                    raise StoreLockedError("wal_sidecars_incomplete")
                elif not (os.access(shm, os.R_OK | os.W_OK) and os.access(wal, os.R_OK)):
                    # SQLite would fall back to an unlocked heap-memory read;
                    # the policy is to refuse rather than read without the lock.
                    raise StoreLockedError("wal_shm_unusable")
            uri = "file:" + quote(str(path.resolve())) + "?" + params
            try:
                conn = sqlite3.connect(uri, uri=True, check_same_thread=False,
                                       factory=_LockedReaderConnection)
                conn._memora_owner = threading.get_ident()
                conn._memora_check_thread = check_same_thread
                conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            except sqlite3.Error as exc:
                raise StoreLockedError("store_locked_or_unreadable") from exc
            holder = threading.get_ident()
            conn._memora_release = lambda: lock.release_shared(holder)  # released by close()
        except BaseException:
            lock.release_shared()
            raise
        conn.row_factory = sqlite3.Row
        return conn

    def sync_before_use(self) -> None:
        """No-op for local backend."""
        pass

    def sync_after_write(self) -> None:
        """No-op for local backend."""
        pass

    def get_info(self) -> dict:
        """Return backend information."""
        return {
            "backend_type": "local_sqlite",
            "db_path": str(self.db_path),
            "exists": self.db_path.exists(),
            "size_bytes": self.db_path.stat().st_size if self.db_path.exists() else 0,
        }


class CloudSQLiteBackend(StorageBackend):
    """Cloud-backed SQLite using local cache with sync to/from S3-compatible storage.

    This backend:
    - Downloads the SQLite file from cloud storage to a local cache
    - Serves all queries from the local cache (fast)
    - Uploads changes back to cloud storage after writes
    - Uses file locking to prevent concurrent corruption
    - Tracks dirty state to avoid unnecessary uploads
    """

    def __init__(
        self,
        cloud_url: str,
        cache_dir: Optional[Path] = None,
        encrypt: bool = False,
        compress: bool = False,
        auto_sync: bool = True,
    ):
        """Initialize cloud SQLite backend.

        Args:
            cloud_url: S3 URL (e.g., s3://bucket/path/to/db.sqlite)
            cache_dir: Local cache directory (default: ~/.cache/memora)
            encrypt: Enable server-side encryption on upload
            compress: Compress database before upload
            auto_sync: Automatically sync before/after operations
        """
        if boto3 is None:
            raise ImportError(
                "boto3 is required for cloud storage. "
                "Install with: pip install boto3"
            )

        if filelock is None:
            raise ImportError(
                "filelock is required for cloud storage. "
                "Install with: pip install filelock"
            )

        self.cloud_url = cloud_url
        self.encrypt = encrypt
        self.compress = compress
        self.auto_sync = auto_sync

        # Parse S3 URL
        self.bucket, self.key = self._parse_s3_url(cloud_url)

        # Set up cache directory
        if cache_dir is None:
            cache_dir = Path.home() / ".cache" / "memora"
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # Create a unique cache path based on bucket + key
        cache_key = hashlib.sha256(f"{self.bucket}/{self.key}".encode()).hexdigest()[:16]
        self.cache_path = self.cache_dir / cache_key / "memories.db"
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)

        # Lock file to prevent concurrent access
        self.lock_path = self.cache_path.parent / "sync.lock"
        self.lock = filelock.FileLock(self.lock_path, timeout=30)

        # Metadata file to track sync state
        self.meta_path = self.cache_path.parent / "metadata.json"

        # S3 client
        self.s3_client = boto3.client("s3")

        # Dirty tracking
        self._is_dirty = False
        self._last_hash = None

        # TTL cache for sync checks (avoids redundant HEAD requests)
        self._last_sync_check: float = 0.0

        logger.info(f"Initialized CloudSQLiteBackend: {cloud_url} -> {self.cache_path}")

    def _parse_s3_url(self, url: str) -> tuple[str, str]:
        """Parse S3 URL into bucket and key.

        Args:
            url: S3 URL like s3://bucket/path/to/file.db

        Returns:
            (bucket, key) tuple
        """
        if not url.startswith("s3://"):
            raise ValueError(f"Cloud URL must start with s3://, got: {url}")

        parts = url[5:].split("/", 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid S3 URL format: {url}")

        bucket, key = parts
        return bucket, key

    def _compute_hash(self) -> Optional[str]:
        """Compute hash of the local database file.

        Returns:
            SHA256 hash of the file, or None if file doesn't exist
        """
        if not self.cache_path.exists():
            return None

        sha256 = hashlib.sha256()
        with open(self.cache_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()

    def _load_metadata(self) -> dict:
        """Load sync metadata from cache."""
        if self.meta_path.exists():
            try:
                with open(self.meta_path, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load metadata: {e}")
        return {}

    def _save_metadata(self, metadata: dict) -> None:
        """Save sync metadata to cache."""
        try:
            with open(self.meta_path, "w") as f:
                json.dump(metadata, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save metadata: {e}")

    def _create_and_upload_empty_database(self) -> None:
        """Create an empty database locally and upload it to cloud storage.

        This is called when the remote database doesn't exist yet (first-time setup).
        """
        logger.info(f"Creating empty database and uploading to {self.bucket}/{self.key}")

        # Create empty local database with schema
        # Import here to avoid circular imports
        from .storage import ensure_schema

        conn = sqlite3.connect(self.cache_path, check_same_thread=True)
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)
        conn.close()

        logger.info(f"Created empty database at {self.cache_path}")

        # Upload to cloud
        extra_args = {}
        if self.encrypt:
            extra_args["ServerSideEncryption"] = "AES256"

        self.s3_client.upload_file(
            str(self.cache_path),
            self.bucket,
            self.key,
            ExtraArgs=extra_args if extra_args else None
        )

        # Get the ETag of the uploaded file
        head_response = self.s3_client.head_object(
            Bucket=self.bucket,
            Key=self.key
        )

        # Save metadata
        metadata = {
            "etag": head_response.get("ETag", "").strip('"'),
            "last_sync": datetime.now().isoformat(),
            "remote_modified": head_response.get("LastModified").isoformat() if head_response.get("LastModified") else None,
        }
        self._save_metadata(metadata)

        # Update tracking
        self._last_hash = self._compute_hash()
        self._is_dirty = False

        logger.info(f"Uploaded empty database to {self.bucket}/{self.key}")

    def _create_local_database_only(self) -> None:
        """Create an empty database locally without uploading to cloud.

        This is used as a fallback when cloud upload fails, allowing
        the user to continue working in local-only mode.
        """
        logger.info(f"Creating local-only database at {self.cache_path}")

        # Create empty local database with schema
        from .storage import ensure_schema

        conn = sqlite3.connect(self.cache_path, check_same_thread=True)
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)
        conn.close()

        # Mark as dirty so next sync attempt will try to upload
        self._last_hash = self._compute_hash()
        self._is_dirty = True

        logger.warning(
            "Running in local-only mode. Changes will not sync to cloud until "
            "connectivity is restored. Run 'memora-server sync-push' to retry upload."
        )

    def sync_before_use(self) -> None:
        """Download database from S3 if needed."""
        if not self.auto_sync:
            return

        # Skip if we checked recently (within TTL)
        now = time.time()
        if now - self._last_sync_check < SYNC_CHECK_TTL and self.cache_path.exists():
            logger.debug(f"Skipping sync check (TTL: {SYNC_CHECK_TTL}s)")
            return

        with self.lock:
            try:
                # Check if remote object exists and get metadata
                try:
                    head_response = _retry_with_backoff(
                        lambda: self.s3_client.head_object(
                            Bucket=self.bucket,
                            Key=self.key
                        ),
                        "checking remote database"
                    )
                    remote_etag = head_response.get("ETag", "").strip('"')
                    remote_modified = head_response.get("LastModified")
                except ClientError as e:
                    error_code = e.response["Error"]["Code"]
                    # Handle both 404 (Not Found) and 403 (Forbidden/Access Denied)
                    # R2 and some S3 configurations return 403 for non-existent objects
                    # when the bucket policy doesn't allow ListBucket
                    if error_code in ("404", "403"):
                        logger.info(
                            f"Remote database not found (error {error_code}), "
                            f"creating empty database"
                        )
                        try:
                            self._create_and_upload_empty_database()
                        except Exception as upload_error:
                            # Graceful fallback: use local-only mode if upload fails
                            friendly_msg = _get_user_friendly_error(
                                upload_error, "uploading initial database"
                            )
                            logger.warning(
                                f"Failed to upload initial database, using local-only mode.\n"
                                f"{friendly_msg}"
                            )
                            # Create local database without uploading
                            self._create_local_database_only()
                        self._last_sync_check = time.time()
                        return
                    raise

                # Load local metadata
                metadata = self._load_metadata()
                local_etag = metadata.get("etag")

                # Skip download if local cache is up to date
                if local_etag == remote_etag and self.cache_path.exists():
                    logger.debug(f"Local cache is up to date (ETag: {remote_etag})")
                    self._last_hash = self._compute_hash()
                    self._last_sync_check = time.time()
                    return

                # Download from S3 with retry
                logger.info(f"Downloading {self.bucket}/{self.key} to {self.cache_path}")
                start_time = time.time()

                # Download to temporary file first
                temp_path = self.cache_path.parent / f"{self.cache_path.name}.tmp"

                _retry_with_backoff(
                    lambda: self.s3_client.download_file(
                        self.bucket, self.key, str(temp_path)
                    ),
                    "downloading database"
                )

                # Move to final location
                shutil.move(str(temp_path), str(self.cache_path))

                duration = time.time() - start_time
                size_mb = self.cache_path.stat().st_size / (1024 * 1024)
                logger.info(f"Downloaded {size_mb:.2f} MB in {duration:.2f}s")

                # Update metadata
                metadata["etag"] = remote_etag
                metadata["last_sync"] = datetime.now().isoformat()
                metadata["remote_modified"] = remote_modified.isoformat() if remote_modified else None
                self._save_metadata(metadata)

                # Update hash and sync check time
                self._last_hash = self._compute_hash()
                self._is_dirty = False
                self._last_sync_check = time.time()

            except NoCredentialsError as e:
                friendly_msg = _get_user_friendly_error(e, "syncing from cloud")
                logger.error(friendly_msg)
                raise RuntimeError(friendly_msg) from e
            except ClientError as e:
                friendly_msg = _get_user_friendly_error(e, "syncing from cloud")
                logger.error(friendly_msg)
                raise RuntimeError(friendly_msg) from e
            except Exception as e:
                friendly_msg = _get_user_friendly_error(e, "syncing from cloud")
                logger.error(friendly_msg)
                raise

    def sync_after_write(self) -> None:
        """Upload database to S3 if dirty."""
        if not self.auto_sync:
            return

        # Fast path: check dirty flag first (avoids expensive hashing)
        if not self._is_dirty:
            logger.debug("Database not dirty, skipping sync")
            return

        with self.lock:
            try:
                # Double-check dirty flag under lock
                if not self._is_dirty:
                    logger.debug("Database not dirty (checked under lock), skipping sync")
                    return

                if not self.cache_path.exists():
                    logger.warning("Cache file doesn't exist, nothing to upload")
                    return

                # Compute hash to detect changes (only when dirty flag is set)
                current_hash = self._compute_hash()
                if current_hash == self._last_hash:
                    # False positive - dirty flag was set but content unchanged
                    logger.debug("Database unchanged after hashing, skipping upload")
                    self._is_dirty = False
                    return

                # Check for conflicts before uploading
                # Load the last known remote ETag
                metadata = self._load_metadata()
                last_known_etag = metadata.get("etag")

                # Verify remote hasn't changed since our last sync
                if last_known_etag:
                    try:
                        current_remote = _retry_with_backoff(
                            lambda: self.s3_client.head_object(
                                Bucket=self.bucket,
                                Key=self.key
                            ),
                            "checking remote state before upload"
                        )
                        current_remote_etag = current_remote.get("ETag", "").strip('"')

                        if current_remote_etag != last_known_etag:
                            # Conflict detected: remote was modified by another writer
                            logger.error(
                                f"Conflict detected! Remote object changed since last sync. "
                                f"Expected ETag: {last_known_etag}, "
                                f"Current ETag: {current_remote_etag}"
                            )
                            raise ConflictError(
                                "Database was modified by another process. "
                                "Run 'memora-server sync-pull' to get latest changes."
                            )
                    except ClientError as e:
                        if e.response["Error"]["Code"] != "404":
                            raise

                # Upload to S3 with retry
                logger.info(f"Uploading {self.cache_path} to {self.bucket}/{self.key}")
                start_time = time.time()

                extra_args = {}
                if self.encrypt:
                    extra_args["ServerSideEncryption"] = "AES256"

                _retry_with_backoff(
                    lambda: self.s3_client.upload_file(
                        str(self.cache_path),
                        self.bucket,
                        self.key,
                        ExtraArgs=extra_args if extra_args else None
                    ),
                    "uploading database"
                )

                duration = time.time() - start_time
                size_mb = self.cache_path.stat().st_size / (1024 * 1024)
                logger.info(f"Uploaded {size_mb:.2f} MB in {duration:.2f}s")

                # Update metadata with new remote state
                head_response = _retry_with_backoff(
                    lambda: self.s3_client.head_object(
                        Bucket=self.bucket,
                        Key=self.key
                    ),
                    "verifying upload"
                )
                metadata = {
                    "etag": head_response.get("ETag", "").strip('"'),
                    "last_sync": datetime.now().isoformat(),
                    "remote_modified": head_response.get("LastModified").isoformat() if head_response.get("LastModified") else None,
                }
                self._save_metadata(metadata)

                # Update tracking
                self._last_hash = current_hash
                self._is_dirty = False

            except ConflictError:
                # Re-raise conflict errors without wrapping
                raise
            except NoCredentialsError as e:
                friendly_msg = _get_user_friendly_error(e, "uploading to cloud")
                logger.error(friendly_msg)
                # Keep dirty flag set so next attempt will try again
                raise RuntimeError(friendly_msg) from e
            except ClientError as e:
                friendly_msg = _get_user_friendly_error(e, "uploading to cloud")
                logger.error(friendly_msg)
                # Keep dirty flag set so next attempt will try again
                raise RuntimeError(friendly_msg) from e
            except Exception as e:
                friendly_msg = _get_user_friendly_error(e, "uploading to cloud")
                logger.error(friendly_msg)
                # Keep dirty flag set so next attempt will try again
                raise

    def connect(self, *, check_same_thread: bool = True) -> sqlite3.Connection:
        """Return a connection to the cached SQLite database.

        This will sync from cloud if needed before returning the connection.
        """
        # Sync from cloud before use
        self.sync_before_use()

        # Create connection to local cache
        conn = sqlite3.connect(self.cache_path, check_same_thread=check_same_thread)
        conn.row_factory = sqlite3.Row

        # Mark backend as dirty when connection commits
        # We use a wrapper class to intercept commits since in Python 3.13+
        # sqlite3.Connection methods are read-only
        class TrackedConnection:
            def __init__(self, conn, backend):
                self._conn = conn
                self._backend = backend

            def __getattr__(self, name):
                attr = getattr(self._conn, name)
                if name == 'commit':
                    def wrapped_commit(*args, **kwargs):
                        result = attr(*args, **kwargs)
                        self._backend._is_dirty = True
                        logger.debug("Database marked as dirty after commit")
                        return result
                    return wrapped_commit
                return attr

            def __enter__(self):
                return self._conn.__enter__()

            def __exit__(self, *args):
                return self._conn.__exit__(*args)

        return TrackedConnection(conn, self)

    def get_info(self) -> dict:
        """Return backend information."""
        metadata = self._load_metadata()
        return {
            "backend_type": "cloud_sqlite",
            "cloud_url": self.cloud_url,
            "bucket": self.bucket,
            "key": self.key,
            "cache_path": str(self.cache_path),
            "cache_exists": self.cache_path.exists(),
            "cache_size_bytes": self.cache_path.stat().st_size if self.cache_path.exists() else 0,
            "is_dirty": self._is_dirty,
            "last_etag": metadata.get("etag"),
            "last_sync": metadata.get("last_sync"),
            "auto_sync": self.auto_sync,
            "encrypt": self.encrypt,
        }

    def force_sync_pull(self) -> None:
        """Force download from cloud, ignoring local state."""
        with self.lock:
            logger.info("Forcing sync pull from cloud")
            # Clear metadata to force download
            if self.meta_path.exists():
                self.meta_path.unlink()
            # Reset TTL cache to ensure sync_before_use() actually downloads
            self._last_sync_check = 0.0
            self.sync_before_use()

    def force_sync_push(self) -> None:
        """Force upload to cloud, even if not dirty."""
        with self.lock:
            logger.info("Forcing sync push to cloud")
            self._is_dirty = True
            self._last_hash = None  # Force hash mismatch
            self.sync_after_write()


class D1Row:
    """A dict-like row that supports both index and key access (like sqlite3.Row)."""

    def __init__(self, data: dict, columns: list):
        self._data = data
        self._columns = columns

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._data[self._columns[key]]
        return self._data[key]

    def __iter__(self):
        return iter(self._data.values())

    def keys(self):
        return self._columns

    def values(self):
        return [self._data[c] for c in self._columns]

    def items(self):
        return [(c, self._data[c]) for c in self._columns]

    def __repr__(self):
        return f"D1Row({self._data})"


class D1Cursor:
    """A cursor-like object for D1 query results."""

    def __init__(self, results: list, columns: list, lastrowid: int = 0, rowcount: int = 0):
        self._results = results
        self._columns = columns
        self._index = 0
        self.lastrowid = lastrowid
        self.rowcount = rowcount
        self.description = [(col, None, None, None, None, None, None) for col in columns] if columns else None

    def fetchone(self):
        if self._index >= len(self._results):
            return None
        row = self._results[self._index]
        self._index += 1
        return D1Row(row, self._columns)

    def fetchall(self):
        rows = self._results[self._index:]
        self._index = len(self._results)
        return [D1Row(row, self._columns) for row in rows]

    def fetchmany(self, size=None):
        if size is None:
            size = 1
        rows = self._results[self._index:self._index + size]
        self._index += len(rows)
        return [D1Row(row, self._columns) for row in rows]

    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    def close(self):
        pass


class D1DefiniteError(RuntimeError):
    """D1 answered and the statement definitely did NOT apply: an HTTP 4xx, or
    a response with success=false. Anything else that fails after a request
    was sent (a timeout, a reset, a 5xx) has an UNKNOWN outcome."""


class D1Connection:
    """A connection-like object that talks to Cloudflare D1 via HTTP API.

    Session tokens (D1 read-your-writes bookmarks) are stored **per connection
    instance** and also mirrored onto the owning :class:`D1Backend` so that the
    next connection opened by that backend inherits the latest known bookmark.
    This gives two useful properties:

    1. **No cross-instance stomping.** A background thread (``cloud_sync``'s
       ``threading.Timer``) or an unrelated concurrent tool call no longer
       clobbers this connection's token mid-request — the field lives on the
       instance, not in class or thread-local state.
    2. **Bookmark continuity across tool calls.** When a tool call finishes and
       its connection is closed, the last observed bookmark is parked on the
       backend singleton. The next tool call opens a fresh connection and is
       seeded with that bookmark, preserving read-your-writes across calls.

    Concurrent writers race on the backend-level bookmark update; D1 bookmarks
    are monotonically advancing so last-writer-wins yields a valid continuation
    point.
    """

    def __init__(self, account_id: str, database_id: str, api_token: str):
        self.account_id = account_id
        self.database_id = database_id
        self.api_token = api_token
        self.base_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}"
        self.row_factory = None
        self._pending_statements = []
        self._session_token: Optional[str] = None
        # One per HTTPS POST to the query API; read by memora.absorb_profile.
        self.request_count = 0
        # Set by D1Backend.connect() so _execute_api can push new bookmarks
        # back up to the backend-level singleton. May be None for connections
        # constructed directly without a backend (tests, ad-hoc tooling).
        self._backend: Optional["D1Backend"] = None

    supports_transactions = False  # every statement autocommits (plan §1 M10)

    # Set when the backend could not give this connection a usable write
    # journal (plan §1; leader 7583): reads work, every mutation raises
    # StoreReadOnlyError, and schema setup is skipped.
    read_only_reason: Optional[str] = None

    @property
    def read_only(self) -> bool:
        return self.read_only_reason is not None

    def execute_batch(self, statements) -> list:
        """Reserved for the replicator (plan §2.4, slice L3). Not available
        on application connections."""
        raise StoreReadOnlyError("execute_batch is not available on this connection")

    def _gate_and_journal(self):
        backend = self._backend
        if backend is not None and hasattr(backend, "write_gate"):
            return backend.write_gate(), backend.journal()
        name = self.database_id
        gate = gate_for_key(f"d1:{self.database_id}")
        journal = journal_for(name)
        gate.journal_status = journal.status
        return gate, journal

    def _execute_api(self, sql: str, params: tuple = None) -> dict:
        """One statement over the D1 HTTP API, behind the write gate and the
        write-ahead intent journal (plan §1).

        Reads go straight through. A mutating statement enters the store's
        gate (refused while frozen), is journaled and fsynced BEFORE it is
        sent (not sent if that fails), and is resolved after a KNOWN outcome.
        An unknown outcome leaves the intent open; the gate token is released
        once the outcome is determined either way."""
        c = classify_statement(sql)
        if c.kind == READ:
            return self._send(sql, params)
        if self.read_only_reason is not None:
            raise StoreReadOnlyError(f"read-only D1 connection: {self.read_only_reason}")
        gate, journal = self._gate_and_journal()
        token = gate.enter(c.main or c.kind)
        try:
            target, keys, post_state = derive_effect(sql, params)
            iid = journal.append_intent(sql, params, target=target or c.target,
                                        keys=keys, post_state=post_state)
            # Re-check immediately before the send (plan round-2 P1-3): a
            # journal that broke after the append (a failed compaction, a
            # concurrent failed repair) must not let this write go out.
            broken = journal.status()[1]
            if broken:
                journal.abandon(iid, "not-sent")
                raise IntentJournalError(f"not sent: the write journal became unusable: {broken}")
            try:
                result = self._send(sql, params)
            except D1DefiniteError:
                journal.resolve(iid, "failed")
                raise
            # Any other exception: unknown outcome, the intent stays open.
            journal.resolve(iid, "ok")
            return result
        finally:
            gate.leave(token)

    def _send(self, sql: str, params: tuple = None) -> dict:
        """Execute SQL via D1 HTTP API with session affinity for read-your-writes."""
        import urllib.error
        import urllib.request

        url = f"{self.base_url}/query"
        self.request_count += 1

        body = {"sql": sql}
        if params:
            # D1 expects positional params as a list
            body["params"] = list(params)

        data = json.dumps(body).encode("utf-8")

        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }

        # Include session token for read-your-writes consistency
        # (per-instance — see class docstring).
        if self._session_token:
            headers["cf-d1-session-token"] = self._session_token

        if not _proxy_configured():
            transport = (
                self._backend._transport(self.base_url)
                if self._backend is not None
                else self._own_transport()
            )
            status, getheader, raw = transport.post(
                "/query", data, headers, retry_safe=_is_read_statement(sql),
            )
            if status >= 400:
                err = D1DefiniteError if status < 500 else RuntimeError
                raise err(f"D1 API error ({status}): {raw.decode(errors='replace')}")
            result = json.loads(raw.decode())
            self._absorb_session_token(getheader("cf-d1-session-token"))
        else:
            req = urllib.request.Request(
                url,
                data=data,
                headers=headers,
                method="POST",
            )

            try:
                with urllib.request.urlopen(req, timeout=_D1_TIMEOUT_SECONDS) as resp:
                    result = json.loads(resp.read().decode())
                    self._absorb_session_token(resp.headers.get("cf-d1-session-token"))

            except urllib.error.HTTPError as e:
                error_body = e.read().decode() if e.fp else str(e)
                err = D1DefiniteError if e.code < 500 else RuntimeError
                raise err(f"D1 API error ({e.code}): {error_body}")

        if not result.get("success"):
            errors = result.get("errors", [])
            error_msg = errors[0].get("message") if errors else "Unknown error"
            raise D1DefiniteError(f"D1 query failed: {error_msg}")

        return result

    def _absorb_session_token(self, response_token: Optional[str]) -> None:
        # Extract session token from response for subsequent requests.
        # D1 returns the updated bookmark after writes so the next
        # query on this connection can see its own writes. We also
        # mirror it up to the owning D1Backend so the *next*
        # connection (next tool call) inherits the latest bookmark
        # and preserves read-your-writes across calls.
        if response_token:
            self._session_token = response_token
            if self._backend is not None:
                self._backend.update_bookmark(response_token)

    def _own_transport(self) -> "_D1Transport":
        # Connections built without a backend (tools, tests) keep their own.
        t = getattr(self, "_transport_obj", None)
        if t is None:
            t = _D1Transport(self.base_url)
            self._transport_obj = t
        return t

    def execute(self, sql: str, params: tuple = None) -> D1Cursor:
        """Execute a single SQL statement."""
        result = self._execute_api(sql, params)

        # D1 returns results in a nested structure
        query_result = result.get("result", [{}])[0]
        rows = query_result.get("results", [])
        meta = query_result.get("meta", {})

        # Extract columns from first row if available
        columns = list(rows[0].keys()) if rows else []

        return D1Cursor(
            results=rows,
            columns=columns,
            lastrowid=meta.get("last_row_id", 0),
            rowcount=meta.get("changes", len(rows)),
        )

    def executemany(self, sql: str, params_list: list) -> D1Cursor:
        """Execute SQL for multiple parameter sets."""
        # D1 doesn't have native executemany, so we batch execute
        lastrowid = 0
        total_changes = 0

        for params in params_list:
            result = self._execute_api(sql, params)
            query_result = result.get("result", [{}])[0]
            meta = query_result.get("meta", {})
            lastrowid = meta.get("last_row_id", lastrowid)
            total_changes += meta.get("changes", 0)

        return D1Cursor(results=[], columns=[], lastrowid=lastrowid, rowcount=total_changes)

    def executescript(self, sql_script: str) -> D1Cursor:
        """Execute multiple SQL statements separated by semicolons."""
        # Split by semicolons and execute each
        statements = [s.strip() for s in sql_script.split(";") if s.strip()]
        lastrowid = 0
        total_changes = 0

        for stmt in statements:
            result = self._execute_api(stmt)
            query_result = result.get("result", [{}])[0]
            meta = query_result.get("meta", {})
            lastrowid = meta.get("last_row_id", lastrowid)
            total_changes += meta.get("changes", 0)

        return D1Cursor(results=[], columns=[], lastrowid=lastrowid, rowcount=total_changes)

    def cursor(self) -> "D1Connection":
        """Return self as cursor (D1Connection acts as both)."""
        return self

    def commit(self):
        """No-op - D1 auto-commits each query."""
        pass

    def rollback(self):
        """No-op - D1 doesn't support transactions via HTTP API."""
        pass

    def close(self):
        """Close this connection's own transport, if it has one. Transports
        owned by a D1Backend are per-thread and outlive the connection on
        purpose (keep-alive across tool calls)."""
        t = getattr(self, "_transport_obj", None)
        if t is not None:
            t.close()
            self._transport_obj = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# Reconnect proactively after this much idle time rather than discover a
# keep-alive socket the server already closed.
_D1_KEEPALIVE_IDLE_SECONDS = 25.0
_D1_TIMEOUT_SECONDS = 30


def _proxy_configured() -> bool:
    # http.client ignores proxy env vars; urllib honours them. Keep urllib
    # whenever a proxy is configured so behaviour there is unchanged.
    return any(os.environ.get(k) for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"))


def _is_read_statement(sql: str) -> bool:
    return sql.lstrip().upper().startswith("SELECT")


class _D1Transport:
    """One persistent HTTP(S) connection to the D1 query endpoint.

    Owned by exactly one thread (see D1Backend._transport), so no socket is
    ever shared. Saves a TCP + TLS handshake per statement.

    Retry policy, deliberately narrow: only when a REUSED connection fails
    before any response (the server closed an idle keep-alive socket), and
    only for a SELECT. A write is never re-sent: D1 may have executed it
    with the response lost, and a blind resend could insert twice.
    """

    def __init__(self, base_url: str):
        from urllib.parse import urlsplit

        parts = urlsplit(base_url)
        self._scheme = parts.scheme
        self._host = parts.hostname
        self._port = parts.port
        self._path = parts.path
        self._conn = None
        self._last_used = 0.0

    def _new(self):
        import http.client

        cls = http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
        return cls(self._host, self._port, timeout=_D1_TIMEOUT_SECONDS)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _send_and_receive_status(self, suffix: str, body: bytes, headers: dict):
        """Send the request and read the status line + headers.

        Raises _NoResponse only when NOT ONE response byte arrived: the send
        failed (the socket was already closed or reset), or the server closed
        the connection without answering (http.client.RemoteDisconnected is
        raised exactly when the status line read returns zero bytes). Anything
        after the first response byte propagates unchanged and is never
        retried.
        """
        import http.client

        try:
            self._conn.request("POST", self._path + suffix, body=body, headers=headers)
        except (http.client.CannotSendRequest, ConnectionResetError, BrokenPipeError) as exc:
            raise _NoResponse(exc) from exc
        try:
            return self._conn.getresponse()
        except http.client.RemoteDisconnected as exc:
            raise _NoResponse(exc) from exc

    def post(self, suffix: str, body: bytes, headers: dict, *, retry_safe: bool):
        """POST and return (status, response header getter, body bytes).

        A retry happens at most once, only for a SELECT (retry_safe), only on
        a REUSED socket, and only when no response byte arrived at all
        (_NoResponse). A write -- including get_memory's track_access UPDATE,
        whose SQL is not a SELECT -- is never re-sent; a failure after the
        status line (e.g. while reading the body) is never retried either.
        """
        now = time.monotonic()
        if self._conn is not None and now - self._last_used > _D1_KEEPALIVE_IDLE_SECONDS:
            self.close()
        reused = self._conn is not None
        if self._conn is None:
            self._conn = self._new()
        try:
            try:
                resp = self._send_and_receive_status(suffix, body, headers)
            except _NoResponse as no_resp:
                self.close()
                if not (reused and retry_safe):
                    raise no_resp.cause
                logger.debug("D1 keep-alive connection was stale (%s); retrying read once", no_resp.cause)
                self._conn = self._new()
                resp = self._send_and_receive_status(suffix, body, headers)
            data = resp.read()
        except _NoResponse as no_resp:
            self.close()
            raise no_resp.cause
        except Exception:
            self.close()
            raise
        self._last_used = time.monotonic()
        if (resp.getheader("connection") or "").lower() == "close":
            self.close()
        return resp.status, resp.getheader, data


class _NoResponse(Exception):
    """Internal: the request failed before any response byte arrived."""

    def __init__(self, cause: BaseException):
        super().__init__(str(cause))
        self.cause = cause


class D1Backend(StorageBackend):
    """Cloudflare D1 backend - uses D1 as primary database via HTTP API.

    This backend:
    - Executes all queries directly against D1 (no local caching)
    - Uses R2 for media/image storage only
    - No sync needed - D1 is the source of truth

    Holds a single "latest known" D1 session bookmark so that new connections
    inherit the most recent read-your-writes point at open time and write back
    the advanced bookmark on each successful HTTP call. Concurrent writers race
    on the final assignment (last-writer-wins), which is fine because D1
    bookmarks are monotonically advancing — any surviving bookmark is a valid
    continuation point.
    """

    def __init__(self, account_id: str, database_id: str, api_token: str):
        """Initialize D1 backend.

        Args:
            account_id: Cloudflare account ID
            database_id: D1 database ID
            api_token: Cloudflare API token with D1 permissions
        """
        self.account_id = account_id
        self.database_id = database_id
        self.api_token = api_token
        self._latest_bookmark: Optional[str] = None
        self._bookmark_lock = threading.Lock()
        self._transports = threading.local()
        # The registry name (set by storage.backend_for); None when the
        # backend was built directly -- the journal is then keyed by the
        # database id.
        self.store_name: Optional[str] = None

        logger.info(f"Initialized D1Backend: database={database_id}")

    def _transport(self, base_url: str) -> "_D1Transport":
        """This thread's persistent connection to D1 (created on first use).
        Worker threads are reused across tool calls, so the connection is too."""
        t = getattr(self._transports, "d1", None)
        if t is None:
            t = _D1Transport(base_url)
            self._transports.d1 = t
        return t

    def get_latest_bookmark(self) -> Optional[str]:
        with self._bookmark_lock:
            return self._latest_bookmark

    def update_bookmark(self, bookmark: str) -> None:
        """Advance the backend bookmark iff ``bookmark`` is newer.

        D1 session bookmarks sort lexicographically oldest-to-newest (per
        Cloudflare docs), so a slower request that started from an older
        bookmark can finish after a faster one and must NOT clobber the
        newer bookmark with its older response. We keep the lexicographic
        max under the lock.
        """
        with self._bookmark_lock:
            if self._latest_bookmark is None or bookmark > self._latest_bookmark:
                self._latest_bookmark = bookmark

    def write_gate(self) -> _WriteGate:
        """This store's write gate (plan §1), with the journal's status."""
        gate = gate_for_key(f"d1:{self.database_id}", self.store_name)
        if gate.journal_status is None:
            gate.journal_status = self.journal().status
        return gate

    def journal(self):
        """This store's write-ahead intent journal (opened and replayed on
        first use; see memora/intent_journal.py)."""
        return journal_for(self.store_name or self.database_id)

    def connect(self, *, check_same_thread: bool = True) -> D1Connection:
        """Return a D1 connection seeded with the backend's latest bookmark.

        When the store's intent journal cannot be used (held by another
        process, no writable data dir, corrupt before its final line, or
        broken), the connection is READ-ONLY: SELECTs work, every mutation
        raises StoreReadOnlyError, and schema.connect skips schema setup.
        A connection whose journal breaks later refuses mutations too (the
        gate refuses while the journal is broken)."""
        journal = self.journal()
        unusable = journal.fatal or journal.broken
        self.write_gate()
        conn = D1Connection(self.account_id, self.database_id, self.api_token)
        if unusable:
            # Single writer, but reads stay available: this process gets a
            # read-only connection (leader 7583, review 7584 P1-2).
            conn.read_only_reason = unusable
        conn._session_token = self.get_latest_bookmark()
        conn._backend = self
        return conn

    def sync_before_use(self) -> None:
        """No-op - D1 is always up to date."""
        pass

    def sync_after_write(self) -> None:
        """No-op - D1 writes are immediate."""
        pass

    def get_info(self) -> dict:
        """Return backend information."""
        return {
            "backend_type": "d1",
            "account_id": self.account_id,
            "database_id": self.database_id,
        }


class D1SelectOnlyConnection:
    """A D1 reader that can run exactly one SELECT per call, and nothing else
    (plan §2.9 P0-1). It is NOT a D1Connection and does not use the P2
    statement checker: per-key UPSERTs and DELETEs, PRAGMA, EXPLAIN, VALUES,
    DDL, RETURNING and multi-statement bodies are all refused. It has no
    executemany, executescript, commit or execute_batch. Meant for the D1
    Read token (MEMORA_D1_READ_TOKEN)."""

    def __init__(self, account_id: str, database_id: str, api_token: str):
        self.account_id = account_id
        self.database_id = database_id
        self._api_token = api_token
        self.base_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}"
        self._transport_obj: Optional[_D1Transport] = None

    @classmethod
    def from_env(cls, account_id: str, database_id: str) -> "D1SelectOnlyConnection":
        token = os.getenv("MEMORA_D1_READ_TOKEN", "").strip()
        if not token:
            raise RuntimeError("MEMORA_D1_READ_TOKEN is not set: no D1 read credential")
        return cls(account_id, database_id, token)

    def _post(self, body: bytes) -> tuple:
        if self._transport_obj is None:
            self._transport_obj = _D1Transport(self.base_url)
        headers = {"Authorization": f"Bearer {self._api_token}", "Content-Type": "application/json"}
        return self._transport_obj.post("/query", body, headers, retry_safe=True)

    def execute(self, sql: str, params: tuple = None) -> tuple:
        """(rows, meta) of one SELECT. Raises ValueError for anything else."""
        if not isinstance(sql, str) or not is_select_only(sql):
            raise ValueError("D1SelectOnlyConnection runs a single SELECT only")
        body = {"sql": sql}
        if params:
            body["params"] = list(params)
        status, _getheader, raw = self._post(json.dumps(body).encode("utf-8"))
        if status >= 400:
            raise RuntimeError(f"D1 API error ({status}): {raw.decode(errors='replace')}")
        result = json.loads(raw.decode())
        if not result.get("success"):
            errors = result.get("errors", [])
            raise RuntimeError(f"D1 query failed: {errors[0].get('message') if errors else 'Unknown error'}")
        first = (result.get("result") or [{}])[0]
        return first.get("results", []), first.get("meta", {})

    def close(self) -> None:
        if self._transport_obj is not None:
            self._transport_obj.close()
            self._transport_obj = None


def primary_lock_path(db_path: Path) -> Path:
    """The lock file of a store, derived from its CANONICAL path (review 7588
    P1): the database path and every parent directory are resolved first, so
    a symlink alias of the file, or of a directory above it, names the same
    lock. The suffix is appended after canonicalisation."""
    return Path(os.path.realpath(str(db_path)) + ".primary-lock")


# realpath of the lock file -> fd, for the locks THIS process holds. A flock
# belongs to an open file description, so a second open+flock in the same
# process would conflict with the first: every holder shares this one fd.
_PRIMARY_LOCKS: Dict[str, int] = {}
_PRIMARY_LOCKS_GUARD = threading.Lock()


def acquire_primary_lock(db_path: Path) -> int:
    """flock(LOCK_EX|LOCK_NB) on `<db>.primary-lock` (plan §1 M10). The one
    process that holds it is the store's only writer: memora-all for its
    lifetime, or a maintenance script while memora-all is stopped. Idempotent
    within a process (returns the held fd). Raises StoreLockedError when
    another process holds it."""
    import fcntl

    path = primary_lock_path(db_path)
    key = str(path)  # already canonical
    with _PRIMARY_LOCKS_GUARD:
        held = _PRIMARY_LOCKS.get(key)
        if held is not None:
            return held
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise StoreLockedError(f"{path} is held by another process: it is this live primary's writer")
        _PRIMARY_LOCKS[key] = fd
        return fd


def release_primary_lock(db_path: Path) -> None:
    key = str(primary_lock_path(db_path))  # the same canonical identity as acquire
    with _PRIMARY_LOCKS_GUARD:
        fd = _PRIMARY_LOCKS.pop(key, None)
    if fd is not None:
        os.close(fd)  # releases the flock


def parse_backend_uri(uri: str) -> StorageBackend:
    """Parse a storage URI and return the appropriate backend.

    Supported URI formats:
    - file:///path/to/db.sqlite (local SQLite)
    - /path/to/db.sqlite (local SQLite)
    - s3://bucket/path/to/db.sqlite (S3-compatible cloud storage)
    - d1://account_id/database_id (Cloudflare D1)

    Args:
        uri: Storage URI string

    Returns:
        StorageBackend instance
    """
    if uri.startswith("d1://"):
        # D1 URI format: d1://account_id/database_id
        # API token from environment: CLOUDFLARE_API_TOKEN or CF_API_TOKEN
        parts = uri[5:].split("/", 1)
        if len(parts) != 2:
            raise ValueError(
                f"Invalid D1 URI format: {uri}\n"
                "Expected: d1://account_id/database_id"
            )

        account_id, database_id = parts

        api_token = os.getenv("CLOUDFLARE_API_TOKEN") or os.getenv("CF_API_TOKEN")
        if not api_token:
            raise ValueError(
                "D1 backend requires CLOUDFLARE_API_TOKEN or CF_API_TOKEN environment variable.\n"
                "Create a token at: https://dash.cloudflare.com/profile/api-tokens\n"
                "Required permissions: D1 Edit"
            )

        return D1Backend(account_id=account_id, database_id=database_id, api_token=api_token)

    elif uri.startswith("s3://"):
        # Parse cloud storage options from environment
        encrypt = os.getenv("MEMORA_CLOUD_ENCRYPT", "").lower() in ("1", "true", "yes")
        compress = os.getenv("MEMORA_CLOUD_COMPRESS", "").lower() in ("1", "true", "yes")
        cache_dir_env = os.getenv("MEMORA_CACHE_DIR")
        cache_dir = Path(cache_dir_env) if cache_dir_env else None

        return CloudSQLiteBackend(
            cloud_url=uri,
            cache_dir=cache_dir,
            encrypt=encrypt,
            compress=compress,
        )

    elif uri.startswith("file://"):
        # file:// URI
        path = uri[7:]  # Remove file://
        return LocalSQLiteBackend(Path(path))

    else:
        # Assume local path
        return LocalSQLiteBackend(Path(uri))
