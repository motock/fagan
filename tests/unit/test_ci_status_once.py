"""Tests for ``_ci_status_once`` in pipeline/ci.py — a single-poll, non-blocking
variant of ``_ci_status``.

``_ci_status`` loops with ``time.sleep(10)`` until a deadline expires, which
blocks every other plan's tick inside ``advance_all_plans`` (sequential, single
thread). ``_ci_status_once`` performs exactly ONE poll and returns immediately,
reusing the same classification logic as ``_ci_status``.

These tests are written FIRST (TDD) and are expected to be RED until the
implementation adds ``_ci_status_once`` to pipeline/ci.py. We mock
``subprocess.run`` and never call the real ``gh`` binary.
"""

import pytest

from pipeline import ci as p

# ---------- helpers ----------

def _run(returncode=0, stdout="", stderr=""):
    class R:
        pass

    r = R()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


class _SleepRecorder:
    """Patches ``time.sleep`` and records every call so a test can assert the
    function under test NEVER sleeps."""

    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)


@pytest.fixture
def no_sleep(monkeypatch):
    rec = _SleepRecorder()
    monkeypatch.setattr(p.time, "sleep", rec)
    return rec


# ---------- module surface ----------

def test_ci_status_once_is_exported_in_all():
    """``_ci_status_once`` must be added to the module's ``__all__``."""
    assert "_ci_status_once" in p.__all__


def test_ci_status_once_callable_exists():
    """The function must exist and be callable (import/attribute error until
    implemented)."""
    assert callable(getattr(p, "_ci_status_once", None))


# ---------- happy path: pass ----------

def test_pass_when_all_check_runs_success(monkeypatch, no_sleep):
    """All check-runs concluded success -> state 'pass'."""
    def _fake_run(argv, **_):
        return _run(stdout=(
            '{"name":"Lint","status":"completed","conclusion":"success"}\n'
            '{"name":"Test","status":"completed","conclusion":"success"}\n'
        ))

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    result = p._ci_status_once("agent/x", sha="deadbeef")
    assert result["state"] == "pass"
    assert no_sleep.calls == []


def test_pass_when_conclusions_subset_of_pass_set(monkeypatch, no_sleep):
    """Conclusions within {success, neutral, skipped} -> pass (boundary: mix)."""
    def _fake_run(argv, **_):
        return _run(stdout=(
            '{"name":"Lint","status":"completed","conclusion":"success"}\n'
            '{"name":"Skip","status":"completed","conclusion":"skipped"}\n'
            '{"name":"Neut","status":"completed","conclusion":"neutral"}\n'
        ))

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    result = p._ci_status_once("agent/x", sha="deadbeef")
    assert result["state"] == "pass"
    assert no_sleep.calls == []


# ---------- fail ----------

def test_fail_when_any_check_run_failure(monkeypatch, no_sleep):
    """Any check-run concluded failure/timed_out/action_required -> state 'fail',
    and the error names the failing check."""
    def _fake_run(argv, **_):
        return _run(stdout=(
            '{"name":"Lint","status":"completed","conclusion":"success"}\n'
            '{"name":"Build","status":"completed","conclusion":"failure"}\n'
        ))

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    result = p._ci_status_once("agent/x", sha="deadbeef")
    assert result["state"] == "fail"
    assert "Build" in result["error"]
    assert "failure" in result["error"]
    assert no_sleep.calls == []


def test_fail_when_any_check_run_timed_out(monkeypatch, no_sleep):
    def _fake_run(argv, **_):
        return _run(stdout=(
            '{"name":"Build","status":"completed","conclusion":"timed_out"}\n'
        ))

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    result = p._ci_status_once("agent/x", sha="deadbeef")
    assert result["state"] == "fail"
    assert "Build" in result["error"]
    assert no_sleep.calls == []


def test_fail_when_any_check_run_action_required(monkeypatch, no_sleep):
    def _fake_run(argv, **_):
        return _run(stdout=(
            '{"name":"Deploy","status":"completed","conclusion":"action_required"}\n'
        ))

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    result = p._ci_status_once("agent/x", sha="deadbeef")
    assert result["state"] == "fail"
    assert "Deploy" in result["error"]
    assert no_sleep.calls == []


# ---------- cancelled ----------

def test_cancelled_on_cancelled_conclusion(monkeypatch, no_sleep):
    """A cancelled conclusion (with no failure) -> state 'cancelled', error
    names the cancelled check."""
    def _fake_run(argv, **_):
        return _run(stdout=(
            '{"name":"Build","status":"completed","conclusion":"cancelled"}\n'
        ))

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    result = p._ci_status_once("agent/x", sha="deadbeef")
    assert result["state"] == "cancelled"
    assert "Build" in result["error"]
    assert no_sleep.calls == []


# ---------- pending ----------

def test_pending_when_check_run_not_completed(monkeypatch, no_sleep):
    """A check-run whose status is not 'completed' -> state 'pending'
    immediately, with NO sleep and NO blocking loop."""
    def _fake_run(argv, **_):
        return _run(stdout=(
            '{"name":"Test","status":"in_progress","conclusion":null}\n'
        ))

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    result = p._ci_status_once("agent/x", sha="deadbeef")
    assert result["state"] == "pending"
    assert no_sleep.calls == [], "must not sleep on a single poll"


def test_pending_when_zero_runs_but_ci_configured(monkeypatch, no_sleep):
    """Zero runs registered but the repo HAS CI configured -> state 'pending'
    (checks not registered yet), returned immediately without sleeping."""
    def _fake_run(argv, **_):
        return _run(stdout="")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    monkeypatch.setattr(p, "_repo_has_ci_configured", lambda: True)
    result = p._ci_status_once("agent/x", sha="deadbeef")
    assert result["state"] == "pending"
    assert no_sleep.calls == []


# ---------- none ----------

def test_none_when_zero_runs_and_no_ci_configured(monkeypatch, no_sleep):
    """Zero runs registered and the repo has NO CI configured -> state 'none'."""
    def _fake_run(argv, **_):
        return _run(stdout="")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    monkeypatch.setattr(p, "_repo_has_ci_configured", lambda: False)
    result = p._ci_status_once("agent/x", sha="deadbeef")
    assert result["state"] == "none"
    assert no_sleep.calls == []


def test_none_when_gh_exits_nonzero(monkeypatch, no_sleep):
    """A nonzero gh returncode -> state 'none', error is stderr trimmed to 200
    chars."""
    long_err = "x" * 500
    def _fake_run(argv, **_):
        return _run(returncode=1, stderr=long_err)

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    result = p._ci_status_once("agent/x", sha="deadbeef")
    assert result["state"] == "none"
    assert len(result["error"]) <= 200
    assert result["error"] == long_err[:200]
    assert no_sleep.calls == []


def test_none_when_gh_raises_oserror(monkeypatch, no_sleep):
    """An OSError from gh -> state 'none', error prefixed 'gh unavailable:'."""
    def _fake_run(argv, **_):
        raise OSError("boom")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    result = p._ci_status_once("agent/x", sha="deadbeef")
    assert result["state"] == "none"
    assert result["error"].startswith("gh unavailable:")
    assert "boom" in result["error"]
    assert no_sleep.calls == []


def test_none_on_unparseable_output(monkeypatch, no_sleep):
    """Unparseable gh api check-runs output -> state 'none' with a descriptive
    error."""
    def _fake_run(argv, **_):
        return _run(stdout="this is not json\n{broken")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    result = p._ci_status_once("agent/x", sha="deadbeef")
    assert result["state"] == "none"
    assert result["error"]  # non-empty descriptive error
    assert no_sleep.calls == []


# ---------- gate disabled ----------

def test_pass_when_gate_disabled(monkeypatch, no_sleep):
    """When PIPELINE_MERGE_CI_GATE is falsy -> state 'pass' with error
    'CI gate disabled', exactly as _ci_status does."""
    def _fake_run(argv, **_):  # pragma: no cover - must not be called
        raise AssertionError("gh must not be invoked when gate is disabled")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", False)
    result = p._ci_status_once("agent/x", sha="deadbeef")
    assert result == {"state": "pass", "error": "CI gate disabled"}
    assert no_sleep.calls == []


# ---------- never sleeps ----------

def test_never_calls_time_sleep_even_on_pending(monkeypatch, no_sleep):
    """The single most important contract: the function NEVER calls time.sleep,
    even in the cases where _ci_status would sleep-and-continue."""
    def _fake_run(argv, **_):
        # A run that is not completed -> _ci_status would sleep(10); continue.
        return _run(stdout=(
            '{"name":"Test","status":"queued","conclusion":null}\n'
        ))

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    p._ci_status_once("agent/x", sha="deadbeef")
    assert no_sleep.calls == []


def test_never_calls_time_sleep_on_zero_runs_configured(monkeypatch, no_sleep):
    """Zero runs + CI configured is the other _ci_status sleep-and-continue
    branch; _ci_status_once must still not sleep."""
    def _fake_run(argv, **_):
        return _run(stdout="")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    monkeypatch.setattr(p, "_repo_has_ci_configured", lambda: True)
    p._ci_status_once("agent/x", sha="deadbeef")
    assert no_sleep.calls == []


# ---------- single poll ----------

def test_single_poll_makes_one_gh_call(monkeypatch, no_sleep):
    """A single poll issues exactly ONE gh invocation (no retry loop)."""
    calls = []
    def _fake_run(argv, **_):
        calls.append(argv)
        return _run(stdout=(
            '{"name":"Lint","status":"completed","conclusion":"success"}\n'
        ))

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    p._ci_status_once("agent/x", sha="deadbeef")
    assert len(calls) == 1
    assert no_sleep.calls == []


# ---------- return shape ----------

def test_return_value_is_dict_with_state_and_error_keys(monkeypatch, no_sleep):
    """Every return value is a dict containing 'state' and 'error' keys."""
    def _fake_run(argv, **_):
        return _run(stdout=(
            '{"name":"Lint","status":"completed","conclusion":"success"}\n'
        ))

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    result = p._ci_status_once("agent/x", sha="deadbeef")
    assert isinstance(result, dict)
    assert "state" in result
    assert "error" in result
    assert result["state"] in {"pass", "fail", "cancelled", "pending", "none"}


# ---------- signature ----------

def test_sha_is_keyword_only(monkeypatch, no_sleep):
    """``sha`` must be keyword-only (matches _ci_status)."""
    import inspect
    sig = inspect.signature(p._ci_status_once)
    assert "sha" in sig.parameters
    assert sig.parameters["sha"].kind == inspect.Parameter.KEYWORD_ONLY


# ---------- _ci_status not regressed (sanity) ----------

def test_ci_status_still_unchanged_behavior(monkeypatch):
    """The existing _ci_status must keep its blocking-loop behavior intact:
    a not-completed run with a tiny timeout still yields the timeout-pending
    message. Guards against the implementer accidentally altering _ci_status."""
    def _fake_run(argv, **_):
        return _run(stdout=(
            '{"name":"Test","status":"in_progress","conclusion":null}\n'
        ))

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p.time, "sleep", lambda s: None)
    monkeypatch.setattr(p, "PIPELINE_MERGE_CI_GATE", True)
    result = p._ci_status("agent/x", sha="deadbeef", timeout_s=0.05)
    assert result["state"] == "pending"
    assert result["error"] == "CI did not complete within timeout"