"""``unblock_story``: the MCP path to ``pipeline.unpark.unpark_story``.

The tool authorizes like ``set_story_status`` (plan owner or delegate, deny by
default), delegates the unblock to ``unpark_story`` and appends an optional
note to the story's brief under the same plan lock. A changed brief or ``files``
resets the plan approval. A refused value is never echoed back.
"""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

import pipeline.server as p
from app.identity import Principal
from pipeline import persistence as ppers
from pipeline import plan_approval
from pipeline import server_tools_lifecycle as lifecycle
from pipeline import unpark as unpark_mod
from pipeline.actor import ANONYMOUS, bind_principal
from pipeline.authorization import ENFORCE_ENV
from pipeline.remote_tool_policy import REMOTE_ENFORCED
from pipeline.store import FileStore

PLAN = "forge"
STORY = "S1"
ALICE = Principal(subject="https://idp#alice", auth_method="oidc")
BOB = Principal(subject="https://idp#bob", auth_method="oidc")
BASE_BRIEF = "Do the thing."


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    monkeypatch.delenv(plan_approval.APPROVAL_ENV, raising=False)
    return d


@pytest.fixture
def notices(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(
        unpark_mod,
        "_notify_user",
        lambda plan, message, **kw: seen.append({"plan": plan, "message": message, **kw}),
    )
    monkeypatch.setattr(unpark_mod, "_record_resolution", lambda *a, **k: None)
    return seen


def _write(**extra) -> None:
    story = {
        "summary": "s",
        "status": "parked",
        "agent_instructions": BASE_BRIEF,
        "files": ["a.py"],
        "rework_attempts": 3,
        "parked_reason": "x",
    }
    FileStore().save_manifest(
        PLAN, {"plan": PLAN, "owner": "https://idp#alice", "stories": {STORY: story}, **extra}
    )


def _story() -> dict:
    return FileStore().get_manifest(PLAN)["stories"][STORY]


def _call(principal=ALICE, **kwargs):
    args = {"plan_name": PLAN, "story_key": STORY, "clear_fields": ["rework_attempts"]}
    args.update(kwargs)
    with bind_principal(principal):
        return lifecycle.unblock_story(**args)


def test_owner_can_unblock(plan_dir, notices):
    _write()
    result = _call(status="interrupted", note="fixed the fixture")
    assert result["ok"] is True
    story = _story()
    assert "rework_attempts" not in story
    assert story["status"] == "interrupted"
    assert story["agent_instructions"].startswith(BASE_BRIEF)
    assert "fixed the fixture" in story["agent_instructions"]
    assert [n["event"] for n in notices] == ["story_unparked_manually"]


def test_registered_and_remote_enforced():
    assert "unblock_story" in REMOTE_ENFORCED
    assert p.unblock_story is lifecycle.unblock_story
    assert lifecycle.unblock_story in lifecycle.TOOLS


@pytest.mark.parametrize("principal", [BOB, ANONYMOUS])
def test_non_owner_and_unknown_principal_refused(plan_dir, notices, principal):
    _write()
    result = _call(principal=principal, note="n", add_files=["b.py"])
    assert result["ok"] is False
    story = _story()
    assert story["rework_attempts"] == 3
    assert story["agent_instructions"] == BASE_BRIEF
    assert story["files"] == ["a.py"]
    assert notices == []


@pytest.mark.parametrize(
    "bad", ["/etc/passwd", "../x.py", "a/../b.py", "a\\b.py", "a\x00b.py"]
)
def test_unsafe_add_files_refused_and_nothing_written(plan_dir, notices, bad):
    _write()
    result = _call(add_files=[bad], note="n")
    assert result["ok"] is False
    assert bad not in json.dumps(result)
    story = _story()
    assert story["rework_attempts"] == 3
    assert story["files"] == ["a.py"]
    assert story["agent_instructions"] == BASE_BRIEF
    assert notices == []


def test_add_files_appended(plan_dir, notices):
    _write()
    assert _call(add_files=["b.py"])["ok"] is True
    assert _story()["files"] == ["a.py", "b.py"]


def test_unknown_field_refused_without_echo(plan_dir, notices):
    _write()
    result = _call(clear_fields=["rework_attempts", "pr_url_secretname"])
    assert result["ok"] is False
    assert "pr_url_secretname" not in json.dumps(result)
    assert _story()["rework_attempts"] == 3


def test_invalid_status_refused_without_echo(plan_dir, notices):
    _write()
    result = _call(status="bogus-status")
    assert result["ok"] is False
    assert "bogus-status" not in json.dumps(result)
    assert _story()["rework_attempts"] == 3


def test_overlong_note_refused(plan_dir, notices):
    _write()
    assert _call(note="x" * 8001)["ok"] is False
    assert _story()["rework_attempts"] == 3
    assert _call(note="x" * 8000)["ok"] is True


def test_unknown_story_refused(plan_dir, notices):
    _write()
    result = _call(story_key="NOPE", note="n")
    assert result["ok"] is False


def test_lock_contention_is_retryable(plan_dir, notices, monkeypatch):
    _write()

    @contextmanager
    def busy(name, wait_seconds=0.0):
        yield False

    monkeypatch.setattr(p._store, "transaction", busy)
    result = _call(note="n")
    assert result["ok"] is True
    assert result["skipped"] == "locked"
    assert "retry_after_seconds" in result
    assert _story()["rework_attempts"] == 3
    assert notices == []


def test_approval_reset_and_reported(plan_dir, notices, monkeypatch):
    monkeypatch.setenv(plan_approval.APPROVAL_ENV, "1")
    _write()
    manifest = FileStore().get_manifest(PLAN)
    manifest["approval"] = {
        "status": "approved",
        "digest": plan_approval.canonical_digest(manifest),
        "digest_version": plan_approval.APPROVAL_DIGEST_VERSION,
    }
    FileStore().save_manifest(PLAN, manifest)
    result = _call(note="more detail")
    assert result["approval_reset"] is True
    assert FileStore().get_manifest(PLAN)["approval"]["status"] == "pending_approval"


def test_approval_untouched_when_gate_off(plan_dir, notices):
    _write()
    result = _call(note="more detail")
    assert result["approval_reset"] is False
    assert "approval" not in FileStore().get_manifest(PLAN)


def test_note_rolled_back_when_unpark_fails(plan_dir, notices, monkeypatch):
    _write()

    def boom(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(lifecycle, "unpark_story", boom)
    result = _call(note="n")
    assert result["ok"] is False
    assert _story()["agent_instructions"] == BASE_BRIEF
    assert notices == []


def test_cli_unchanged(plan_dir, notices, capsys):
    _write()
    assert unpark_mod.main([PLAN, STORY, "--clear", "rework_attempts"]) == 0
    story = _story()
    assert "rework_attempts" not in story
    assert story["agent_instructions"] == BASE_BRIEF
    assert story["status"] == "interrupted"
    assert "unparked" in capsys.readouterr().out
