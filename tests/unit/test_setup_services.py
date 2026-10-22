"""Tests for ``pipeline.setup.services``.

Every test injects ``runner``/``opener``/``sleep``: no real process is
started, no socket is opened, and nothing sleeps. The API-key sentinel below
must never surface in a returned dict, an output tail, or a reason string.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pipeline.setup import services

API_KEY = "sk-test-SENTINEL-do-not-leak-0123456789"

DARWIN_PLIST = "com.fagan.pipeline.advance-scheduler.plist"
LINUX_UNIT = "com.fagan.pipeline.advance-scheduler.service"


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeRunner:
    """Records every argv and answers from an optional ``responder``."""

    def __init__(self, responder=None):
        self.calls: list[dict] = []
        self._responder = responder

    def __call__(self, argv, timeout=None, env=None):
        argv = list(argv)
        self.calls.append({"argv": argv, "timeout": timeout, "env": env})
        if self._responder is None:
            return (0, "", "")
        result = self._responder(argv, env)
        return (0, "", "") if result is None else result

    @property
    def argvs(self):
        return [call["argv"] for call in self.calls]


class FakeResponse:
    def __init__(self, status=200, body=b'{"status": "ok"}'):
        self.status = status
        self._body = body
        self.closed = False

    def read(self):
        return self._body

    def close(self):
        self.closed = True


class FakeOpener:
    """Replays ``outcomes``; the last outcome repeats once the list runs dry."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if len(self.outcomes) > 1:
            outcome = self.outcomes.pop(0)
        else:
            outcome = self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _write_rendered(argv, names):
    out_dir = Path(argv[argv.index("--out-dir") + 1])
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (out_dir / name).write_text("rendered\n")


# --------------------------------------------------------------------------
# render_services - positive
# --------------------------------------------------------------------------


def test_darwin_render_issues_exact_generator_argv(tmp_path):
    runner = FakeRunner()

    result = services.render_services(
        repo_root=tmp_path,
        platform="darwin",
        mlx_model_path="/models/llama-3",
        runner=runner,
    )

    assert runner.argvs == [
        [
            "bash",
            str(tmp_path / "scripts" / "generate_launchd_plists.sh"),
            "--repo-root",
            str(tmp_path),
            "--out-dir",
            str(tmp_path / "launchd"),
            "--mlx-model-path",
            "/models/llama-3",
        ]
    ]
    assert runner.calls[0]["timeout"] == 120
    assert result["platform"] == "darwin"
    assert result["status"] == "rendered"
    assert result["enabled"] is None
    assert result["error"] is None


def test_darwin_render_reports_rendered_names(tmp_path):
    names = [
        "com.fagan.pipeline.advance-scheduler.plist",
        "com.fagan.pipeline.usage-poller.plist",
        "com.fagan.pipeline.mlx-supervisor.plist",
        "pipeline-logs.newsyslog.conf",
    ]

    def responder(argv, env):
        _write_rendered(argv, names + ["notes.txt", "com.other.tool.plist"])
        return (0, "rendered", "")

    result = services.render_services(
        repo_root=tmp_path,
        platform="darwin",
        mlx_model_path="/models/llama-3",
        runner=FakeRunner(responder),
    )

    assert result["status"] == "rendered"
    assert result["rendered"] == sorted(names)


def test_linux_render_issues_exact_systemd_argv(tmp_path):
    names = [
        "com.fagan.pipeline.advance-scheduler.service",
        "com.fagan.pipeline.usage-poller.service",
        "com.fagan.pipeline.usage-poller.timer",
        "pipeline-logs.logrotate.conf",
    ]

    def responder(argv, env):
        _write_rendered(argv, names)
        return (0, "", "")

    runner = FakeRunner(responder)

    result = services.render_services(repo_root=tmp_path, platform="linux", runner=runner)

    assert runner.argvs == [
        [
            "bash",
            str(tmp_path / "scripts" / "generate_systemd_units.sh"),
            "--repo-root",
            str(tmp_path),
            "--out-dir",
            str(tmp_path / "systemd"),
        ]
    ]
    assert runner.calls[0]["timeout"] == 120
    assert result["platform"] == "linux"
    assert result["status"] == "rendered"
    assert result["rendered"] == sorted(names)


def test_platform_defaults_to_sys_platform(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    runner = FakeRunner()

    result = services.render_services(repo_root=tmp_path, runner=runner)

    assert result["platform"] == "linux"
    assert runner.argvs[0][1] == str(tmp_path / "scripts" / "generate_systemd_units.sh")


# --------------------------------------------------------------------------
# render_services - negative / boundary
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mlx_model_path", [None, "", "   "])
def test_darwin_without_mlx_path_skips_without_running(tmp_path, mlx_model_path):
    runner = FakeRunner()

    result = services.render_services(
        repo_root=tmp_path,
        platform="darwin",
        mlx_model_path=mlx_model_path,
        enable=True,
        runner=runner,
    )

    assert result["platform"] == "darwin"
    assert result["status"] == "skipped"
    assert result["rendered"] == []
    assert result["enabled"] is None
    assert "MLX_MODEL_PATH" in result["reason"]
    assert runner.calls == []
    assert list(tmp_path.iterdir()) == []


def test_generator_failure_returns_failed_with_stderr_tail(tmp_path):
    stderr = (
        "error: --mlx-model-path not given and MLX_MODEL_PATH env var is "
        "unset or empty"
    )
    runner = FakeRunner(lambda argv, env: (1, "", stderr))

    result = services.render_services(
        repo_root=tmp_path,
        platform="darwin",
        mlx_model_path="/models/llama-3",
        enable=True,
        runner=runner,
    )

    assert result["status"] == "failed"
    assert "MLX_MODEL_PATH env var is unset" in result["error"]
    assert result["rendered"] == []
    assert result["enabled"] is None
    # A failed render must not attempt to enable anything.
    assert len(runner.calls) == 1


def test_generator_failure_with_empty_stderr_still_reports_error(tmp_path):
    runner = FakeRunner(lambda argv, env: (2, "", ""))

    result = services.render_services(
        repo_root=tmp_path, platform="linux", runner=runner
    )

    assert result["status"] == "failed"
    assert result["error"]


def test_unsupported_platform_skipped(tmp_path):
    runner = FakeRunner()

    result = services.render_services(repo_root=tmp_path, platform="win32", runner=runner)

    assert result["status"] == "skipped"
    assert "win32" in result["reason"]
    assert result["rendered"] == []
    assert result["enabled"] is None
    assert runner.calls == []


def test_runner_raising_is_returned_as_failure(tmp_path):
    def boom(argv, env):
        raise OSError("bash not found")

    result = services.render_services(
        repo_root=tmp_path,
        platform="linux",
        runner=FakeRunner(boom),
    )

    assert result["status"] == "failed"
    assert "bash not found" in result["error"]


# --------------------------------------------------------------------------
# render_services - enable
# --------------------------------------------------------------------------


def test_enable_darwin_loads_when_not_registered(tmp_path):
    def responder(argv, env):
        if argv[:2] == ["launchctl", "list"]:
            return (0, "PID\tStatus\tLabel\n-\t0\tcom.fagan.pipeline.usage-poller", "")
        return (0, "", "")

    runner = FakeRunner(responder)

    result = services.render_services(
        repo_root=tmp_path,
        platform="darwin",
        mlx_model_path="/models/llama-3",
        enable=True,
        runner=runner,
    )

    assert result["status"] == "rendered"
    assert result["enabled"] is True
    assert result["error"] is None
    assert runner.argvs[1] == ["launchctl", "list"]
    assert runner.argvs[2] == [
        "launchctl",
        "load",
        "-w",
        str(tmp_path / "launchd" / DARWIN_PLIST),
    ]


def test_enable_darwin_skips_load_when_already_registered(tmp_path):
    def responder(argv, env):
        if argv[:2] == ["launchctl", "list"]:
            return (0, "PID\tStatus\tLabel\n123\t0\tcom.fagan.pipeline.advance-scheduler", "")
        return (0, "", "")

    runner = FakeRunner(responder)

    result = services.render_services(
        repo_root=tmp_path,
        platform="darwin",
        mlx_model_path="/models/llama-3",
        enable=True,
        runner=runner,
    )

    assert result["enabled"] is True
    assert result["error"] is None
    assert runner.argvs[1] == ["launchctl", "list"]
    assert len(runner.calls) == 2  # generator + list, no load


def test_enable_linux_runs_daemon_reload_then_enable(tmp_path):
    runner = FakeRunner()

    result = services.render_services(
        repo_root=tmp_path, platform="linux", enable=True, runner=runner
    )

    assert result["enabled"] is True
    assert result["error"] is None
    assert runner.argvs[1] == ["systemctl", "--user", "daemon-reload"]
    assert runner.argvs[2] == [
        "systemctl",
        "--user",
        "enable",
        "--now",
        LINUX_UNIT,
    ]


def test_enable_false_skips_enable_commands(tmp_path):
    runner = FakeRunner()

    result = services.render_services(
        repo_root=tmp_path,
        platform="darwin",
        mlx_model_path="/models/llama-3",
        enable=False,
        runner=runner,
    )

    assert result["status"] == "rendered"
    assert result["enabled"] is None
    assert len(runner.calls) == 1


def test_enable_failure_returns_false_with_error_and_no_exception(tmp_path):
    def responder(argv, env):
        if argv[:2] == ["launchctl", "list"]:
            return (0, "", "")
        if argv[0] == "launchctl":
            return (1, "", "Load failed: service already loaded")
        return (0, "", "")

    runner = FakeRunner(responder)

    result = services.render_services(
        repo_root=tmp_path,
        platform="darwin",
        mlx_model_path="/models/llama-3",
        enable=True,
        runner=runner,
    )

    assert result["status"] == "rendered"
    assert result["enabled"] is False
    assert "Load failed" in result["error"]


def test_enable_linux_daemon_reload_failure_returns_false(tmp_path):
    def responder(argv, env):
        if argv[:3] == ["systemctl", "--user", "daemon-reload"]:
            return (1, "", "Failed to connect to bus")
        return (0, "", "")

    runner = FakeRunner(responder)

    result = services.render_services(
        repo_root=tmp_path, platform="linux", enable=True, runner=runner
    )

    assert result["enabled"] is False
    assert "bus" in result["error"]
    assert len(runner.calls) == 2  # generator + daemon-reload, no enable


# --------------------------------------------------------------------------
# start_services
# --------------------------------------------------------------------------


def test_start_services_issues_both_commands_and_scopes_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PORT", "9999")
    runner = FakeRunner()

    result = services.start_services(repo_root=tmp_path, port=8080, runner=runner)

    assert runner.argvs == [
        ["bash", str(tmp_path / "scripts" / "dashboard.sh"), "start"],
        ["bash", str(tmp_path / "scripts" / "scheduler.sh"), "start"],
    ]
    assert [call["timeout"] for call in runner.calls] == [120, 120]
    assert runner.calls[0]["env"]["DASHBOARD_PORT"] == "8080"
    assert runner.calls[0]["env"]["PATH"] == os.environ["PATH"]
    assert runner.calls[1]["env"] is None
    # os.environ must not be mutated.
    assert os.environ["DASHBOARD_PORT"] == "9999"
    assert result == {
        "dashboard": {"ok": True, "output": ""},
        "scheduler": {"ok": True, "output": ""},
        "port": 8080,
    }


def test_start_services_one_failure_does_not_stop_the_other(tmp_path):
    def responder(argv, env):
        if "dashboard.sh" in argv[1]:
            return (1, "", "dashboard failed to bind port")
        return (0, "scheduler started", "")

    runner = FakeRunner(responder)

    result = services.start_services(repo_root=tmp_path, port=8080, runner=runner)

    assert result["dashboard"]["ok"] is False
    assert "dashboard failed to bind port" in result["dashboard"]["output"]
    assert result["scheduler"]["ok"] is True
    assert "scheduler started" in result["scheduler"]["output"]
    assert len(runner.calls) == 2


def test_start_services_scheduler_failure_does_not_stop_dashboard(tmp_path):
    def responder(argv, env):
        if "scheduler.sh" in argv[1]:
            return (1, "", "scheduler exploded")
        return (0, "dashboard started", "")

    result = services.start_services(
        repo_root=tmp_path, port=8080, runner=FakeRunner(responder)
    )

    assert result["dashboard"]["ok"] is True
    assert result["scheduler"]["ok"] is False
    assert "scheduler exploded" in result["scheduler"]["output"]


def test_start_services_runner_raising_is_reported_not_raised(tmp_path):
    def boom(argv, env):
        raise OSError("permission denied")

    result = services.start_services(
        repo_root=tmp_path, port=8080, runner=FakeRunner(boom)
    )

    assert result["dashboard"]["ok"] is False
    assert result["scheduler"]["ok"] is False
    assert "permission denied" in result["dashboard"]["output"]


@pytest.mark.parametrize("port", [0, 65536, -1, 70000])
def test_start_services_rejects_out_of_range_port(tmp_path, port):
    runner = FakeRunner()

    with pytest.raises(ValueError):
        services.start_services(repo_root=tmp_path, port=port, runner=runner)

    assert runner.calls == []


@pytest.mark.parametrize("port", ["8080", None, 80.5, True])
def test_start_services_rejects_non_integer_port(tmp_path, port):
    runner = FakeRunner()

    with pytest.raises(ValueError):
        services.start_services(repo_root=tmp_path, port=port, runner=runner)

    assert runner.calls == []


@pytest.mark.parametrize("port", [1, 65535])
def test_start_services_accepts_boundary_ports(tmp_path, port):
    result = services.start_services(
        repo_root=tmp_path, port=port, runner=FakeRunner()
    )

    assert result["port"] == port


# --------------------------------------------------------------------------
# verify_health
# --------------------------------------------------------------------------


def test_verify_health_success_sends_exact_header():
    opener = FakeOpener([FakeResponse()])
    sleeps: list[float] = []

    ok, message = services.verify_health(
        port=8080, api_key=API_KEY, opener=opener, sleep=sleeps.append
    )

    assert ok is True
    assert message == "dashboard healthy"
    assert sleeps == []
    assert len(opener.requests) == 1
    request = opener.requests[0]
    assert request.full_url == "http://127.0.0.1:8080/api/health"
    assert request.get_method() == "GET"
    headers = {name.lower(): value for name, value in request.header_items()}
    assert headers["x-pipeline-api-key"] == API_KEY


def test_verify_health_retries_oserror_until_attempts_exhausted():
    opener = FakeOpener([ConnectionRefusedError("connection refused")])
    sleeps: list[float] = []

    ok, message = services.verify_health(
        port=8080,
        api_key=API_KEY,
        attempts=4,
        delay=0.5,
        opener=opener,
        sleep=sleeps.append,
    )

    assert ok is False
    assert len(opener.requests) == 4
    assert sleeps == [0.5, 0.5, 0.5]
    assert API_KEY not in message


def test_verify_health_recovers_after_first_connection_error():
    opener = FakeOpener([ConnectionRefusedError("refused"), FakeResponse()])
    sleeps: list[float] = []

    ok, message = services.verify_health(
        port=8080, api_key=API_KEY, delay=0.25, opener=opener, sleep=sleeps.append
    )

    assert ok is True
    assert message == "dashboard healthy"
    assert len(opener.requests) == 2
    assert sleeps == [0.25]


def test_verify_health_non_200_retries_then_reports_false():
    opener = FakeOpener([FakeResponse(status=503, body=b"unavailable")])

    ok, message = services.verify_health(
        port=8080, api_key=API_KEY, attempts=3, opener=opener, sleep=lambda _: None
    )

    assert ok is False
    assert len(opener.requests) == 3
    assert "503" in message
    assert API_KEY not in message


def test_verify_health_invalid_json_retries_then_reports_false():
    opener = FakeOpener([FakeResponse(body=b"not json at all")])

    ok, message = services.verify_health(
        port=8080, api_key=API_KEY, attempts=2, opener=opener, sleep=lambda _: None
    )

    assert ok is False
    assert len(opener.requests) == 2
    assert "json" in message.lower()
    assert API_KEY not in message


def test_verify_health_redacts_api_key_from_error_text():
    opener = FakeOpener([OSError(f"bad request with key {API_KEY}")])

    ok, message = services.verify_health(
        port=8080, api_key=API_KEY, attempts=1, opener=opener, sleep=lambda _: None
    )

    assert ok is False
    assert API_KEY not in message


@pytest.mark.parametrize("port", [0, 65536, -1])
def test_verify_health_rejects_invalid_port(port):
    with pytest.raises(ValueError):
        services.verify_health(port=port, api_key=API_KEY, opener=FakeOpener([]))


@pytest.mark.parametrize("attempts", [0, -1])
def test_verify_health_rejects_non_positive_attempts(attempts):
    opener = FakeOpener([FakeResponse()])

    with pytest.raises(ValueError):
        services.verify_health(
            port=8080, api_key=API_KEY, attempts=attempts, opener=opener
        )

    assert opener.requests == []


# --------------------------------------------------------------------------
# Isolation: the API key never leaks into any returned structure
# --------------------------------------------------------------------------


def test_api_key_never_appears_in_returned_structures(tmp_path):
    render = services.render_services(
        repo_root=tmp_path,
        platform="darwin",
        mlx_model_path="/models/llama-3",
        enable=True,
        runner=FakeRunner(),
    )
    start = services.start_services(
        repo_root=tmp_path, port=8080, runner=FakeRunner()
    )
    ok, message = services.verify_health(
        port=8080,
        api_key=API_KEY,
        attempts=2,
        opener=FakeOpener([ConnectionRefusedError("refused")]),
        sleep=lambda _: None,
    )

    assert ok is False
    for payload in (render, start):
        assert API_KEY not in json.dumps(payload)
    assert API_KEY not in message
