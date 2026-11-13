"""Scoped baseline snapshot for ``test_scope: scoped`` stories (tsc-13).

In ``scoped`` mode the pipeline host never runs a repo's full test suite, so
the pre-dispatch baseline must not run it either. ``baseline_snapshot_for``
swaps the full-suite snapshot for one that runs the detected command scoped
to the story's acceptance fixtures, and falls back to "no baseline" for any
runner that cannot be scoped. Full mode must stay byte-for-byte identical:
the unscoped snapshot callable is returned by identity.
"""

import subprocess
import sys

import pytest

from pipeline import dispatch_baseline
from tests.unit._line_limit_policy import assert_within_line_limit

_PYTEST_CMD = [sys.executable, "-m", "pytest", "-q"]
_ACCEPTANCE = [{"path": "tests/acceptance_x.py", "source": "def test_t(): pass\n"}]
_SCOPED = {"test_scope": "scoped"}
_RESULT_KEYS = {"cmd", "returncode", "stdout_tail", "failed_node_ids", "stderr_tail"}


def _scoped_story(acceptance=None):
    story = {"test_scope_resolution": _SCOPED}
    if acceptance is not None:
        story["acceptance"] = acceptance
    return story


def _stub_detect(monkeypatch, tmp_path, cmd):
    monkeypatch.setattr(
        dispatch_baseline, "detect_test_command", lambda _wt: (tmp_path, cmd)
    )


def _stub_run(monkeypatch, calls, stdout="", returncode=1):
    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(dispatch_baseline.subprocess, "run", fake_run)


def _forbid_run(monkeypatch):
    def fail_run(cmd, **kwargs):
        raise AssertionError(f"subprocess.run must not be called: {cmd!r}")

    monkeypatch.setattr(dispatch_baseline.subprocess, "run", fail_run)


def _full_snapshot(_worktree_path):
    return {"full": True}


# ---------------------------------------------------------------------------
# Full mode: the unscoped snapshot is passed through by identity.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "story",
    [{}, {"test_scope_resolution": {"test_scope": "full"}}],
    ids=["no-resolution", "resolved-full"],
)
def test_full_mode_returns_the_unscoped_snapshot_itself(story):
    assert dispatch_baseline.baseline_snapshot_for(story, _full_snapshot) is _full_snapshot


# ---------------------------------------------------------------------------
# Scoped mode without acceptance fixtures: no baseline, nothing is run.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("acceptance", [None, []], ids=["absent", "empty"])
def test_scoped_story_without_acceptance_has_no_baseline(
    monkeypatch, tmp_path, acceptance
):
    _stub_detect(monkeypatch, tmp_path, _PYTEST_CMD)
    _forbid_run(monkeypatch)
    snapshot = dispatch_baseline.baseline_snapshot_for(
        _scoped_story(acceptance), _full_snapshot
    )
    assert snapshot(tmp_path) is None


def test_scoped_snapshot_is_not_the_unscoped_snapshot(monkeypatch, tmp_path):
    _stub_detect(monkeypatch, tmp_path, _PYTEST_CMD)
    snapshot = dispatch_baseline.baseline_snapshot_for(
        _scoped_story(_ACCEPTANCE), _full_snapshot
    )
    assert snapshot is not _full_snapshot


# ---------------------------------------------------------------------------
# Scoped mode with a pytest runner: only the acceptance fixture is run.
# ---------------------------------------------------------------------------
def test_scoped_pytest_snapshot_runs_detected_cmd_on_acceptance_path(
    monkeypatch, tmp_path
):
    _stub_detect(monkeypatch, tmp_path, _PYTEST_CMD)
    calls = []
    _stub_run(monkeypatch, calls)
    snapshot = dispatch_baseline.baseline_snapshot_for(
        _scoped_story(_ACCEPTANCE), _full_snapshot
    )
    snapshot(tmp_path)
    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert cmd == [*_PYTEST_CMD, str(tmp_path / "tests/acceptance_x.py")]
    assert kwargs["cwd"] == tmp_path


def test_scoped_pytest_snapshot_result_has_the_baseline_shape(monkeypatch, tmp_path):
    _stub_detect(monkeypatch, tmp_path, _PYTEST_CMD)
    _stub_run(monkeypatch, [], stdout="FAILED tests/acceptance_x.py::t - x\n")
    snapshot = dispatch_baseline.baseline_snapshot_for(
        _scoped_story(_ACCEPTANCE), _full_snapshot
    )
    result = snapshot(tmp_path)
    assert set(result) == _RESULT_KEYS


def test_scoped_pytest_snapshot_parses_failed_acceptance_ids(monkeypatch, tmp_path):
    _stub_detect(monkeypatch, tmp_path, _PYTEST_CMD)
    _stub_run(monkeypatch, [], stdout="FAILED tests/acceptance_x.py::t - x\n")
    snapshot = dispatch_baseline.baseline_snapshot_for(
        _scoped_story(_ACCEPTANCE), _full_snapshot
    )
    result = snapshot(tmp_path)
    assert result["failed_node_ids"] == ["tests/acceptance_x.py::t"]
    assert result["returncode"] == 1
    assert result["cmd"] == [*_PYTEST_CMD, str(tmp_path / "tests/acceptance_x.py")]


# ---------------------------------------------------------------------------
# Scoped mode, negative cases: a runner that cannot be scoped, or a timeout.
# ---------------------------------------------------------------------------
def test_scoped_unscopable_runner_has_no_baseline(monkeypatch, tmp_path):
    _stub_detect(monkeypatch, tmp_path, ["make", "test"])
    _forbid_run(monkeypatch)
    snapshot = dispatch_baseline.baseline_snapshot_for(
        _scoped_story(_ACCEPTANCE), _full_snapshot
    )
    assert snapshot(tmp_path) is None


def test_scoped_timed_out_run_has_no_baseline(monkeypatch, tmp_path):
    _stub_detect(monkeypatch, tmp_path, _PYTEST_CMD)

    def timeout_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(dispatch_baseline.subprocess, "run", timeout_run)
    snapshot = dispatch_baseline.baseline_snapshot_for(
        _scoped_story(_ACCEPTANCE), _full_snapshot
    )
    assert snapshot(tmp_path) is None


# ---------------------------------------------------------------------------
# Scoped snapshot must not read or write the per-commit cache: its key covers
# the unscoped command, so a scoped result stored there would poison it.
# ---------------------------------------------------------------------------
def test_scoped_snapshot_never_touches_the_cache_dir(monkeypatch, tmp_path):
    _stub_detect(monkeypatch, tmp_path, _PYTEST_CMD)
    _stub_run(monkeypatch, [], stdout="")

    def forbidden_cache_dir():
        raise AssertionError("scoped baseline must not use the snapshot cache")

    monkeypatch.setattr(dispatch_baseline, "_cache_dir", forbidden_cache_dir)
    snapshot = dispatch_baseline.baseline_snapshot_for(
        _scoped_story(_ACCEPTANCE), _full_snapshot
    )
    assert snapshot(tmp_path) is not None


# ---------------------------------------------------------------------------
# Guard: the dispatch module stays under the 1000-line production limit.
# ---------------------------------------------------------------------------
def test_dispatch_module_stays_under_line_limit():
    from pathlib import Path

    dispatch_path = Path(__file__).resolve().parents[2] / "pipeline" / "dispatch.py"
    with dispatch_path.open() as handle:
        line_count = sum(1 for _ in handle)
    assert_within_line_limit(line_count, 1000, "pipeline/dispatch.py")
