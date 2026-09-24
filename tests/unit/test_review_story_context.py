"""Tests for the reviewer's story-context injection.

Covers the two new module-level helpers in ``pipeline.review``
(``_deferred_lines`` and ``build_review_story_context``), the three new
module-level constants, and the ``story_context`` keyword parameter that
``_run_reviewer`` threads into the reviewer prompt.

The reviewer prompt is a shared artifact that later stories extend, so these
tests assert membership of the phrases this story owns plus ordering relative
to fixed anchors - never the prompt's total contents or length.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

# pipeline.server is imported explicitly; import order is not required for
# cold-importability (every pipeline module imports cold, see
# tests/unit/test_hub_satellite_cold_imports.py).
import pipeline.review as review_mod
import pipeline.server  # noqa: F401

# ---------------------------------------------------------------------------
# Fixed anchors / literals this story owns
# ---------------------------------------------------------------------------

BRIEF_HEADER = "--- This story's brief (what the diff was asked to do) ---"
SIBLING_HEADER = "--- Sibling stories in this plan (summary — status) ---"
DEFERRED_HEADER = "--- Work this brief defers to named sibling stories ---"
TRUNCATION_MARKER = "[... brief truncated ...]"
PRIOR_FINDINGS_MARKER = "--- Your prior Blocking findings on this branch ---"

# REVIEW_CONTEXT_SCOPE_RULE verbatim, as the brief specifies it.
SCOPE_RULE = (
    "Work that this story's brief explicitly assigns to another named sibling story "
    "(a `Deferred to <sibling> — not a finding:` line above) is out of scope for this diff: "
    "if you mention its absence at all, label it Suggestion, never Blocking. Documentation "
    "or comments made stale BY THIS DIFF remain Blocking under criterion (3)."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _manifest(stories: dict) -> dict:
    return {"stories": stories}


def _sibling_section(ctx: str) -> str:
    """The text of the sibling section, up to the next section header."""
    start = ctx.index(SIBLING_HEADER) + len(SIBLING_HEADER)
    rest = ctx[start:]
    if DEFERRED_HEADER in rest:
        rest = rest[: rest.index(DEFERRED_HEADER)]
    return rest


def _capture_review_prompt(monkeypatch, **kwargs) -> str:
    """Run _run_reviewer against a fake backend driver and return the prompt
    the reviewer persona was handed."""
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, **kw):
            captured["prompt"] = prompt
            captured["calls"] = captured.get("calls", 0) + 1
            return "VERDICT: APPROVE"

    monkeypatch.setattr(
        review_mod.backend, "get_backend", lambda role, name=None: _FakeDriver()
    )
    result = review_mod._run_reviewer(
        "/tmp/some-worktree", "agent/some-branch", **kwargs
    )
    assert result == "VERDICT: APPROVE"
    assert captured["calls"] == 1
    prompt = captured["prompt"]
    assert isinstance(prompt, str) and prompt
    return prompt


def _review_source_lines() -> list[str]:
    return Path(review_mod.__file__).read_text(encoding="utf-8").splitlines()


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


def test_review_brief_max_chars_constant():
    assert review_mod.REVIEW_BRIEF_MAX_CHARS == 12000


def test_review_siblings_max_constant():
    assert review_mod.REVIEW_SIBLINGS_MAX == 40


def test_review_context_scope_rule_constant():
    assert review_mod.REVIEW_CONTEXT_SCOPE_RULE == SCOPE_RULE


# ---------------------------------------------------------------------------
# _deferred_lines
# ---------------------------------------------------------------------------


def test_deferred_lines_accepts_em_dash_and_double_hyphen():
    text = (
        "Deferred to sibling-a — not a finding: alpha\n"
        "Deferred to sibling-b -- not a finding: beta\n"
    )
    assert review_mod._deferred_lines(text) == [
        "Deferred to sibling-a — not a finding: alpha",
        "Deferred to sibling-b -- not a finding: beta",
    ]


def test_deferred_lines_preserves_order():
    text = (
        "Deferred to sibling-z — not a finding: zed\n"
        "Deferred to sibling-a — not a finding: alpha\n"
    )
    assert review_mod._deferred_lines(text) == [
        "Deferred to sibling-z — not a finding: zed",
        "Deferred to sibling-a — not a finding: alpha",
    ]


def test_deferred_lines_returns_stripped_lines():
    text = "   Deferred to sibling-a — not a finding: alpha   \n"
    assert review_mod._deferred_lines(text) == [
        "Deferred to sibling-a — not a finding: alpha"
    ]


def test_deferred_lines_empty_and_none():
    assert review_mod._deferred_lines("") == []
    assert review_mod._deferred_lines(None) == []


def test_deferred_lines_ignores_mid_sentence_and_single_hyphen():
    text = (
        "This work is Deferred to sibling-a — not a finding: alpha\n"
        "Deferred to sibling-b - not a finding: beta\n"
        "Deferred to sibling-c — not a finding\n"
    )
    assert review_mod._deferred_lines(text) == []


# ---------------------------------------------------------------------------
# build_review_story_context - positive
# ---------------------------------------------------------------------------


def test_context_contains_brief_sibling_deferred_and_scope_rule():
    brief = (
        "Do the thing.\n"
        "Deferred to sibling-b — not a finding: sibling-b owns the widget.\n"
        "More work."
    )
    manifest = _manifest(
        {
            "story-a": {
                "agent_instructions": brief,
                "summary": "Story A",
                "status": "in_progress",
            },
            "sibling-b": {"summary": "Sibling B", "status": "pending"},
        }
    )
    ctx = review_mod.build_review_story_context("story-a", manifest)
    assert isinstance(ctx, str)
    assert "Do the thing." in ctx
    assert "- Sibling B — pending" in ctx
    assert "Deferred to sibling-b — not a finding: sibling-b owns the widget." in ctx
    assert review_mod.REVIEW_CONTEXT_SCOPE_RULE in ctx


def test_context_section_order():
    brief = "Deferred to sibling-b — not a finding: beta"
    manifest = _manifest(
        {
            "story-a": {"agent_instructions": brief},
            "sibling-b": {"summary": "B", "status": "pending"},
        }
    )
    ctx = review_mod.build_review_story_context("story-a", manifest)
    assert ctx.index(BRIEF_HEADER) < ctx.index(SIBLING_HEADER)
    assert ctx.index(SIBLING_HEADER) < ctx.index(DEFERRED_HEADER)
    assert ctx.index(DEFERRED_HEADER) < ctx.index(review_mod.REVIEW_CONTEXT_SCOPE_RULE)


def test_siblings_are_sorted_by_story_key():
    manifest = _manifest(
        {
            "story-a": {"agent_instructions": "brief", "summary": "A", "status": "done"},
            "c": {"summary": "C", "status": "done"},
            "a": {"summary": "A2", "status": "done"},
            "b": {"summary": "B", "status": "done"},
        }
    )
    ctx = review_mod.build_review_story_context("story-a", manifest)
    assert ctx.index("- A2 — done") < ctx.index("- B — done")
    assert ctx.index("- B — done") < ctx.index("- C — done")


def test_sibling_summary_and_status_fall_back():
    manifest = _manifest(
        {
            "story-a": {"agent_instructions": "brief"},
            "sib": {},
        }
    )
    ctx = review_mod.build_review_story_context("story-a", manifest)
    assert "- sib — unknown" in ctx


def test_deferred_section_present_when_lines_exist():
    brief = "Deferred to sibling-b — not a finding: beta"
    manifest = _manifest(
        {
            "story-a": {"agent_instructions": brief},
            "sibling-b": {"summary": "B", "status": "pending"},
        }
    )
    ctx = review_mod.build_review_story_context("story-a", manifest)
    assert DEFERRED_HEADER in ctx
    assert "Deferred to sibling-b — not a finding: beta" in ctx


# ---------------------------------------------------------------------------
# build_review_story_context - sibling cap
# ---------------------------------------------------------------------------


def test_more_than_max_siblings_are_capped():
    stories = {"story-a": {"agent_instructions": "brief"}}
    for i in range(45):
        stories[f"sib-{i:02d}"] = {"summary": f"S{i:02d}", "status": "done"}
    ctx = review_mod.build_review_story_context("story-a", _manifest(stories))
    section = _sibling_section(ctx)
    lines = [ln for ln in section.splitlines() if ln.startswith("- ")]
    more = [ln for ln in lines if ln.startswith("- (+")]
    assert more == ["- (+5 more)"]
    assert len(lines) - len(more) == review_mod.REVIEW_SIBLINGS_MAX == 40


def test_exactly_max_siblings_has_no_more_marker():
    stories = {"story-a": {"agent_instructions": "brief"}}
    for i in range(40):
        stories[f"sib-{i:02d}"] = {"summary": f"S{i:02d}", "status": "done"}
    ctx = review_mod.build_review_story_context("story-a", _manifest(stories))
    section = _sibling_section(ctx)
    lines = [ln for ln in section.splitlines() if ln.startswith("- ")]
    assert len(lines) == 40
    assert "(+" not in section


def test_one_over_max_siblings_reports_one_more():
    stories = {"story-a": {"agent_instructions": "brief"}}
    for i in range(41):
        stories[f"sib-{i:02d}"] = {"summary": f"S{i:02d}", "status": "done"}
    ctx = review_mod.build_review_story_context("story-a", _manifest(stories))
    section = _sibling_section(ctx)
    lines = [ln for ln in section.splitlines() if ln.startswith("- ")]
    more = [ln for ln in lines if ln.startswith("- (+")]
    assert more == ["- (+1 more)"]
    assert len(lines) - len(more) == 40


# ---------------------------------------------------------------------------
# build_review_story_context - truncation
# ---------------------------------------------------------------------------


def test_long_brief_is_truncated():
    brief = "X" * (review_mod.REVIEW_BRIEF_MAX_CHARS + 1000)
    manifest = _manifest({"story-a": {"agent_instructions": brief}})
    ctx = review_mod.build_review_story_context("story-a", manifest)
    assert "X" * review_mod.REVIEW_BRIEF_MAX_CHARS + "\n" + TRUNCATION_MARKER in ctx
    assert "X" * (review_mod.REVIEW_BRIEF_MAX_CHARS + 1) not in ctx


def test_short_brief_is_not_truncated():
    brief = "short brief"
    manifest = _manifest({"story-a": {"agent_instructions": brief}})
    ctx = review_mod.build_review_story_context("story-a", manifest)
    assert TRUNCATION_MARKER not in ctx
    assert brief in ctx


def test_brief_exactly_at_max_is_not_truncated():
    brief = "Y" * review_mod.REVIEW_BRIEF_MAX_CHARS
    manifest = _manifest({"story-a": {"agent_instructions": brief}})
    ctx = review_mod.build_review_story_context("story-a", manifest)
    assert TRUNCATION_MARKER not in ctx
    assert brief in ctx


# ---------------------------------------------------------------------------
# build_review_story_context - negative / boundary
# ---------------------------------------------------------------------------


def test_missing_story_returns_none():
    assert review_mod.build_review_story_context("story-a", _manifest({})) is None
    assert (
        review_mod.build_review_story_context(
            "story-a", _manifest({"other": {"agent_instructions": "x"}})
        )
        is None
    )


@pytest.mark.parametrize("instructions", [None, "", "   ", "\n\t "])
def test_blank_agent_instructions_return_none(instructions):
    story = {"summary": "A", "status": "pending"}
    if instructions is not None:
        story["agent_instructions"] = instructions
    manifest = _manifest({"story-a": story})
    assert review_mod.build_review_story_context("story-a", manifest) is None


def test_no_other_stories_omits_sibling_section():
    manifest = _manifest({"story-a": {"agent_instructions": "brief"}})
    ctx = review_mod.build_review_story_context("story-a", manifest)
    assert BRIEF_HEADER in ctx
    assert SIBLING_HEADER not in ctx


def test_no_deferred_lines_omits_deferred_section_but_keeps_scope_rule():
    manifest = _manifest({"story-a": {"agent_instructions": "brief, no deferrals"}})
    ctx = review_mod.build_review_story_context("story-a", manifest)
    assert DEFERRED_HEADER not in ctx
    # The scope rule is appended whenever the function returns non-None, not
    # only when a Deferred section is present.
    assert review_mod.REVIEW_CONTEXT_SCOPE_RULE in ctx


def test_scope_rule_present_with_no_siblings_and_no_deferrals():
    manifest = _manifest({"story-a": {"agent_instructions": "brief"}})
    ctx = review_mod.build_review_story_context("story-a", manifest)
    assert review_mod.REVIEW_CONTEXT_SCOPE_RULE in ctx


# ---------------------------------------------------------------------------
# _run_reviewer wiring
# ---------------------------------------------------------------------------


def test_story_context_is_injected_into_prompt(monkeypatch):
    ctx = "UNIQUE-STORY-CONTEXT-MARKER: do the thing"
    prompt = _capture_review_prompt(monkeypatch, story_context=ctx)
    assert ctx in prompt


def test_story_context_follows_prior_findings(monkeypatch):
    ctx = "UNIQUE-STORY-CONTEXT-MARKER"
    prompt = _capture_review_prompt(
        monkeypatch, prior_feedback="Fix the widget.", story_context=ctx
    )
    assert PRIOR_FINDINGS_MARKER in prompt
    assert prompt.index(ctx) > prompt.index(PRIOR_FINDINGS_MARKER)


def test_story_context_scope_rule_reaches_prompt(monkeypatch):
    manifest = _manifest({"story-a": {"agent_instructions": "brief"}})
    ctx = review_mod.build_review_story_context("story-a", manifest)
    prompt = _capture_review_prompt(monkeypatch, story_context=ctx)
    assert review_mod.REVIEW_CONTEXT_SCOPE_RULE in prompt


@pytest.mark.parametrize("story_context", [None, "", "   ", "\n\t"])
def test_blank_story_context_omits_scope_rule(monkeypatch, story_context):
    prompt = _capture_review_prompt(monkeypatch, story_context=story_context)
    assert review_mod.REVIEW_CONTEXT_SCOPE_RULE not in prompt


def test_story_context_is_last_keyword_parameter():
    sig = inspect.signature(review_mod._run_reviewer)
    params = list(sig.parameters.values())
    last = params[-1]
    assert last.name == "story_context"
    assert last.default is None
    assert last.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    )


def test_story_context_is_documented_in_docstring():
    doc = review_mod._run_reviewer.__doc__ or ""
    assert "story_context" in doc


def test_story_context_note_is_built_after_prior_findings_note():
    lines = _review_source_lines()
    idx = next(
        i for i, ln in enumerate(lines) if ln.strip() == 'prior_findings_note = ""'
    )
    assert "story_context_note" in lines[idx + 1]


def test_story_context_note_is_inserted_after_prior_findings_note_in_prompt():
    lines = _review_source_lines()
    idx = next(i for i, ln in enumerate(lines) if 'f"{prior_findings_note}"' in ln)
    assert 'f"{story_context_note}"' in lines[idx + 1]
