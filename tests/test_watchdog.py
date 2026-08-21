"""memora #965 phase 4: the watchdog that CONSUMES the health signals.

Under the consolidated deployment ONE container serves every workspace, so a
watchdog that restarts wrongly takes the whole fleet down. These assert on what
it EXECUTES and on the failure paths — codex found that the previous version's
injected runner returned None and therefore could not exercise a single command
failure.
"""
import importlib.util
import json
import pathlib
import subprocess

import pytest

_spec = importlib.util.spec_from_file_location(
    "memora_watchdog",
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "memora_watchdog.py",
)
watchdog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(watchdog)


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _CLI:
    """A container CLI double that can actually FAIL, unlike returning None."""

    def __init__(self, state="running"):
        self.state = state
        self.calls = []
        self.fail = {}          # verb -> _Proc or Exception
        self.on_start = None

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        verb = cmd[1] if len(cmd) > 1 else ""
        rigged = self.fail.get(verb)
        if isinstance(rigged, Exception):
            raise rigged
        if isinstance(rigged, _Proc):
            return rigged
        if verb == "list":
            rows = [] if self.state == "absent" else [
                {"id": "memora-all", "status": {"state": self.state}}]
            return _Proc(stdout=json.dumps(rows))
        if verb == "stop":
            self.state = "stopped"
        elif verb == "kill":
            self.state = "stopped"
        elif verb == "start":
            self.state = "running"
            if self.on_start:
                self.on_start()
        return _Proc()


@pytest.fixture
def cli():
    return _CLI()


@pytest.fixture
def dog(cli):
    clock = _Clock()
    w = watchdog.Watchdog(
        "memora-all",
        "http://127.0.0.1:8920/health",
        threshold=3, backoff=30, backoff_max=120, healthy_run=3,
        startup=10, runner=cli, clock=clock, sleeper=lambda s: clock.advance(s),
    )
    w.cli = cli
    w.clock = clock
    return w


def _liveness(dog, healthy):
    dog.probe = lambda: healthy


class TestTheFiveRequiredCases:
    def test_healthy_never_restarts(self, dog):
        _liveness(dog, True)
        for _ in range(20):
            assert dog.tick() == "healthy"
        assert dog.restarts == 0 and dog.cli.calls == []

    def test_a_degraded_database_never_restarts(self, dog):
        """Liveness is fine, the databases are not. The watchdog must not care."""
        _liveness(dog, True)
        for _ in range(20):
            dog.tick()
        assert dog.restarts == 0

    def test_below_threshold_does_not_restart(self, dog):
        _liveness(dog, False)
        assert dog.tick() == "below_threshold"
        assert dog.tick() == "below_threshold"
        assert dog.restarts == 0

    def test_threshold_triggers_exactly_one_restart(self, dog):
        _liveness(dog, False)
        dog.tick(); dog.tick()
        # Healthy once the container is running again — the post-start
        # verification must observe a real transition, not a rigged constant.
        dog.probe = lambda: dog.cli.state == "running" and any(
            c[1] == "start" for c in dog.cli.calls)
        assert dog.tick() == "restarted"
        assert dog.restarts == 1
        verbs = [c[1] for c in dog.cli.calls]
        assert verbs[0] == "list" and "stop" in verbs and "start" in verbs

    def test_backoff_prevents_a_restart_loop(self, dog):
        _liveness(dog, False)
        dog.cli.on_start = lambda: None
        for _ in range(3):
            dog.tick()
        assert dog.restarts + dog.failed_restarts == 1
        before = dog.restarts + dog.failed_restarts
        for _ in range(30):
            dog.tick()
        assert dog.restarts + dog.failed_restarts == before, "backoff did not hold"


class TestUrlIsEnforcedNotDocumented:
    """codex P0: the default pointed at :8910 while the live service is :8920,
    so deployed as-is it would have restarted a HEALTHY container forever."""

    @pytest.mark.parametrize("url", [
        "",
        "http://127.0.0.1:8920/health/db",          # database health must never restart
        "http://127.0.0.1:8920/health/db/memora",
        "http://127.0.0.1:8920/healthz",
        "http://127.0.0.1:8920/health?x=1",
        "http://10.0.0.5:8920/health",              # not loopback
        "ftp://127.0.0.1/health",
    ])
    def test_bad_liveness_urls_are_refused(self, url):
        with pytest.raises(watchdog.ConfigError):
            watchdog.validate_liveness_url(url)

    def test_the_exact_liveness_url_is_accepted(self):
        assert watchdog.validate_liveness_url("http://127.0.0.1:8920/health")


class TestProbeValidation:
    def _resp(self, dog, status, body):
        """Drive the watchdog's own opener — probe() no longer uses urlopen
        directly, because redirects had to be refused."""
        url = dog.url

        class _R:
            def __init__(self):
                self.status = status

            def geturl(self):
                return url

            def read(self):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class _Opener:
            def open(self, u, timeout=None):
                return _R()

        dog._opener = _Opener()

    def test_200_without_status_ok_is_not_healthy(self, dog):
        """codex P0: any 200 with any JSON was treated as healthy."""
        self._resp(dog, 200, b'{"something":"else"}')
        assert dog.probe() is False

    def test_200_with_status_ok_is_healthy(self, dog):
        self._resp(dog, 200, b'{"status":"ok"}')
        assert dog.probe() is True

    def test_200_with_a_json_array_is_not_healthy(self, dog):
        self._resp(dog, 200, b'[]')
        assert dog.probe() is False

    def test_503_is_not_healthy(self, dog):
        self._resp(dog, 503, b'{"status":"ok"}')
        assert dog.probe() is False


class TestRestartIsNotFailOpen:
    """codex P0: return codes were ignored and the restart was counted before
    it succeeded, so the watchdog could report a restart that never happened."""

    def _trigger(self, dog):
        _liveness(dog, False)
        dog.tick(); dog.tick()
        return dog.tick()

    def test_a_failed_start_is_not_counted_as_a_restart(self, dog):
        dog.cli.fail["start"] = _Proc(returncode=1, stderr="no such container")
        assert self._trigger(dog) == "restart_failed"
        assert dog.restarts == 0 and dog.failed_restarts == 1

    def test_a_missing_cli_does_not_crash_the_watchdog(self, dog):
        dog.cli.fail["list"] = FileNotFoundError("container")
        assert self._trigger(dog) == "restart_failed"
        assert dog.restarts == 0

    def test_a_hung_cli_is_bounded(self, dog):
        dog.cli.fail["stop"] = subprocess.TimeoutExpired("container stop", 60)
        assert self._trigger(dog) == "restart_failed"

    def test_an_absent_container_is_reported_not_started(self, dog):
        dog.cli.state = "absent"
        assert self._trigger(dog) == "restart_failed"
        assert [c[1] for c in dog.cli.calls] == ["list"], "tried to act on a missing container"

    def test_a_stopped_container_is_started_without_stop_or_kill(self, dog):
        dog.cli.state = "stopped"
        _liveness(dog, False)
        dog.tick(); dog.tick()
        dog.probe = lambda: dog.cli.state == "running"
        dog.tick()
        verbs = [c[1] for c in dog.cli.calls]
        assert "stop" not in verbs and "kill" not in verbs, verbs
        assert "start" in verbs

    def test_start_that_never_becomes_healthy_is_a_failed_restart(self, dog):
        """The container comes up and immediately dies: command success is not
        evidence that the service is serving."""
        _liveness(dog, False)          # never healthy, even after start
        assert self._trigger(dog) == "restart_failed"
        assert dog.restarts == 0
        assert any(c[1] == "start" for c in dog.cli.calls)

    def test_a_failed_restart_still_grows_the_backoff(self, dog):
        dog.cli.fail["start"] = _Proc(returncode=1, stderr="boom")
        self._trigger(dog)
        assert dog.failed_restarts == 1
        for _ in range(30):
            dog.tick()
        assert dog.failed_restarts == 1, "a failing recovery retried at the shortest interval"

    def test_restart_failure_fires_the_alert_hook(self, dog):
        dog._alert = "/usr/bin/true"
        dog.cli.fail["start"] = _Proc(returncode=1, stderr="boom")
        self._trigger(dog)
        assert any(c and c[0] == "/usr/bin/true" for c in dog.cli.calls), (
            "no alert on a failed restart of the shared container"
        )


class TestSustainedHealthRecovery:
    """codex P1: one lucky 200 reset the backoff, so a flapping process
    returned to the shortest restart cadence. My old test blessed that."""

    def test_one_transient_healthy_probe_does_not_reset_backoff(self, dog):
        _liveness(dog, False)
        dog.cli.on_start = lambda: None
        for _ in range(3):
            dog.tick()
        assert dog._backoff > 0
        _liveness(dog, True)
        dog.tick()                      # ONE healthy probe
        assert dog._backoff > 0, "a single 200 cleared the backoff"

    def test_sustained_health_clears_the_backoff(self, dog):
        _liveness(dog, False)
        for _ in range(3):
            dog.tick()
        assert dog._backoff > 0
        _liveness(dog, True)
        for _ in range(dog.healthy_run):
            dog.tick()
        assert dog._backoff == 0.0


class TestExclusiveLock:
    """codex P0: two watchdogs could concurrently TERM/KILL/start the one
    container that now serves every workspace."""

    def test_a_second_watchdog_cannot_take_the_lock(self, tmp_path):
        path = str(tmp_path / "wd.lock")
        first = watchdog.acquire_lock(path)
        try:
            with pytest.raises(watchdog.ConfigError):
                watchdog.acquire_lock(path)
        finally:
            first.close()

    def test_the_lock_is_released_when_the_holder_exits(self, tmp_path):
        path = str(tmp_path / "wd.lock")
        watchdog.acquire_lock(path).close()
        second = watchdog.acquire_lock(path)      # must not raise
        second.close()


class TestConfigValidation:
    def test_fractional_values_are_not_truncated(self, monkeypatch):
        """int(float("1.9")) silently became 1."""
        monkeypatch.setenv("MEMORA_WATCHDOG_TIMEOUT", "1.9")
        assert watchdog._num_env("MEMORA_WATCHDOG_TIMEOUT", 5) == 1.9

    @pytest.mark.parametrize("value", ["0", "-1", "abc", "inf"])
    def test_bad_numbers_fail_closed(self, monkeypatch, value):
        monkeypatch.setenv("MEMORA_WATCHDOG_TIMEOUT", value)
        with pytest.raises(watchdog.ConfigError):
            watchdog._num_env("MEMORA_WATCHDOG_TIMEOUT", 5)

    def test_backoff_max_below_backoff_is_refused(self, cli):
        with pytest.raises(watchdog.ConfigError):
            watchdog.Watchdog("c", "http://127.0.0.1:8920/health",
                              backoff=60, backoff_max=30, runner=cli)


class TestLivenessCannotBeRedirectedIntoReadiness:
    """codex P1: urlopen follows redirects by default, so a /health that 302s to
    /health/db would make the watchdog consult DATABASE health after all — a
    degraded store would look like a dead process and restart six workspaces.
    Validating the initial URL is not enough if the server can move you."""

    def test_trailing_slash_is_rejected(self):
        """/health/ was accepted by rstrip; FastMCP may 404 it, which reads as
        a dead service and restarts a healthy one."""
        with pytest.raises(watchdog.ConfigError):
            watchdog.validate_liveness_url("http://127.0.0.1:8920/health/")

    def test_a_redirect_is_not_followed(self, monkeypatch, dog):
        seen = {}

        class _Opener:
            def open(self, url, timeout=None):
                seen["url"] = url
                raise watchdog.urllib.error.HTTPError(
                    url, 302, "redirect to /health/db", {}, None)

        dog._opener = _Opener()
        assert dog.probe() is False
        assert seen["url"].endswith("/health")

    def test_a_response_from_another_url_is_refused(self, dog):
        """Defence in depth: even if a redirect were followed, a response whose
        final URL is not the validated one is not liveness."""
        class _R:
            status = 200

            def geturl(self):
                return "http://127.0.0.1:8920/health/db"

            def read(self):
                return b'{"status":"ok"}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class _Opener:
            def open(self, url, timeout=None):
                return _R()

        dog._opener = _Opener()
        assert dog.probe() is False, "accepted a readiness response as liveness"


class TestCountsMustBeWholeNumbers:
    """codex P1: THRESHOLD=0.5 became 0, so the FIRST failed probe restarted
    immediately; HEALTHY_RUN=0.5 became 0, so one healthy probe cleared the
    backoff. Both silently destroy the discipline they configure."""

    @pytest.mark.parametrize("name", ["MEMORA_WATCHDOG_THRESHOLD",
                                      "MEMORA_WATCHDOG_HEALTHY_RUN"])
    @pytest.mark.parametrize("value", ["0.5", "1.9", "0", "-1", "abc"])
    def test_fractional_or_zero_counts_are_refused(self, monkeypatch, name, value):
        monkeypatch.setenv(name, value)
        with pytest.raises(watchdog.ConfigError):
            watchdog._int_env(name, 3)

    def test_whole_numbers_are_accepted(self, monkeypatch):
        monkeypatch.setenv("MEMORA_WATCHDOG_THRESHOLD", "5")
        assert watchdog._int_env("MEMORA_WATCHDOG_THRESHOLD", 3) == 5

    @pytest.mark.parametrize("kwargs", [{"threshold": 0.5}, {"healthy_run": 0.5},
                                        {"threshold": 0}, {"healthy_run": 0}])
    def test_the_constructor_refuses_them_too(self, cli, kwargs):
        with pytest.raises(watchdog.ConfigError):
            watchdog.Watchdog("c", "http://127.0.0.1:8920/health", runner=cli, **kwargs)


class TestAlertDeliveryIsChecked:
    """codex P1: a hook exiting 1 was silently treated as delivered, which
    contradicts the documented behaviour and hides a shared outage."""

    def test_a_failing_hook_is_logged_as_undelivered(self, dog, caplog):
        dog._alert = "/usr/bin/false"
        dog.cli.fail["start"] = _Proc(returncode=1, stderr="boom")
        # the hook itself returns non-zero
        dog.cli.fail["/usr/bin/false"] = None
        original = dog.cli.__call__

        def runner(cmd, **kw):
            if cmd and cmd[0] == "/usr/bin/false":
                dog.cli.calls.append(cmd)
                return _Proc(returncode=1, stderr="hook exploded")
            return original(cmd, **kw)

        dog._run = runner
        with caplog.at_level("ERROR"):
            _liveness(dog, False)
            dog.tick(); dog.tick(); dog.tick()
        assert any("alert hook" in r.message and "exit" in r.message
                   for r in caplog.records), [r.message for r in caplog.records]

    def test_a_successful_restart_also_alerts(self, dog):
        """Six workspaces share this process; a restart is an event to report."""
        fired = []
        original = dog.cli.__call__

        def runner(cmd, **kw):
            if cmd and cmd[0] == "/usr/bin/true":
                fired.append(cmd)
                return _Proc()
            return original(cmd, **kw)

        dog._alert = "/usr/bin/true"
        dog._run = runner
        _liveness(dog, False)
        dog.tick(); dog.tick()
        dog.probe = lambda: dog.cli.state == "running" and any(
            c[1] == "start" for c in dog.cli.calls if len(c) > 1)
        dog.tick()
        assert dog.restarts == 1
        assert fired, "a successful restart of the shared container was silent"


class TestLockDiagnostics:
    def test_a_losing_process_does_not_wipe_the_holders_pid(self, tmp_path):
        """codex P2: opening 'w' truncated the CURRENT holder's pid before the
        loser discovered it had lost — blank exactly when you need to know."""
        path = str(tmp_path / "wd.lock")
        holder = watchdog.acquire_lock(path)
        try:
            with pytest.raises(watchdog.ConfigError):
                watchdog.acquire_lock(path)
            assert open(path).read().strip(), "holder's pid was truncated by the loser"
        finally:
            holder.close()


class TestNoRedirectHandlerOverRealHttp:
    """codex follow-up: the other redirect test swaps in a double, so it never
    exercised the real _NoRedirect handler. This serves an actual 302 to
    /health/db and asserts the watchdog neither follows it nor requests it —
    the 'never consult readiness' claim, attested over HTTP."""

    def test_a_real_302_to_health_db_is_not_followed(self):
        import http.server
        import threading

        requested = []

        class _H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requested.append(self.path)
                if self.path == "/health":
                    self.send_response(302)
                    self.send_header("Location", "/health/db")
                    self.end_headers()
                else:
                    body = b'{"status":"ok"}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            dog = watchdog.Watchdog(
                "memora-all", f"http://127.0.0.1:{port}/health",
                runner=_CLI(), clock=_Clock())
            assert dog.probe() is False, "followed a redirect off /health"
        finally:
            srv.shutdown()

        assert requested == ["/health"], (
            f"the watchdog requested {requested}; it must never fetch readiness"
        )
