#!/usr/bin/env python3
"""Reproduce (and after the fix, refute) event-loop head-of-line blocking.

The container pilot measured idle SQLite reads at 4-13ms, then 2-3.5s when
issued during concurrent writes — every reader releasing with the writers.
That is a blocked asyncio loop, not SQLite contention.

This script drives the MCP tool coroutines directly (the same loop the
server uses). A stall inside add_memory stands in for the embedding HTTP
hop / D1 round-trip so the numbers are stable without Cloudflare.

Usage (from a memora checkout, with the package on PYTHONPATH):

    python3 scripts/bench_event_loop.py
    python3 scripts/bench_event_loop.py --stall-ms 500 --writes 6 --reads 6

On b308c85, read median tracks the write stall (seconds). After the
to_thread offload, reads stay idle-order (tens of ms) while writes run.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

# Allow `python3 scripts/bench_event_loop.py` from a worktree.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("MEMORA_EMBEDDING_MODEL", "tfidf")
os.environ.setdefault("MEMORA_LLM_ENABLED", "false")
os.environ.setdefault("MEMORA_ALLOW_ANY_TAG", "1")


def _install_stall(stall_s: float) -> None:
    import memora.server as server

    real = server.add_memory

    def stalled(*args, **kwargs):
        if stall_s > 0:
            time.sleep(stall_s)
        return real(*args, **kwargs)

    server.add_memory = stalled


def _setup_db() -> Path:
    import memora
    import memora.storage as storage
    from memora.backends import LocalSQLiteBackend

    tmp = Path(tempfile.mkdtemp(prefix="memora-loop-bench-"))
    backend = LocalSQLiteBackend(tmp / "bench.db")
    storage.STORAGE_BACKEND = backend
    storage.EMBEDDING_MODEL = "tfidf"
    memora.TAG_WHITELIST = set()
    with storage.connect() as conn:
        conn.commit()
    return tmp


async def _idle_reads(n: int) -> list[float]:
    import memora.server as server

    times = []
    for _ in range(n):
        t0 = time.monotonic()
        await server.memory_stats()
        times.append(time.monotonic() - t0)
    return times


async def _burst(n_writes: int, n_reads: int) -> dict:
    import memora.server as server

    write_times: list[float] = []
    read_times: list[float] = []
    wall0 = time.monotonic()

    async def writer(i: int) -> None:
        t0 = time.monotonic()
        await server.memory_create(
            content=f"Bench write {i} extra words for event loop probe",
            tags=["bench"],
            suggest_similar=False,
        )
        write_times.append(time.monotonic() - t0)

    async def reader(i: int) -> None:
        await asyncio.sleep(0.02)
        # Completion from burst start, not service time: queueing behind a
        # blocked loop is the pathology (service time is always small).
        await server.memory_stats()
        read_times.append(time.monotonic() - wall0)

    await asyncio.gather(
        *[writer(i) for i in range(n_writes)],
        *[reader(i) for i in range(n_reads)],
    )
    return {
        "wall": time.monotonic() - wall0,
        "writes": write_times,
        "reads": read_times,
    }


def _fmt(xs: list[float]) -> str:
    if not xs:
        return "n/a"
    ms = [x * 1000 for x in xs]
    return "min=%.1f median=%.1f max=%.1f ms (n=%d)" % (
        min(ms),
        statistics.median(ms),
        max(ms),
        len(ms),
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--stall-ms", type=float, default=400.0)
    p.add_argument("--writes", type=int, default=6)
    p.add_argument("--reads", type=int, default=6)
    p.add_argument("--idle", type=int, default=5)
    args = p.parse_args()

    _setup_db()
    stall_s = max(0.0, args.stall_ms / 1000.0)
    _install_stall(stall_s)

    idle = asyncio.run(_idle_reads(args.idle))
    burst = asyncio.run(_burst(args.writes, args.reads))

    print("memora event-loop bench")
    print("  stall_ms=%.0f writes=%d reads=%d" % (args.stall_ms, args.writes, args.reads))
    print("  idle reads :", _fmt(idle))
    print("  burst writes:", _fmt(burst["writes"]))
    print("  burst reads :", _fmt(burst["reads"]))
    print("  burst wall  : %.1f ms" % (burst["wall"] * 1000))
    read_med = statistics.median(burst["reads"]) * 1000
    idle_med = statistics.median(idle) * 1000
    print("  read_median / idle_median = %.1fx" % (read_med / idle_med if idle_med else float("inf")))
    if stall_s > 0 and read_med > stall_s * 1000 * 0.6:
        print(
            "  verdict: READS STALLED with the write hop "
            "(event loop blocked — expected on b308c85)"
        )
        return 2
    print("  verdict: reads stayed near idle while writes ran")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
