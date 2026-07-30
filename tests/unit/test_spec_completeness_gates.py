"""Mode 47 (green-but-incomplete merge) gates.

Live failure 2026-07-30, TRANSPORT-ALIAS-READERS PR #200: the agent made the
MINIMAL edit that flipped the one failing test green (migrated
PIPELINE_TRANSPORT_NUM_CTX) and left the rest of the brief undone (a now-dead
LOCAL_AGENT_MAX_STEPS key, a stale docstring). No test asserted on those, so
every suite-gate - the acceptance oracle, the full-suite done-bar, and the CI
full-suite gate added in PR #199 - was blind to it: they only ever answer
pass/fail, never "is the spec complete". The merge landed green and incomplete.

Two prompt-level gates close that:

1. Test author: every mechanically-checkable requirement in the brief must
   become an assertion, so incompleteness shows up as a RED test rather than
   an invisible gap. An accidental A/B in the same session proved this is the
   real lever - the first run's test author DID write those assertions and the
   identical defect was caught; the second run's didn't and it merged.

2. Reviewer: on a re-review, each prior Blocking finding must be explicitly
   confirmed resolved (quote the hunk) or re-raised. The existing Mode 24/28
   finding-target guard can't catch this - it asks "was the file touched",
   and the file WAS touched (partially). Only per-finding verification catches
   a partial fix.
"""
import pytest

import pipeline.persona as pper
from app import pipeline_mcp_server as p
from pipeline import planner


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    """Local copy of test_pipeline_mcp_server.py's fixture (it is defined in
    that module, not conftest, so it isn't visible here). Only the
    code-reviewer persona is needed for these tests."""
    d = tmp_path / "agents"
    d.mkdir()
    (d / "code-reviewer.md").write_text(
        '---\nname: "code-reviewer"\nmodel: sonnet\n---\n\nReviewer body.\n'
    )
    monkeypatch.setattr(p, "AGENTS_DIR", d)
    monkeypatch.setattr(pper, "AGENTS_DIR", d)
    return d


# --- Gate 1: the test author must grade the whole brief ---

def test_test_author_prompt_requires_assertion_per_checkable_requirement():
    """The initial test-authoring prompt must tell the author to turn every
    mechanically-checkable requirement into an assertion, not just the
    headline behavior."""
    prompt = planner._test_author_prompt("Migrate the FOO_BAR env var.")
    lowered = prompt.lower()
    assert "every" in lowered
    assert "assertion" in lowered or "assert" in lowered
    # It must name the failure mode it exists to prevent: a requirement that
    # no test grades gets skipped by the implementer.
    assert "grade" in lowered or "graded" in lowered


def test_test_author_prompt_names_the_minimal_edit_failure_mode():
    """The prompt must warn that an ungraded requirement will be skipped -
    without the 'why', a weak author drops the mechanical assertions."""
    prompt = planner._test_author_prompt("Migrate the FOO_BAR env var.")
    lowered = prompt.lower()
    assert "skip" in lowered or "incomplete" in lowered


def test_test_author_prompt_calls_out_mechanical_requirements():
    """Renames/removals/doc updates are exactly the class that went ungraded
    live; the prompt must name that class explicitly."""
    prompt = planner._test_author_prompt("Migrate the FOO_BAR env var.")
    lowered = prompt.lower()
    assert "rename" in lowered or "docstring" in lowered or "removal" in lowered


def test_test_author_prompt_preserves_existing_contract():
    """The new guidance must not displace the existing test-authoring rules."""
    prompt = planner._test_author_prompt("Migrate the FOO_BAR env var.")
    # Original brief is still led with, and the core contract survives.
    assert prompt.startswith("Migrate the FOO_BAR env var.")
    assert "do " in prompt.lower()
    assert "implementation" in prompt.lower()
    assert "red" in prompt.lower()
    assert "commit" in prompt.lower()


# --- Gate 2: the reviewer must verify each prior finding ---

def _capture_prompt(monkeypatch):
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, **kwargs):
            captured["prompt"] = prompt
            return "VERDICT: APPROVE"

    monkeypatch.setattr(
        p.backend, "get_backend", lambda role, name=None: _FakeDriver()
    )
    return captured


def test_reviewer_prompt_requires_per_finding_verification_when_prior_findings(
    agents_dir, monkeypatch
):
    """A re-review that carries prior feedback must instruct the reviewer to
    resolve each prior finding one by one."""
    captured = _capture_prompt(monkeypatch)
    p._run_reviewer(
        "/tmp/some-worktree",
        "agent/some-branch",
        prior_feedback="- Blocking: foo.py: migrate BOTH keys, not just one",
    )
    prompt = captured["prompt"]
    lowered = prompt.lower()
    # The prior findings themselves must reach the reviewer.
    assert "migrate BOTH keys" in prompt
    # And it must be told to verify each one individually.
    assert "each" in lowered
    assert "quoting" in lowered or "quote" in lowered or "cite" in lowered
    assert "partial" in lowered or "partially" in lowered


def test_reviewer_prompt_forbids_approve_on_partially_addressed_finding(
    agents_dir, monkeypatch
):
    """The whole point: a partial fix must not be APPROVEd."""
    captured = _capture_prompt(monkeypatch)
    p._run_reviewer(
        "/tmp/some-worktree",
        "agent/some-branch",
        prior_feedback="- Blocking: foo.py: migrate BOTH keys",
    )
    lowered = captured["prompt"].lower()
    assert "request_changes" in lowered
    # Must explicitly cover the "tests are green but the finding isn't fully
    # resolved" case, which is exactly what slipped through live.
    assert "green" in lowered or "passing" in lowered


def test_reviewer_prompt_omits_prior_finding_block_on_first_review(
    agents_dir, monkeypatch
):
    """A first review has no prior findings; the block must not appear (and
    must not invent findings to check)."""
    captured = _capture_prompt(monkeypatch)
    p._run_reviewer("/tmp/some-worktree", "agent/some-branch")
    lowered = captured["prompt"].lower()
    assert "prior blocking finding" not in lowered


def test_reviewer_prompt_omits_prior_finding_block_when_feedback_empty(
    agents_dir, monkeypatch
):
    """Empty-string feedback is the same as none - no block, no crash."""
    captured = _capture_prompt(monkeypatch)
    p._run_reviewer("/tmp/some-worktree", "agent/some-branch", prior_feedback="")
    lowered = captured["prompt"].lower()
    assert "prior blocking finding" not in lowered


def test_reviewer_prompt_preserves_existing_checks_with_prior_findings(
    agents_dir, monkeypatch
):
    """The new block must be additive - the existing numbered criteria and the
    every-finding-in-one-pass instruction must survive."""
    captured = _capture_prompt(monkeypatch)
    p._run_reviewer(
        "/tmp/some-worktree",
        "agent/some-branch",
        prior_feedback="- Blocking: foo.py: something",
    )
    prompt = captured["prompt"]
    for num in ["(1)", "(2)", "(3)", "(4)"]:
        assert num in prompt
    assert "EVERY Blocking finding" in prompt
    assert "VERDICT" in prompt


def test_run_reviewer_still_callable_without_prior_feedback(agents_dir, monkeypatch):
    """Back-compat: prior_feedback is optional; existing call sites and mocks
    that never pass it keep working."""
    captured = _capture_prompt(monkeypatch)
    out = p._run_reviewer("/tmp/some-worktree", "agent/some-branch")
    assert out == "VERDICT: APPROVE"
    assert "prompt" in captured


# --- Gate 2 wiring: review_story must actually pass the prior feedback ---

def test_review_story_passes_prior_feedback_to_reviewer(monkeypatch, tmp_path):
    """The prompt gate is useless if the call site never forwards the stored
    feedback. Assert the real wiring, not just the prompt builder (the
    isolation-only-fixture trap)."""
    import inspect

    # review_story is wrapped by a re-entrancy guard (_original_review_story),
    # so getsource on the public name returns the wrapper, not the real body.
    src = inspect.getsource(p._original_review_story)
    assert "prior_feedback" in src, (
        "review_story must forward the story's stored review_feedback to "
        "_run_reviewer as prior_feedback"
    )
    # Every _run_reviewer call site in review_story must forward it, not just
    # the first - the rate-limit-fallback and transient-retry paths are real
    # review cycles too.
    assert src.count("**prior_kw") == src.count("_run_reviewer("), (
        "every _run_reviewer call in review_story must forward prior_kw"
    )
    # It must be conditional on there actually being prior feedback, so a
    # first review's call signature is unchanged.
    assert 'if _prior_fb else {}' in src, (
        "prior_feedback must only be passed when prior feedback exists"
    )
