"""memora #1011: clmux's probe scheduler dispatches several workspaces at
once and their completions stay phase-aligned, so a CACHE_TTL expiry in
memora_proxy.resolve() arrived as a BURST of concurrent callers. Each one
independently saw a stale cache and launched its own `container list`,
producing clmux DOWN transitions at duration_ms=801-802 -- the proxy's own
subprocess, not memora, ate the probe's 800ms budget.

These assert on the two properties the fix claims:

1. SINGLE-FLIGHT: N concurrent callers needing a fresh lookup share exactly
   ONE `container list` subprocess, not N.
2. STALE-WHILE-REVALIDATE: a known-good cached IP is served immediately
   (never blocks on a subprocess), with a background refresh kicked instead.

Plus a regression test for the property that makes serving stale safe at
all: the forced-fresh retry after a failed connect (ttl=0.0) must never take
the stale path, or a proxy could hand out the very address that just failed
to connect, indefinitely.
"""
from __future__ import annotations

import importlib.util
import pathlib
import threading
import time
from types import SimpleNamespace

import pytest

_spec = importlib.util.spec_from_file_location(
    "memora_proxy",
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "memora_proxy.py",
)
proxy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(proxy)


@pytest.fixture(autouse=True)
def _reset_proxy_state():
    """Module-level singleton state -- reset between tests, not just at import.

    A leftover in-flight `_resolve_event` from a prior test's background
    thread would make the next test's leader claim silently become a
    follower, waiting on a group that has nothing to do with it.
    """
    proxy.NAME = "test-container"
    proxy.STALE_GRACE = 300.0
    proxy._cache.update(ip=None, at=0.0, good_at=0.0)
    proxy._resolve_event = None
    proxy._warn_at.clear()
    yield
    # Let any straggler background thread finish before the next test reuses
    # the module-level lock/event.
    deadline = time.monotonic() + 2.0
    while proxy._resolve_event is not None and time.monotonic() < deadline:
        time.sleep(0.01)


class _FakeRun:
    """`subprocess.run` double for `container list`. Counts calls; can block
    on demand so a test can prove a caller did NOT wait for it."""

    def __init__(self, ip="10.0.0.7", delay=0.0, block: threading.Event | None = None):
        self.ip = ip
        self.delay = delay
        self.block = block
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, cmd, **kwargs):
        with self._lock:
            self.calls += 1
        if self.block is not None:
            self.block.wait(kwargs.get("timeout", 5))
        elif self.delay:
            time.sleep(self.delay)
        stdout = "ID          STATUS   ADDR\n%s   running  %s/24\n" % (proxy.NAME, self.ip)
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def test_concurrent_resolvers_share_one_subprocess_call(monkeypatch):
    """THE claim: 8 concurrent callers past an expired, cold cache produce
    exactly ONE `container list`, and all 8 get its answer."""
    fake = _FakeRun(ip="10.0.0.9", delay=0.05)
    monkeypatch.setattr(proxy.subprocess, "run", fake)

    results = [None] * 8

    def worker(i):
        results[i] = proxy.resolve(ttl=2.0)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert fake.calls == 1, "expected a single shared subprocess call, got %d" % fake.calls
    assert results == ["10.0.0.9"] * 8


def test_stale_ip_served_without_waiting_for_refresh(monkeypatch):
    """THE other claim: an expired-but-known-good IP returns immediately even
    while `container list` is deliberately blocked, and a background refresh
    still lands once it is unblocked."""
    block = threading.Event()
    fake = _FakeRun(ip="10.0.0.55", block=block)
    monkeypatch.setattr(proxy.subprocess, "run", fake)

    now = time.time()
    proxy._cache.update(ip="10.0.0.5", at=now - 100.0, good_at=now - 1.0)

    started = time.monotonic()
    ip = proxy.resolve(ttl=2.0)
    elapsed = time.monotonic() - started

    assert ip == "10.0.0.5", "must serve the stale-but-good IP, not wait for the refresh"
    assert elapsed < 0.5, "resolve() blocked on the subprocess instead of serving stale (%.3fs)" % elapsed

    # The background refresh this call kicked should still be running (or
    # queued to run) -- release it and confirm it lands.
    block.set()
    deadline = time.monotonic() + 2.0
    while proxy._cache["ip"] != "10.0.0.55" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert proxy._cache["ip"] == "10.0.0.55", "background refresh never updated the cache"
    assert fake.calls == 1


def test_stale_path_kicks_at_most_one_background_refresh(monkeypatch):
    """Multiple callers hitting the same stale-but-good cache must not each
    kick their own background refresh -- that is the same herd, one hop later."""
    block = threading.Event()
    fake = _FakeRun(ip="10.0.0.66", block=block)
    monkeypatch.setattr(proxy.subprocess, "run", fake)

    now = time.time()
    proxy._cache.update(ip="10.0.0.5", at=now - 100.0, good_at=now - 1.0)

    results = [None] * 5

    def worker(i):
        results[i] = proxy.resolve(ttl=2.0)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert results == ["10.0.0.5"] * 5
    block.set()
    deadline = time.monotonic() + 2.0
    while proxy._resolve_event is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert fake.calls == 1, "one background refresh should serve every caller in the burst, got %d" % fake.calls


def test_forced_fresh_retry_never_serves_the_stale_ip(monkeypatch):
    """Regression guard for the property that makes stale-serving safe at all:
    ttl=0.0 (handle()'s retry after a failed connect) must get a REAL answer,
    never the address that just failed to connect.
    """
    fake = _FakeRun(ip="10.0.0.100")
    monkeypatch.setattr(proxy.subprocess, "run", fake)

    now = time.time()
    # A good-looking stale entry -- exactly what a naive stale-check would hand back.
    proxy._cache.update(ip="10.0.0.5", at=now - 100.0, good_at=now - 1.0)

    ip = proxy.resolve(ttl=0.0)

    assert ip == "10.0.0.100", "ttl=0.0 must not short-circuit to the stale IP"
    assert fake.calls == 1, "ttl=0.0 must actually invoke a real resolve"


def test_cold_cache_blocks_on_a_real_resolve(monkeypatch):
    """No known-good IP at all -- nothing safe to serve, so this must still
    wait for the real answer rather than inventing a fast path."""
    fake = _FakeRun(ip="10.0.0.200", delay=0.05)
    monkeypatch.setattr(proxy.subprocess, "run", fake)

    ip = proxy.resolve(ttl=2.0)

    assert ip == "10.0.0.200"
    assert fake.calls == 1
