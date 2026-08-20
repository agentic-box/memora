"""Event-loop must keep serving reads while a write is in flight.

Named red EventLoopBlockedByWrite: a 400ms stall inside add_memory (stand-in
for the embedding / D1 hop) used to delay an overlapping memory_stats by
the same amount because both ran on the single asyncio thread.
"""
from __future__ import annotations

import asyncio
import time

import memora.server as server


WRITE_STALL_S = 0.40
READ_BOUND_S = 0.25


def test_slow_write_does_not_block_fast_read(local_db, monkeypatch):
    # server.py does `from .storage import add_memory`; patch the bound name.
    real_add = server.add_memory

    def slow_add(*args, **kwargs):
        time.sleep(WRITE_STALL_S)
        return real_add(*args, **kwargs)

    monkeypatch.setattr(server, "add_memory", slow_add)

    async def overlap():
        t0 = time.monotonic()
        read_at = {}

        async def writer():
            return await server.memory_create(
                content="Event loop stall write extra words for probe",
                tags=["test"],
                suggest_similar=False,
            )

        async def reader():
            await asyncio.sleep(0.05)
            result = await server.memory_stats()
            read_at["elapsed"] = time.monotonic() - t0
            return result

        written, stats = await asyncio.gather(writer(), reader())
        return written, stats, read_at["elapsed"]

    written, stats, read_elapsed = asyncio.run(overlap())
    assert written.get("memory", {}).get("id"), written
    assert "total_memories" in stats or stats.get("error") is None
    assert read_elapsed < READ_BOUND_S, (
        "EventLoopBlockedByWrite: fast read waited "
        f"{read_elapsed:.3f}s behind a {WRITE_STALL_S:.2f}s write"
    )
