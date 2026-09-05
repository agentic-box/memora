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

CODEX REVIEW ROUND 2 found three races in the single-flight/SWR design
itself, once stale-serving became the routine (not exceptional) case:

1. A follower re-reading the global `_cache` after waking can observe an
   UNRELATED mutation (a stale caller's poisoned connect, or an
   authoritative list-miss) instead of the outcome of the flight it
   actually joined -- the cache is not monotonic.
2. `poison_cache()` was unconditional, so a stale caller's late connect
   failure could delete a background refresh's NEWER answer.
3. `_kick_background_refresh()` claimed the flight before `Thread.start()`;
   a start() failure left the claim installed forever -- deadlock for every
   later caller with nothing safe to serve.

Tests for all three are below, alongside the originals.
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

    A leftover in-flight `_resolve_group` from a prior test's background
    thread would make the next test's leader claim silently become a
    follower, waiting on a group that has nothing to do with it.
    """
    proxy.NAME = "test-container"
    proxy.STALE_GRACE = 300.0
    proxy._cache.update(ip=None, at=0.0, good_at=0.0)
    proxy._resolve_group = None
    proxy._warn_at.clear()
    yield
    # Let any straggler background thread finish before the next test reuses
    # the module-level lock/group.
    deadline = time.monotonic() + 2.0
    while proxy._resolve_group is not None and time.monotonic() < deadline:
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
    while proxy._resolve_group is not None and time.monotonic() < deadline:
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


def test_follower_returns_group_result_despite_racing_cache_mutation(monkeypatch):
    """Blocker 1 (codex round 2): a follower must return the outcome of the
    FLIGHT it joined, not whatever `_cache` holds when it wakes.

    Deliberately single-threaded and deterministic: it drives _claim_or_join
    / _do_resolve / _release_leader by hand instead of racing real threads,
    because the bug this guards against IS a race -- the whole point is that
    the follower's answer must not depend on how the scheduler interleaves
    with an unrelated cache mutation.
    """
    fake = _FakeRun(ip="10.0.0.77")
    monkeypatch.setattr(proxy.subprocess, "run", fake)

    # The "leader" claims the group and actually resolves.
    is_leader, group = proxy._claim_or_join()
    assert is_leader
    group.result = proxy._do_resolve()
    assert group.result == "10.0.0.77"
    assert proxy._cache["ip"] == "10.0.0.77"

    # A "follower" joins the SAME group before it is released -- exactly
    # what _resolve_singleflight() does while a resolve is still in flight.
    is_follower_leader, follower_group = proxy._claim_or_join()
    assert not is_follower_leader
    assert follower_group is group

    # codex's race: something clears the cache AFTER the leader resolved
    # successfully but BEFORE the follower's wait() returns and it reads a
    # result -- an unrelated stale caller's failed connect, or an
    # authoritative list-miss, landing in between.
    proxy._cache.update(ip=None, at=0.0, good_at=0.0)

    proxy._release_leader(group)  # what the real leader does when _do_resolve() returns
    follower_group.event.wait(timeout=2)
    result = follower_group.result if follower_group.exc is None else None

    assert result == "10.0.0.77", (
        "follower must read the GROUP's result, not the raced cache (got %r)" % result
    )


def test_stale_connect_failure_cannot_poison_a_newer_refresh(monkeypatch):
    """Blocker 2 (codex round 2): a background refresh can land a NEWER IP
    while an older caller's connect() is still failing on the STALE address
    it was handed earlier. poison_cache must be a compare-and-clear keyed on
    the address that actually failed, or it deletes the newer answer.
    """
    now = time.time()
    proxy._cache.update(ip="10.0.0.5", at=now - 100.0, good_at=now - 1.0)

    # A background refresh lands Y after this caller already read stale X.
    proxy._cache.update(ip="10.0.0.99", at=time.time(), good_at=time.time())

    # The (late) connect failure is against the STALE address X, not the
    # current Y -- handle() calls poison_cache(ip) with the IP IT tried.
    proxy.poison_cache("10.0.0.5")

    assert proxy._cache["ip"] == "10.0.0.99", (
        "poison_cache cleared a newer refresh it had nothing to do with"
    )


def test_poison_cache_still_clears_when_the_failed_ip_is_current():
    """Control for the above: compare-and-clear must not become a no-op --
    it still clears when the cache genuinely holds the address that failed.
    """
    proxy._cache.update(ip="10.0.0.5", at=time.time(), good_at=time.time())
    proxy.poison_cache("10.0.0.5")
    assert proxy._cache["ip"] is None


def test_background_thread_start_failure_releases_the_group(monkeypatch):
    """Blocker 3 (codex round 2): if Thread.start() raises, the claimed
    group must be released immediately. Otherwise stale calls keep serving
    until STALE_GRACE expires, and every caller after that -- a cold cache,
    or a forced-fresh ttl=0 retry -- waits forever on an Event nobody will
    ever set.
    """

    def _boom(*args, **kwargs):
        raise RuntimeError("injected: thread limit reached")

    monkeypatch.setattr(proxy.threading, "Thread", _boom)

    proxy._kick_background_refresh()

    assert proxy._resolve_group is None, "a failed Thread.start() must release its claim"

    # And the NEXT caller must be able to lead a fresh group -- not join a
    # phantom one that will never finish.
    monkeypatch.undo()  # restore threading.Thread before the real resolve needs it
    fake = _FakeRun(ip="10.0.0.42")
    monkeypatch.setattr(proxy.subprocess, "run", fake)

    ip = proxy.resolve(ttl=0.0)

    assert ip == "10.0.0.42"
    assert fake.calls == 1
