"""Planner and test-author prompts carry their phase's recorded context block."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import pipeline.review as review_mod
import pipeline.server  # noqa: F401
from pipeline import context_sources as ctxsrc
from pipeline import paths, planner, test_author

PLANNER_TEXT = "Planner-only guidance."
AUTHOR_TEXT = "Test-author-only guidance."
DISPATCH_TEXT = "Dispatch-only guidance."
BRIEF = "Do the thing."


@pytest.fixture
def manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    specs = [
        ("plan-src", PLANNER_TEXT, ["planner"]),
        ("author-src", AUTHOR_TEXT, ["test_author"]),
        ("dispatch-src", DISPATCH_TEXT, ["dispatch"]),
    ]
    sources, digests = [], {}
    for name, text, phases in specs:
        sources.append({
            "name": name, "provider": "dir", "uri": "/x", "mode": "prompt",
            "authority": "reference", "phases": phases, "required": True,
        })
        digests[name] = {"digest": ctxsrc.snapshot_context(text), "bytes": len(text)}
    story = {"summary": "S", "agent_instructions": BRIEF, "context_digests": digests}
    return {"context": {"sources": sources}, "stories": {"CL-1": story}}


def _planner_prompt(context_block):
    backend = MagicMock()
    backend.complete.return_value = "1. step"
    with (
        patch.object(planner.backend, "get_backend", return_value=backend),
        patch.object(planner, "_resolve_planner_backend", return_value=("claude", "m")),
    ):
        planner._run_planner(
            BRIEF, dispatch_backend="local", local_model="m", context_block=context_block
        )
    return backend.complete.call_args.args[0]


def test_should_include_planner_block_after_instructions(manifest):
    block = review_mod.build_phase_context_block("CL-1", manifest, "planner")
    prompt = _planner_prompt(block)
    assert prompt.index(BRIEF) < prompt.index(PLANNER_TEXT)


def test_should_omit_other_phase_sources_from_planner_prompt(manifest):
    block = review_mod.build_phase_context_block("CL-1", manifest, "planner")
    prompt = _planner_prompt(block)
    assert DISPATCH_TEXT not in prompt
    assert AUTHOR_TEXT not in prompt


def test_should_leave_planner_prompt_unchanged_without_context():
    assert _planner_prompt("") == BRIEF


def test_should_include_test_author_block_after_instructions(manifest):
    block = review_mod.build_phase_context_block("CL-1", manifest, "test_author")
    prompt = test_author._test_author_prompt(BRIEF, block)
    assert prompt.index(BRIEF) < prompt.index(AUTHOR_TEXT)


def test_should_omit_other_phase_sources_from_test_author_prompt(manifest):
    block = review_mod.build_phase_context_block("CL-1", manifest, "test_author")
    prompt = test_author._test_author_prompt(BRIEF, block)
    assert DISPATCH_TEXT not in prompt
    assert PLANNER_TEXT not in prompt


def test_should_leave_test_author_prompt_unchanged_without_context():
    assert test_author._test_author_prompt(BRIEF, "") == test_author._test_author_prompt(BRIEF)


def test_should_pass_context_block_into_started_test_author_dispatch(tmp_path):
    handle = MagicMock(pid=1)
    fake = MagicMock()
    fake.dispatch.return_value = handle
    story = {"agent_instructions": BRIEF, "files": ["pipeline/x.py"]}
    with (
        patch.object(test_author.backend, "get_backend", return_value=fake),
        patch.object(test_author, "_resolve_test_author_backend", return_value=("claude", "m")),
    ):
        test_author.start_test_author_phase(
            story, story_key="CL-1", worktree_path=tmp_path, dispatch_backend="local",
            local_model="m", plan_name="p", context_block="CTX-BLOCK",
        )
    assert "CTX-BLOCK" in fake.dispatch.call_args.kwargs["prompt"]
