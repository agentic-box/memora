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


# ---------------------------------------------------------------------------
# Item 108: the Linux deploy host (docker), the deploy hold, the restart cap, the clmux alert, the installer
# ---------------------------------------------------------------------------

import os
import stat
import time


class _Docker:
    """A docker CLI double: `inspect -f {{.State.Status}}`, stop, kill, start, unpause. Records every argv."""

    def __init__(self, state="running"):
        self.state = state
        self.calls = []
        self.fail = {}
        self.on_start = None
        self.started_at = "2020-01-01T00:00:00.000000000Z"      # long ago: not warming up
        self.started_queries = 0

    def __call__(self, cmd, **kw):
        if cmd[1] == "inspect" and "{{.State.StartedAt}}" in cmd:      # the warm-up query: not a mutation, not recorded
            self.started_queries += 1
            return _Proc(stdout=self.started_at + "\n")
        self.calls.append(cmd)
        verb = cmd[1]
        rigged = self.fail.get(verb)
        if isinstance(rigged, Exception):
            raise rigged
        if isinstance(rigged, _Proc):
            return rigged
        if verb == "inspect":
            if self.state == "absent":
                return _Proc(returncode=1, stderr="Error: No such object: memora-all")
            return _Proc(stdout=self.state + "\n")
        if verb in ("stop", "kill"):
            self.state = "exited"
        elif verb == "start":
            self.state = "running"
            if self.on_start:
                self.on_start()
        elif verb == "unpause":
            self.state = "running"
        return _Proc()


@pytest.fixture
def ddog(tmp_path):
    docker = _Docker()
    clock = _Clock()
    wall = [1_000_000.0]
    alerts = []

    def runner(cmd, **kw):
        if cmd[0] == "alert-hook":
            alerts.append(cmd[1:])
            return _Proc()
        return docker(cmd, **kw)

    w = watchdog.Watchdog(
        "memora-all", "http://127.0.0.1:8920/health", threshold=3, backoff=1, backoff_max=1, healthy_run=1,
        startup=10, runner=runner, clock=clock, sleeper=lambda s: clock.advance(s), alert="alert-hook",
        runtime="docker", hold_path=str(tmp_path / "hold"), max_restarts_per_hour=3, wall_clock=lambda: wall[0],
    )
    w.docker, w.clock, w.wall, w.alerts, w.tmp = docker, clock, wall, alerts, tmp_path
    return w


def _up_after_start(w):
    """Healthy exactly when the container is running AND something has started it since the failures began."""
    w.probe = lambda: w.docker.state == "running" and any(c[1] in ("start", "unpause") for c in w.docker.calls)


class TestDockerRuntime:
    def test_a_killed_container_is_started_after_the_threshold_without_stop_or_kill(self, ddog):
        ddog.docker.state = "exited"            # `docker kill`: a manual stop, restart=unless-stopped does nothing
        _up_after_start(ddog)
        assert [ddog.tick() for _ in range(3)] == ["below_threshold", "below_threshold", "restarted"]
        verbs = [c[1] for c in ddog.docker.calls]
        assert verbs == ["inspect", "start"] and all(c[0] == "docker" for c in ddog.docker.calls)
        assert ddog.alerts and "restarted" in ddog.alerts[0][1]

    def test_a_hung_running_container_is_stopped_gracefully_then_started(self, ddog):
        _up_after_start(ddog)
        for _ in range(3):
            ddog.tick()
        assert ddog.docker.calls == [["docker", "inspect", "-f", "{{.State.Status}}", "memora-all"],
                                     ["docker", "stop", "-t", "15", "memora-all"],
                                     ["docker", "inspect", "-f", "{{.State.Status}}", "memora-all"],
                                     ["docker", "start", "memora-all"]]

    def test_a_paused_container_is_unpaused_then_restarted_cleanly(self, ddog):
        ddog.docker.state = "paused"
        _up_after_start(ddog)
        for _ in range(3):
            ddog.tick()
        assert ["docker", "unpause", "memora-all"] in ddog.docker.calls and ddog.restarts == 1

    def test_a_missing_container_is_never_created(self, ddog):
        ddog.docker.state = "absent"
        ddog.probe = lambda: False
        for _ in range(3):
            assert ddog.tick() in ("below_threshold", "restart_failed")
        assert [c[1] for c in ddog.docker.calls] == ["inspect"], "acted on a container that does not exist"
        assert ddog.alerts and "does not exist" in ddog.alerts[0][1]

    def test_an_unreadable_inspect_is_a_failed_restart_not_a_guess(self, ddog):
        ddog.docker.fail["inspect"] = _Proc(returncode=1, stderr="Cannot connect to the Docker daemon")
        ddog.probe = lambda: False
        for _ in range(3):
            last = ddog.tick()
        assert last == "restart_failed" and [c[1] for c in ddog.docker.calls] == ["inspect"]

    @pytest.mark.parametrize("verb", ["rm", "run", "create", "volume", "rename", "exec", "prune", "kill-all", ""])
    def test_no_destructive_verb_can_reach_the_runtime(self, ddog, verb):
        with pytest.raises(watchdog.RestartFailed, match="refused runtime verb"):
            ddog._cli(*([verb] if verb else []), "memora-all")
        assert ddog.docker.calls == []

    def test_the_code_names_no_destructive_runtime_verb(self):
        text = (pathlib.Path(watchdog.__file__)).read_text()
        for verb in ("rm", "run", "create", "volume", "rename", "exec"):
            assert f'_cli("{verb}"' not in text

    def test_the_runtime_must_be_known(self):
        with pytest.raises(watchdog.ConfigError):
            watchdog.Watchdog("c", "http://127.0.0.1:1/health", runtime="rkt")


class TestHold:
    def _hold(self, w, minutes=45):
        return watchdog.write_hold(w.hold_path, w.wall[0], minutes, "deploy v0.5.7.5")

    def test_a_held_watchdog_neither_probes_nor_acts(self, ddog):
        self._hold(ddog)
        ddog.probe = lambda: (_ for _ in ()).throw(AssertionError("probed while held"))
        for _ in range(10):
            assert ddog.tick() == "held"
        assert ddog.docker.calls == [] and ddog.alerts == []

    def test_the_failure_count_does_not_carry_over_a_hold(self, ddog):
        ddog.probe = lambda: False
        ddog.tick(); ddog.tick()                                  # two failures
        self._hold(ddog)
        assert ddog.tick() == "held"
        ddog.wall[0] += 46 * 60                                   # expired
        assert ddog.tick() == "below_threshold" and ddog.failures == 1

    def test_a_stale_hold_expires_and_is_removed_so_recovery_resumes(self, ddog):
        self._hold(ddog, minutes=45)
        ddog.wall[0] += 45 * 60 + 1
        ddog.docker.state = "exited"
        _up_after_start(ddog)
        assert [ddog.tick() for _ in range(3)][-1] == "restarted"
        assert not os.path.exists(ddog.hold_path)

    @pytest.mark.parametrize("content", ["", "not json", "{}", '{"expires": "soon"}', "[1]"])
    def test_an_unreadable_hold_never_blocks_recovery(self, ddog, content):
        pathlib.Path(ddog.hold_path).write_text(content)
        ddog.probe = lambda: True
        assert ddog.tick() == "healthy" and not os.path.exists(ddog.hold_path)

    def test_a_hold_set_while_a_restart_is_being_decided_stops_it_before_any_command(self, ddog):
        ddog.probe = lambda: False
        ddog.tick(); ddog.tick()
        original = ddog.container_state

        def state_then_hold():
            result = original()
            self._hold(ddog)                                      # the deploy takes its hold right now
            return result

        ddog.container_state = state_then_hold
        assert ddog.tick() == "held"
        assert [c[1] for c in ddog.docker.calls] == ["inspect"], "stopped a container a deploy was holding"

    def test_the_cli_holds_releases_and_reports(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        assert watchdog.main(["hold", "--container", "memora-all", "--minutes", "5", "--reason", "rollback"]) == 0
        path = watchdog.default_hold_path("memora-all")
        assert path == str(tmp_path / "memora-watchdog" / "memora-all.hold")
        hold = json.loads(pathlib.Path(path).read_text())
        assert hold["reason"] == "rollback" and 290 < hold["expires"] - time.time() < 301
        assert "watchdog held for memora-all" in capsys.readouterr().out
        assert watchdog.main(["release", "--container", "memora-all"]) == 0 and not os.path.exists(path)
        assert watchdog.main(["release", "--container", "memora-all"]) == 0
        assert "no hold was set" in capsys.readouterr().out

    @pytest.mark.parametrize("minutes", ["0", "-3", "1441"])
    def test_a_silly_hold_length_is_refused(self, tmp_path, monkeypatch, minutes):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        assert watchdog.main(["hold", "--container", "c", "--minutes", minutes]) == 2

    def test_the_daemon_and_the_cli_agree_on_the_default_hold_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        w = watchdog.Watchdog("memora-all", "http://127.0.0.1:8920/health", runtime="docker")
        assert w.hold_path == watchdog.default_hold_path("memora-all")


class TestRestartCap:
    def _fail_forever(self, w):
        w.docker.on_start = lambda: None
        w.probe = lambda: False                                   # started but never healthy: every attempt fails

    def _ticks(self, w, n, step=100):
        out = []
        for _ in range(n):
            w.clock.advance(step)
            out.append(w.tick())
        return out

    def test_the_fourth_attempt_in_an_hour_gives_up_alerts_once_and_acts_no_more(self, ddog):
        self._fail_forever(ddog)
        outcomes = self._ticks(ddog, 40)
        assert outcomes.count("restart_failed") == 3, outcomes
        assert "gave_up" in outcomes and ddog.gave_up
        calls_then = len(ddog.docker.calls)
        assert [a for a in ddog.alerts if "GIVING UP" in a[1]].__len__() == 1
        self._ticks(ddog, 50)
        assert len(ddog.docker.calls) == calls_then, "kept acting after giving up"
        assert len([a for a in ddog.alerts if "GIVING UP" in a[1]]) == 1, "alerted more than once"

    def test_it_stays_given_up_after_the_hour_passes_and_resumes_only_when_health_returns(self, ddog):
        self._fail_forever(ddog)
        self._ticks(ddog, 40)
        assert ddog.gave_up
        ddog.clock.advance(7200)
        assert ddog.tick() == "gave_up"                           # the hour is over; still latched
        ddog.probe = lambda: True
        assert ddog.tick() == "healthy" and not ddog.gave_up
        ddog.probe = lambda: False
        out = self._ticks(ddog, 5)
        assert "restart_failed" in out or "restarted" in out      # acting again after it answered once

    def test_attempts_older_than_an_hour_do_not_count(self, ddog):
        self._fail_forever(ddog)
        out = self._ticks(ddog, 12, step=1000)                    # an attempt about every 3-4 ticks, spread over hours
        assert "gave_up" not in out and not ddog.gave_up

    def test_zero_means_no_cap(self, ddog):
        ddog.max_restarts_per_hour = 0
        self._fail_forever(ddog)
        out = self._ticks(ddog, 60)
        assert "gave_up" not in out and out.count("restart_failed") > 3

    @pytest.mark.parametrize("bad", ["-1", "1.5", "x", "nan", "inf"])
    def test_a_bad_cap_in_the_environment_is_refused(self, monkeypatch, bad):
        monkeypatch.setenv("MEMORA_WATCHDOG_MAX_RESTARTS_PER_HOUR", bad)
        with pytest.raises(watchdog.ConfigError):
            watchdog._count_env("MEMORA_WATCHDOG_MAX_RESTARTS_PER_HOUR", 0)

    def test_the_apple_watchdog_default_is_unchanged(self):
        w = watchdog.Watchdog("memora-all", "http://127.0.0.1:8920/health")
        assert w.runtime == "container" and w.max_restarts_per_hour == 0


# --- the clmux alert hook and the installer (shell scripts) ---------------------------------------------

SCRIPTS = pathlib.Path(watchdog.__file__).parent


def _fake_bin(tmp_path, name, body):
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text("#!/bin/bash\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


class TestClmuxAlertHook:
    def _run(self, tmp_path, message, clmux_body=None, to="mac/leader-x"):
        env = dict(os.environ, HOME=str(tmp_path), MEMORA_WATCHDOG_ALERT_LOG=str(tmp_path / "alerts.log"))
        env.pop("MEMORA_WATCHDOG_ALERT_TO", None)
        if clmux_body is not None:
            env["MEMORA_WATCHDOG_CLMUX"] = str(_fake_bin(tmp_path, "clmux", clmux_body))
        else:
            env["MEMORA_WATCHDOG_CLMUX"] = str(tmp_path / "no-such-clmux")
        if to:
            env["MEMORA_WATCHDOG_ALERT_TO"] = to
        return subprocess.run(["bash", str(SCRIPTS / "memora_watchdog_alert_clmux.sh"), "memora-all", message],
                              env=env, capture_output=True, text=True, timeout=30)

    def test_one_message_goes_to_the_leader_and_the_line_is_logged(self, tmp_path):
        out = tmp_path / "sent.txt"
        proc = self._run(tmp_path, "restarted (#1)", f'printf "%s\\n" "$@" > "{out}"\n')
        assert proc.returncode == 0
        sent = out.read_text().splitlines()
        assert sent[:3] == ["registry", "send", "mac/leader-x"] and "memora-all -- restarted (#1)" in sent[3]
        assert sent[3].startswith("[UNVERIFIED sender, no clmux service identity yet] memora watchdog on ")
        assert "memora-all -- restarted (#1)" in (tmp_path / "alerts.log").read_text()

    def test_a_token_like_string_never_travels(self, tmp_path):
        secret = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0"
        out = tmp_path / "sent.txt"
        proc = self._run(tmp_path, f"start failed: bearer {secret}", f'printf "%s\\n" "$@" > "{out}"\n')
        assert proc.returncode == 0
        assert secret not in out.read_text() and secret not in (tmp_path / "alerts.log").read_text()
        assert "[redacted]" in out.read_text()

    def test_unreachable_clmux_is_logged_and_reported_undelivered(self, tmp_path):
        proc = self._run(tmp_path, "gave up", 'echo "daemon down" >&2\nexit 7\n')
        assert proc.returncode == 1
        log = (tmp_path / "alerts.log").read_text()
        assert "gave up" in log and "FAILED" in log and "daemon down" in log

    def test_a_missing_clmux_is_logged_and_reported_undelivered(self, tmp_path):
        proc = self._run(tmp_path, "gave up")
        assert proc.returncode == 1 and "clmux not found" in (tmp_path / "alerts.log").read_text()

    def test_a_sender_name_is_always_set_because_clmux_drops_an_empty_sender(self, tmp_path):
        out = tmp_path / "sender.txt"
        env_before = os.environ.pop("CLMUX_AGENT_NAME", None)
        try:
            proc = self._run(tmp_path, "x", f'echo "$CLMUX_AGENT_NAME" > "{out}"\n')
        finally:
            if env_before is not None:
                os.environ["CLMUX_AGENT_NAME"] = env_before
        assert proc.returncode == 0 and out.read_text().strip() == "memora-watchdog"    # one stable claim, no host prefix

    def test_there_is_no_default_recipient_and_the_alert_is_only_logged(self, tmp_path):
        out = tmp_path / "sent.txt"
        proc = self._run(tmp_path, "gave up", f'echo sent > "{out}"\n', to=None)
        assert proc.returncode == 1 and not out.exists()
        assert "no alert recipient" in (tmp_path / "alerts.log").read_text()

    def test_the_script_names_no_agent_of_the_fleet(self):
        text = (SCRIPTS / "memora_watchdog_alert_clmux.sh").read_text()
        assert "ab-leader" not in text and "mac/" not in text


class TestInstaller:
    def _install(self, tmp_path, *args, active=True):
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        log = tmp_path / "sysctl.log"
        _fake_bin(tmp_path, "systemctl", f'echo "$*" >> "{log}"\n'
                                         f'[ "$2" = is-active ] && exit {0 if active else 3}\nexit 0\n')
        _fake_bin(tmp_path, "loginctl", "echo Linger=yes\n")
        env = dict(os.environ, HOME=str(home), PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}")
        proc = subprocess.run(["bash", str(SCRIPTS / "install_memora_watchdog.sh"), *args], env=env,
                              capture_output=True, text=True, timeout=60)
        return proc, home, (log.read_text().splitlines() if log.exists() else [])

    def test_the_unit_carries_the_container_the_port_and_the_numbers(self, tmp_path):
        proc, home, sysctl = self._install(tmp_path, "memora-all", "8920", "docker")
        assert proc.returncode == 0, proc.stderr
        unit = (home / ".config/systemd/user/memora-watchdog-memora-all.service").read_text()
        for needle in ("MEMORA_WATCHDOG_CONTAINER=memora-all", "MEMORA_WATCHDOG_URL=http://127.0.0.1:8920/health",
                       "MEMORA_WATCHDOG_RUNTIME=docker", "MEMORA_WATCHDOG_MAX_RESTARTS_PER_HOUR=3",
                       "MEMORA_WATCHDOG_THRESHOLD=3", "MEMORA_WATCHDOG_INTERVAL=10", "Restart=always",
                       f"{home}/.local/libexec/memora/watchdog-memora-all/memora_watchdog_alert_clmux.sh",
                       f"EnvironmentFile=-{home}/.config/memora/watchdog.env"):
            assert needle in unit, needle
        assert "/health/db" not in unit
        assert (home / ".local/libexec/memora/watchdog-memora-all/memora_watchdog.py").stat().st_mode & stat.S_IXUSR
        assert "memora-watchdog-alerts-memora-all.log" in unit
        assert sysctl[:3] == ["--user daemon-reload", "--user enable memora-watchdog-memora-all.service",
                              "--user restart memora-watchdog-memora-all.service"]
        assert "memora-watchdog-memora-all.service active" in proc.stdout

    def test_the_unit_name_follows_the_container_so_a_scratch_one_never_touches_production(self, tmp_path):
        proc, home, _ = self._install(tmp_path, "scratch-wd", "18921", "docker")
        assert proc.returncode == 0
        assert (home / ".config/systemd/user/memora-watchdog-scratch-wd.service").exists()
        assert not (home / ".config/systemd/user/memora-watchdog-memora-all.service").exists()

    @pytest.mark.parametrize("args", [("bad name", "8920"), ("-x", "8920"), ("c", "80x"), ("c", "123456"),
                                      ("c", "8920", "rkt"), ("c",), ()])
    def test_bad_arguments_install_nothing(self, tmp_path, args):
        proc, home, sysctl = self._install(tmp_path, *args)
        assert proc.returncode != 0 and sysctl == []
        assert not (home / ".config/systemd/user").exists() or not list((home / ".config/systemd/user").iterdir())

    def test_an_inactive_service_fails_the_install(self, tmp_path):
        proc, _, _ = self._install(tmp_path, "memora-all", "8920", active=False)
        assert proc.returncode != 0 and "is not active" in proc.stderr


def test_the_embedding_host_switch_holds_the_watchdog_around_its_intentional_stop():
    text = (SCRIPTS / "switch-embedding-host.sh").read_text()
    hold = text.index("hold --container memora-all --minutes 45")
    stop = text.index("docker stop memora-all\ndocker rename")
    release = text.index("release --container memora-all")
    healthy = text.index('"$healthy" -ne 1')
    assert hold < stop < healthy < release


class TestHoldDuringARestartInFlight:
    """Review 12909: a hold taken AFTER restart() began must still stop it, and `hold` must not return while the
    old restart is still working on the container."""

    def _arm(self, w):
        """Fail liveness until the threshold so the next tick restarts."""
        w.probe = lambda: False
        w.tick(); w.tick()

    def test_a_hold_taken_during_the_stop_prevents_the_kill_and_the_start(self, ddog):
        self._arm(ddog)
        original = ddog.docker.__call__

        def stop_that_gets_held(cmd, **kw):
            result = original(cmd, **kw)
            if cmd[1] == "stop":
                watchdog.write_hold(ddog.hold_path, ddog.wall[0], 45, "deploy")   # the deploy's hold lands now
                ddog.docker.state = "running"                                    # the kill branch would be next
            return result

        ddog._run = lambda cmd, **kw: stop_that_gets_held(cmd, **kw)
        assert ddog.tick() == "held"
        verbs = [c[1] for c in ddog.docker.calls]
        assert "start" not in verbs and "kill" not in verbs, verbs
        assert ddog.restarts == 0 and ddog.failed_restarts == 0 and ddog.alerts == []
        assert ddog._attempts == [], "an aborted restart must not count toward the cap"

    def test_a_hold_taken_during_the_verification_stops_the_waiting_loop(self, ddog):
        self._arm(ddog)
        rounds = []

        def probe():
            rounds.append(1)
            if len(rounds) == 2:                                 # first verification round
                watchdog.write_hold(ddog.hold_path, ddog.wall[0], 45, "deploy")
            return False

        ddog.probe = probe
        assert ddog.tick() == "held" and len(rounds) <= 3
        assert not [a for a in ddog.alerts]

    def test_hold_waits_for_the_restart_in_flight_and_returns_only_after_it_stopped(self, ddog, tmp_path, monkeypatch):
        import threading
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        ddog.hold_path = watchdog.default_hold_path("memora-all")
        self._arm(ddog)
        entered, release = threading.Event(), threading.Event()
        original = ddog.docker.__call__

        def blocking(cmd, **kw):
            if cmd[1] == "stop":
                entered.set()
                assert release.wait(10)
            return original(cmd, **kw)

        ddog._run = lambda cmd, **kw: blocking(cmd, **kw)
        outcome = []
        t = threading.Thread(target=lambda: outcome.append(ddog.tick()))
        t.start()
        assert entered.wait(10)                                  # the restart is inside `docker stop`
        result = []
        h = threading.Thread(target=lambda: result.append(
            watchdog.main(["hold", "--container", "memora-all", "--minutes", "5", "--reason", "deploy"])))
        h.start()
        h.join(1.0)
        assert h.is_alive(), "hold returned while a restart was still in flight"
        release.set()
        t.join(10); h.join(10)
        assert result == [0] and outcome == ["held"]
        assert "start" not in [c[1] for c in ddog.docker.calls], "the old restart carried on into start after the hold"

    def test_hold_fails_loudly_when_a_restart_will_not_finish(self, ddog, tmp_path, monkeypatch, capsys):
        import fcntl
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        monkeypatch.setattr(watchdog, "HOLD_WAIT_S", 0.5)
        path = watchdog.default_hold_path("memora-all")
        os.makedirs(os.path.dirname(path))
        with open(path + ".busy", "a+") as busy:
            fcntl.flock(busy, fcntl.LOCK_EX | fcntl.LOCK_NB)      # a restart that never ends
            rc = watchdog.main(["hold", "--container", "memora-all", "--minutes", "5"])
        assert rc == 3 and "still in flight" in capsys.readouterr().err
        assert os.path.exists(path), "the hold stays: the safe direction"

    def test_a_tick_does_not_begin_a_restart_while_a_hold_is_waiting_for_the_lock(self, ddog, tmp_path, monkeypatch):
        import fcntl
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        ddog.hold_path = watchdog.default_hold_path("memora-all")
        os.makedirs(os.path.dirname(ddog.hold_path))
        self._arm(ddog)
        with open(ddog.hold_path + ".busy", "a+") as busy:
            fcntl.flock(busy, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert ddog.tick() == "held"
        assert ddog.docker.calls == []

    def test_an_ordinary_restart_still_completes_and_releases_the_lock(self, ddog):
        import fcntl
        _up_after_start(ddog)
        for _ in range(3):
            out = ddog.tick()
        assert out == "restarted"
        with open(ddog.hold_path + ".busy", "a+") as busy:
            fcntl.flock(busy, fcntl.LOCK_EX | fcntl.LOCK_NB)       # free again


class TestInstallerIsolation:
    def test_a_scratch_install_cannot_change_production_files(self, tmp_path):
        import hashlib

        def tree(root):
            return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted(pathlib.Path(root).rglob("*")) if p.is_file()}

        a, b = tmp_path / "A", tmp_path / "B"
        for d in (a, b):
            d.mkdir()
            for name in ("install_memora_watchdog.sh", "memora_watchdog.py", "memora_watchdog_alert_clmux.sh"):
                (d / name).write_text((SCRIPTS / name).read_text())
        (b / "memora_watchdog_alert_clmux.sh").write_text("#!/bin/bash\necho SCRATCH-VERSION\n")
        (b / "memora_watchdog.py").write_text("# scratch checkout\n" + (SCRIPTS / "memora_watchdog.py").read_text())
        home = tmp_path / "home"
        home.mkdir()
        _fake_bin(tmp_path, "systemctl", "exit 0\n")
        _fake_bin(tmp_path, "loginctl", "echo Linger=yes\n")
        env = dict(os.environ, HOME=str(home), PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}")

        def install(src, container, port):
            return subprocess.run(["bash", str(src / "install_memora_watchdog.sh"), container, port, "docker"], env=env,
                                  capture_output=True, text=True, timeout=60)

        assert install(a, "memora-all", "8920").returncode == 0
        production = tree(home)
        assert install(b, "scratch-wd", "18921").returncode == 0
        after = tree(home)
        for path, digest in production.items():
            assert after[path] == digest, f"the scratch install changed production's {path}"
        assert (home / ".local/libexec/memora/watchdog-scratch-wd/memora_watchdog_alert_clmux.sh").read_text().count("SCRATCH") == 1
        assert "SCRATCH" not in (home / ".local/libexec/memora/watchdog-memora-all/memora_watchdog_alert_clmux.sh").read_text()
        unit = (home / ".config/systemd/user/memora-watchdog-scratch-wd.service").read_text()
        assert "watchdog-scratch-wd/memora_watchdog.py" in unit and "watchdog-memora-all" not in unit
        assert "alerts-scratch-wd.log" in unit

    def test_updating_and_removing_scratch_files_leaves_production_byte_identical(self, tmp_path):
        import hashlib, shutil
        home = tmp_path / "home"
        home.mkdir()
        _fake_bin(tmp_path, "systemctl", "exit 0\n")
        _fake_bin(tmp_path, "loginctl", "echo Linger=yes\n")
        env = dict(os.environ, HOME=str(home), PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}")

        def install(container, port):
            return subprocess.run(["bash", str(SCRIPTS / "install_memora_watchdog.sh"), container, port, "docker"],
                                  env=env, capture_output=True, text=True, timeout=60)

        def prod():
            files = [home / ".config/systemd/user/memora-watchdog-memora-all.service"] + sorted(
                (home / ".local/libexec/memora/watchdog-memora-all").iterdir())
            return {str(f): hashlib.sha256(f.read_bytes()).hexdigest() for f in files}

        assert install("memora-all", "8920").returncode == 0
        before = prod()
        assert install("rehearsal-x", "18921").returncode == 0
        assert install("rehearsal-x", "18922").returncode == 0                      # an update
        shutil.rmtree(home / ".local/libexec/memora/watchdog-rehearsal-x")           # its cleanup
        (home / ".config/systemd/user/memora-watchdog-rehearsal-x.service").unlink()
        assert prod() == before

    def test_a_reinstall_replaces_atomically_an_open_old_file_keeps_its_content(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        _fake_bin(tmp_path, "systemctl", "exit 0\n")
        _fake_bin(tmp_path, "loginctl", "echo Linger=yes\n")
        env = dict(os.environ, HOME=str(home), PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}")
        src = tmp_path / "src"
        src.mkdir()
        for name in ("install_memora_watchdog.sh", "memora_watchdog.py", "memora_watchdog_alert_clmux.sh"):
            (src / name).write_text((SCRIPTS / name).read_text())

        def install():
            return subprocess.run(["bash", str(src / "install_memora_watchdog.sh"), "memora-all", "8920", "docker"],
                                  env=env, capture_output=True, text=True, timeout=60)

        assert install().returncode == 0
        target = home / ".local/libexec/memora/watchdog-memora-all/memora_watchdog_alert_clmux.sh"
        with open(target) as old:
            (src / "memora_watchdog_alert_clmux.sh").write_text("#!/bin/bash\necho NEW\n")
            assert install().returncode == 0
            assert "NEW" not in old.read(), "the running file was rewritten in place"
        assert "NEW" in target.read_text()
        assert not [p for p in target.parent.iterdir() if p.name.startswith(".")], "temp files left behind"


class TestHoldFileRaces:
    def test_a_hold_replaced_between_the_stale_read_and_the_cleanup_survives(self, tmp_path):
        path = str(tmp_path / "w.hold")
        pathlib.Path(path).write_text(json.dumps({"expires": 10.0, "reason": "old"}))
        fresh = {}

        def deploy_replaces_it():
            fresh.update(watchdog.write_hold(path, 1000.0, 45, "deploy"))     # atomically, after the stale read

        watchdog._after_stale_read = deploy_replaces_it
        try:
            got = watchdog.read_hold(path, 1000.0)
        finally:
            watchdog._after_stale_read = None
        assert got is not None and got["reason"] == "deploy", "the stale-cleanup deleted the NEW hold"
        assert json.loads(pathlib.Path(path).read_text())["reason"] == "deploy"

    def test_an_unreadable_hold_replaced_in_between_survives_too(self, tmp_path):
        path = str(tmp_path / "w.hold")
        pathlib.Path(path).write_text("garbage")
        watchdog._after_stale_read = lambda: watchdog.write_hold(path, 1000.0, 45, "deploy")
        try:
            assert watchdog.read_hold(path, 1000.0)["reason"] == "deploy"
        finally:
            watchdog._after_stale_read = None
        assert os.path.exists(path)

    def test_a_stale_hold_is_still_removed_when_nothing_replaces_it(self, tmp_path):
        path = str(tmp_path / "w.hold")
        pathlib.Path(path).write_text(json.dumps({"expires": 10.0}))
        assert watchdog.read_hold(path, 1000.0) is None and not os.path.exists(path)

    def test_stale_cleanup_waits_for_a_hold_being_created(self, tmp_path):
        """The hold lock serialises create, release and cleanup: a cleanup cannot run inside a create."""
        import threading
        path = str(tmp_path / "w.hold")
        pathlib.Path(path).write_text(json.dumps({"expires": 10.0}))
        order = []
        inside = threading.Event()
        proceed = threading.Event()

        def creator():
            with watchdog._HoldLock(path):
                inside.set()
                assert proceed.wait(10)
                order.append("create")
                watchdog.os.replace(_write_tmp(path), path)

        def _write_tmp(p):
            tmp = p + ".t"
            pathlib.Path(tmp).write_text(json.dumps({"expires": 5000.0, "reason": "deploy"}))
            return tmp

        t = threading.Thread(target=creator)
        t.start()
        assert inside.wait(10)
        result = []
        r = threading.Thread(target=lambda: result.append(watchdog.read_hold(path, 1000.0)))
        r.start()
        r.join(0.5)
        assert r.is_alive(), "the cleanup ran while a hold was being created"
        proceed.set()
        t.join(10); r.join(10)
        assert result[0] is not None and result[0]["reason"] == "deploy"

    def test_release_is_serialised_and_idempotent(self, tmp_path):
        path = str(tmp_path / "w.hold")
        watchdog.write_hold(path, 1000.0, 5, "x")
        assert watchdog.release_hold(path) is True and watchdog.release_hold(path) is False


class TestHoldBeforeStart:
    def test_a_hold_arriving_after_the_stop_and_before_the_start_prevents_the_start(self, ddog):
        ddog.probe = lambda: False
        ddog.tick(); ddog.tick()
        original = ddog.docker.__call__
        seen = {"inspects": 0}

        def runner(cmd, **kw):
            out = original(cmd, **kw)
            if cmd[1] == "inspect":
                seen["inspects"] += 1
                if seen["inspects"] == 2:             # the state read right after the stop: the hold lands before `start`
                    watchdog.write_hold(ddog.hold_path, ddog.wall[0], 45, "deploy")
            return out

        ddog._run = lambda cmd, **kw: runner(cmd, **kw)
        assert ddog.tick() == "held"
        assert "start" not in [c[1] for c in ddog.docker.calls]


class TestColdStart:
    def _failing(self, w):
        w.probe = lambda: False

    def test_a_container_that_just_started_is_not_restarted_while_it_warms_up(self, ddog):
        ddog.docker.started_at = time.strftime("%Y-%m-%dT%H:%M:%S.000000000Z", time.gmtime(ddog.wall[0] - 3))
        self._failing(ddog)
        outcomes = [ddog.tick() for _ in range(6)]
        assert set(outcomes) == {"warming"} and ddog.docker.calls == [] and ddog._attempts == []
        ddog.probe = lambda: True
        assert ddog.tick() == "healthy"

    def test_the_grace_ends_with_the_startup_window(self, ddog):
        ddog.docker.started_at = time.strftime("%Y-%m-%dT%H:%M:%S.000000000Z", time.gmtime(ddog.wall[0] - 3))
        self._failing(ddog)
        assert ddog.tick() == "warming"
        ddog.wall[0] += ddog.startup + 1
        assert ddog.tick() == "below_threshold"

    def test_a_hung_old_process_is_not_graced(self, ddog):
        self._failing(ddog)                                          # started in 2020
        assert [ddog.tick() for _ in range(3)][-1] in ("restarted", "restart_failed")

    def test_a_crash_loop_gets_no_second_grace(self, ddog):
        self._failing(ddog)
        fmt = "%Y-%m-%dT%H:%M:%S.000000000Z"
        ddog.docker.started_at = time.strftime(fmt, time.gmtime(ddog.wall[0] - 3))
        assert ddog.tick() == "warming"
        ddog.wall[0] += 20
        ddog.docker.started_at = time.strftime(fmt, time.gmtime(ddog.wall[0] - 3))      # restarted again by docker
        assert ddog.tick() == "below_threshold"

    def test_an_unreadable_started_at_means_no_grace(self, ddog):
        ddog.docker.started_at = "not a time"
        self._failing(ddog)
        assert ddog.tick() == "below_threshold"

    def test_the_apple_runtime_never_asks(self):
        w = watchdog.Watchdog("memora-all", "http://127.0.0.1:8920/health")
        assert w._warming_up() is False
