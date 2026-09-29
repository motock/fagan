"""Tests for the pid-reuse guard in pipeline/checkpoint.py.

``_terminate_and_checkpoint`` SIGTERMs the stored pid with no identity check,
so a pid reused between dispatch and the watchdog firing would hit an unrelated
process. A reused pid always belongs to a process that STARTED AFTER the
story's ``dispatched_at``, so comparing ``ps -o lstart=`` with ``dispatched_at``
detects reuse. The process boundary (``subprocess.run``, ``os.kill``) and
``_commit_wip`` are stubbed; lstart strings come from a known UTC instant
converted to local time, so these tests hold in any timezone.
"""

import json
import signal
import subprocess
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import pipeline.checkpoint as pcheckpoint
import pipeline.persistence as ppersist
from pipeline.checkpoint import _pid_started_after_dispatch, _terminate_and_checkpoint

PLAN_NAME = "plan"
STORY_KEY = "SEW-4"
PID = 4242
STEP = "interrupt"
SUMMARY = "manual interrupt"
BASE = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)


def _lstart(dt_utc: datetime) -> str:
    """``ps -o lstart=`` rendering of a UTC instant, in the host's local zone."""
    return dt_utc.astimezone().strftime("%a %b %d %H:%M:%S %Y")


class _PsStub:
    """Records subprocess.run calls; answers the ``ps`` probe with ``stdout``."""

    def __init__(self, stdout: str = "", exc: Exception | None = None):
        self.stdout = stdout
        self.exc = exc
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.exc is not None:
            raise self.exc
        if argv and argv[0] == "ps":
            return subprocess.CompletedProcess(argv, 0, self.stdout, "")
        # Any other subprocess (the watchdog's git evidence probe) is
        # irrelevant here: answer with a benign empty result.
        return subprocess.CompletedProcess(argv, 0, "0", "")

    @property
    def ps_calls(self):
        return [c for c in self.calls if c[0] and c[0][0] == "ps"]


@pytest.fixture
def harness(plan_dir, monkeypatch):
    """Build a running story and stub the process boundary + _commit_wip."""

    def _make(*, ps_stdout="", ps_exc=None, dispatched_at=None, step=STEP):
        worktree = plan_dir / "wt"
        worktree.mkdir(exist_ok=True)
        story = {"worktree": str(worktree), "status": "running", "pid": PID, "last_commit": ""}
        if dispatched_at is not None:
            story["dispatched_at"] = dispatched_at
        manifest = {"stories": {STORY_KEY: story}}
        manifest_path = plan_dir / f"{PLAN_NAME}.manifest.json"
        manifest_path.write_text(json.dumps(manifest))
        ps = _PsStub(ps_stdout, ps_exc)
        monkeypatch.setattr(pcheckpoint.subprocess, "run", ps)
        kills: list[tuple[int, int]] = []
        monkeypatch.setattr(pcheckpoint.os, "kill", lambda pid, sig: kills.append((pid, sig)))
        monkeypatch.setattr(pcheckpoint, "_commit_wip", lambda *a, **k: "deadbeef")
        return SimpleNamespace(
            story=story, manifest=manifest, manifest_path=manifest_path,
            ps=ps, kills=kills, step=step,
        )

    return _make


def _run(h) -> str:
    return _terminate_and_checkpoint(
        h.manifest, h.manifest_path, PLAN_NAME, STORY_KEY, h.story,
        pid=PID, step=h.step, summary=SUMMARY,
    )


def _last_record() -> dict:
    return ppersist._read_journal(PLAN_NAME, STORY_KEY)[-1]


# ---------- the guard function itself ----------


def test_guard_true_only_when_started_after_dispatch(harness):
    started = BASE + timedelta(hours=1)
    harness(ps_stdout=_lstart(started), dispatched_at=BASE.isoformat())
    assert _pid_started_after_dispatch(PID, BASE.isoformat()) is True
    assert _pid_started_after_dispatch(PID, (started - timedelta(seconds=1)).isoformat()) is False


def test_guard_false_without_dispatched_at_and_skips_ps(harness):
    h = harness(ps_stdout=_lstart(BASE + timedelta(hours=1)))
    assert _pid_started_after_dispatch(PID, None) is False
    assert _pid_started_after_dispatch(PID, "") is False
    assert h.ps.ps_calls == []


# ---------- kill / skip behaviour through _terminate_and_checkpoint ----------


def test_should_kill_when_process_started_before_dispatch(harness):
    started = BASE
    h = harness(
        ps_stdout=_lstart(started),
        dispatched_at=(started + timedelta(seconds=1)).isoformat(),
    )
    _run(h)
    assert h.kills == [(PID, signal.SIGTERM)]
    assert _last_record().get("pid_reused") is not True


def test_should_skip_kill_when_process_started_after_dispatch(harness):
    started = BASE + timedelta(hours=1)
    h = harness(ps_stdout=_lstart(started), dispatched_at=BASE.isoformat())
    _run(h)
    assert h.kills == []
    assert _last_record()["pid_reused"] is True
    assert h.story["status"] == "interrupted"
    on_disk = json.loads(h.manifest_path.read_text())["stories"][STORY_KEY]
    assert on_disk["status"] == "interrupted"


def test_boundary_five_seconds_after_is_killed_six_is_skipped(harness):
    started = BASE
    at_five = harness(
        ps_stdout=_lstart(started + timedelta(seconds=5)),
        dispatched_at=started.isoformat(),
    )
    _run(at_five)
    assert at_five.kills == [(PID, signal.SIGTERM)]

    at_six = harness(
        ps_stdout=_lstart(started + timedelta(seconds=6)),
        dispatched_at=started.isoformat(),
    )
    _run(at_six)
    assert at_six.kills == []
    assert _last_record()["pid_reused"] is True


def test_should_kill_when_ps_reports_no_process(harness):
    h = harness(ps_stdout="", dispatched_at=BASE.isoformat())
    _run(h)
    assert h.kills == [(PID, signal.SIGTERM)]
    assert _last_record().get("pid_reused") is not True


def test_should_kill_when_lstart_unparseable(harness):
    h = harness(ps_stdout="garbage", dispatched_at=BASE.isoformat())
    _run(h)
    assert h.kills == [(PID, signal.SIGTERM)]
    assert _last_record().get("pid_reused") is not True


def test_should_kill_when_ps_probe_raises(harness):
    h = harness(ps_exc=OSError("ps exploded"), dispatched_at=BASE.isoformat())
    _run(h)
    assert h.kills == [(PID, signal.SIGTERM)]
    assert _last_record().get("pid_reused") is not True


def test_should_kill_when_dispatched_at_missing_or_empty(harness):
    for dispatched_at in (None, ""):
        h = harness(
            ps_stdout=_lstart(BASE + timedelta(hours=1)),
            dispatched_at=dispatched_at,
        )
        _run(h)
        assert h.kills == [(PID, signal.SIGTERM)]
        assert h.ps.ps_calls == []
        assert _last_record().get("pid_reused") is not True


# ---------- probe shape and per-step visibility ----------


def test_ps_probe_uses_lstart_with_c_locale(harness):
    h = harness(ps_stdout=_lstart(BASE), dispatched_at=BASE.isoformat())
    _run(h)
    assert len(h.ps.ps_calls) == 1
    argv, kwargs = h.ps.ps_calls[0]
    assert argv == ["ps", "-p", str(PID), "-o", "lstart="]
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["check"] is False
    assert kwargs["timeout"] == 10
    assert kwargs["env"]["LC_ALL"] == "C"


def test_pid_reused_recorded_for_watchdog_step_too(harness):
    started = BASE + timedelta(hours=1)
    h = harness(
        ps_stdout=_lstart(started), dispatched_at=BASE.isoformat(),
        step="dispatch_watchdog_timeout",
    )
    _run(h)
    assert h.kills == []
    assert _last_record()["pid_reused"] is True
    assert h.story["status"] == "interrupted"
