"""The /data volume startup check (local-primary plan §8 L2a).

A store whose files live under /data -- a local SQLite store there, and every
d1:// primary, whose write gate (L2) keeps its freeze file and intent journal in
/data/intent/ -- is only as durable as /data itself.
In a container, /data without a mount is the container's own root
filesystem, and it is discarded with the container. The Dockerfile's VOLUME /data turns that into an
ANONYMOUS volume, which a recreate (docker rm + run) silently replaces with a
new empty one. Either way the store would come back empty, or the journal
would lose the open intents that make a freeze unsafe.

So the server refuses to serve such a store unless all of these hold:
  1. MEMORA_DATA_VOLUME is set and is not an anonymous (64-hex) volume name.
     The launchers (scripts/memora-instance.sh, scripts/deploy-memora-all.sh)
     set it to the named volume they mount;
  2. /data is a mount point: its st_dev differs from that of "/";
  3. a probe file can be created, written, fsynced and removed in /data and
     in /data/intent/.
Refusal is per store: the process stays up, the other stores keep serving,
and /health/db names the reason for the refused one.
"""
from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional

DATA_DIR = Path("/data")  # the container path; data_dir() honours MEMORA_DATA_DIR
INTENT_SUBDIR = "intent"
MARKER_ENV = "MEMORA_DATA_VOLUME"

_ANONYMOUS_VOLUME = re.compile(r"[0-9a-f]{64}")

# A d1:// primary keeps its write gate's freeze file and intent journal under
# data_dir() (/data/freeze/<db>, /data/intent/<db>.jsonl; memora/write_gate.py
# and memora/intent_journal.py, L2). A code constant, not an env flag: the
# plan allows no bypass.
D1_PRIMARY_USES_DATA = True


def data_dir() -> Path:
    """The directory the write gate and journal use (MEMORA_DATA_DIR, default
    /data): the one this check must vouch for."""
    from .write_gate import data_dir as gate_data_dir

    return gate_data_dir()


class DataVolumeRefused(RuntimeError):
    """A store that needs /data was refused at startup. str() is the reason."""


def uri_needs_data_volume(uri: str, data_root: Optional[Path] = None, *,
                          d1_primary_uses_data: Optional[bool] = None) -> bool:
    """Whether a store URI keeps state under data_dir.

    d1:// -- D1_PRIMARY_USES_DATA: from L2 on its write gate journals every
    mutation to /data/intent/<db>.jsonl. s3:// -- no: its cache is disposable
    (the object store is the copy). Anything else is a local SQLite path
    (file:// or bare, the forms parse_backend_uri accepts), which needs /data
    when it resolves under data_root (default data_dir()).
    """
    if uri.startswith("d1://"):
        return D1_PRIMARY_USES_DATA if d1_primary_uses_data is None else d1_primary_uses_data
    if uri.startswith("s3://"):
        return False
    path = uri[len("file://"):] if uri.startswith("file://") else uri
    # abspath (lexical), not resolve(): /data may not exist on this host, and
    # a store path's own symlinks are the operator's business, not this check's.
    norm = os.path.abspath(os.path.expanduser(path))
    root = os.path.abspath(str(data_root if data_root is not None else data_dir()))
    return norm == root or norm.startswith(root + os.sep)


def _probe(directory: Path) -> Optional[str]:
    """Create, write, fsync and remove a probe file in directory."""
    probe = directory / f".memora-probe-{os.getpid()}-{secrets.token_hex(4)}"
    try:
        fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as exc:
        return f"{directory} is not writable: cannot create a probe file ({exc.strerror or exc})"
    try:
        try:
            os.write(fd, b"memora data volume probe\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.unlink(probe)
    except OSError as exc:
        try:
            os.unlink(probe)
        except OSError:
            pass
        return f"{directory}: probe write/fsync/remove failed ({exc.strerror or exc})"
    return None


def check_data_volume(
    data_root: Optional[Path] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    root: Path = Path("/"),
    stat: Callable[[Path], os.stat_result] = os.stat,
) -> Optional[str]:
    """None when data_root (default data_dir()) is fit to hold store state, else the reason.

    root and stat exist so a test can model "is a mount" on a host where
    tmp_path shares the root filesystem's device.
    """
    data_root = data_dir() if data_root is None else data_root
    env = os.environ if env is None else env
    marker = (env.get(MARKER_ENV) or "").strip()
    if not marker:
        return (f"{MARKER_ENV} is not set: {data_root} was not mounted by a launcher "
                "(scripts/memora-instance.sh or scripts/deploy-memora-all.sh)")
    if _ANONYMOUS_VOLUME.fullmatch(marker):
        return (f"{MARKER_ENV}={marker} is an anonymous volume; a recreated container "
                "would get a new empty one. Mount a named volume")
    try:
        data_st = stat(data_root)
    except OSError as exc:
        return f"{data_root} does not exist ({exc.strerror or exc})"
    if not os.path.isdir(data_root):
        return f"{data_root} is not a directory"
    try:
        root_st = stat(root)
    except OSError as exc:  # pragma: no cover - "/" always exists
        return f"cannot stat {root} ({exc.strerror or exc})"
    if data_st.st_dev == root_st.st_dev:
        return (f"{data_root} is not a mount point (same device as {root}); its files "
                "would live in the container's root filesystem and die with it")
    reason = _probe(data_root)
    if reason:
        return reason
    intent = data_root / INTENT_SUBDIR
    try:
        intent.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        return f"cannot create {intent} ({exc.strerror or exc})"
    return _probe(intent)


def startup_refusals(
    registry: Mapping[str, str],
    single_uri: Optional[str],
    *,
    data_root: Optional[Path] = None,
    check: Optional[Callable[[], Optional[str]]] = None,
) -> Dict[Optional[str], str]:
    """{store name: reason} for every store that needs the data directory
    while the check fails. With no registry, the single store is keyed None.

    The check runs at most once, and only if some store needs it: a server
    whose stores are all s3:// (or local outside the data directory) has no
    reason to require a mount.
    """
    data_root = data_dir() if data_root is None else data_root
    stores: Dict[Optional[str], str] = dict(registry) if registry else {None: single_uri or ""}
    needing = [n for n, uri in stores.items() if uri and uri_needs_data_volume(uri, data_root)]
    if not needing:
        return {}
    reason = (check or (lambda: check_data_volume(data_root)))()
    if reason is None:
        return {}
    return {n: reason for n in needing}
