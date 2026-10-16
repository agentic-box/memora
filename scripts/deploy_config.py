#!/usr/bin/env python3
"""Read the operator's deploy configuration (instances/deploy.env).

The real infrastructure identifiers -- the deploy host, the address the
graph UI is published on, the embedding hosts, the store/project map --
live ONLY in this git-ignored file, never in the (public) repository.
instances/deploy.env.example documents every key with placeholders.

  scripts/deploy_config.py FILE KEY [KEY ...]
  scripts/deploy_config.py --optional FILE KEY [KEY ...]

prints the requested values in order, each terminated by NUL, and exits 0.
It prints nothing and exits 2 when the file is missing or unreadable, a line
is malformed, a key is unknown, or a requested key is absent or empty -- so
a caller that counts the values it read can never run on a guessed default.
With --optional, only the OPTIONAL_KEYS may be requested and an absent key
prints an empty value (the caller applies its own default).

Format: one KEY=VALUE per line; blank lines and lines starting with # are
ignored; a value may be wrapped in one pair of matching single or double
quotes, which are removed. Nothing is expanded or executed.

Standard library only (scripts/audit_configs.py imports it on the Mac).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, List

KEYS = (
    "DEPLOY_HOST",          # ssh name of the host that runs memora-all
    "DEPLOY_GRAPH_BIND",    # the host address the graph UI is published on
    "DEPLOY_REPO",          # the memora checkout on DEPLOY_HOST
    "DEPLOY_BACKUP_DIR",    # cutover_store.sh: where all.env backups go (outside the repo)
    "MEMORA_PROJECTS",      # JSON: store name -> list of project names
    "EMBEDDING_OLD_URL",    # scripts/switch-embedding-host.sh only
    "EMBEDDING_NEW_URL",    # scripts/switch-embedding-host.sh only
    # deploy-memora-all.sh only, optional: token file NAMES inside DEPLOY_SECRETS_DIR
    # (the environment variable of the same name wins; absent = the script's default)
    "DEPLOY_CLOUDFLARE_TOKEN_FILE",
    "DEPLOY_D1_READ_TOKEN_FILE",
    "DEPLOY_D1_REPLICATOR_TOKEN_FILE",
    "DEPLOY_GRAPH_TOKEN_FILE",
    "DEPLOY_API_TOKENS_FILE",
    "DEPLOY_API_SMOKE_TOKEN_FILE",
)
OPTIONAL_KEYS = KEYS[KEYS.index("DEPLOY_CLOUDFLARE_TOKEN_FILE"):]
_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


class ConfigError(Exception):
    pass


def load(path: Path) -> Dict[str, str]:
    """Parse FILE; raise ConfigError on any problem."""
    try:
        text = Path(path).read_text()
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror or exc}") from None
    out: Dict[str, str] = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            raise ConfigError(f"{path}:{n}: not KEY=VALUE")
        key, value = m.group(1), m.group(2).strip()
        if key not in KEYS:
            raise ConfigError(f"{path}:{n}: unknown key {key} (known: {', '.join(KEYS)})")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key in out:
            raise ConfigError(f"{path}:{n}: {key} is set twice")
        out[key] = value
    return out


def require(cfg: Dict[str, str], keys: List[str], path: Path) -> List[str]:
    missing = [k for k in keys if not cfg.get(k)]
    if missing:
        raise ConfigError(f"{path}: missing {', '.join(missing)} (see instances/deploy.env.example)")
    return [cfg[k] for k in keys]


def main(argv: List[str]) -> int:
    optional = len(argv) > 1 and argv[1] == "--optional"
    if optional:
        argv = argv[:1] + argv[2:]
    if len(argv) < 3:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print("usage: deploy_config.py FILE KEY [KEY ...]", file=sys.stderr)
        return 2
    path = Path(argv[1])
    try:
        unknown = [k for k in argv[2:] if k not in KEYS]
        if unknown:
            raise ConfigError(f"unknown key requested: {', '.join(unknown)}")
        if optional:
            bad = [k for k in argv[2:] if k not in OPTIONAL_KEYS]
            if bad:
                raise ConfigError(f"not an optional key: {', '.join(bad)}")
            cfg = load(path)
            values = [cfg.get(k, "") for k in argv[2:]]
        else:
            values = require(load(path), argv[2:], path)
    except ConfigError as exc:
        print(f"deploy config: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write("".join(v + "\0" for v in values))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
