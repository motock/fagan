"""TDD tests for the detached merge-gate re-verify job.

``pipeline/merge_reverify.py`` moves the inline merge-gate re-verify
(``_reverify_acceptance`` + ``_reverify_build`` from ``pipeline/ci.py``) off the
scheduler pass: the job runs in its own session, writes a result JSON, and the
caller polls ``collect_merge_reverify``.  These tests pin the guarantees that
make that safe:

* the child is spawned with ``cwd`` set to the control plane's OWN source root,
  never the story worktree (a ``-m`` child started inside a worktree would
  import that branch's unreviewed ``pipeline`` package and run it);
* a job that dies without a result is a harness error (``state == "error"``),
  never a red suite (``state == "fail"``);
* the job never writes the manifest - it reports ``last_test_check`` so the
  caller can copy it back under the plan lock.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# isort: off
# pipeline.server is imported first on purpose: pre-existing import cycle
# (tests/unit/test_triage_cap_silent_skip.py uses the same order).
import pipeline.server
import pipeline
from pipeline import server
import pipeline.merge_reverify as mr
# isort: on
# isort: on

SOURCE_ROOT = Path(pipeline.__file__).resolve().parent.parent
ENV_VAR = "PIPELINE_MERGE_REVERIFY_DETACHED"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _at(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp)


class _Completed:
    """Stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


class _Proc:
    pid = 4242


def _fake_popen(monkeypatch) -> dict:
    """Patch ``Popen`` and record exactly what ``start_merge_reverify`` passed."""
    calls: dict = {}

    def _popen(argv, **kwargs):
        calls["argv"] = argv
        calls["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(mr.subprocess, "Popen", _popen)
    return calls


def _job(tmp_path: Path, started: str = "2026-10-28T12:00:00+00:00", pid: int = 4242) -> dict:
    return {
        "pid": pid,
        "result_path": str(tmp_path / "result.json"),
        "log_path": str(tmp_path / "job.log"),
        "started_at": started,
    }


class _Store:
    """Minimal ``FileStore`` stand-in: only ``manifest_path`` is used."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def manifest_path(self, plan_name: str) -> Path:
        return self._path


def _write_manifest(tmp_path: Path, story: dict | None = None, key: str = "S-1") -> Path:
    manifest = {
        "stories": {
            key: story
            if story is not None
            else {
                "agent_instructions": "GOAL: do the thing",
                "last_test_check": "2026-10-01T00:00:00Z",
            }
        }
    }
    path = tmp_path / "P.manifest.json"
    path.write_text(json.dumps(manifest))
    return path


# ---------------------------------------------------------------------------
# detached_enabled
# ---------------------------------------------------------------------------


def test_detached_enabled_is_false_under_pytest(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert "pytest" in sys.modules
    assert mr.detached_enabled() is False


def test_detached_enabled_is_true_without_pytest(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.delitem(sys.modules, "pytest")
    assert mr.detached_enabled() is True


def test_detached_enabled_env_zero_disables(monkeypatch):
    monkeypatch.delitem(sys.modules, "pytest")
    monkeypatch.setenv(ENV_VAR, "0")
    assert mr.detached_enabled() is False


def test_detached_enabled_only_zero_disables(monkeypatch):
    monkeypatch.delitem(sys.modules, "pytest")
    monkeypatch.setenv(ENV_VAR, "false")
    assert mr.detached_enabled() is True


def test_detached_enabled_reads_env_on_every_call(monkeypatch):
    monkeypatch.delitem(sys.modules, "pytest")
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert mr.detached_enabled() is True
    monkeypatch.setenv(ENV_VAR, "0")
    assert mr.detached_enabled() is False
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert mr.detached_enabled() is True


# ---------------------------------------------------------------------------
# start_merge_reverify: validation happens before anything is spawned
# ---------------------------------------------------------------------------


def test_start_rejects_traversing_plan_name_before_spawning(tmp_path, monkeypatch):
    calls = _fake_popen(monkeypatch)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    with pytest.raises(ValueError) as excinfo:
        mr.start_merge_reverify("../evil", "S-1", str(worktree), tmp_path / "state")
    message = str(excinfo.value)
    assert "plan_name" in message
    assert "evil" not in message
    assert calls == {}


def test_start_rejects_traversing_story_key_before_spawning(tmp_path, monkeypatch):
    calls = _fake_popen(monkeypatch)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    with pytest.raises(ValueError) as excinfo:
        mr.start_merge_reverify("P", "S/../1", str(worktree), tmp_path / "state")
    message = str(excinfo.value)
    assert "story_key" in message
    assert "S/../1" not in message
    assert calls == {}


def test_start_rejects_missing_worktree_before_spawning(tmp_path, monkeypatch):
    calls = _fake_popen(monkeypatch)
    with pytest.raises(ValueError) as excinfo:
        mr.start_merge_reverify("P", "S-1", str(tmp_path / "absent"), tmp_path / "state")
    message = str(excinfo.value)
    assert "worktree" in message
    assert "absent" not in message
    assert calls == {}


# ---------------------------------------------------------------------------
# start_merge_reverify: isolation guarantees
# ---------------------------------------------------------------------------


def test_start_spawns_detached_job_from_the_source_root(tmp_path, monkeypatch):
    calls = _fake_popen(monkeypatch)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    state_root = tmp_path / "sr"

    job = mr.start_merge_reverify("P", "S-1", str(worktree), state_root)

    job_dir = state_root / "merge_reverify" / "S-1"
    result_path = job_dir / "result.json"
    log_path = job_dir / "job.log"
    assert calls["argv"] == [
        sys.executable,
        "-m",
        "pipeline.merge_reverify",
        "P",
        "S-1",
        str(worktree),
        str(result_path),
    ]
    kwargs = calls["kwargs"]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert Path(kwargs["cwd"]) == SOURCE_ROOT
    assert Path(kwargs["cwd"]) != worktree
    assert job["pid"] == 4242
    assert job["result_path"] == str(result_path)
    assert job["log_path"] == str(log_path)
    started = datetime.fromisoformat(job["started_at"])
    assert started.tzinfo is not None
    assert started.utcoffset() == timedelta(0)


def test_start_creates_job_dir_private(tmp_path, monkeypatch):
    _fake_popen(monkeypatch)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    state_root = tmp_path / "sr"

    mr.start_merge_reverify("P", "S-1", str(worktree), state_root)

    job_dir = state_root / "merge_reverify" / "S-1"
    assert job_dir.is_dir()
    assert job_dir.stat().st_mode & 0o777 == 0o700


def test_start_tightens_permissions_of_an_existing_job_dir(tmp_path, monkeypatch):
    _fake_popen(monkeypatch)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    job_dir = tmp_path / "sr" / "merge_reverify" / "S-1"
    job_dir.mkdir(parents=True)
    os.chmod(job_dir, 0o755)

    mr.start_merge_reverify("P", "S-1", str(worktree), tmp_path / "sr")

    assert job_dir.stat().st_mode & 0o777 == 0o700


def test_start_deletes_a_stale_result_before_spawning(tmp_path, monkeypatch):
    calls = _fake_popen(monkeypatch)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    job_dir = tmp_path / "sr" / "merge_reverify" / "S-1"
    job_dir.mkdir(parents=True)
    os.chmod(job_dir, 0o700)
    stale = job_dir / "result.json"
    stale.write_text(json.dumps({"state": "pass"}))

    seen: list[bool] = []
    real_popen = mr.subprocess.Popen

    def _popen(argv, **kwargs):
        seen.append(stale.exists())
        return _Proc()

    monkeypatch.setattr(mr.subprocess, "Popen", _popen)
    mr.start_merge_reverify("P", "S-1", str(worktree), tmp_path / "sr")

    assert seen == [False], "the stale result must be gone BEFORE the child starts"
    assert not stale.exists()
    assert calls == {}
    assert real_popen is not None


# ---------------------------------------------------------------------------
# _pid_alive
# ---------------------------------------------------------------------------


def test_pid_alive_false_when_kill_raises_process_lookup(monkeypatch):
    def _kill(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(mr.os, "kill", _kill)
    assert mr._pid_alive(4242) is False


def test_pid_alive_true_for_a_running_process(monkeypatch):
    monkeypatch.setattr(mr.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(mr.subprocess, "run", lambda *a, **k: _Completed(stdout="S\n"))
    assert mr._pid_alive(4242) is True


def test_pid_alive_false_for_a_zombie(monkeypatch):
    monkeypatch.setattr(mr.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(mr.subprocess, "run", lambda *a, **k: _Completed(stdout="Z+\n"))
    assert mr._pid_alive(4242) is False


def test_pid_alive_false_when_ps_reports_no_process(monkeypatch):
    monkeypatch.setattr(mr.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(mr.subprocess, "run", lambda *a, **k: _Completed(stdout=""))
    assert mr._pid_alive(4242) is False


def test_pid_alive_true_when_the_probe_is_denied(monkeypatch):
    def _kill(pid, sig):
        raise PermissionError

    monkeypatch.setattr(mr.os, "kill", _kill)
    assert mr._pid_alive(4242) is True


def test_pid_alive_true_when_ps_fails(monkeypatch):
    monkeypatch.setattr(mr.os, "kill", lambda pid, sig: None)

    def _run(*args, **kwargs):
        raise OSError("no ps here")

    monkeypatch.setattr(mr.subprocess, "run", _run)
    assert mr._pid_alive(4242) is True


# ---------------------------------------------------------------------------
# collect_merge_reverify
# ---------------------------------------------------------------------------


def test_collect_returns_none_while_a_young_job_runs(tmp_path, monkeypatch):
    job = _job(tmp_path)
    monkeypatch.setattr(mr, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(mr, "_now", lambda: _at("2026-10-28T12:00:20+00:00"))
    assert mr.collect_merge_reverify(job, 30) is None


def test_collect_does_not_kill_at_exactly_the_timeout(tmp_path, monkeypatch):
    job = _job(tmp_path)
    killed: list = []
    monkeypatch.setattr(mr, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(mr, "_now", lambda: _at("2026-10-28T12:00:30+00:00"))
    monkeypatch.setattr(mr.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    assert mr.collect_merge_reverify(job, 30) is None
    assert killed == []


def test_collect_returns_none_when_the_clock_went_backwards(tmp_path, monkeypatch):
    job = _job(tmp_path)
    killed: list = []
    monkeypatch.setattr(mr, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(mr, "_now", lambda: _at("2026-10-28T11:59:55+00:00"))
    monkeypatch.setattr(mr.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    assert mr.collect_merge_reverify(job, 30) is None
    assert killed == []


def test_collect_kills_and_reports_a_timeout(tmp_path, monkeypatch):
    job = _job(tmp_path)
    killed: list = []
    monkeypatch.setattr(mr, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(mr, "_now", lambda: _at("2026-10-28T12:00:31+00:00"))
    monkeypatch.setattr(mr.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    result = mr.collect_merge_reverify(job, 30)

    assert result == {
        "state": "error",
        "error": "merge re-verify timed out after 30s",
        "timed_out": True,
    }
    assert killed == [(4242, signal.SIGTERM)]


def test_collect_repeats_the_timeout_and_never_clamps_the_start_time(tmp_path, monkeypatch):
    started = "2026-10-28T12:00:00+00:00"
    job = _job(tmp_path, started=started)
    killed: list = []
    monkeypatch.setattr(mr, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(mr, "_now", lambda: _at("2026-10-28T12:00:40+00:00"))
    monkeypatch.setattr(mr.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    first = mr.collect_merge_reverify(job, 30)
    second = mr.collect_merge_reverify(job, 30)

    assert first == second
    assert second["state"] == "error"
    assert second["timed_out"] is True
    assert killed == [(4242, signal.SIGTERM), (4242, signal.SIGTERM)]
    assert job["started_at"] == started


def test_collect_swallows_a_varnished_killpg(tmp_path, monkeypatch):
    job = _job(tmp_path)
    monkeypatch.setattr(mr, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(mr, "_now", lambda: _at("2026-10-28T12:00:31+00:00"))

    def _killpg(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(mr.os, "killpg", _killpg)
    result = mr.collect_merge_reverify(job, 30)
    assert result["state"] == "error"
    assert result["timed_out"] is True


def test_collect_returns_the_parsed_pass_result(tmp_path, monkeypatch):
    job = _job(tmp_path)
    payload = {"state": "pass", "error": "", "sha": "abc", "last_test_check": "x"}
    Path(job["result_path"]).write_text(json.dumps(payload))
    monkeypatch.setattr(mr, "_pid_alive", lambda pid: False)
    assert mr.collect_merge_reverify(job, 30) == payload


def test_collect_returns_the_parsed_fail_result(tmp_path, monkeypatch):
    job = _job(tmp_path)
    payload = {"state": "fail", "error": "acceptance reverify fail: 2 failed", "sha": "abc"}
    Path(job["result_path"]).write_text(json.dumps(payload))
    monkeypatch.setattr(mr, "_pid_alive", lambda pid: False)
    assert mr.collect_merge_reverify(job, 30) == payload


def test_collect_returns_the_parsed_error_result(tmp_path, monkeypatch):
    job = _job(tmp_path)
    payload = {"state": "error", "error": "merge re-verify raised RuntimeError"}
    Path(job["result_path"]).write_text(json.dumps(payload))
    monkeypatch.setattr(mr, "_pid_alive", lambda pid: False)
    assert mr.collect_merge_reverify(job, 30) == payload


def test_collect_reports_a_missing_result_as_error(tmp_path, monkeypatch):
    job = _job(tmp_path)
    Path(job["log_path"]).write_text("boom: traceback here")
    monkeypatch.setattr(mr, "_pid_alive", lambda pid: False)

    result = mr.collect_merge_reverify(job, 30)

    assert result["state"] == "error"
    assert result["state"] != "fail"
    assert result["error"] == "merge re-verify exited without a result"
    assert result["log_tail"] == "boom: traceback here"
    assert "timed_out" not in result


def test_collect_reports_garbage_json_as_error(tmp_path, monkeypatch):
    job = _job(tmp_path)
    Path(job["result_path"]).write_text("{not json")
    monkeypatch.setattr(mr, "_pid_alive", lambda pid: False)

    result = mr.collect_merge_reverify(job, 30)

    assert result["state"] == "error"
    assert result["error"] == "merge re-verify exited without a result"


def test_collect_reports_a_non_dict_result_as_error(tmp_path, monkeypatch):
    job = _job(tmp_path)
    Path(job["result_path"]).write_text(json.dumps(["pass"]))
    monkeypatch.setattr(mr, "_pid_alive", lambda pid: False)

    result = mr.collect_merge_reverify(job, 30)

    assert result["state"] == "error"
    assert result["error"] == "merge re-verify exited without a result"


def test_collect_reports_an_unknown_state_as_error(tmp_path, monkeypatch):
    job = _job(tmp_path)
    Path(job["result_path"]).write_text(json.dumps({"state": "maybe", "error": ""}))
    monkeypatch.setattr(mr, "_pid_alive", lambda pid: False)

    result = mr.collect_merge_reverify(job, 30)

    assert result["state"] == "error"
    assert result["state"] != "fail"
    assert result["error"] == "merge re-verify exited without a result"


def test_collect_treats_a_zombie_as_dead_not_timed_out(tmp_path, monkeypatch):
    job = _job(tmp_path)
    monkeypatch.setattr(mr.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(mr.subprocess, "run", lambda *a, **k: _Completed(stdout="Z\n"))
    monkeypatch.setattr(mr, "_now", lambda: _at("2026-10-28T13:00:00+00:00"))

    result = mr.collect_merge_reverify(job, 30)

    assert result["state"] == "error"
    assert result["error"] == "merge re-verify exited without a result"
    assert "timed_out" not in result


def test_collect_returns_none_when_the_liveness_probe_errors(tmp_path, monkeypatch):
    job = _job(tmp_path)

    def _kill(pid, sig):
        raise PermissionError

    monkeypatch.setattr(mr.os, "kill", _kill)
    monkeypatch.setattr(mr, "_now", lambda: _at("2026-10-28T12:00:01+00:00"))
    assert mr.collect_merge_reverify(job, 30) is None


def test_collect_truncates_the_log_tail_to_2000_chars(tmp_path, monkeypatch):
    job = _job(tmp_path)
    body = "x" * 5000
    Path(job["log_path"]).write_text(body)
    monkeypatch.setattr(mr, "_pid_alive", lambda pid: False)

    result = mr.collect_merge_reverify(job, 30)

    assert result["log_tail"] == body[-2000:]
    assert len(result["log_tail"]) == 2000


def test_collect_reports_a_missing_log_as_an_empty_tail(tmp_path, monkeypatch):
    job = _job(tmp_path)
    monkeypatch.setattr(mr, "_pid_alive", lambda pid: False)
    assert mr.collect_merge_reverify(job, 30)["log_tail"] == ""


# ---------------------------------------------------------------------------
# cancel_merge_reverify
# ---------------------------------------------------------------------------


def test_cancel_kills_the_process_group(tmp_path, monkeypatch):
    job = _job(tmp_path)
    killed: list = []
    monkeypatch.setattr(mr, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(mr.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    assert mr.cancel_merge_reverify(job) is None
    assert killed == [(4242, signal.SIGTERM)]


def test_cancel_swallows_a_vanished_process(tmp_path, monkeypatch):
    job = _job(tmp_path)
    monkeypatch.setattr(mr, "_pid_alive", lambda pid: True)

    def _killpg(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(mr.os, "killpg", _killpg)
    assert mr.cancel_merge_reverify(job) is None


def test_cancel_does_nothing_for_a_dead_pid(tmp_path, monkeypatch):
    job = _job(tmp_path)
    killed: list = []
    monkeypatch.setattr(mr, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(mr.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    assert mr.cancel_merge_reverify(job) is None
    assert killed == []


def test_cancel_of_an_empty_job_never_raises():
    assert mr.cancel_merge_reverify({}) is None


# ---------------------------------------------------------------------------
# main: the job body
# ---------------------------------------------------------------------------


def _patch_git(monkeypatch, sha: str = "deadbeef" * 5) -> list:
    calls: list = []

    def _run(argv, **kwargs):
        calls.append((argv, kwargs))
        return _Completed(stdout=sha + "\n")

    monkeypatch.setattr(mr.subprocess, "run", _run)
    return calls


def test_main_passes_and_keeps_the_manifest_untouched(tmp_path, monkeypatch):
    manifest = _write_manifest(tmp_path)
    before = manifest.read_bytes()
    monkeypatch.setattr(server, "_store", _Store(manifest))
    worktree = tmp_path / "wt"
    worktree.mkdir()
    result_path = tmp_path / "result.json"
    git_calls = _patch_git(monkeypatch, sha="cafe1234")

    def _acceptance(story, path, key):
        story["last_test_check"] = "2026-10-28T12:00:00Z"
        return {"state": "pass", "error": ""}

    monkeypatch.setattr(server, "_reverify_acceptance", _acceptance)
    monkeypatch.setattr(
        server, "_reverify_build", lambda path: {"state": "pass", "error": ""}
    )

    rc = mr.main(["P", "S-1", str(worktree), str(result_path)])

    assert rc == 0
    assert result_path.exists()
    assert result_path.stat().st_mode & 0o777 == 0o600
    result = json.loads(result_path.read_text())
    assert result["state"] == "pass"
    assert result["error"] == ""
    assert result["sha"] == "cafe1234"
    assert result["last_test_check"] == "2026-10-28T12:00:00Z"
    assert git_calls[0][0] == ["git", "rev-parse", "HEAD"]
    assert Path(git_calls[0][1]["cwd"]) == worktree
    assert manifest.read_bytes() == before


def test_main_reports_an_acceptance_failure_without_running_the_build(tmp_path, monkeypatch):
    manifest = _write_manifest(tmp_path)
    monkeypatch.setattr(server, "_store", _Store(manifest))
    worktree = tmp_path / "wt"
    worktree.mkdir()
    result_path = tmp_path / "result.json"
    _patch_git(monkeypatch, sha="cafe1234")
    build_calls: list = []

    monkeypatch.setattr(
        server,
        "_reverify_acceptance",
        lambda story, path, key: {"state": "fail", "error": "2 failed"},
    )

    def _build(path):
        build_calls.append(path)
        return {"state": "pass", "error": ""}

    monkeypatch.setattr(server, "_reverify_build", _build)

    rc = mr.main(["P", "S-1", str(worktree), str(result_path)])

    assert rc == 0
    assert result_path.exists()
    result = json.loads(result_path.read_text())
    assert result["state"] == "fail"
    assert result["error"] == "acceptance reverify fail: 2 failed"
    assert result["sha"] == "cafe1234"
    assert result["last_test_check"] == "2026-10-01T00:00:00Z"
    assert build_calls == []


def test_main_reports_a_build_failure(tmp_path, monkeypatch):
    manifest = _write_manifest(tmp_path)
    monkeypatch.setattr(server, "_store", _Store(manifest))
    worktree = tmp_path / "wt"
    worktree.mkdir()
    result_path = tmp_path / "result.json"
    _patch_git(monkeypatch, sha="cafe1234")
    monkeypatch.setattr(
        server, "_reverify_acceptance", lambda story, path, key: {"state": "pass", "error": ""}
    )
    monkeypatch.setattr(
        server, "_reverify_build", lambda path: {"state": "fail", "error": "lint"}
    )

    rc = mr.main(["P", "S-1", str(worktree), str(result_path)])

    assert rc == 0
    assert result_path.exists()
    result = json.loads(result_path.read_text())
    assert result["state"] == "fail"
    assert result["error"] == "build reverify fail: lint"


def test_main_reports_a_raising_acceptance_as_an_error(tmp_path, monkeypatch):
    manifest = _write_manifest(tmp_path)
    monkeypatch.setattr(server, "_store", _Store(manifest))
    worktree = tmp_path / "wt"
    worktree.mkdir()
    result_path = tmp_path / "result.json"
    _patch_git(monkeypatch, sha="cafe1234")

    def _acceptance(story, path, key):
        raise RuntimeError("secret token abc")

    monkeypatch.setattr(server, "_reverify_acceptance", _acceptance)
    monkeypatch.setattr(
        server, "_reverify_build", lambda path: {"state": "pass", "error": ""}
    )

    rc = mr.main(["P", "S-1", str(worktree), str(result_path)])

    assert rc == 1
    assert result_path.exists()
    raw = result_path.read_text()
    result = json.loads(raw)
    assert result["state"] == "error"
    assert result["error"] == "merge re-verify raised RuntimeError"
    assert "secret token abc" not in raw
    assert result["sha"] == "cafe1234"


def test_main_reports_a_raising_build_as_an_error(tmp_path, monkeypatch):
    manifest = _write_manifest(tmp_path)
    monkeypatch.setattr(server, "_store", _Store(manifest))
    worktree = tmp_path / "wt"
    worktree.mkdir()
    result_path = tmp_path / "result.json"
    _patch_git(monkeypatch, sha="cafe1234")

    def _acceptance(story, path, key):
        story["last_test_check"] = "2026-10-28T12:00:00Z"
        return {"state": "pass", "error": ""}

    def _build(path):
        raise ValueError("internal path /home/someone/secret")

    monkeypatch.setattr(server, "_reverify_acceptance", _acceptance)
    monkeypatch.setattr(server, "_reverify_build", _build)

    rc = mr.main(["P", "S-1", str(worktree), str(result_path)])

    assert rc == 1
    assert result_path.exists()
    raw = result_path.read_text()
    result = json.loads(raw)
    assert result["state"] == "error"
    assert result["error"] == "merge re-verify raised ValueError"
    assert "/home/someone/secret" not in raw
    assert result["last_test_check"] == "2026-10-28T12:00:00Z"


def test_main_rejects_a_wrong_argument_count(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_store", _Store(_write_manifest(tmp_path)))
    assert mr.main(["P", "S-1"]) == 1


def test_main_reports_a_missing_manifest_as_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_store", _Store(tmp_path / "absent.manifest.json"))
    worktree = tmp_path / "wt"
    worktree.mkdir()
    result_path = tmp_path / "result.json"
    _patch_git(monkeypatch, sha="cafe1234")

    rc = mr.main(["P", "S-1", str(worktree), str(result_path)])

    assert rc == 1
    assert result_path.exists()
    result = json.loads(result_path.read_text())
    assert result["state"] == "error"
    assert result["error"].startswith("merge re-verify raised ")
    assert str(tmp_path) not in result["error"]
