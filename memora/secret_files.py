"""Credentials read from mounted files (REL1, review 7758).

Each credential below can be given as FOO (an environment value, as before)
or as FOO_FILE, the path of a file holding it. A deploy mounts the token
directory READ-ONLY into the container and sets only the *_FILE paths, so no
token value is in the container's configuration (`docker inspect`).

The file rule, adapted from local_primary.load_credential_file for a
container mount:
  * the path is absolute;
  * lstat: not a symlink (refused, not followed), a regular file;
  * no group or other permission bits (mode & 0o077 == 0; 0600 or 0400);
  * readable by this process. The OWNER is not checked: inside a container
    the host uid maps to another uid (root under docker, the rootless user's
    mapping under podman), so "owned by this user" cannot hold;
  * the content, whitespace-stripped, is not empty.
FOO and FOO_FILE both set is refused (which one wins would be a guess), and
so is CLOUDFLARE_API_TOKEN_FILE with its alias CF_API_TOKEN. A refusal names
the variable and the path, never the value; values are never logged.

The files are read on use, not cached: the server checks them all at
startup (check_secret_files, a refusal stops it) and the D1 backend, the
readers and the replicator read them when they build a connection.
"""

from __future__ import annotations

import os
import stat
from typing import Dict

FILE_BACKED = ("MEMORA_D1_READ_TOKEN", "MEMORA_D1_REPLICATOR_TOKEN", "CLOUDFLARE_API_TOKEN",
               "MEMORA_GRAPH_TOKEN")  # the graph UI's credential (G1)
ALIASES = {"CLOUDFLARE_API_TOKEN": ("CF_API_TOKEN",)}


class SecretFileError(RuntimeError):
    """A *_FILE credential is unusable, or given twice. Fatal at startup."""


def read_secret_file(var: str, path: str) -> str:
    """The credential in PATH, under the file rule above."""
    if not os.path.isabs(path):
        raise SecretFileError(f"{var}: {path!r} is not an absolute path")
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise SecretFileError(f"{var}: {path}: {exc.strerror or exc}")
    if stat.S_ISLNK(st.st_mode):
        raise SecretFileError(f"{var}: {path} is a symlink")
    if not stat.S_ISREG(st.st_mode):
        raise SecretFileError(f"{var}: {path} is not a regular file")
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise SecretFileError(
            f"{var}: {path} is group/other accessible (mode {oct(stat.S_IMODE(st.st_mode))}; needs 0600 or 0400)")
    try:
        with open(path, encoding="utf-8") as fh:
            value = fh.read().strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise SecretFileError(f"{var}: {path} is not readable by this process ({type(exc).__name__})")
    if not value:
        raise SecretFileError(f"{var}: {path} is empty")
    return value


def secret(var: str) -> str:
    """VAR's value: from VAR_FILE when set, else the environment ("" when
    neither). Raises SecretFileError when both are set or the file fails."""
    path = os.environ.get(f"{var}_FILE", "")
    if not path:
        return os.environ.get(var, "").strip()
    clashing = [v for v in (var, *ALIASES.get(var, ())) if os.environ.get(v)]
    if clashing:
        raise SecretFileError(f"{var}_FILE and {' and '.join(clashing)} are both set; set only one")
    return read_secret_file(var, path)


def check_secret_files() -> Dict[str, bool]:
    """Every file-backed credential, checked at server startup: {var: set?}.
    The first unusable one raises SecretFileError."""
    return {var: bool(secret(var)) for var in FILE_BACKED}
