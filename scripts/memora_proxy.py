#!/usr/bin/env python3
"""Stable localhost address in front of an Apple `container`.

Apple's `container` reassigns an IP on EVERY start — not just on recreate —
which silently breaks any client config holding a hardcoded URL. MCP clients
only read their config at startup, so without a stable address every container
restart costs N config edits AND N client restarts.

This forwarder listens on a fixed 127.0.0.1 port and re-resolves the
container's current IP for each new connection, so a container restart costs
the client nothing but a reconnect.

Config (env, then optional argv). The LaunchAgent plist is the supported
way to pass these; argv remains for ad-hoc runs:

    MEMORA_PROXY_CONTAINER      container name (argv[1])
    MEMORA_PROXY_LISTEN_PORT    local listen port (argv[2])
    MEMORA_PROXY_LISTEN_HOST    default 127.0.0.1
    MEMORA_PROXY_TARGET_PORT    container-side port (default 8000)
    MEMORA_PROXY_LOG            log file path; empty/unset logs to stderr
    MEMORA_PROXY_MAX_CONN       concurrent spliced connections (default 64)
    MEMORA_PROXY_CONNECT_TIMEOUT  seconds for upstream CONNECT (default 2)
    MEMORA_PROXY_RESOLVE_TIMEOUT  seconds for `container list` (default 2)

Startup ordering: bind the listen socket first, then serve. `container list`
is consulted only on a connection. If the runtime is not up yet, that
connection fails fast and the next one retries; the process does not exit.

This process does NOT start or restart the container. A dead container must
surface as fail-fast connection errors, not as an auto-healed silent start.
"""
from __future__ import annotations

import os
import select
import socket
import subprocess
import sys
import threading
import time
import traceback
from typing import Optional

# ---------------------------------------------------------------------------
# Config: env first (LaunchAgent), argv overrides (ad-hoc).
# ---------------------------------------------------------------------------

def _env(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return float(raw)


NAME = _env("MEMORA_PROXY_CONTAINER", "memora-pilot")
LISTEN_HOST = _env("MEMORA_PROXY_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = _env_int("MEMORA_PROXY_LISTEN_PORT", 8900)
TARGET_PORT = _env_int("MEMORA_PROXY_TARGET_PORT", 8000)
LOG_PATH = os.environ.get("MEMORA_PROXY_LOG", "")
MAX_CONN = _env_int("MEMORA_PROXY_MAX_CONN", 64)
CONNECT_TIMEOUT = _env_float("MEMORA_PROXY_CONNECT_TIMEOUT", 2.0)
RESOLVE_TIMEOUT = _env_float("MEMORA_PROXY_RESOLVE_TIMEOUT", 2.0)
CACHE_TTL = _env_float("MEMORA_PROXY_CACHE_TTL", 2.0)

# Guarded on __main__ so importing this module (tests) does not parse the
# importer's own argv -- pytest's argv[1] is a test path, not a container name.
if __name__ == "__main__":
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        NAME = sys.argv[1]
    if len(sys.argv) > 2:
        LISTEN_PORT = int(sys.argv[2])

# good_at = when we last SAW a real address. Distinct from `at` (last lookup
# attempt) because the stale-grace window must measure the age of the ADDRESS,
# not the age of the failed attempt that keeps refreshing `at`.
_cache = {"ip": None, "at": 0.0, "good_at": 0.0}
_cache_lock = threading.Lock()
_log_lock = threading.Lock()
_warn_at = {}
_slots = threading.BoundedSemaphore(MAX_CONN)
_log_fp = None


def _open_log() -> None:
    global _log_fp
    if not LOG_PATH:
        _log_fp = None
        return
    parent = os.path.dirname(LOG_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    _log_fp = open(LOG_PATH, "a", buffering=1)


def log(level: str, msg: str) -> None:
    line = "%s [%s] %s\n" % (
        time.strftime("%Y-%m-%d %H:%M:%S") + ".%03d" % int((time.time() % 1) * 1000),
        level,
        msg,
    )
    with _log_lock:
        fp = _log_fp if _log_fp is not None else sys.stderr
        try:
            fp.write(line)
            fp.flush()
        except OSError:
            pass


def warn_limited(key: str, msg: str, interval: float = 5.0) -> None:
    now = time.time()
    last = _warn_at.get(key, 0.0)
    if now - last < interval:
        return
    _warn_at[key] = now
    log("WARN", msg)


def parse_ipv4(token: str) -> Optional[str]:
    """Strict dotted-quad; strip CIDR suffix. Rejects 999.1.1.1 and hostnames."""
    host = token.split("/", 1)[0]
    parts = host.split(".")
    if len(parts) != 4:
        return None
    octs = []
    for part in parts:
        if not part.isdigit() or (len(part) > 1 and part.startswith("0")):
            return None
        try:
            value = int(part)
        except ValueError:
            return None
        if value < 0 or value > 255:
            return None
        octs.append(value)
    if octs == [0, 0, 0, 0]:
        return None
    return "%d.%d.%d.%d" % tuple(octs)


# How long a last-known-good IP may be served after the lookup stopped working.
# Fail SAFE first (a healthy container keeps serving), then fail CLOSED, so a
# permanently broken `container list` does not pin a wrong address forever.
STALE_GRACE = float(os.environ.get("MEMORA_PROXY_STALE_GRACE", "300"))


def _serve_stale(reason: str) -> Optional[str]:
    """Lookup unavailable: keep the last known good IP, bounded by STALE_GRACE."""
    with _cache_lock:
        ip = _cache.get("ip")
        good_at = _cache.get("good_at") or 0.0
    age = time.time() - good_at
    if ip and age <= STALE_GRACE:
        warn_limited("stale-serve", "%s; serving last known good %s (%.0fs old)" % (reason, ip, age))
        return ip
    if ip:
        warn_limited("stale-expired", "%s; last known good %s is %.0fs old (> %.0fs), refusing"
                     % (reason, ip, age, STALE_GRACE))
    else:
        warn_limited("stale-none", "%s; no last known good address" % reason)
    with _cache_lock:
        _cache.update(ip=None, at=time.time())
    return None


# ---------------------------------------------------------------------------
# Single-flight resolve (memora #1011). clmux's probe scheduler dispatches
# several workspaces at once and their completions stay phase-aligned, so a
# CACHE_TTL expiry arrives as a BURST of concurrent callers -- observed in
# production as clmux logging nine DOWN transitions at reason=no-status-line,
# duration_ms=801-802, all against this proxy's stale-cache path: six
# `container list` processes launched together, none able to warm the cache
# for the other five before each independently decided it needed a refresh.
# Every caller that needs a fresh lookup now shares exactly one in-flight
# subprocess call: the first to ask becomes the leader and runs it, everyone
# else waits on the same Event and reads the result the leader wrote to
# `_cache` rather than launching a redundant `container list` of its own.
# ---------------------------------------------------------------------------
_resolve_group_lock = threading.Lock()
_resolve_event: Optional[threading.Event] = None


def _claim_or_join() -> tuple[bool, threading.Event]:
    """Atomically become leader of the current resolve group, or get the
    Event to wait on if someone already is.

    The check-and-create happens under ONE lock acquisition so two
    concurrent callers can never both become leader -- that race is exactly
    the herd this exists to remove.
    """
    global _resolve_event
    with _resolve_group_lock:
        event = _resolve_event
        if event is None:
            event = _resolve_event = threading.Event()
            return True, event
        return False, event


def _release_leader(event: threading.Event) -> None:
    global _resolve_event
    with _resolve_group_lock:
        _resolve_event = None
    event.set()


def _resolve_singleflight() -> Optional[str]:
    """Run one real resolve on behalf of every concurrent caller needing one.

    A follower reads `_cache` after waking rather than being handed the
    leader's return value directly: by the time it wakes, a THIRD caller may
    already be leading a NEWER group, and the cache is always at least as
    fresh as the group this follower joined.
    """
    is_leader, event = _claim_or_join()
    if not is_leader:
        event.wait()
        with _cache_lock:
            return _cache["ip"]
    try:
        return _do_resolve()
    finally:
        _release_leader(event)


def _kick_background_refresh() -> None:
    """Start exactly one background resolve if none is already in flight.

    Fire-and-forget, and joins the SAME single-flight group as a foreground
    caller: if one is already resolving -- another kick, or a caller that
    found no usable stale IP -- this is a no-op rather than a second process.
    """
    is_leader, event = _claim_or_join()
    if not is_leader:
        return

    def _run() -> None:
        try:
            _do_resolve()
        finally:
            _release_leader(event)

    threading.Thread(target=_run, name="proxy-bg-resolve", daemon=True).start()


def _do_resolve() -> Optional[str]:
    """Actually run `container list` and update the cache.

    Never called by more than one thread at a time for the same group --
    see _resolve_singleflight / _kick_background_refresh, which serialize
    entry via _claim_or_join.

    Two failure modes that MUST be handled differently (memora #982):

    * The lookup RAN and the name was absent -> the container really is gone.
      Negative-cache it and refuse.
    * The lookup could not run (timeout, exec failure) -> we know NOTHING new.
      Keep serving the last known good IP.

    Conflating them caused a production outage on 2026-08-20: host memory
    pressure made `container list` exceed the timeout, all four proxies
    concluded "container gone", and MCP went dark across every workspace while
    the containers sat there answering on their unchanged addresses.
    """
    try:
        proc = subprocess.run(
            ["container", "list"],
            capture_output=True,
            text=True,
            timeout=RESOLVE_TIMEOUT,
        )
        out = proc.stdout or ""
        if proc.returncode not in (0, None) and not out:
            warn_limited(
                "list-rc",
                "container list rc=%s stderr=%r" % (proc.returncode, (proc.stderr or "")[:200]),
            )
    except FileNotFoundError:
        warn_limited("list-missing", "container binary not found on PATH")
        with _cache_lock:
            _cache.update(ip=None, at=time.time())
        return None
    except subprocess.TimeoutExpired:
        return _serve_stale("container list timed out after %.1ss" % RESOLVE_TIMEOUT)
    except Exception as exc:
        return _serve_stale("container list failed: %s: %s" % (type(exc).__name__, exc))

    found = None
    for line in out.splitlines():
        parts = line.split()
        if not parts or parts[0] != NAME:
            continue
        for token in parts[1:]:
            ip = parse_ipv4(token)
            if ip:
                found = ip
                break
        if found:
            break
    now2 = time.time()
    with _cache_lock:
        if found is not None:
            _cache.update(ip=found, at=now2, good_at=now2)
        else:
            # The lookup RAN and the name was not there: a positive statement
            # that the container is gone. Negative-cache and clear good_at so a
            # later lookup failure cannot resurrect the dead address.
            _cache.update(ip=None, at=now2, good_at=0.0)
    if found is None:
        warn_limited("list-miss", "container %r not in `container list`" % NAME)
    return found


def resolve(ttl: float = CACHE_TTL) -> Optional[str]:
    """Current container IP.

    STALE-WHILE-REVALIDATE (memora #1011): when ttl>0 (the normal,
    cache-respecting path -- NOT the forced-fresh retry below) and a
    known-good IP exists within STALE_GRACE, an expired cache entry is
    returned IMMEDIATELY and a background refresh is kicked, rather than
    putting `container list` -- a subprocess exec plus an IPC round trip to
    the container runtime -- in this caller's critical path. A cold cache
    (no known-good IP at all) still blocks on a real resolve: there is
    nothing safe to serve in the meantime.

    ttl=0.0 (handle()'s forced-fresh retry after a failed connect) never
    takes the stale path: the caller already tried the cached address and it
    did not work, so it must wait for a REAL answer rather than another one
    that might be exactly as wrong. It still benefits from single-flight: if
    a resolve is already running -- foreground, or a kicked background one --
    it joins that one instead of starting its own.

    Serving a stale IP is safe because a genuinely moved address is caught one
    connection later: handle() poisons the cache on CONNECT failure and forces
    a fresh lookup (ttl=0.0, above). STALE_GRACE bounds it, so a permanently
    broken lookup does eventually surface instead of pinning a wrong address
    forever.
    """
    now = time.time()
    with _cache_lock:
        if ttl > 0 and now - _cache["at"] < ttl:
            return _cache["ip"]
        stale_ip = _cache.get("ip")
        stale_good_at = _cache.get("good_at") or 0.0

    if ttl > 0 and stale_ip is not None and (now - stale_good_at) <= STALE_GRACE:
        _kick_background_refresh()
        return stale_ip

    return _resolve_singleflight()


def poison_cache() -> None:
    with _cache_lock:
        _cache.update(ip=None, at=0.0)


def splice(left: socket.socket, right: socket.socket) -> None:
    """Bidirectional copy in THIS thread (no extra pump threads)."""
    sockets = [left, right]
    try:
        while True:
            readable, _, exceptional = select.select(sockets, [], sockets, 300.0)
            if exceptional:
                break
            if not readable:
                continue
            for src in readable:
                dst = right if src is left else left
                try:
                    data = src.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                try:
                    dst.sendall(data)
                except OSError:
                    return
    finally:
        for sock in (left, right):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass


def handle(client: socket.socket, peer: str) -> None:
    upstream = None
    try:
        for attempt in (0, 1):
            # attempt 1 forces a fresh lookup: a container restart changes the IP,
            # and a cached-but-dead Apple bridge address BLACKHOLES rather than
            # refusing, so a stale cache would surface as a long hang.
            ip = resolve(ttl=0.0 if attempt else CACHE_TTL)
            if not ip:
                log("INFO", "fail-fast no-upstream peer=%s attempt=%d" % (peer, attempt))
                break
            try:
                log("INFO", "connect peer=%s -> %s:%d attempt=%d" % (peer, ip, TARGET_PORT, attempt))
                upstream = socket.create_connection((ip, TARGET_PORT), timeout=CONNECT_TIMEOUT)
                # CRITICAL: create_connection's timeout stays on the RETURNED socket
                # and would then apply to every recv() in splice(). socket.timeout is
                # an OSError, so a timed-out recv would tear down a healthy connection
                # mid-response — the server commits the write but the client is told
                # it failed (phantom write; retries duplicate).
                # Fail fast on CONNECT only; the data path must not be capped.
                upstream.settimeout(None)
                client.settimeout(None)
                break
            except OSError as exc:
                warn_limited(
                    "connect-%s" % ip,
                    "upstream connect %s:%d failed: %s" % (ip, TARGET_PORT, exc),
                )
                poison_cache()
                upstream = None
        if upstream is None:
            return
        splice(client, upstream)
        client = None  # splice closed it
        upstream = None
    except Exception:
        log("ERROR", "handler crashed peer=%s\n%s" % (peer, traceback.format_exc()))
    finally:
        if upstream is not None:
            try:
                upstream.close()
            except OSError:
                pass
        if client is not None:
            try:
                client.close()
            except OSError:
                pass
        _slots.release()


def serve() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((LISTEN_HOST, LISTEN_PORT))
    srv.listen(MAX_CONN)
    # Don't pin a deleted worktree cwd for a login-session daemon.
    try:
        os.chdir("/")
    except OSError:
        pass
    log(
        "INFO",
        "listen %s:%d -> container %s:%d (resolve-on-connect, max_conn=%d)"
        % (LISTEN_HOST, LISTEN_PORT, NAME, TARGET_PORT, MAX_CONN),
    )
    while True:
        try:
            conn, addr = srv.accept()
        except OSError as exc:
            log("ERROR", "accept failed: %s" % exc)
            time.sleep(0.1)
            continue
        peer = "%s:%d" % (addr[0], addr[1])
        if not _slots.acquire(blocking=False):
            warn_limited("max-conn", "reject peer=%s at max_conn=%d" % (peer, MAX_CONN))
            try:
                conn.close()
            except OSError:
                pass
            continue
        try:
            threading.Thread(
                target=handle, args=(conn, peer), name="proxy-%s" % peer, daemon=True
            ).start()
        except Exception as exc:
            log("ERROR", "thread start failed peer=%s: %s" % (peer, exc))
            _slots.release()
            try:
                conn.close()
            except OSError:
                pass


def main() -> int:
    _open_log()
    log(
        "INFO",
        "start pid=%d container=%s listen=%s:%d target_port=%d log=%s"
        % (os.getpid(), NAME, LISTEN_HOST, LISTEN_PORT, TARGET_PORT, LOG_PATH or "stderr"),
    )
    try:
        serve()
    except KeyboardInterrupt:
        log("INFO", "stop (interrupt)")
        return 0
    except Exception:
        log("ERROR", "fatal\n%s" % traceback.format_exc())
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
