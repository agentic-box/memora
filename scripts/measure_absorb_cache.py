#!/usr/bin/env python3
"""Measure absorb wall-clock before/after the process-local vector cache.

Seeds a realistic corpus (~900 rows) and times absorb_memory for 1, 4 and 10
facts, with the cache cold (first call, full D1-equivalent scan) and warm
(subsequent calls reusing the cached snapshot). Prints per-fact and wall times.

Usage:  ./.venv/bin/python scripts/measure_absorb_cache.py [--rows N]
"""

import argparse
import sys
import time
from pathlib import Path

import memora.storage as storage
from memora.backends import LocalSQLiteBackend

CORPUS_PREFIX = (
    "deployment config alpha bravo charlie delta echo foxtrot golf hotel "
    "india juliet kilo lima mike november oscar papa quebec romeo sierra tango "
    "uniform victor whiskey xray yankee zulu "
)

FACT_POOL = [
    "the build pipeline now uses cache warming on deploy",
    "database connection pool raised to 40 for the ob1 store",
    "rollback procedure documented in the operations runbook",
    "the proxy re-resolves the container address per connection",
    "memory absorb writes a durable inflight row before the first insert",
    "the sidebar shows a yellow NO RESPONSE state on probe timeout",
    "watchdog probe timeout raised from 5s to 20s",
    "container reassigns an IP on every restart, so the proxy is required",
    "supersession forks collapse to one leaf on the next absorb update",
    "the integrity epoch invalidates stale embedding caches on rebuild",
]


def seed(rows: int) -> LocalSQLiteBackend:
    backend = LocalSQLiteBackend(Path("/tmp/memora-cache-bench.db"))
    storage.STORAGE_BACKEND = backend
    storage.EMBEDDING_MODEL = "tfidf"
    with storage.connect() as conn:
        for i in range(rows):
            content = f"{CORPUS_PREFIX} row-{i:04d} extra distinguishing words {i}"
            storage.add_memory(conn, content=content, commit=False)
        # Seed the FACT_POOL as pre-existing memories so absorbing them is a
        # NON-WRITING (duplicate) operation -- the store is unchanged between
        # absorbs, which is where the cache pays off.
        for fact in FACT_POOL:
            storage.add_memory(conn, content=fact, commit=False)
        conn.commit()
    return backend


def time_absorb(conn, facts) -> float:
    start = time.perf_counter()
    storage.absorb_memory(conn, facts)
    return time.perf_counter() - start


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=900)
    args = ap.parse_args()

    backend = seed(args.rows)
    print(f"corpus rows: {args.rows}")
    print(f"backend: {type(backend).__name__}")
    print()

    # Measure the cache's cross-call value. Absorb only reads the corpus once per
    # call (step 2); the cache (step 3) removes that read across calls WHEN the
    # store is unchanged. Absorbing NEW facts writes, which correctly invalidates
    # the cache (the next call must see the new rows), so those show no win.
    # The honest "before/after the cache" comparison for a static corpus is to
    # absorb DUPLICATE facts (no DB write): the first call cold-scans, subsequent
    # calls reuse the cached snapshot.
    for n in [1, 4, 10]:
        facts = FACT_POOL[:n]
        # Cold: clear the cache so the first absorb does a full scan.
        storage._corpus_cache.clear()
        with storage.connect() as conn:
            cold = time_absorb(conn, facts)
        # Warm: the store is unchanged, so subsequent absorbs reuse the cache.
        with storage.connect() as conn:
            warm = time_absorb(conn, facts)
        print(f"{n:>2} facts  cold={cold*1000:8.1f} ms  warm={warm*1000:8.1f} ms  "
              f"speedup={cold/warm if warm else float('inf'):6.1f}x")
    return 0


if __name__ == "__main__":
    sys.exit(main())
