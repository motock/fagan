"""_build_dispatch_command places the context block after the brief and
before the rework instruction, exactly once, on fresh and resume paths."""
from pipeline.persona import _build_dispatch_command

STORY = {"summary": "s", "agent_instructions": "BRIEF-TEXT", "rework_attempts": 0}
BLOCK = "CONTEXT-BLOCK-MARKER"
JOURNAL = [{"step": "a", "summary": "did a", "next_hint": "go"}]


def test_should_append_block_after_brief_before_rework_on_fresh_path():
    prompt = _build_dispatch_command(
        STORY, "K-1", review_feedback="FIX-THIS", context_block=BLOCK)["prompt"]

    assert prompt.count(BLOCK) == 1
    assert prompt.index("BRIEF-TEXT") < prompt.index(BLOCK) < prompt.index("FIX-THIS")


def test_should_include_block_once_on_resume_path():
    prompt = _build_dispatch_command(
        STORY, "K-1", resume_journal=JOURNAL, review_feedback="FIX-THIS",
        context_block=BLOCK)["prompt"]

    assert prompt.count(BLOCK) == 1
    assert prompt.index("BRIEF-TEXT") < prompt.index(BLOCK) < prompt.index("FIX-THIS")


def test_should_omit_block_when_empty():
    prompt = _build_dispatch_command(STORY, "K-1")["prompt"]

    assert BLOCK not in prompt
