"""The dispatched prompt carries the overlord diagnosis (ov-2).

Drives the real ``dispatch_story`` path with the agent backend stubbed to
capture its prompt, reusing the harness of
``test_dispatch_local_scope_kwargs_wiring``. A story with no diagnosis must
dispatch with no diagnosis block, and the authored ``agent_instructions`` must
be left untouched.
"""

import json

import pytest

from pipeline.config import WORKTREE_SCOPE_RULE
from tests.unit.test_dispatch_local_scope_kwargs_wiring import (  # noqa: F401
    _dispatch,
    _make_repo,
    _scoped_survives_dispatch,
    agents_dir,
    bus,
    captured,
    plan_dir,
    worktree_root,
)

_BRIEF = "Build it."


def _entry(text: str) -> dict:
    return {"ts": "2026-11-13T00:00:00+00:00", "class": "no_commit_after_rework", "text": text}


@pytest.fixture
def dispatch(plan_dir, worktree_root, agents_dir, captured, bus, monkeypatch, tmp_path):  # noqa: F811
    """Return ``run(story) -> prompt`` that dispatches one story and yields its prompt."""
    monkeypatch.delenv("PIPELINE_TEST_SCOPE", raising=False)
    repo, branch = _make_repo(tmp_path)

    def run(story: dict) -> str:
        _dispatch(plan_dir, repo, branch, monkeypatch, {"backend": "ollama", **story})
        return captured["ollama"][-1]["prompt"]

    return run


def test_dispatched_prompt_carries_the_latest_overlord_diagnosis(dispatch):
    prompt = dispatch({
        "dispatch_attempts": 2,
        "triage_diagnosis": [_entry("old cause"), _entry("Commit the WIP before the suite.")],
    })

    block = "OVERLORD DIAGNOSIS (attempt 2): Commit the WIP before the suite.\n\n"
    assert f"{WORKTREE_SCOPE_RULE}\n\n{block}You are completing issue" in prompt
    assert "old cause" not in prompt


def test_story_without_a_diagnosis_dispatches_no_diagnosis_block(dispatch):
    prompt = dispatch({})

    assert f"{WORKTREE_SCOPE_RULE}\n\nYou are completing issue" in prompt
    assert "OVERLORD DIAGNOSIS" not in prompt


@pytest.mark.parametrize("history", [[], ["not a dict"]], ids=["empty", "malformed"])
def test_unusable_diagnosis_history_dispatches_no_diagnosis_block(dispatch, history):
    prompt = dispatch({"triage_diagnosis": history})

    assert "OVERLORD DIAGNOSIS" not in prompt


def test_dispatch_snapshots_the_authored_brief_not_the_diagnosis(dispatch, tmp_path):
    dispatch({
        "agent_instructions": _BRIEF,
        "triage_diagnosis": [_entry("Commit early.")],
    })

    manifest = json.loads((tmp_path / "plans" / "scope.manifest.json").read_text())
    story = manifest["stories"]["S1"]
    assert story["_dispatched_agent_instructions"] == _BRIEF
    assert story["agent_instructions"] == _BRIEF
