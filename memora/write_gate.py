"""Per-store write admission gate: the freeze barrier.

docs/local-primary-implementation.md §1 "Freeze: a quiescence barrier".

Every mutating statement on a gated connection (a local writer, or a D1
connection handed out by D1Backend) enters the store's gate before it runs
and leaves it once its outcome is known. `freeze()` closes admission
atomically, waits for the in-flight set to drain, and only then reports
`frozen`; on timeout it reopens the gate and raises FreezeTimeout, so the
step that asked for the freeze does not run.

The gate is in-process: a freeze is only a barrier while memora-all is the
sole writer of the store (plan §6 F1-F6).

States: open | draining | frozen | frozen-unsafe. `frozen-unsafe` means
frozen with open D1 write intents (memora/intent_journal.py), or a journal
that cannot be trusted; every export/seed/recheck/repoint step refuses it.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


class StoreReadOnlyError(RuntimeError):
    """A mutation was refused: the store is frozen, draining, or its write
    journal is broken."""


class FreezeTimeout(RuntimeError):
    """freeze() could not drain in time. The gate has been reopened."""

    def __init__(self, name: str, in_flight: List[Dict[str, Any]]):
        super().__init__(f"freeze of {name!r} timed out with {len(in_flight)} write(s) in flight")
        self.in_flight = in_flight


def data_dir() -> Path:
    """Where the gate and journal keep their state (/data in the container)."""
    return Path(os.getenv("MEMORA_DATA_DIR", "/data"))


def freeze_file(name: str) -> Path:
    return data_dir() / "freeze" / name


def _readonly_names() -> set:
    raw = os.getenv("MEMORA_READONLY_DBS", "")
    return {n.strip() for n in raw.split(",") if n.strip()}


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass
class _GateToken:
    desc: str
    thread: int
    started: float = field(default_factory=time.monotonic)
    released: bool = False

    def describe(self) -> Dict[str, Any]:
        return {"statement": self.desc, "thread": self.thread,
                "age_seconds": round(time.monotonic() - self.started, 3)}


class _WriteGate:
    def __init__(self, key: str, name: Optional[str] = None):
        self.key = key
        self.name = name
        self._cond = threading.Condition()
        self._frozen = False
        self._draining = False
        self._in_flight: Dict[int, _GateToken] = {}
        # Supplied by the store's intent journal (D1 stores only):
        # () -> (open intent ids, broken reason or None).
        self.journal_status: Optional[Callable[[], tuple]] = None

    # ------------------------------------------------------------ admission

    def enter(self, desc: str) -> _GateToken:
        with self._cond:
            if self._frozen or self._draining:
                raise StoreReadOnlyError(
                    f"store {self.name or self.key!r} is {self.state_locked()}: writes are refused"
                )
            broken = self._journal_broken()
            if broken:
                raise StoreReadOnlyError(f"store {self.name or self.key!r}: write journal unusable: {broken}")
            tok = _GateToken(desc, threading.get_ident())
            self._in_flight[id(tok)] = tok
            return tok

    def leave(self, token: _GateToken) -> None:
        with self._cond:
            if token.released:
                return
            token.released = True
            self._in_flight.pop(id(token), None)
            if not self._in_flight:
                self._cond.notify_all()

    # --------------------------------------------------------------- freeze

    def freeze(self, timeout_s: float = 30.0) -> None:
        deadline = time.monotonic() + timeout_s
        with self._cond:
            if self._frozen:
                return
            self._draining = True
            while self._in_flight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    stuck = [t.describe() for t in self._in_flight.values()]
                    self._draining = False  # abort: reopen admission
                    self._cond.notify_all()
                    raise FreezeTimeout(self.name or self.key, stuck)
                self._cond.wait(remaining)
            self._draining = False
            self._frozen = True

    def freeze_at_start(self) -> None:
        """Start frozen (persisted freeze file or MEMORA_READONLY_DBS):
        nothing can be in flight before the first connection."""
        with self._cond:
            self._frozen = True

    def thaw(self) -> None:
        with self._cond:
            self._frozen = False
            self._draining = False
            self._cond.notify_all()

    # --------------------------------------------------------------- status

    def _journal_broken(self) -> Optional[str]:
        if self.journal_status is None:
            return None
        return self.journal_status()[1]

    def _open_intents(self) -> List[int]:
        if self.journal_status is None:
            return []
        return list(self.journal_status()[0])

    def state_locked(self) -> str:
        if self._journal_broken():
            return "frozen-unsafe"  # refusing every mutation until an operator repairs it
        if self._draining:
            return "draining"
        if self._frozen:
            if self._open_intents() or self._journal_broken():
                return "frozen-unsafe"
            return "frozen"
        return "open"

    @property
    def state(self) -> str:
        with self._cond:
            return self.state_locked()

    def status(self) -> Dict[str, Any]:
        with self._cond:
            out: Dict[str, Any] = {
                "state": self.state_locked(),
                "in_flight": len(self._in_flight),
            }
            if self.journal_status is not None:
                ids, broken = self.journal_status()
                out["open_intents"] = sorted(ids)
                if broken:
                    out["journal_error"] = broken
            return out

    def in_flight(self) -> List[Dict[str, Any]]:
        with self._cond:
            return [t.describe() for t in self._in_flight.values()]


_GATES: Dict[str, _WriteGate] = {}
_GATES_GUARD = threading.Lock()


def gate_for_key(key: str, name: Optional[str] = None) -> _WriteGate:
    """The process-wide gate for one store identity (a local file's real path,
    or a D1 database id). Attaching a registry name applies the persisted
    freeze file and MEMORA_READONLY_DBS the first time the name is seen."""
    with _GATES_GUARD:
        gate = _GATES.get(key)
        if gate is None:
            gate = _GATES[key] = _WriteGate(key, name)
            if name is not None:
                _apply_start_state(gate, name)
        elif name is not None and gate.name is None:
            gate.name = name
            _apply_start_state(gate, name)
        return gate


def _apply_start_state(gate: _WriteGate, name: str) -> None:
    if name in _readonly_names() or freeze_file(name).exists():
        gate.freeze_at_start()


def write_gate(name: str) -> _WriteGate:
    """The gate of a registry store NAME."""
    from .storage import backend_for

    backend = backend_for(name)
    return backend.write_gate()


def persist_freeze(name: str) -> None:
    """Write /data/freeze/<name> (the persisted intent) durably."""
    path = freeze_file(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, f"{time.time():.3f}\n".encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(path.parent)


def remove_freeze(name: str) -> None:
    path = freeze_file(name)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_dir(path.parent)


def fence_live_primaries() -> Dict[str, Optional[str]]:
    """Take the primary lock of every live-primary registry store BEFORE any
    writer or prewarm open (review 7584 P1-1). {name: None} when held, or
    {name: reason} when another process holds it -- that store is then
    refused in this process (every open raises; health says why)."""
    from .storage import backend_for, database_registry
    from .backends import StoreLockedError

    out: Dict[str, Optional[str]] = {}
    for name in database_registry():
        backend = backend_for(name)
        if not getattr(backend, "live_primary", False):
            continue
        try:
            backend.fence()
            out[name] = None
        except StoreLockedError as exc:
            out[name] = str(exc)
    return out


def initialize_registry_gates() -> Dict[str, Dict[str, Any]]:
    """Server startup (plan §1): create every registry store's gate (applying
    the persisted freeze file and MEMORA_READONLY_DBS), open and replay each
    d1:// store's intent journal. Live primaries are fenced earlier, by
    fence_live_primaries() (server.main, before any open). Returns a
    per-store summary; a store whose journal cannot be used gives read-only
    connections in this process."""
    import logging

    from .storage import backend_for, database_registry

    log = logging.getLogger(__name__)
    summary: Dict[str, Dict[str, Any]] = {}
    for name in database_registry():
        try:
            backend = backend_for(name)
            if not hasattr(backend, "write_gate"):
                continue
            if getattr(backend, "refused_reason", None):
                summary[name] = {"state": "refused", "error": backend.refused_reason}
                continue
            gate = backend.write_gate()
            summary[name] = gate.status()
        except Exception as exc:
            log.error("write gate for %s: %s", name, exc)
            summary[name] = {"state": "unknown", "error": f"{type(exc).__name__}: {exc}"}
    return summary


def _reset_for_tests() -> None:
    with _GATES_GUARD:
        _GATES.clear()
