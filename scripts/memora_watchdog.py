#!/usr/bin/env python3
"""Restart a memora container that has stopped SERVING (memora #965 phase 4).

A health endpoint is observability. This is the consumer that ACTS on it, and
it is deliberately a separate process from scripts/memora_proxy.py, whose
documented contract says it does not start or restart containers.

THE ONE RULE: only repeated /health (LIVENESS) failures may cause a restart.
/health/db and /health/db/{name} report DATABASE health and must NEVER restart
the process. Under the consolidated deployment one container serves every
workspace, so restarting because a remote store is slow would remove memory
from all of them at once. The URL is validated to be exactly /health for that
reason -- the rule has to be enforced, not merely documented.

Restart discipline:
  * EXCLUSIVE LOCK, held for the process lifetime and keyed to the container,
    so two watchdogs cannot TERM/KILL/start the same container concurrently.
  * FAILURE THRESHOLD, so one dropped probe is not a restart.
  * STATE-AWARE ACTION: inspect first; start a stopped container, gracefully
    stop a running one, force-kill only if it is still running after the grace.
  * POST-START VERIFICATION: a restart counts only when the container starts AND
    /health becomes healthy within a deadline. Command success is not evidence.
  * EXPONENTIAL BACKOFF, and a FAILED recovery keeps/raises it.
  * SUSTAINED-HEALTH RECOVERY: backoff clears only after consecutive healthy
    probes, so a flapping process cannot return to the shortest cadence.
  * ALERT HOOK on restart failure, because a shared outage nobody is told about
    is the failure mode this whole phase exists to remove.

Environment (see launchd/WATCHDOG_RUNBOOK.md):
  MEMORA_WATCHDOG_CONTAINER    container to supervise (required)
  MEMORA_WATCHDOG_URL          liveness URL (required; must end in /health)
  MEMORA_WATCHDOG_LOCK         lock file path
  MEMORA_WATCHDOG_INTERVAL     seconds between probes (default 10)
  MEMORA_WATCHDOG_THRESHOLD    consecutive failures before restart (default 3)
  MEMORA_WATCHDOG_TIMEOUT      per-probe timeout seconds (default 5)
  MEMORA_WATCHDOG_GRACE        seconds after TERM before KILL (default 15)
  MEMORA_WATCHDOG_STARTUP      seconds to wait for health after start (default 60)
  MEMORA_WATCHDOG_BACKOFF      first backoff seconds (default 30, doubling)
  MEMORA_WATCHDOG_BACKOFF_MAX  backoff cap seconds (default 600)
  MEMORA_WATCHDOG_HEALTHY_RUN  consecutive healthy probes to clear backoff (default 3)
  MEMORA_WATCHDOG_CMD_TIMEOUT  seconds per container CLI call (default 60)
  MEMORA_WATCHDOG_ALERT        optional command run on restart failure
  MEMORA_WATCHDOG_LOG          log file path
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("memora.watchdog")


class ConfigError(SystemExit):
    pass


def _num_env(name: str, default: float, *, minimum: float = 0.0) -> float:
    """Numbers are parsed as floats. int(float(x)) silently turned 1.9 into 1."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a number")
    if value != value or value in (float("inf"), float("-inf")) or value <= minimum:
        raise ConfigError(f"{name}={raw!r} must be a finite number > {minimum}")
    return value


def _int_env(name: str, default: int) -> int:
    """Counts must be whole numbers >= 1.

    int(float(x)) turned THRESHOLD=0.5 into 0, so the FIRST failed probe would
    have restarted immediately; HEALTHY_RUN=0.5 became 0, so one healthy probe
    cleared the backoff. Both silently destroy the discipline they configure.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a number")
    if value != value or value in (float("inf"), float("-inf")):
        # inf/nan reached int() and raised OverflowError/ValueError instead of
        # the intended ConfigError. It failed closed either way, but the error
        # an operator sees should name the setting.
        raise ConfigError(f"{name}={raw!r} must be a finite whole number >= 1")
    if value != int(value) or int(value) < 1:
        raise ConfigError(f"{name}={raw!r} must be a whole number >= 1")
    return int(value)


def validate_liveness_url(url: str) -> str:
    """Only a loopback http(s) URL whose path is EXACTLY /health.

    A watchdog pointed at /health/db would restart the shared container because
    one database was slow; a watchdog pointed at the wrong port would restart a
    perfectly healthy one. Both are worse than no watchdog, so this is enforced
    rather than documented.
    """
    if not url:
        raise ConfigError("MEMORA_WATCHDOG_URL is required")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ConfigError(f"MEMORA_WATCHDOG_URL scheme must be http(s): {url!r}")
    host = (parts.hostname or "").lower()
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ConfigError(f"MEMORA_WATCHDOG_URL must be loopback: {url!r}")
    if parts.path != "/health":
        raise ConfigError(
            f"MEMORA_WATCHDOG_URL path must be exactly /health (liveness), got {parts.path!r}; "
            "note /health/ is rejected too -- FastMCP may 404 it, which would look like a dead service; "
            "/health/db reports DATABASE health and must never trigger a restart"
        )
    if parts.query or parts.fragment:
        raise ConfigError(f"MEMORA_WATCHDOG_URL must have no query or fragment: {url!r}")
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects outright: a 3xx from the liveness endpoint is a
    misconfiguration, not something to follow."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code,
            f"liveness endpoint redirected to {newurl!r}; refusing to follow",
            headers, fp)


class RestartFailed(RuntimeError):
    pass


class Watchdog:
    def __init__(
        self,
        container: str,
        url: str,
        *,
        threshold: int = 3,
        timeout: float = 5.0,
        grace: float = 15.0,
        startup: float = 60.0,
        backoff: float = 30.0,
        backoff_max: float = 600.0,
        healthy_run: int = 3,
        cmd_timeout: float = 60.0,
        runner=None,
        clock=time.monotonic,
        sleeper=time.sleep,
        alert=None,
    ):
        if backoff_max < backoff:
            raise ConfigError("MEMORA_WATCHDOG_BACKOFF_MAX must be >= MEMORA_WATCHDOG_BACKOFF")
        self.container = container
        self.url = validate_liveness_url(url)
        if threshold != int(threshold) or int(threshold) < 1:
            raise ConfigError(f"threshold must be a whole number >= 1, got {threshold!r}")
        if healthy_run != int(healthy_run) or int(healthy_run) < 1:
            raise ConfigError(f"healthy_run must be a whole number >= 1, got {healthy_run!r}")
        self.threshold = int(threshold)
        self.timeout = timeout
        self.grace = grace
        self.startup = startup
        self.backoff_base = backoff
        self.backoff_max = backoff_max
        self.healthy_run = int(healthy_run)
        self.cmd_timeout = cmd_timeout
        self._run = runner or (lambda cmd, **kw: subprocess.run(cmd, **kw))
        self._clock = clock
        self._sleep = sleeper
        self._alert = alert
        self._opener = urllib.request.build_opener(_NoRedirect())

        self.failures = 0
        self.healthy_streak = 0
        self.restarts = 0
        self.failed_restarts = 0
        self._backoff = 0.0
        self._blocked_until = 0.0

    # -- probing -------------------------------------------------------------

    def probe(self) -> bool:
        """True only for HTTP 200 with a JSON object whose status == "ok".

        Accepting "any 200 with any JSON" would treat an unrelated service on
        the port, or a partially-initialised server, as healthy.
        """
        try:
            # NO REDIRECTS. urlopen follows them by default, so a /health that
            # redirects to /health/db would make the watchdog consult DATABASE
            # health after all -- a degraded store would then look like a dead
            # process and restart every workspace. Validating the initial URL
            # is not enough if the server can move us.
            with self._opener.open(self.url, timeout=self.timeout) as resp:
                if resp.status != 200:
                    return False
                if resp.geturl() != self.url:
                    log.error("liveness URL moved to %s; refusing to treat it as "
                              "liveness", resp.geturl())
                    return False
                body = json.loads(resp.read().decode())
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return False
        return isinstance(body, dict) and body.get("status") == "ok"

    # -- container state -----------------------------------------------------

    def _cli(self, *args) -> subprocess.CompletedProcess:
        cmd = ["container", *args]
        try:
            return self._run(cmd, capture_output=True, text=True, timeout=self.cmd_timeout)
        except FileNotFoundError as exc:
            raise RestartFailed(f"container CLI not found: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RestartFailed(f"`{' '.join(cmd)}` timed out after {self.cmd_timeout}s") from exc

    def container_state(self) -> str:
        """'running', 'stopped', or 'absent'. Never raises for a missing name."""
        proc = self._cli("list", "-a", "--format", "json")
        if proc.returncode != 0:
            raise RestartFailed(f"`container list` exit {proc.returncode}: {(proc.stderr or '')[:200]}")
        try:
            rows = json.loads(proc.stdout or "[]")
        except ValueError as exc:
            raise RestartFailed(f"`container list` returned non-JSON: {exc}") from exc
        for row in rows if isinstance(rows, list) else []:
            if row.get("id") == self.container:
                return (row.get("status") or {}).get("state") or "stopped"
        return "absent"

    # -- decision ------------------------------------------------------------

    def tick(self) -> str:
        if self.probe():
            self.failures = 0
            self.healthy_streak += 1
            # SUSTAINED health, not one lucky 200: a flapping process that
            # answers once between crashes must not reset to the shortest
            # restart cadence.
            if self._backoff and self.healthy_streak >= self.healthy_run:
                log.info("%s healthy for %d consecutive probes; clearing backoff",
                         self.container, self.healthy_streak)
                self._backoff = 0.0
                self._blocked_until = 0.0
            return "healthy"

        self.healthy_streak = 0
        self.failures += 1
        if self.failures < self.threshold:
            log.warning("%s liveness failure %d/%d", self.container, self.failures, self.threshold)
            return "below_threshold"

        now = self._clock()
        if now < self._blocked_until:
            log.warning("%s still failing; backoff holds for %.0fs",
                        self.container, self._blocked_until - now)
            return "backoff"

        self.failures = 0
        try:
            self.restart()
            self.restarts += 1
            # Six workspaces share this process: a successful restart is still
            # an event someone should be told about.
            self._emit_alert(f"restarted (#{self.restarts})")
            outcome = "restarted"
        except RestartFailed as exc:
            self.failed_restarts += 1
            log.error("%s restart FAILED: %s", self.container, exc)
            self._emit_alert(str(exc))
            outcome = "restart_failed"

        # Backoff grows whether or not the restart worked. A failing recovery
        # must not retry at the shortest interval forever.
        self._backoff = (self.backoff_base if self._backoff == 0.0
                         else min(self._backoff * 2, self.backoff_max))
        self._blocked_until = self._clock() + self._backoff
        return outcome

    # -- action --------------------------------------------------------------

    def restart(self) -> None:
        """State-aware restart, verified by health. Raises RestartFailed."""
        state = self.container_state()
        log.error("%s failed liveness %d times; restarting (state=%s)",
                  self.container, self.threshold, state)
        if state == "absent":
            raise RestartFailed(f"container {self.container!r} does not exist")

        if state == "running":
            # Graceful first. --time is the CLI's own kill delay; KILL below is
            # only for the case where it is somehow still running after.
            proc = self._cli("stop", "--signal", "TERM", "--time", str(int(self.grace)),
                             self.container)
            if proc.returncode != 0:
                log.warning("graceful stop exit %d: %s", proc.returncode,
                            (proc.stderr or "").strip()[:200])
            if self.container_state() == "running":
                kill = self._cli("kill", self.container)
                if kill.returncode != 0:
                    raise RestartFailed(
                        f"kill exit {kill.returncode}: {(kill.stderr or '').strip()[:200]}")

        start = self._cli("start", self.container)
        if start.returncode != 0:
            raise RestartFailed(
                f"start exit {start.returncode}: {(start.stderr or '').strip()[:200]}")

        # POST-START VERIFICATION. A successful CLI call is not evidence that
        # the service is serving; without this a container that starts and
        # immediately dies would be counted as a successful restart.
        deadline = self._clock() + self.startup
        while self._clock() < deadline:
            if self.probe():
                log.info("%s restarted and healthy", self.container)
                return
            self._sleep(2)
        raise RestartFailed(f"started but /health did not become healthy within {self.startup}s")

    def _emit_alert(self, message: str) -> None:
        if not self._alert:
            return
        try:
            proc = self._run([self._alert, self.container, message],
                             capture_output=True, text=True, timeout=self.cmd_timeout)
        except Exception as exc:  # an alert hook must never kill the watchdog
            log.error("alert hook %r raised: %s", self._alert, exc)
            return
        rc = getattr(proc, "returncode", 0)
        if rc:
            # A hook that exits non-zero has NOT delivered. Saying nothing here
            # would mean a silent shared outage, which is the failure this
            # phase exists to remove.
            log.error("alert hook %r exit %s: %s", self._alert, rc,
                      (getattr(proc, "stderr", "") or "").strip()[:200])


def acquire_lock(path: str):
    """Exclusive, non-blocking, held for the process lifetime.

    Two watchdogs concurrently stopping and starting one container would take
    every workspace down; with a single shared container that is the whole
    fleet.
    """
    # "a" (create, no truncate): opening "w" wiped the CURRENT holder's pid
    # before the loser discovered it had lost, leaving an empty file exactly
    # when an operator needs to know who holds it.
    fh = open(path, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise ConfigError(f"another watchdog already holds {path}")
    fh.seek(0)
    fh.truncate()
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    return fh


def main(argv=None) -> int:
    container = os.environ.get("MEMORA_WATCHDOG_CONTAINER", "").strip()
    if not container:
        print("MEMORA_WATCHDOG_CONTAINER is required", file=sys.stderr)
        return 2
    url = os.environ.get("MEMORA_WATCHDOG_URL", "").strip()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        filename=os.environ.get("MEMORA_WATCHDOG_LOG") or None,
    )

    lock_path = os.environ.get("MEMORA_WATCHDOG_LOCK") or f"/tmp/memora-watchdog-{container}.lock"
    lock = acquire_lock(lock_path)          # held for the process lifetime

    dog = Watchdog(
        container, url,
        threshold=_int_env("MEMORA_WATCHDOG_THRESHOLD", 3),
        timeout=_num_env("MEMORA_WATCHDOG_TIMEOUT", 5),
        grace=_num_env("MEMORA_WATCHDOG_GRACE", 15),
        startup=_num_env("MEMORA_WATCHDOG_STARTUP", 60),
        backoff=_num_env("MEMORA_WATCHDOG_BACKOFF", 30),
        backoff_max=_num_env("MEMORA_WATCHDOG_BACKOFF_MAX", 600),
        healthy_run=_int_env("MEMORA_WATCHDOG_HEALTHY_RUN", 3),
        cmd_timeout=_num_env("MEMORA_WATCHDOG_CMD_TIMEOUT", 60),
        alert=os.environ.get("MEMORA_WATCHDOG_ALERT") or None,
    )
    interval = _num_env("MEMORA_WATCHDOG_INTERVAL", 10)
    log.info("watching %s at %s (threshold=%d interval=%.0fs lock=%s)",
             container, dog.url, dog.threshold, interval, lock_path)
    try:
        while True:
            dog.tick()
            time.sleep(interval)
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
