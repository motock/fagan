"""Probe: does check_story_status reach start_detached_grade (Popen) on macOS
under the target test's exact fake? Run with:
    python3 -m pytest tests/unit/_probe_spawn.py -q -s -n0
"""
import json
import sys

from pipeline import server as p
from pipeline import story_status as ss
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    agents_dir as _agents_dir,
    plan_dir as _plan_dir,
    worktree_root as _worktree_root,
)


def test_probe_spawn_on_macos(plan_dir, worktree_root, agents_dir, monkeypatch):
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / "agent.log").write_text("some output\n")
    (plan_dir / "escfallback.manifest.json").write_text(json.dumps({
        "epics": {}, "local_model_fallback": "glm-5.2:cloud",
        "stories": {
            "S1": {"summary": "Thing", "agent_instructions": "Build.",
                   "status": "in_progress", "pid": 9005,
                   "worktree": str(worktree_path),
                   "log": str(worktree_path / "agent.log"),
                   "backend": "local", "dispatched_model": "gpt-oss:20b",
                   "dependencies": []},
        },
    }))

    class _FailResult:
        stdout = "test failed"
        stderr = ""
        returncode = 1

    def _fake_subprocess(cmd, **kw):
        print(f"SUBPROCESS.RUN CALLED: {cmd}", flush=True)
        if cmd and cmd[0] == "ps":
            class _Gone:
                returncode = 1
                stdout = ""
                stderr = ""
            return _Gone()
        return _FailResult()

    popen_calls = []

    class _FakePopen:
        def __init__(self, *a, **kw):
            popen_calls.append((a, kw))
            raise OSError("PROBE: Popen blocked")

    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p.subprocess, "run", _fake_subprocess)
    monkeypatch.setattr(ss.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (True, ""))
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg, **kw: None)

    print(f"starter present: {'start_detached_grade' in vars(ss)}", flush=True)
    print(f"sys.executable: {sys.executable!r}", flush=True)

    result = p.advance_pipeline("escfallback")
    print(f"POPCALLS: {popen_calls}", flush=True)
    print(f"RESULT: {result}", flush=True)
    story = json.loads((plan_dir / "escfallback.manifest.json").read_text())["stories"]["S1"]
    print(f"STATUS: {story.get('status')} model={story.get('model')} tried={story.get('tried_fallback_model')}", flush=True)
    assert True