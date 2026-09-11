"""Regression pin for test_advance_pipeline_retries_on_local_fallback_model.

That test passes on macOS and fails on ubuntu because its subprocess fake
returns a failure for EVERY command except ``ps`` - including the ``git``
commands the reap path legitimately runs (pipeline/checkpoint.py
``_commit_wip``).  On Linux, check_story_status reaches a ``_commit_wip``
call site that macOS never reaches, so the fake's bogus git failure
surfaces as a non-benign commit error and aborts the tick before the dead
local story can be requeued onto the fallback model.

The tests here pin the same requirement under a FAITHFUL fake: ``ps``
reports the pid gone, ``git`` succeeds, every other command fails.  That
is the environment the production reap path is actually written for; a
fake that fails ``git`` is not a realistic checkout.
"""
import json

from pipeline import server as p
from tests.unit._pipeline_mcp_server_test_helpers import _read_manifest


class _FailResult:
    stdout = "test failed"
    stderr = ""
    returncode = 1


class _OkResult:
    stdout = ""
    stderr = ""
    returncode = 0


def _faithful_fake(cmd, **kw):
    """Fail everything except `ps` (pid gone) and `git` (real checkout)."""
    if cmd and cmd[0] == "ps":
        class _Gone:
            returncode = 1
            stdout = ""
            stderr = ""
        return _Gone()
    if cmd and cmd[0] == "git":
        return _OkResult()
    return _FailResult()


def test_reap_requeues_dead_local_story_onto_fallback_model(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Dead local agent + fallback model configured: the reap path must
    requeue the story to "todo" for the fallback model without tripping
    _commit_wip's non-benign guard."""
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir(parents=True, exist_ok=True)
    (worktree_path / "agent.log").write_text("some output\n")
    (plan_dir / "escfallback.manifest.json").write_text(json.dumps({
        "epics": {}, "local_model_fallback": "glm-5.2:cloud",
        "stories": {
            "S1": {"summary": "Thing", "agent_instructions": "Build.",
                   "status": "in_progress", "pid": 9004,
                   "worktree": str(worktree_path),
                   "log": str(worktree_path / "agent.log"),
                   "backend": "local", "dependencies": []},
        },
    }))

    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p.subprocess, "run", _faithful_fake)
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (True, ""))

    result = p.advance_pipeline("escfallback")

    story = _read_manifest(plan_dir, "escfallback")["stories"]["S1"]
    assert story["status"] == "todo"
    assert story["backend"] == "local"
    assert story.get("model") == "glm-5.2:cloud"
    assert story.get("tried_fallback_model") is True
    assert "S1" in result.get("retry", result.get("dispatched", []))