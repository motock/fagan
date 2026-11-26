"""pipeline.server_bindings resolves server names for pipeline.ci at call time.

pipeline.ci reads a handful of names that live on pipeline.server. Those names
are looked up through ``server_bindings.server()`` when the function runs, so a
test that monkeypatches them on pipeline.server is honoured, and pipeline.ci
still imports without importing the server.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline import ci, detached_grade, server_bindings, story_status
from pipeline import server as server_module
from pipeline.config import DISPATCH_MAX_ATTEMPTS

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_should_return_the_loaded_server_module():
    assert server_bindings.server() is sys.modules["pipeline.server"]


def test_should_raise_runtime_error_when_the_server_is_not_loaded(monkeypatch):
    monkeypatch.delitem(sys.modules, "pipeline.server")

    with pytest.raises(RuntimeError) as excinfo:
        server_bindings.server()

    assert type(excinfo.value) is RuntimeError
    assert str(excinfo.value) == "pipeline.server is not loaded"


def test_should_see_a_monkeypatched_server_name_at_call_time(tmp_path, monkeypatch):
    path = tmp_path / "PENDING.md"
    monkeypatch.setattr(server_module, "RETRO_PENDING_PATH", path)

    ci._record_retro_pending("patched-path-plan", 1)

    assert path.read_text().startswith("- patched-path-plan ")


def test_should_import_ci_without_importing_the_server():
    code = "import sys, pipeline.ci; print('pipeline.server' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


class _FakeRecords:
    def __init__(self, manifest):
        self.manifest = manifest
        self.get_calls = []
        self.put_calls = []

    def get_manifest(self, namespace, plan_name):
        self.get_calls.append(plan_name)
        return self.manifest

    def put_manifest(self, namespace, plan_name, manifest):
        self.put_calls.append((plan_name, manifest))


class _FakeStore:
    def __init__(self, records):
        self._records = records

    def records(self):
        return self._records


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _launch_failing_manifest(worktree, pid):
    return {
        "stories": {
            "story-1": {
                "status": "in_progress",
                "pid": pid,
                "worktree": str(worktree),
                "dispatch_attempts": DISPATCH_MAX_ATTEMPTS - 1,
            }
        }
    }


def test_should_use_a_patched_store_in_check_story_status(monkeypatch):
    records = _FakeRecords(manifest=None)
    monkeypatch.setattr(server_module, "_store", _FakeStore(records))

    result = story_status.check_story_status("patched-store-plan", "story-1")

    assert records.get_calls == ["patched-store-plan"]
    assert result == {"ok": False, "error": "Plan not found"}


def test_should_use_a_patched_notify_user_in_check_story_status(tmp_path, monkeypatch):
    notified = []
    monkeypatch.setattr(
        server_module,
        "_notify_user",
        lambda plan_name, message, **kwargs: notified.append((plan_name, message)),
    )
    pid = _dead_pid()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    agent_log = worktree / "agent.log"
    agent_log.write_text("")
    os.utime(agent_log, (0, 0))
    records = _FakeRecords(_launch_failing_manifest(worktree, pid))
    monkeypatch.setattr(server_module, "_store", _FakeStore(records))

    result = story_status.check_story_status("notify-plan", "story-1")

    assert result == {"status": "failed", "pid": pid}
    assert [plan for plan, _ in notified] == ["notify-plan"]
    assert "failed to launch" in notified[0][1]


def test_should_treat_a_missing_pytest_only_export_as_absent(tmp_path, monkeypatch):
    monkeypatch.delattr(server_module, "start_detached_grade", raising=False)
    assert getattr(server_bindings.server(), "start_detached_grade", None) is None

    result = detached_grade._detached_grade_lifecycle(
        {"status": "in_progress", "worktree": str(tmp_path)},
        "story-1",
        _dead_pid(),
        tmp_path,
        {},
        "absent-export-plan",
        ["true"],
        tmp_path,
        {},
        _exports=server_bindings.server(),
    )

    assert result == (None, None)


def test_should_import_story_status_without_importing_the_server():
    code = "import sys, pipeline.story_status; print('pipeline.server' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"
