"""Write-ahead intent journal for mutating D1 requests.

docs/local-primary-implementation.md §1 "Write-ahead intent journal".

Before a mutating D1 request is sent, an intent record is appended to
`<data>/intent/<db>.jsonl` and fsynced; if that fails, the request is not
sent. After a KNOWN outcome (success, or a definite HTTP/SQL failure) a
resolution record is appended, fsynced lazily (the next intent's fsync, or a
1 s timer). An unknown outcome -- a timeout, a reset after send, the process
dying -- writes nothing, so the intent stays open. Open intents make a frozen
store `frozen-unsafe`, and every migration step refuses until an operator
accepts them (there is no automatic resolution: plan round-10 P2).

Invariants:
- One process writes the journal: it holds flock(LOCK_EX) on the stable,
  never-renamed `<db>.lock` for its lifetime (a flock belongs to an inode,
  and compaction replaces the journal file).
- One mutex per journal covers intent append+fsync, resolution append, the
  open-set update and the whole compaction.
- Before every intent append the active fd's (st_dev, st_ino) must equal the
  path's; a mismatch refuses the send and marks the journal broken.
- After ANY failed append or fsync the file is truncated back to its last
  newline and fsynced (file and directory) before anything else is sent; if
  that repair fails the journal is broken and every mutation is refused.
- Startup replay: a malformed record that is not the final line refuses the
  store (never discard later bytes); a torn final line is dropped and logged.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .sql_classify import params_digest
from .write_gate import data_dir

logger = logging.getLogger(__name__)

RESOLUTION_FSYNC_INTERVAL_S = 1.0
RECONCILE_MIN_AGE_S = 60.0  # 2 x _D1_TIMEOUT_SECONDS (backends.py)


def _compact_threshold() -> int:
    return int(os.getenv("MEMORA_INTENT_COMPACT_BYTES", str(8 * 1024 * 1024)))


class IntentJournalError(RuntimeError):
    """The journal could not durably record an intent (the request was NOT
    sent), or the journal is unusable (corrupt, held by another process, or
    broken after a failed repair)."""


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _validate_record(rec: Any) -> None:
    """Every field replay relies on (review 7584 P2): a record that would
    not apply cleanly is malformed, handled like one that does not parse."""
    if not isinstance(rec, dict):
        raise ValueError("record is not an object")
    kind = rec.get("type")
    if kind == "meta":
        if not isinstance(rec.get("next_id"), int) or rec["next_id"] < 1:
            raise ValueError("meta.next_id must be a positive integer")
    elif kind in ("intent", "resolved"):
        if not isinstance(rec.get("id"), int) or isinstance(rec.get("id"), bool) or rec["id"] < 1:
            raise ValueError(f"{kind}.id must be a positive integer")
        if kind == "intent" and not isinstance(rec.get("sql"), str):
            raise ValueError("intent.sql must be a string")
        if kind == "resolved" and not isinstance(rec.get("outcome"), str):
            raise ValueError("resolved.outcome must be a string")
    else:
        raise ValueError("unknown record type")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


class IntentJournal:
    def __init__(self, name: str, directory: Optional[Path] = None):
        self.name = name
        self.dir = Path(directory) if directory is not None else data_dir() / "intent"
        self.path = self.dir / f"{name}.jsonl"
        self.lock_path = self.dir / f"{name}.lock"
        self._mutex = threading.RLock()
        self._fd: Optional[int] = None
        self._lock_fd: Optional[int] = None
        self._open: Dict[int, Dict[str, Any]] = {}
        self._next_id = 1
        self._dirty = False
        self.fatal: Optional[str] = None   # refuses the store (corrupt, lock held, cannot open)
        self.broken: Optional[str] = None  # refuses mutations (failed repair, inode mismatch)
        self.evidence: Dict[int, Dict[str, Any]] = {}
        self._stop = threading.Event()
        self._timer: Optional[threading.Thread] = None
        self._opened = False

    # ------------------------------------------------------------------ open

    def open(self) -> "IntentJournal":
        with self._mutex:
            if self._opened:
                return self
            self._opened = True
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                lock_fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o644)
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(lock_fd)
                    raise IntentJournalError(
                        f"{self.lock_path} is held by another process: this process is not the journal's writer"
                    )
                self._lock_fd = lock_fd
                self._replay()
                self._fd = os.open(str(self.path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
                _fsync_dir(self.dir)
            except IntentJournalError as exc:
                self.fatal = str(exc)
                logger.error("intent journal %s refused: %s", self.path, exc)
                return self
            except OSError as exc:
                self.fatal = f"{self.path}: cannot open: {exc}"
                logger.error("intent journal %s: %s", self.path, exc)
                return self
            self._timer = threading.Thread(target=self._fsync_loop, name=f"memora-intent-fsync-{self.name}",
                                           daemon=True)
            self._timer.start()
            return self

    def _replay(self) -> None:
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            return
        end = data.rfind(b"\n") + 1
        complete, tail = data[:end], data[end:]
        offset = 0
        for raw in complete.split(b"\n")[:-1]:
            try:
                rec = json.loads(raw.decode("utf-8"))
                _validate_record(rec)
            except (ValueError, UnicodeDecodeError, TypeError, KeyError) as exc:
                raise IntentJournalError(
                    f"{self.path}: malformed record at byte offset {offset} (not the final line): {exc}; "
                    "refusing to start this store (later bytes are never discarded)"
                )
            self._apply(rec)
            offset += len(raw) + 1
        if tail:
            logger.warning("intent journal %s: dropping a torn final line (%d bytes) at offset %d",
                           self.path, len(tail), end)
            fd = os.open(str(self.path), os.O_WRONLY)
            try:
                os.ftruncate(fd, end)
                os.fsync(fd)
            finally:
                os.close(fd)

    def _apply(self, rec: Dict[str, Any]) -> None:  # rec passed _validate_record
        kind = rec["type"]
        if kind == "meta":
            self._next_id = max(self._next_id, int(rec.get("next_id", 1)))
        elif kind == "intent":
            iid = int(rec["id"])
            self._open[iid] = rec
            self._next_id = max(self._next_id, iid + 1)
        else:
            self._open.pop(int(rec["id"]), None)

    # ---------------------------------------------------------------- status

    def status(self) -> Tuple[List[int], Optional[str]]:
        with self._mutex:
            return list(self._open), (self.fatal or self.broken)

    def open_intents(self) -> List[Dict[str, Any]]:
        with self._mutex:
            return [dict(v) for v in self._open.values()]

    # ---------------------------------------------------------------- writes

    def _check_inode(self) -> None:
        st_fd = os.fstat(self._fd)
        try:
            st_path = os.stat(self.path)
        except FileNotFoundError:
            st_path = None
        if st_path is None or (st_fd.st_dev, st_fd.st_ino) != (st_path.st_dev, st_path.st_ino):
            self.broken = f"{self.path}: the open journal fd no longer names the journal path (inode mismatch)"
            raise IntentJournalError(self.broken)

    def append_intent(self, sql: str, params: Optional[Sequence[Any]], *, target: Optional[str],
                      keys: Optional[Dict[str, Any]], post_state: Optional[Dict[str, Any]]) -> int:
        with self._mutex:
            if self.fatal or self.broken:
                raise IntentJournalError(self.fatal or self.broken)
            if self._fd is None:
                raise IntentJournalError(f"{self.path}: journal is not open")
            self._check_inode()
            iid = self._next_id
            rec = {"type": "intent", "id": iid, "sql": sql, "params_sha256": params_digest(params),
                   "target": target, "keys": keys, "post_state": post_state, "sent_at": time.time()}
            line = (json.dumps(rec, default=str, separators=(",", ":")) + "\n").encode("utf-8")
            try:
                _write_all(self._fd, line)
                os.fsync(self._fd)
            except OSError as exc:
                self._repair()
                raise IntentJournalError(f"{self.path}: could not record the write intent: {exc}") from exc
            self._next_id = iid + 1
            self._open[iid] = rec
            self._dirty = False  # this fsync also made any earlier resolution durable
            self._maybe_compact()
            if self.broken:
                # A compaction that failed after its rename broke the journal
                # (review 7584 P1-3): this intent must NOT be sent.
                self.abandon(iid, "not-sent")
                raise IntentJournalError(f"not sent: {self.broken}")
            return iid

    def resolve(self, iid: int, outcome: str, *, durable: bool = False,
                extra: Optional[Dict[str, Any]] = None) -> bool:
        """Append a resolution. Never raises for I/O: a failed append leaves
        the intent open (an extra unsafe record, which is safe)."""
        with self._mutex:
            if iid not in self._open:
                return False
            if self._fd is None or self.fatal or self.broken:
                return False
            rec = {"type": "resolved", "id": iid, "outcome": outcome, "at": time.time()}
            if extra:
                rec.update(extra)
            line = (json.dumps(rec, default=str, separators=(",", ":")) + "\n").encode("utf-8")
            try:
                _write_all(self._fd, line)
                if durable:
                    os.fsync(self._fd)
                else:
                    self._dirty = True
            except OSError as exc:
                logger.error("intent journal %s: resolution of %d not recorded (%s); it stays open",
                             self.path, iid, exc)
                self._repair()
                return False
            self._open.pop(iid, None)
            self.evidence.pop(iid, None)
            return True

    def abandon(self, iid: int, outcome: str = "not-sent") -> None:
        """An intent whose request was never sent. Dropped from the open set
        (nothing is outstanding on D1), and a resolution is appended when the
        journal can still take one. If it cannot (the journal is broken), the
        on-disk intent may reappear as open after a restart: that is safe --
        an open intent only blocks migration steps until an operator accepts
        it, and it was never sent."""
        with self._mutex:
            self._open.pop(iid, None)
            self.evidence.pop(iid, None)
            if self._fd is None or self.fatal or self.broken:
                return
            line = (json.dumps({"type": "resolved", "id": iid, "outcome": outcome, "at": time.time()},
                               separators=(",", ":")) + "\n").encode("utf-8")
            try:
                _write_all(self._fd, line)
                self._dirty = True
            except OSError as exc:
                logger.error("intent journal %s: %s resolution of %d not recorded (%s)", self.path, outcome, iid, exc)
                self._repair()

    def _repair(self) -> None:
        """Truncate to the last complete line and fsync file and directory.
        Called with the mutex held; failure marks the journal broken."""
        try:
            data = self.path.read_bytes()
            keep = data.rfind(b"\n") + 1
            os.ftruncate(self._fd, keep)
            os.fsync(self._fd)
            _fsync_dir(self.dir)
            logger.warning("intent journal %s repaired: truncated to %d bytes", self.path, keep)
        except OSError as exc:
            self.broken = f"{self.path}: repair after a failed write failed ({exc}); an operator must repair it"
            logger.error("intent journal: %s", self.broken)

    def _fsync_loop(self) -> None:
        while not self._stop.wait(RESOLUTION_FSYNC_INTERVAL_S):
            with self._mutex:
                if self._dirty and self._fd is not None and not self.broken:
                    try:
                        os.fsync(self._fd)
                        self._dirty = False
                    except OSError as exc:
                        logger.error("intent journal %s: lazy fsync failed: %s", self.path, exc)
                        self._repair()

    # ------------------------------------------------------------ compaction

    def _maybe_compact(self) -> None:
        try:
            size = os.fstat(self._fd).st_size
        except OSError:
            return
        if size > _compact_threshold():
            self.compact()

    def compact(self) -> None:
        """Replace the journal with the open intents only. Holds the mutex
        from snapshot to the reopened fd, so no append can land on the old
        inode (plan round-11 P0)."""
        with self._mutex:
            if self._fd is None or self.broken or self.fatal:
                return
            tmp = self.dir / f"{self.name}.jsonl.compact"
            lines = [json.dumps({"type": "meta", "next_id": self._next_id}, separators=(",", ":"))]
            lines += [json.dumps(r, default=str, separators=(",", ":")) for r in self._open.values()]
            try:
                fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
                try:
                    _write_all(fd, ("\n".join(lines) + "\n").encode("utf-8"))
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError as exc:
                logger.error("intent journal %s: compaction skipped: %s", self.path, exc)
                try:
                    tmp.unlink()
                except OSError:
                    pass
                return
            try:
                os.replace(tmp, self.path)
                _fsync_dir(self.dir)
                old, self._fd = self._fd, None
                os.close(old)
                self._fd = os.open(str(self.path), os.O_WRONLY | os.O_APPEND)
                self._dirty = False
            except OSError as exc:
                self.broken = f"{self.path}: compaction failed after the rename ({exc})"
                logger.error("intent journal: %s", self.broken)

    # ----------------------------------------------------------------- close

    def close(self) -> None:
        self._stop.set()
        with self._mutex:
            if self._fd is not None:
                try:
                    os.fsync(self._fd)
                except OSError:
                    pass
                os.close(self._fd)
                self._fd = None
            if self._lock_fd is not None:
                os.close(self._lock_fd)  # releases the flock
                self._lock_fd = None


_JOURNALS: Dict[str, IntentJournal] = {}
_JOURNALS_GUARD = threading.Lock()


def journal_for(name: str) -> IntentJournal:
    """The process's one journal for store `name` (opened on first use)."""
    directory = data_dir() / "intent"
    key = os.path.realpath(str(directory / f"{name}.lock"))
    with _JOURNALS_GUARD:
        j = _JOURNALS.get(key)
        if j is None:
            j = _JOURNALS[key] = IntentJournal(name, directory)
    return j.open()


def all_journals() -> List[IntentJournal]:
    with _JOURNALS_GUARD:
        return list(_JOURNALS.values())


def _reset_for_tests() -> None:
    with _JOURNALS_GUARD:
        for j in _JOURNALS.values():
            j.close()
        _JOURNALS.clear()
