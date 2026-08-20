"""Event-loop must keep serving reads while a write is in flight.

Named red EventLoopBlockedByWrite: a 400ms stall inside add_memory (stand-in
for the embedding / D1 hop) used to delay an overlapping memory_stats by
the same amount because both ran on the single asyncio thread.

Named red PhantomCancelReleasedCooldown: raw asyncio.to_thread lets
CancelledError hit the tool `finally` while the worker is still writing.
Named red UploadBlocksEventLoop: memory_upload_image on the loop delays stats.
Named red DoubleCancelDetachedWorker: await raw fut (not shield-in-a-loop) after one
CancelledError; a second cancel detaches the thread.
Named red WaitReraiseOnPy310: re-raise when Task.uncancel is missing.
Named red CancelHidesWorkerError: cancel then worker raise reports CancelledError.
Named red UploadPathToctou: reopen the pathname after validation.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

import memora.server as server


WRITE_STALL_S = 0.40
READ_BOUND_S = 0.25


async def _wait_flag(flag: threading.Event, timeout: float = 2.0) -> None:
    """Wait for a worker thread flag without blocking the event loop."""
    deadline = time.monotonic() + timeout
    while not flag.is_set():
        if time.monotonic() >= deadline:
            raise AssertionError("worker never reached the gate")
        await asyncio.sleep(0.02)


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


def test_cancelled_write_waits_for_worker_and_reports_commit(local_db, monkeypatch):
    """Cancel must not report failure while the write commits.

    Contract: the worker runs to completion; the tool task returns the
    committed result, cooldown/single-flight stays held until then, and
    the connection is closed.
    """
    started = threading.Event()
    gate = threading.Event()
    closed = []
    real_connect = server.connect
    real_add = server.add_memory
    real_export = server.export_memories

    class _CloseProbe:
        def __init__(self, conn):
            object.__setattr__(self, "_conn", conn)

        def close(self):
            closed.append(True)
            return self._conn.close()

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()

    def tracking_connect(*args, **kwargs):
        return _CloseProbe(real_connect(*args, **kwargs))

    def gated_add(*args, **kwargs):
        started.set()
        assert gate.wait(timeout=5), "worker never released"
        return real_add(*args, **kwargs)

    def gated_export(*args, **kwargs):
        started.set()
        assert gate.wait(timeout=5), "worker never released"
        return real_export(*args, **kwargs)

    monkeypatch.setattr(server, "connect", tracking_connect)
    monkeypatch.setattr(server, "add_memory", gated_add)
    monkeypatch.setattr(server, "export_memories", gated_export)
    server._tool_running.clear()
    server._tool_last_call.clear()

    async def scenario():
        # --- cooldown / single-flight: cancel must not _finish_tool early ---
        started.clear()
        gate.clear()
        export_task = asyncio.create_task(server.memory_export())
        await _wait_flag(started)
        await asyncio.sleep(0.05)
        export_task.cancel()
        await asyncio.sleep(0.05)
        assert server._tool_running.get("memory_export") is True, (
            "PhantomCancelReleasedCooldown: single-flight cleared while the "
            "worker was still gated"
        )
        second = await asyncio.wait_for(server.memory_export(), timeout=1)
        assert "already running" in (second.get("message") or ""), second
        gate.set()
        exported = await export_task
        assert "memories" in exported, (
            "PhantomCancelReportedCancel: cancelled export hid the completed result: "
            f"{exported!r}"
        )
        assert server._tool_running.get("memory_export") is not True

        # --- write commit: cancel reports the created row, connection closed ---
        started.clear()
        gate.clear()
        closed.clear()
        create_task = asyncio.create_task(
            server.memory_create(
                content="Cancelled write still commits extra words",
                tags=["test"],
                suggest_similar=False,
            )
        )
        await _wait_flag(started)
        create_task.cancel()
        await asyncio.sleep(0.05)
        assert closed == [], "connection closed before the gated worker finished"
        gate.set()
        created = await create_task
        mid = (created.get("memory") or {}).get("id")
        assert mid, (
            "PhantomCancelReportedCancel: cancelled create hid the commit: "
            f"{created!r}"
        )
        assert closed, "worker connection was not closed after completion"
        stats = await server.memory_stats()
        assert stats.get("total_memories", 0) >= 1
        return mid

    try:
        asyncio.run(scenario())
    finally:
        gate.set()
        server._tool_running.clear()
        server._tool_last_call.clear()


def test_gated_upload_does_not_block_stats(local_db, monkeypatch, tmp_path):
    from PIL import Image

    png = tmp_path / "probe.png"
    Image.new("RGB", (2, 2), color=(1, 2, 3)).save(png)

    class FakeStore:
        def upload_image(self, **kwargs):
            time.sleep(WRITE_STALL_S)
            return "r2://bucket/probe.png"

    import memora.image_storage as image_storage

    monkeypatch.setattr(image_storage, "get_image_storage_instance", lambda: FakeStore())

    async def overlap():
        t0 = time.monotonic()
        read_at = {}

        async def uploader():
            return await server.memory_upload_image(str(png), memory_id=1)

        async def reader():
            await asyncio.sleep(0.05)
            result = await server.memory_stats()
            read_at["elapsed"] = time.monotonic() - t0
            return result

        uploaded, stats = await asyncio.gather(uploader(), reader())
        return uploaded, stats, read_at["elapsed"]

    uploaded, stats, read_elapsed = asyncio.run(overlap())
    assert uploaded.get("r2_url") == "r2://bucket/probe.png", uploaded
    assert "total_memories" in stats or stats.get("error") is None
    assert read_elapsed < READ_BOUND_S, (
        "UploadBlocksEventLoop: memory_stats waited "
        f"{read_elapsed:.3f}s behind a {WRITE_STALL_S:.2f}s upload"
    )


def test_double_cancel_keeps_worker_result(local_db, monkeypatch):
    """A second CancelledError must not detach the executor thread."""
    started = threading.Event()
    gate = threading.Event()
    real_export = server.export_memories

    def gated_export(*args, **kwargs):
        started.set()
        assert gate.wait(timeout=5), "worker never released"
        return real_export(*args, **kwargs)

    monkeypatch.setattr(server, "export_memories", gated_export)
    server._tool_running.clear()
    server._tool_last_call.clear()

    async def scenario():
        task = asyncio.create_task(server.memory_export())
        await _wait_flag(started)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.05)
        assert server._tool_running.get("memory_export") is True, (
            "DoubleCancelDetachedWorker: single-flight cleared after the second cancel"
        )
        gate.set()
        result = await task
        assert "memories" in result, (
            "DoubleCancelDetachedWorker: second cancel hid the worker result: "
            f"{result!r}"
        )

    try:
        asyncio.run(scenario())
    finally:
        gate.set()
        server._tool_running.clear()
        server._tool_last_call.clear()


def test_cancel_then_worker_error_propagates(local_db, monkeypatch):
    """Worker exception must surface; cooldown stays held until the worker ends."""
    started = threading.Event()
    gate = threading.Event()

    def gated_export(*args, **kwargs):
        started.set()
        assert gate.wait(timeout=5), "worker never released"
        raise RuntimeError("worker-boom")

    monkeypatch.setattr(server, "export_memories", gated_export)
    server._tool_running.clear()
    server._tool_last_call.clear()

    async def scenario():
        task = asyncio.create_task(server.memory_export())
        await _wait_flag(started)
        task.cancel()
        await asyncio.sleep(0.05)
        assert server._tool_running.get("memory_export") is True, (
            "CancelHidesWorkerError: cooldown released before the worker raised"
        )
        gate.set()
        try:
            await task
            pytest.fail("worker should have raised")
        except RuntimeError as exc:
            assert "worker-boom" in str(exc)
        except asyncio.CancelledError:
            pytest.fail(
                "CancelHidesWorkerError: cancel hid the worker exception"
            )
        assert server._tool_running.get("memory_export") is not True, (
            "cooldown stuck after the worker's exception"
        )

    try:
        asyncio.run(scenario())
    finally:
        gate.set()
        server._tool_running.clear()
        server._tool_last_call.clear()


def test_uncancel_missing_does_not_reraise():
    """3.10 has no Task.uncancel; looking it up must not raise CancelledError.

    Named red WaitReraiseOnPy310: `if uncancel is None: raise`.
    """
    class Task310:
        pass

    try:
        server._uncancel_if_available(Task310())
        server._uncancel_if_available(None)
    except BaseException as exc:
        pytest.fail(
            f"WaitReraiseOnPy310: missing uncancel raised {type(exc).__name__}"
        )


def test_upload_validates_the_bytes_it_uploads(monkeypatch, tmp_path):
    """Pathname swap after validation must not change the uploaded payload.

    Named red UploadPathToctou: reopen the path for upload instead of using
    the bytes already read from the O_NOFOLLOW fd.
    """
    from PIL import Image

    import memora.image_storage as image_storage

    good = tmp_path / "good.png"
    evil = tmp_path / "evil.png"
    Image.new("RGB", (2, 2), color=(1, 2, 3)).save(good)
    Image.new("RGB", (8, 8), color=(9, 9, 9)).save(evil)
    good_bytes = good.read_bytes()
    evil_bytes = evil.read_bytes()
    assert good_bytes != evil_bytes

    captured = {}

    class FakeStore:
        def upload_image(self, **kwargs):
            captured["data"] = kwargs.get("image_data")
            return "r2://bucket/good.png"

    monkeypatch.setattr(image_storage, "get_image_storage_instance", lambda: FakeStore())

    def swap():
        good.write_bytes(evil_bytes)

    server._after_upload_bytes_validated = swap
    try:
        result = server._upload_image_blocking(str(good), memory_id=1)
    finally:
        server._after_upload_bytes_validated = None

    assert result.get("r2_url") == "r2://bucket/good.png", result
    assert captured.get("data") == good_bytes, (
        "UploadPathToctou: uploaded bytes came from the swapped pathname"
    )
    assert captured.get("data") != evil_bytes
