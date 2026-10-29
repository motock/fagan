"""TDD tests for the detached review job.

``pipeline/review_job.py`` moves the inline ``review_story`` call off the
scheduler pass: a review can run 60-400s and today holds a worker (and the
whole pooled pass) for its duration.  The job runs in its own session, writes a
result JSON, and the caller polls ``collect_review_job``.  These tests pin the
guarantees that make that safe:

* the child is spawned with ``cwd`` set to the control plane's OWN source root,
  never a story worktree (a ``-m`` child started inside a worktree would import
  that branch's unreviewed ``pipeline`` package and run it as the scheduler);
* a job that dies without a result is a harness error (``state == "error"``),
  never a verdict;
* the job never writes the manifest - it reports the review's status so the
  caller can apply it under the plan lock.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

# isort: off
# pipeline.server is imported first on purpose: pre-existing import cycle
# (tests/unit/test_triage_cap_silent_skip.py uses the same order).
import pipeline.server
import pipeline
from pipeline import server
import pipeline.review_job as rj
# isort: on

SOURCE_ROOT = Path(pipeline.__file__).resolve().parent.parent
ENV_VAR = "PIPELINE_REVIEW_DETACHED"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _at(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp)


class _Proc:
    pid = 4242


def _fake_popen(monkeypatch) -> dict:
    """Patch ``Popen`` and record exactly what ``start_review_job`` passed."""
    calls: dict = {}

    def _popen(argv, **kwargs):
        calls["argv"] = argv
        calls["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(rj.subprocess, "Popen", _popen)
    return calls


def _job(tmp_path: Path, started: str = "2026-10-28T12:00:00+00:00", pid: int = 4242) -> dict:
    return {
        "pid": pid,
        "result_path": str(tmp_path / "result.json"),
        "log_path": str(tmp_path / "job.log"),
        "started_at": started,
    }


# ---------------------------------------------------------------------------
# detached_enabled
# ---------------------------------------------------------------------------


def test_detached_enabled_is_false_under_pytest(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert "pytest" in sys.modules
    assert rj.detached_enabled() is False


def test_detached_enabled_is_true_without_pytest(monkeypatch):
    monkeypatch.delitem(sys.modules, "pytest")
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert rj.detached_enabled() is True


def test_detached_enabled_env_zero_disables(monkeypatch):
    monkeypatch.delitem(sys.modules, "pytest")
    monkeypatch.setenv(ENV_VAR, "0")
    assert rj.detached_enabled() is False


def test_detached_enabled_only_zero_disables(monkeypatch):
    monkeypatch.delitem(sys.modules, "pytest")
    monkeypatch.setenv(ENV_VAR, "false")
    assert rj.detached_enabled() is True


def test_detached_enabled_reads_env_on_every_call(monkeypatch):
    monkeypatch.delitem(sys.modules, "pytest")
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert rj.detached_enabled() is True
    monkeypatch.setenv(ENV_VAR, "0")
    assert rj.detached_enabled() is False
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert rj.detached_enabled() is True


# ---------------------------------------------------------------------------
# start_review_job: validation happens before anything is spawned
# ---------------------------------------------------------------------------


def test_start_rejects_traversing_plan_name_before_spawning(tmp_path, monkeypatch):
    calls = _fake_popen(monkeypatch)
    with pytest.raises(ValueError) as excinfo:
        rj.start_review_job("../evil", "S-1", tmp_path / "state")
    message = str(excinfo.value)
    assert "plan_name" in message
    assert "evil" not in message
    assert calls == {}
    assert not (tmp_path / "state").exists()


def test_start_rejects_traversing_story_key_before_spawning(tmp_path, monkeypatch):
    calls = _fake_popen(monkeypatch)
    with pytest.raises(ValueError) as excinfo:
        rj.start_review_job("P", "../../etc", tmp_path / "state")
    message = str(excinfo.value)
    assert "story_key" in message
    assert "etc" not in message
    assert calls == {}
    assert not (tmp_path / "state").exists()


def test_start_rejects_an_empty_key_before_spawning(tmp_path, monkeypatch):
    calls = _fake_popen(monkeypatch)
    with pytest.raises(ValueError):
        rj.start_review_job("P", "", tmp_path / "state")
    assert calls == {}


# ---------------------------------------------------------------------------
# start_review_job: the spawn contract
# ---------------------------------------------------------------------------


def test_start_spawns_from_the_control_plane_source_root(tmp_path, monkeypatch):
    calls = _fake_popen(monkeypatch)

    job = rj.start_review_job("P", "S-1", tmp_path / "state")

    kwargs = calls["kwargs"]
    # The security invariant: a ``-m`` child would import the cwd's ``pipeline``
    # package first on sys.path, so the cwd must never be a story worktree.
    assert kwargs["cwd"] == SOURCE_ROOT
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is subprocess.DEVNULL
    argv = calls["argv"]
    assert argv[:3] == [sys.executable, "-m", "pipeline.review_job"]
    assert argv[3:6] == ["P", "S-1", job["result_path"]]
    assert len(argv) == 6


def test_start_returns_the_handle_fields(tmp_path, monkeypatch):
    _fake_popen(monkeypatch)

    job = rj.start_review_job("P", "S-1", tmp_path / "state")

    assert job["pid"] == 4242
    assert Path(job["result_path"]) == (tmp_path / "state" / "review_job" / "S-1" / "result.json")
    assert Path(job["log_path"]) == tmp_path / "state" / "review_job" / "S-1" / "job.log"
    assert _at(job["started_at"]).tzinfo is not None


def test_start_creates_a_private_job_directory(tmp_path, monkeypatch):
    _fake_popen(monkeypatch)

    rj.start_review_job("P", "S-1", tmp_path / "state")

    job_dir = tmp_path / "state" / "review_job" / "S-1"
    assert job_dir.is_dir()
    assert (job_dir.stat().st_mode & 0o777) == 0o700


def test_start_deletes_a_stale_result_before_spawning(tmp_path, monkeypatch):
    calls = _fake_popen(monkeypatch)
    stale = tmp_path / "state" / "review_job" / "S-1" / "result.json"
    stale.parent.mkdir(parents=True)
    stale.write_text('{"state": "done", "status": "pr_open"}')

    rj.start_review_job("P", "S-1", tmp_path / "state")

    assert calls != {}
    # A stale verdict from an earlier run must never be read as this one's.
    assert not stale.exists()


def test_start_appends_child_output_to_the_job_log(tmp_path, monkeypatch):
    calls = _fake_popen(monkeypatch)

    job = rj.start_review_job("P", "S-1", tmp_path / "state")

    log_path = Path(job["log_path"])
    assert log_path.exists()
    # The child's stdout/stderr are handed the log file itself, not a pipe, so
    # a crash's traceback lands in job.log for the caller's ``log_tail``.
    assert Path(calls["kwargs"]["stdout"].name) == log_path
    assert calls["kwargs"]["stdout"] is calls["kwargs"]["stderr"]


# ---------------------------------------------------------------------------
# collect_review_job
# ---------------------------------------------------------------------------


def test_collect_returns_none_while_a_young_job_runs(tmp_path, monkeypatch):
    job = _job(tmp_path)
    monkeypatch.setattr(rj, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(rj, "_now", lambda: _at("2026-10-28T12:00:20+00:00"))
    assert rj.collect_review_job(job, 30) is None


def test_collect_does_not_kill_at_exactly_the_timeout(tmp_path, monkeypatch):
    job = _job(tmp_path)
    killed: list = []
    monkeypatch.setattr(rj, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(rj, "_now", lambda: _at("2026-10-28T12:00:30+00:00"))
    monkeypatch.setattr(rj.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    assert rj.collect_review_job(job, 30) is None
    assert killed == []


def test_collect_kills_and_reports_a_timeout(tmp_path, monkeypatch):
    job = _job(tmp_path)
    killed: list = []
    monkeypatch.setattr(rj, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(rj, "_now", lambda: _at("2026-10-28T12:00:31+00:00"))
    monkeypatch.setattr(rj.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    result = rj.collect_review_job(job, 30)

    assert result == {
        "state": "error",
        "error": "review job timed out after 30s",
        "timed_out": True,
    }
    assert killed == [(4242, signal.SIGTERM)]


def test_collect_never_mutates_the_handle(tmp_path, monkeypatch):
    started = "2026-10-28T12:00:00+00:00"
    job = _job(tmp_path, started=started)
    monkeypatch.setattr(rj, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(rj, "_now", lambda: _at("2026-10-28T12:00:40+00:00"))
    monkeypatch.setattr(rj.os, "killpg", lambda pid, sig: None)

    first = rj.collect_review_job(job, 30)
    second = rj.collect_review_job(job, 30)

    assert first == second
    assert second["timed_out"] is True
    assert job["started_at"] == started


def test_collect_returns_a_finished_result(tmp_path, monkeypatch):
    job = _job(tmp_path)
    monkeypatch.setattr(rj, "_pid_alive", lambda pid: False)
    Path(job["result_path"]).write_text(
        json.dumps({"state": "done", "status": "pr_open", "skipped": None, "deferred": None})
    )

    assert rj.collect_review_job(job, 30) == {
        "state": "done",
        "status": "pr_open",
        "skipped": None,
        "deferred": None,
    }


def test_collect_reports_a_dead_job_with_no_result(tmp_path, monkeypatch):
    job = _job(tmp_path)
    monkeypatch.setattr(rj, "_pid_alive", lambda pid: False)
    Path(job["log_path"]).write_text("x" * 3000 + "the tail")

    result = rj.collect_review_job(job, 30)

    assert result["state"] == "error"
    assert result["error"] == "review job exited without a result"
    assert result["log_tail"].endswith("the tail")
    assert len(result["log_tail"]) == rj.LOG_TAIL_CHARS


def test_collect_reports_an_unparsable_result_as_an_error(tmp_path, monkeypatch):
    job = _job(tmp_path)
    monkeypatch.setattr(rj, "_pid_alive", lambda pid: False)
    Path(job["result_path"]).write_text("{not json")

    result = rj.collect_review_job(job, 30)

    assert result["state"] == "error"
    assert result["error"] == "review job exited without a result"


def test_collect_reports_an_unknown_state_as_an_error(tmp_path, monkeypatch):
    job = _job(tmp_path)
    monkeypatch.setattr(rj, "_pid_alive", lambda pid: False)
    Path(job["result_path"]).write_text(json.dumps({"state": "pass"}))

    result = rj.collect_review_job(job, 30)

    assert result["state"] == "error"
    assert result["error"] == "review job exited without a result"


def test_collect_reports_a_missing_result_path_as_an_error(tmp_path, monkeypatch):
    job = _job(tmp_path)
    job.pop("result_path")
    monkeypatch.setattr(rj, "_pid_alive", lambda pid: False)

    result = rj.collect_review_job(job, 30)

    assert result["state"] == "error"


# ---------------------------------------------------------------------------
# cancel_review_job
# ---------------------------------------------------------------------------


def test_cancel_kills_a_live_process_group(tmp_path, monkeypatch):
    killed: list = []
    monkeypatch.setattr(rj, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(rj.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    rj.cancel_review_job(_job(tmp_path))

    assert killed == [(4242, signal.SIGTERM)]


def test_cancel_is_a_noop_when_the_job_is_gone(tmp_path, monkeypatch):
    killed: list = []
    monkeypatch.setattr(rj, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(rj.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    rj.cancel_review_job(_job(tmp_path))

    assert killed == []


def test_cancel_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(rj, "_pid_alive", lambda pid: True)

    def _killpg(pid, sig):
        raise OSError("no such process group")

    monkeypatch.setattr(rj.os, "killpg", _killpg)

    rj.cancel_review_job(_job(tmp_path))
    rj.cancel_review_job({})
    rj.cancel_review_job({"pid": "not an int"})


# ---------------------------------------------------------------------------
# main: the job body
# ---------------------------------------------------------------------------


def test_main_writes_done_with_the_review_status(tmp_path, monkeypatch):
    result_path = tmp_path / "result.json"
    monkeypatch.setattr(
        server,
        "review_story",
        lambda plan, key: {"ok": True, "status": "pr_open", "skipped": None, "deferred": None},
    )

    rc = rj.main(["P", "S-1", str(result_path)])

    assert rc == 0
    assert json.loads(result_path.read_text()) == {
        "state": "done",
        "status": "pr_open",
        "skipped": None,
        "deferred": None,
    }


def test_main_records_the_skip_verbatim(tmp_path, monkeypatch):
    result_path = tmp_path / "result.json"
    monkeypatch.setattr(
        server,
        "review_story",
        lambda plan, key: {"ok": True, "status": "todo", "skipped": "not_reviewable_state"},
    )

    rc = rj.main(["P", "S-1", str(result_path)])

    assert rc == 0
    result = json.loads(result_path.read_text())
    assert result["state"] == "done"
    assert result["status"] == "todo"
    assert result["skipped"] == "not_reviewable_state"
    assert result["deferred"] is None


def test_main_reports_a_raise_by_class_name_only(tmp_path, monkeypatch):
    result_path = tmp_path / "result.json"
    seen: dict = {}

    def _review(plan, key):
        seen["plan"] = plan
        seen["key"] = key
        raise RuntimeError("secret token abc")

    monkeypatch.setattr(server, "review_story", _review)

    rc = rj.main(["P", "S-1", str(result_path)])

    raw = result_path.read_text()
    result = json.loads(raw)
    assert rc == 1
    assert result == {"state": "error", "error": "review job raised RuntimeError"}
    assert "secret token abc" not in raw
    assert seen == {"plan": "P", "key": "S-1"}


def test_main_writes_the_result_with_mode_0600(tmp_path, monkeypatch):
    result_path = tmp_path / "result.json"
    monkeypatch.setattr(server, "review_story", lambda plan, key: {"ok": True, "status": "pr_open"})

    rj.main(["P", "S-1", str(result_path)])

    assert (result_path.stat().st_mode & 0o777) == 0o600


def test_main_rejects_a_bad_argument_count(tmp_path):
    result_path = tmp_path / "result.json"
    assert rj.main(["P", "S-1"]) == 2
    assert not result_path.exists()
