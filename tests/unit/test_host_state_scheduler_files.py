"""Host-local scheduler + dashboard files move onto the HostState seam.

ADR 0012 (open question 3) keeps ``.dashboard_ui_state.json`` and
``.scheduler_health.json`` host-local: they belong to THIS host, are never
namespaced by plan and are never replicated.  So they live behind the
``HostState`` seam, next to the other host-local records, and the modules
that used to read them straight off disk read them through that seam:

* the dashboard reads/writes the UI-preference file and reads the
  scheduler's snapshot through the host state,
* the scheduler daemon takes its single-instance lock through the host
  state and no longer binds ``PLAN_DIR`` at module load.

What is graded here is behaviour through the public API only:

* the UI state and the scheduler snapshot round-trip through
  ``<root>/.dashboard_ui_state.json`` and ``<root>/.scheduler_health.json``
  (file names unchanged -- REFERENCE.md documents them),
* a missing or malformed file degrades to the SAME "nothing archived" /
  "no fingerprint" shape the dashboard reports today, never an exception,
* the dashboard follows the LIVE ``pipeline.server.PLAN_DIR`` binding (the
  one the host state resolves its root through), not a module-load copy,
* the daemon's single-instance lock lives under that same root: a second
  daemon is refused while the lock is held, and a lock left behind by a
  dead process does not block the next one.

The root is patched through ``pipeline.server.PLAN_DIR`` because that is
the binding ``FileHostState`` resolves at call time (ADR 0012's binding
decision), so ``monkeypatch.setattr(pipeline.server, "PLAN_DIR", d)``
keeps landing.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

import pipeline.server as server_mod
from app import dashboard
from pipeline import health_freshness
from pipeline import scheduler_daemon as daemon_mod

try:  # the seam under test may not exist yet -> RED, not a collection error
    from pipeline import host_state
except ImportError:  # pragma: no cover - exercised only while the seam is missing
    host_state = None

UI_STATE_FILE = ".dashboard_ui_state.json"
HEALTH_FILE = ".scheduler_health.json"
LOCK_FILE = ".scheduler_daemon.lock"

#: The host-local methods this story adds to the host state.
HOST_STATE_METHODS = (
    "get_dashboard_ui_state",
    "put_dashboard_ui_state",
    "read_scheduler_health",
    "write_scheduler_health",
    "scheduler_pid_lock",
)


@pytest.fixture
def plan_root(tmp_path, monkeypatch):
    """Point the host-local root at a temp plans directory.

    ``pipeline.server.PLAN_DIR`` is the binding the host state resolves
    through, so patching it is what makes the host state read and write
    inside ``tmp_path`` instead of the real plan directory.
    """
    root = tmp_path / "plans"
    root.mkdir()
    monkeypatch.setattr(server_mod, "PLAN_DIR", root)
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    monkeypatch.setattr(server_mod, "WORKTREE_ROOT", worktrees)
    # ``pipeline.paths`` is the module-level default other modules read.
    # Point it at an EMPTY sibling directory so a module that still reads
    # it observes "no file here" instead of the real plan directory.
    other = tmp_path / "other-plans"
    other.mkdir()
    from pipeline import paths as paths_mod

    monkeypatch.setattr(paths_mod, "PLAN_DIR", other)
    monkeypatch.setattr(paths_mod, "WORKTREE_ROOT", other)
    return root


@pytest.fixture
def host(plan_root):
    """The host state, built through its only public constructor."""
    assert host_state is not None, "pipeline/host_state.py does not exist yet"
    return host_state.build_host_state()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _worktree_root(plan_root) -> str:
    """The worktree root the fixture pinned (the dashboard resolves it too)."""
    return str(plan_root.parent / "worktrees")


def _fingerprint_config(plan_root) -> dict:
    """A scheduler config fingerprint that agrees with the dashboard."""
    return {"plan_dir": str(plan_root), "worktree_root": _worktree_root(plan_root)}


# ---------------------------------------------------------------------------
# The host-state surface
# ---------------------------------------------------------------------------


def test_host_state_exposes_the_host_local_scheduler_methods(host):
    """The host state owns the UI state, the scheduler snapshot and the lock."""
    for name in HOST_STATE_METHODS:
        assert hasattr(host, name), f"the host state has no {name}()"


# ---------------------------------------------------------------------------
# Dashboard UI state
# ---------------------------------------------------------------------------


def test_dashboard_ui_state_round_trips(host, plan_root):
    """What is written is what is read, under the documented file name."""
    host.put_dashboard_ui_state({"archived": ["beta", "alpha"]})

    state = host.get_dashboard_ui_state()
    assert sorted(state.get("archived", [])) == ["alpha", "beta"]

    raw = json.loads((plan_root / UI_STATE_FILE).read_text(encoding="utf-8"))
    assert sorted(raw.get("archived", [])) == ["alpha", "beta"], (
        "the UI state must keep its documented on-disk name and shape; got "
        f"{raw!r}"
    )


def test_dashboard_ui_state_round_trips_an_empty_state(host, plan_root):
    """An empty preference set is a valid state, not an error."""
    host.put_dashboard_ui_state({"archived": []})

    state = host.get_dashboard_ui_state()
    assert not (state or {}).get("archived")


def test_dashboard_ui_state_defaults_when_the_file_is_missing(host, plan_root):
    """No file means "nothing archived" -- the same answer as today."""
    assert not (plan_root / UI_STATE_FILE).exists()

    state = host.get_dashboard_ui_state()
    assert not (state or {}).get("archived"), (
        "a missing UI-state file must read as the default state; got "
        f"{state!r}"
    )


def test_dashboard_ui_state_defaults_when_the_file_is_malformed(host, plan_root):
    """A corrupt (or wrong-shaped) preference file must not raise."""
    (plan_root / UI_STATE_FILE).write_text("{not json", encoding="utf-8")
    state = host.get_dashboard_ui_state()
    assert not (state or {}).get("archived"), (
        "a malformed UI-state file must read as the default state; got "
        f"{state!r}"
    )

    (plan_root / UI_STATE_FILE).write_text("[]", encoding="utf-8")
    state = host.get_dashboard_ui_state()
    assert not (state or {}).get("archived"), (
        "a wrong-shaped UI-state file must read as the default state; got "
        f"{state!r}"
    )


# ---------------------------------------------------------------------------
# Scheduler health snapshot
# ---------------------------------------------------------------------------


def test_scheduler_health_round_trips(host, plan_root):
    """The daemon's snapshot survives a write/read round trip."""
    payload = {
        "alive": True,
        "last_scan_ts": _now_iso(),
        "config": {"plan_dir": str(plan_root), "worktree_root": str(plan_root)},
    }

    host.write_scheduler_health(payload)

    read = host.read_scheduler_health()
    assert isinstance(read, dict), f"the snapshot must read back as a dict; got {read!r}"
    for key, value in payload.items():
        assert read.get(key) == value, f"the snapshot lost {key!r}: got {read!r}"

    raw = json.loads((plan_root / HEALTH_FILE).read_text(encoding="utf-8"))
    for key, value in payload.items():
        assert raw.get(key) == value, (
            f"the snapshot must keep its documented on-disk name and shape; got {raw!r}"
        )


def test_scheduler_health_is_unavailable_when_the_file_is_missing(host, plan_root):
    """No snapshot means "not reported" -- never an exception."""
    assert not (plan_root / HEALTH_FILE).exists()

    read = host.read_scheduler_health()
    assert read is None or read == {}, (
        f"a missing scheduler snapshot must read as unavailable; got {read!r}"
    )


def test_scheduler_health_is_unavailable_when_the_file_is_malformed(host, plan_root):
    """A corrupt (or wrong-shaped) snapshot must read as unavailable."""
    (plan_root / HEALTH_FILE).write_text("{not json", encoding="utf-8")
    read = host.read_scheduler_health()
    assert read is None or read == {}, (
        f"a malformed scheduler snapshot must read as unavailable; got {read!r}"
    )

    (plan_root / HEALTH_FILE).write_text("[]", encoding="utf-8")
    read = host.read_scheduler_health()
    assert read is None or read == {}, (
        f"a wrong-shaped scheduler snapshot must read as unavailable; got {read!r}"
    )


# ---------------------------------------------------------------------------
# The dashboard reads them through the host state
# ---------------------------------------------------------------------------


def _health_body(plan_root: str | os.PathLike) -> dict:
    res = TestClient(dashboard.app).get("/api/health")
    assert res.status_code == 200, f"/api/health must never 500; got {res.text}"
    return res.json()


def test_health_reads_the_scheduler_snapshot_from_the_host_state_root(
    plan_root, monkeypatch
):
    """The snapshot is read from the host-state root, not a private binding.

    The dashboard's own ``PLAN_DIR`` name is shadowed with a DIFFERENT
    directory: the snapshot must still come from the root the host state
    resolves (the live ``pipeline.server.PLAN_DIR`` binding).
    """
    other = plan_root.parent / "shadow-plans"
    other.mkdir()
    monkeypatch.setattr(dashboard, "PLAN_DIR", other, raising=False)

    config = _fingerprint_config(plan_root)
    (plan_root / HEALTH_FILE).write_text(
        json.dumps({"alive": True, "last_scan_ts": _now_iso(), "config": config}),
        encoding="utf-8",
    )

    body = _health_body(plan_root)
    assert body["scheduler"] == config, (
        "the scheduler fingerprint must be read from the host-state root; got "
        f"{body['scheduler']!r}"
    )


def test_health_reports_the_fingerprint_when_the_snapshot_agrees(plan_root):
    """A well-formed, agreeing snapshot is surfaced verbatim."""
    config = _fingerprint_config(plan_root)
    (plan_root / HEALTH_FILE).write_text(
        json.dumps({"alive": True, "last_scan_ts": _now_iso(), "config": config}),
        encoding="utf-8",
    )

    body = _health_body(plan_root)
    assert body["scheduler"] == config
    assert body["config_mismatch"] == []


def test_health_reports_the_host_state_root_as_the_plan_dir(plan_root):
    """The endpoint's ``plan_dir`` is the root the host state resolves."""
    body = _health_body(plan_root)
    assert body["plan_dir"] == str(plan_root)


def test_the_liveness_probe_reads_the_snapshot_from_the_host_state_root(
    plan_root, monkeypatch
):
    """``/livez`` grades the snapshot the host state resolves.

    A stale snapshot under the host-state root must make the probe report
    the scheduler as stale -- the probe must not look somewhere else.
    """
    other = plan_root.parent / "shadow-plans"
    other.mkdir()
    monkeypatch.setattr(dashboard, "PLAN_DIR", other, raising=False)

    stale = datetime.now(UTC) - timedelta(hours=1)
    (plan_root / HEALTH_FILE).write_text(
        json.dumps({"alive": True, "last_scan_ts": stale.isoformat()}),
        encoding="utf-8",
    )

    res = TestClient(dashboard.app).get("/livez")
    assert res.status_code == 503, (
        "the liveness probe must grade the snapshot under the host-state "
        f"root; got {res.status_code}: {res.text}"
    )
    assert "scheduler_stale" in res.text


def test_health_reports_the_unavailable_shape_when_the_snapshot_is_missing(plan_root):
    """A missing snapshot degrades to "no fingerprint", never a 500."""
    assert not (plan_root / HEALTH_FILE).exists()

    body = _health_body(plan_root)
    assert body["scheduler"] is None
    assert body["config_mismatch"] == []
    assert body["scheduler_age_s"] is None
    assert body["healthy"] is True
    assert body["degraded"] == []


def test_health_reports_the_unavailable_shape_when_the_snapshot_is_malformed(plan_root):
    """A malformed snapshot degrades to the same shape as a missing one."""
    (plan_root / HEALTH_FILE).write_text("{not json", encoding="utf-8")

    body = _health_body(plan_root)
    assert body["scheduler"] is None
    assert body["config_mismatch"] == []
    assert body["scheduler_age_s"] is None
    assert body["healthy"] is True
    assert body["degraded"] == []


def test_health_extras_reads_the_scheduler_snapshot_from_the_host_state_root(
    plan_root,
):
    """The freshness probe follows the host-state root too."""
    (plan_root / HEALTH_FILE).write_text(
        json.dumps({"alive": True, "last_scan_ts": _now_iso()}), encoding="utf-8"
    )

    extras = health_freshness.health_extras()
    assert extras["scheduler_age_s"] is not None, (
        "the freshness extras must read the snapshot from the host-state root; "
        f"got {extras!r}"
    )
    assert extras["healthy"] is True


# ---------------------------------------------------------------------------
# The dashboard's UI preference file
# ---------------------------------------------------------------------------


def _write_manifest(plan_root, name: str) -> None:
    (plan_root / f"{name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": {"S1": {"summary": "s", "status": "todo"}}}),
        encoding="utf-8",
    )


def _plan_names(client: TestClient) -> list[str]:
    res = client.get("/api/plans")
    assert res.status_code == 200, res.text
    return [plan["name"] for plan in res.json()["plans"]]


def test_archived_plans_are_read_from_the_host_state_root(plan_root, monkeypatch):
    """The archived set comes from the host-state root, not a private binding."""
    other = plan_root.parent / "shadow-plans"
    other.mkdir()
    monkeypatch.setattr(dashboard, "PLAN_DIR", other, raising=False)

    _write_manifest(plan_root, "plan-archived")
    (plan_root / UI_STATE_FILE).write_text(
        json.dumps({"archived": ["plan-archived"]}), encoding="utf-8"
    )

    client = TestClient(dashboard.app)
    assert "plan-archived" not in _plan_names(client), (
        "the archived set must be read from the host-state root"
    )


def test_archiving_a_plan_round_trips_through_the_host_state_root(plan_root):
    """Archive / unarchive keep the documented file and shape."""
    _write_manifest(plan_root, "plan-a")
    client = TestClient(dashboard.app)

    res = client.post("/api/plans/plan-a/archive")
    assert res.status_code == 200, res.text
    assert res.json()["archived"] is True

    raw = json.loads((plan_root / UI_STATE_FILE).read_text(encoding="utf-8"))
    assert "plan-a" in raw.get("archived", [])
    assert "plan-a" not in _plan_names(client)

    res = client.post("/api/plans/plan-a/unarchive")
    assert res.status_code == 200, res.text
    assert res.json()["archived"] is False

    raw = json.loads((plan_root / UI_STATE_FILE).read_text(encoding="utf-8"))
    assert "plan-a" not in raw.get("archived", [])
    assert "plan-a" in _plan_names(client)


def test_a_malformed_ui_state_file_leaves_the_plan_list_intact(plan_root):
    """A corrupt preference file must not break the plan list."""
    _write_manifest(plan_root, "plan-a")
    (plan_root / UI_STATE_FILE).write_text("{not json", encoding="utf-8")

    client = TestClient(dashboard.app)
    assert "plan-a" in _plan_names(client)


# ---------------------------------------------------------------------------
# The scheduler daemon's single-instance lock
# ---------------------------------------------------------------------------


def test_scheduler_daemon_does_not_bind_plan_dir_at_module_load():
    """The module-load copy of PLAN_DIR is gone from the daemon."""
    assert not hasattr(daemon_mod, "PLAN_DIR"), (
        "pipeline/scheduler_daemon.py must not bind PLAN_DIR at module load; "
        "the daemon resolves the host-local root through the host state"
    )


def _run_daemon(monkeypatch, plan_root) -> tuple[int, str]:
    """Run the daemon's composition root with the heavy collaborators stubbed.

    Returns the daemon's exit code and what it printed on stderr.
    """
    from contextlib import redirect_stderr
    from io import StringIO

    class FakeDaemon:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def run_forever(self, stop_event) -> None:
            return None

    monkeypatch.setattr(daemon_mod, "SchedulerDaemon", FakeDaemon)
    monkeypatch.setattr(daemon_mod, "build_bus", lambda: object())
    monkeypatch.setattr(daemon_mod, "scan_all_plans", lambda bus: None)
    monkeypatch.setattr(server_mod, "advance_all_plans", lambda: None)
    monkeypatch.delenv("PIPELINE_SCHEDULER_INTERVAL_S", raising=False)
    monkeypatch.delenv("PIPELINE_SCHEDULER_HEALTH_PATH", raising=False)

    err = StringIO()
    with redirect_stderr(err):
        code = daemon_mod.run_daemon()
    return code, err.getvalue()


def test_daemon_lock_lives_under_the_host_state_root(plan_root, monkeypatch):
    """The single-instance lock is created in the host-state root.

    ``pipeline.paths.PLAN_DIR`` is pinned at an empty sibling directory so
    the daemon cannot fall back to the real plan directory: the lock must
    land in the root the host state resolves.
    """
    code, err = _run_daemon(monkeypatch, plan_root)

    assert code == 0, f"the daemon must start; got exit code {code} (stderr: {err!r})"
    assert (plan_root / LOCK_FILE).exists(), (
        f"the daemon lock must live under the host-state root; expected "
        f"{plan_root / LOCK_FILE}"
    )
    assert not (plan_root.parent / "other-plans" / LOCK_FILE).exists(), (
        "the daemon must not take the module-load copy of PLAN_DIR"
    )


def test_a_second_daemon_is_refused_while_the_lock_is_held(plan_root, monkeypatch):
    """A lock that is held keeps a second daemon out."""
    lock_path = plan_root / LOCK_FILE
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        # The lock file names a LIVE pid and is flocked, so the daemon must
        # refuse whether it detects the holder by pid or by the file lock.
        os.write(fd, str(os.getpid()).encode("utf-8"))
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        code, err = _run_daemon(monkeypatch, plan_root)
    finally:
        os.close(fd)

    assert code == 1, (
        f"a second daemon must be refused while the lock is held; got exit "
        f"code {code} (stderr: {err!r})"
    )
    assert "lock" in err.lower(), (
        f"the refusal must say why; got stderr {err!r}"
    )


def test_a_stale_lock_from_a_dead_pid_does_not_block_the_daemon(
    plan_root, monkeypatch
):
    """A lock left behind by a dead process is taken over, as today."""
    dead = subprocess.Popen([sys.executable, "-c", ""])
    dead.wait()
    lock_path = plan_root / LOCK_FILE
    lock_path.write_text(str(dead.pid), encoding="utf-8")

    code, err = _run_daemon(monkeypatch, plan_root)

    assert code == 0, (
        f"a stale lock from a dead pid must not block the daemon; got exit "
        f"code {code} (stderr: {err!r})"
    )


def test_a_lock_from_a_live_pid_blocks_the_daemon(plan_root, monkeypatch):
    """A lock naming a live process keeps the next daemon out."""
    lock_path = plan_root / LOCK_FILE
    lock_path.write_text(str(os.getpid()), encoding="utf-8")

    code, err = _run_daemon(monkeypatch, plan_root)

    assert code == 1, (
        f"a lock held by a live pid must block the daemon; got exit code "
        f"{code} (stderr: {err!r})"
    )
