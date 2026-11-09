"""Tests for the component health probe in ``pipeline.component_health``.

``component_status(environ=None)`` answers one question for the metrics
scrape: is each component of the pipeline alive, and how long ago did it
last say so? It returns a list of ``{"component", "up", "age_s"}`` rows for
the three components the dashboard already watches:

* ``dashboard`` -- always up, age 0: the dashboard is the thing serving the
  scrape, so it cannot report itself as down;
* ``mcp`` -- up when a TCP connect to ``127.0.0.1:$PIPELINE_MCP_PORT``
  (default 8080) succeeds, down when the connection is refused;
* ``scheduler`` -- up when ``<PLAN_DIR>/.scheduler_health.json`` says
  ``alive`` is true and its ``last_scan_ts`` is younger than
  ``COMPONENT_STALE_SECONDS`` (default 180).

Graded here:

* the probe never raises and never blocks for more than about a second --
  a scrape must not hang on a dead component;
* a refused connection means the MCP transport is down, a live listener
  means it is up, and the connect targets the loopback port the
  environment names (8080 by default);
* a missing, malformed, unreadable, not-alive, stale or unparseable
  scheduler health file means the scheduler is down, with the age the file
  implies (``None`` when the age is unknown);
* the age of a timestamp in the future clamps to zero rather than going
  negative;
* the exposition carries both new series with the right labels and leaves
  every pre-existing metric untouched.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pipeline import component_health as ch
from pipeline import metrics_exposition as m

#: The three components the probe reports.
COMPONENTS = ("dashboard", "mcp", "scheduler")

#: The default staleness threshold, in seconds.
DEFAULT_STALE_SECONDS = 180.0

#: The health file the scheduler daemon writes, relative to the plan dir.
HEALTH_FILE = ".scheduler_health.json"


# --- helpers -----------------------------------------------------------------


def _iso(seconds_ago: float) -> str:
    """Return an ISO-8601 UTC timestamp *seconds_ago* in the past."""
    moment = datetime.now(UTC) - timedelta(seconds=seconds_ago)
    return moment.isoformat()


def _write_health(plan_dir: Path, payload: object) -> Path:
    """Write the scheduler health file into *plan_dir* and return its path."""
    path = Path(plan_dir) / HEALTH_FILE
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")
    return path


def _row(report: list[dict], component: str) -> dict:
    """Return the report row for *component*."""
    matches = [row for row in report if row["component"] == component]
    assert len(matches) == 1, f"expected exactly one {component!r} row, got {matches}"
    return matches[0]


def _flag(value: object) -> int:
    """Return a component gauge as 1 or 0, asserting its type."""
    assert value in (True, False, 1, 0), f"a component gauge must be 0/1, got {value!r}"
    return 1 if value else 0


@pytest.fixture
def plan_dir(tmp_path, monkeypatch) -> Path:
    """Point every plan-directory seam at a throwaway directory.

    The probe may read the plan directory from the ``environ`` mapping it is
    handed, from the process environment, or from
    :data:`pipeline.paths.PLAN_DIR`; all three are pointed at the same
    throwaway directory, so the test does not depend on that choice.
    """
    from pipeline import paths

    monkeypatch.setenv("PLAN_DIR", str(tmp_path))
    monkeypatch.setenv("PIPELINE_SCHEDULER_HEALTH_PATH", str(tmp_path / HEALTH_FILE))
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def env(monkeypatch, plan_dir):
    """Return a helper that publishes an environment for the probe."""

    def _publish(**values) -> dict[str, str]:
        environ = {
            "PLAN_DIR": str(plan_dir),
            "PIPELINE_SCHEDULER_HEALTH_PATH": str(Path(plan_dir) / HEALTH_FILE),
        }
        for key, value in values.items():
            environ[key] = str(value)
            monkeypatch.setenv(key, str(value))
        return environ

    return _publish


@pytest.fixture
def live_port() -> int:
    """Return the port of a listening socket on the loopback interface."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        yield listener.getsockname()[1]
    finally:
        listener.close()


@pytest.fixture
def dead_port() -> int:
    """Return a loopback port that nothing listens on."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


# --- the shape of the report --------------------------------------------------


def test_component_status_reports_the_three_components(env, dead_port) -> None:
    report = ch.component_status(env(PIPELINE_MCP_PORT=dead_port))
    assert isinstance(report, list)
    assert [row["component"] for row in report] == list(COMPONENTS)
    for row in report:
        assert {"component", "up", "age_s"} <= set(row)


def test_component_status_returns_a_full_list_when_a_probe_fails(env, monkeypatch) -> None:
    """A broken socket must not cost the caller the other components."""

    def _boom(*args, **kwargs):
        raise OSError("socket exploded")

    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setattr(socket.socket, "connect", _boom)
    report = ch.component_status(env(PIPELINE_MCP_PORT=9))
    assert [row["component"] for row in report] == list(COMPONENTS)
    assert _flag(_row(report, "mcp")["up"]) == 0


def test_component_status_does_not_block_longer_than_about_a_second(
    env, dead_port
) -> None:
    started = time.monotonic()
    ch.component_status(env(PIPELINE_MCP_PORT=dead_port))
    assert time.monotonic() - started < 1.5


# --- dashboard ----------------------------------------------------------------


def test_dashboard_is_always_up_with_age_zero(env, dead_port) -> None:
    row = _row(ch.component_status(env(PIPELINE_MCP_PORT=dead_port)), "dashboard")
    assert _flag(row["up"]) == 1
    assert row["age_s"] == 0


# --- mcp ----------------------------------------------------------------------


def test_mcp_is_up_when_a_listener_accepts_the_connection(env, live_port) -> None:
    report = ch.component_status(env(PIPELINE_MCP_PORT=live_port))
    assert _flag(_row(report, "mcp")["up"]) == 1


def test_mcp_is_down_when_the_connection_is_refused(env, dead_port) -> None:
    report = ch.component_status(env(PIPELINE_MCP_PORT=dead_port))
    assert _flag(_row(report, "mcp")["up"]) == 0


def test_mcp_probe_connects_to_the_loopback_port_the_environment_names(
    env, monkeypatch
) -> None:
    """The default port is 8080, and the host is always the loopback."""
    seen: list[tuple] = []

    def _connect(self, address):
        seen.append(address)
        raise OSError("recorded, not connected")

    monkeypatch.setattr(socket.socket, "connect", _connect)
    ch.component_status(env())
    assert seen, "the MCP probe must attempt a TCP connect"
    host, port = seen[0][0], seen[0][1]
    assert host == "127.0.0.1"
    assert port == 8080


def test_mcp_probe_uses_a_short_connect_timeout(env, monkeypatch, dead_port) -> None:
    """The connect must not wait longer than a fraction of a second."""
    seen: list[object] = []
    original_settimeout = socket.socket.settimeout
    original_create = socket.create_connection

    def _settimeout(self, timeout):
        seen.append(timeout)
        return original_settimeout(self, timeout)

    def _create(address, *args, **kwargs):
        timeout = kwargs.get("timeout", args[0] if args else None)
        seen.append(timeout)
        return original_create(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "settimeout", _settimeout)
    monkeypatch.setattr(socket, "create_connection", _create)
    ch.component_status(env(PIPELINE_MCP_PORT=dead_port))
    timeouts = [value for value in seen if isinstance(value, (int, float))]
    assert timeouts, "the MCP probe must set a connect timeout"
    assert all(value <= 1.0 for value in timeouts)


def test_mcp_probe_gives_up_on_a_stalled_connect(env) -> None:
    """A transport that accepts nothing must not hold the scrape."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(0)
    port = listener.getsockname()[1]
    fillers = []
    for _ in range(5):
        try:
            fillers.append(socket.create_connection(("127.0.0.1", port), timeout=0.2))
        except OSError:
            break
    outcome: dict = {}

    def _run() -> None:
        outcome["report"] = ch.component_status(env(PIPELINE_MCP_PORT=port))

    try:
        worker = threading.Thread(target=_run, daemon=True)
        started = time.monotonic()
        worker.start()
        worker.join(timeout=3.0)
        assert not worker.is_alive(), "the MCP probe blocked on a stalled connect"
        assert time.monotonic() - started < 1.5
        assert _flag(_row(outcome["report"], "mcp")["up"]) == 0
    finally:
        listener.close()
        for filler in fillers:
            filler.close()


# --- scheduler: healthy -------------------------------------------------------


def test_scheduler_is_up_for_a_fresh_alive_health_file(env, plan_dir) -> None:
    _write_health(plan_dir, {"alive": True, "last_scan_ts": _iso(30)})
    row = _row(ch.component_status(env()), "scheduler")
    assert _flag(row["up"]) == 1
    assert isinstance(row["age_s"], (int, float))
    assert 20 <= row["age_s"] <= 60


# --- scheduler: unhealthy -----------------------------------------------------


def test_scheduler_is_down_when_the_health_file_is_missing(env) -> None:
    row = _row(ch.component_status(env()), "scheduler")
    assert _flag(row["up"]) == 0
    assert row["age_s"] is None


def test_scheduler_is_down_when_the_health_file_is_malformed_json(env, plan_dir) -> None:
    _write_health(plan_dir, "{not json at all")
    row = _row(ch.component_status(env()), "scheduler")
    assert _flag(row["up"]) == 0
    assert row["age_s"] is None


def test_scheduler_is_down_when_the_health_file_cannot_be_read(env, plan_dir) -> None:
    """A read that raises means "no report", never an exception."""
    (Path(plan_dir) / HEALTH_FILE).mkdir()
    report = ch.component_status(env())
    assert [row["component"] for row in report] == list(COMPONENTS)
    row = _row(report, "scheduler")
    assert _flag(row["up"]) == 0
    assert row["age_s"] is None


def test_scheduler_is_down_when_alive_is_false(env, plan_dir) -> None:
    _write_health(plan_dir, {"alive": False, "last_scan_ts": _iso(30)})
    row = _row(ch.component_status(env()), "scheduler")
    assert _flag(row["up"]) == 0
    assert row["age_s"] is not None
    assert 20 <= row["age_s"] <= 60


def test_scheduler_is_down_when_the_last_scan_is_stale(env, plan_dir) -> None:
    _write_health(
        plan_dir, {"alive": True, "last_scan_ts": _iso(DEFAULT_STALE_SECONDS + 60)}
    )
    row = _row(ch.component_status(env()), "scheduler")
    assert _flag(row["up"]) == 0
    assert row["age_s"] is not None
    assert row["age_s"] >= DEFAULT_STALE_SECONDS


def test_scheduler_is_down_when_the_timestamp_is_unparseable(env, plan_dir) -> None:
    _write_health(plan_dir, {"alive": True, "last_scan_ts": "not-a-timestamp"})
    row = _row(ch.component_status(env()), "scheduler")
    assert _flag(row["up"]) == 0
    assert row["age_s"] is None


def test_scheduler_is_down_when_the_timestamp_is_missing(env, plan_dir) -> None:
    _write_health(plan_dir, {"alive": True})
    row = _row(ch.component_status(env()), "scheduler")
    assert _flag(row["up"]) == 0
    assert row["age_s"] is None


def test_scheduler_is_down_when_the_health_file_is_not_an_object(env, plan_dir) -> None:
    _write_health(plan_dir, ["alive", True])
    row = _row(ch.component_status(env()), "scheduler")
    assert _flag(row["up"]) == 0


# --- scheduler: boundaries ----------------------------------------------------


def test_scheduler_at_the_stale_threshold_is_down(env, plan_dir) -> None:
    """Exactly at the threshold is not "younger than" the threshold."""
    _write_health(plan_dir, {"alive": True, "last_scan_ts": _iso(DEFAULT_STALE_SECONDS)})
    row = _row(ch.component_status(env()), "scheduler")
    assert _flag(row["up"]) == 0


def test_scheduler_just_under_the_stale_threshold_is_up(env, plan_dir) -> None:
    _write_health(
        plan_dir, {"alive": True, "last_scan_ts": _iso(DEFAULT_STALE_SECONDS - 30)}
    )
    row = _row(ch.component_status(env()), "scheduler")
    assert _flag(row["up"]) == 1


def test_scheduler_in_the_future_clamps_the_age_to_zero(env, plan_dir) -> None:
    _write_health(plan_dir, {"alive": True, "last_scan_ts": _iso(-3600)})
    row = _row(ch.component_status(env()), "scheduler")
    assert row["age_s"] == 0
    assert _flag(row["up"]) == 1


def test_the_stale_threshold_is_configurable(env, plan_dir) -> None:
    """A tighter threshold turns a fresh report stale."""
    _write_health(plan_dir, {"alive": True, "last_scan_ts": _iso(120)})
    row = _row(ch.component_status(env(COMPONENT_STALE_SECONDS=30)), "scheduler")
    assert _flag(row["up"]) == 0


# --- the exposition -----------------------------------------------------------


def _stub_components(rows: list[dict]):
    def _stub(environ=None):
        return list(rows)

    return _stub


def _patch_components(monkeypatch, rows: list[dict]) -> None:
    """Stub the probe on both import styles the exposition may use."""
    monkeypatch.setattr(ch, "component_status", _stub_components(rows))
    monkeypatch.setattr(m, "component_status", _stub_components(rows), raising=False)


def _patch_other_producers(monkeypatch) -> None:
    monkeypatch.setattr(m, "health_extras", lambda plan_dir=None: {"healthy": True})
    monkeypatch.setattr(m, "outbox_summary", lambda plan_dir=None: {"records": 0})
    monkeypatch.setattr(m, "consumer_status", lambda: {"reachable": False})


def test_the_exposition_carries_both_component_series(monkeypatch, tmp_path) -> None:
    _patch_other_producers(monkeypatch)
    _patch_components(
        monkeypatch,
        [
            {"component": "dashboard", "up": True, "age_s": 0},
            {"component": "mcp", "up": False, "age_s": None},
            {"component": "scheduler", "up": True, "age_s": 12.5},
        ],
    )
    snapshot = m.collect(plan_dir=tmp_path)
    up = {labels["component"]: _flag(value) for labels, value in snapshot["fagan_component_up"]}
    assert up == {"dashboard": 1, "mcp": 0, "scheduler": 1}
    ages = {
        labels["component"]: value
        for labels, value in snapshot["fagan_component_health_age_seconds"]
    }
    assert float(ages["dashboard"]) == 0.0
    assert float(ages["scheduler"]) == 12.5
    assert "mcp" not in ages or ages["mcp"] is None


def test_the_exposition_omits_an_unknown_component_age(monkeypatch, tmp_path) -> None:
    """An unknown age is omitted or null -- never a fabricated zero."""
    _patch_other_producers(monkeypatch)
    _patch_components(
        monkeypatch,
        [
            {"component": "dashboard", "up": True, "age_s": 0},
            {"component": "mcp", "up": False, "age_s": None},
        ],
    )
    snapshot = m.collect(plan_dir=tmp_path)
    rendered = m.render(snapshot)
    assert "NaN" not in rendered
    for labels, value in snapshot.get("fagan_component_health_age_seconds", []):
        if labels.get("component") == "mcp":
            assert value is None


def test_the_exposition_leaves_the_pre_existing_metrics_unchanged(
    monkeypatch, tmp_path
) -> None:
    _patch_other_producers(monkeypatch)
    _patch_components(monkeypatch, [{"component": "dashboard", "up": True, "age_s": 0}])
    snapshot = m.collect(plan_dir=tmp_path)
    assert snapshot["fagan_scheduler_healthy"] == [({}, True)]
    assert snapshot["fagan_broker_reachable"] == [({}, 0)]
    assert snapshot["fagan_outbox_records"] == [({}, 0)]
