"""Tests for the decompose-JSON one-retry story.

``PipelineService.decompose_plan`` must retry the decompose backend call
EXACTLY once (2 total attempts) when the backend returned text but that text
fails to parse as JSON (via ``_extract_json_block`` + ``json.loads``) -- before
giving up and surfacing the ``invalid JSON: ...`` failure.

Out of scope (must NOT be retried):
- the backend produced no text at all (``text`` is None/empty) -- that is the
  distinct "decompose backend returned no output" failure class, and
- valid JSON whose shape is wrong (missing the ``epics`` list) -- that is the
  distinct "response JSON is missing an 'epics' list" failure class.

Total attempts are capped at exactly 2 (1 retry): this is a live LLM call
(~350s observed in production), so the retry must be tightly bounded.

These tests are self-contained and runnable against code that does not yet
implement the retry: before the change, the stateful-stub tests fail because
the stub is only invoked once (no retry exists), not because of a bug in the
test logic.

Seam note (confirmed against test_decompose_error_detail.py and
test_decompose_plan_migration.py): the effective patch target is the
``_run_decompose_detailed`` binding on the ``pipeline.server`` module
(``p`` below) -- the method resolves it as a free variable through
``pipeline.server``'s module globals, so patching ``pipeline.service`` would
silently no-op.
"""

import json

import pytest

from pipeline import persona as pper
from pipeline import server as p


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    """Local copy of the standard agents-dir fixture so this file is
    self-contained (does not depend on fixtures defined elsewhere)."""
    d = tmp_path / "agents"
    d.mkdir()
    (d / "overlord.md").write_text(
        '---\nname: "overlord"\nmodel: opus\nmemory: user\n---\n\n'
        "You are the Overlord body text.\n"
    )
    (d / "software-engineer.md").write_text(
        '---\nname: "software-engineer"\nmodel: sonnet\n---\n\nEngineer body.\n'
    )
    (d / "code-reviewer.md").write_text(
        '---\nname: "code-reviewer"\nmodel: sonnet\n---\n\nReviewer body.\n'
    )
    (d / "product-analyst.md").write_text(
        '---\nname: "product-analyst"\nmodel: opus\n---\n\nAnalyst body.\n'
    )
    monkeypatch.setattr(p, "AGENTS_DIR", d)
    monkeypatch.setattr(pper, "AGENTS_DIR", d)
    return d


class _ScriptedBackend:
    """Stateful stub for _run_decompose_detailed: records every call and
    returns scripted (text, error) tuples in order, holding the last one."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, request, **kwargs):
        self.calls.append(request)
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[idx]


def _patch_backend(monkeypatch, stub):
    monkeypatch.setattr(p, "_run_decompose_detailed", stub)


VALID_PLAN = {"epics": [{"summary": "E1", "stories": []}]}
VALID_PLAN_JSON = json.dumps(VALID_PLAN)

# Exact reproduction of the live bug: an empty fenced json block -- the
# backend returned text, but nothing inside the fence parses as JSON.
EMPTY_FENCE = "```json\n```"


def test_decompose_plan_retries_once_after_a_transient_malformed_json_response(
    agents_dir, monkeypatch
):
    """A malformed/empty JSON fence on attempt 1 must be retried exactly once;
    a valid plan on attempt 2 succeeds transparently (ok=True, the plan)."""
    stub = _ScriptedBackend(
        [
            (EMPTY_FENCE, None),
            (f"```json\n{VALID_PLAN_JSON}\n```", None),
        ]
    )
    _patch_backend(monkeypatch, stub)

    result = p._service.decompose_plan("Build a CLI todo app.")

    assert result == {"ok": True, "plan": VALID_PLAN}, (
        f"retry after transient malformed JSON must succeed with the plan, "
        f"got {result!r}"
    )
    assert len(stub.calls) == 2, (
        f"backend must be invoked exactly 2 times (1 first attempt + 1 retry), "
        f"got {len(stub.calls)}"
    )


def test_decompose_plan_gives_up_after_max_attempts_and_preserves_last_raw_text(
    agents_dir, monkeypatch
):
    """A backend that always returns unparseable text must be retried exactly
    once (2 total attempts, bounded -- not unbounded) and then surface the
    'invalid JSON' failure preserving the LAST raw text."""
    stub = _ScriptedBackend([(EMPTY_FENCE, None)])
    _patch_backend(monkeypatch, stub)

    result = p._service.decompose_plan("Build a CLI todo app.")

    assert result["ok"] is False, f"must fail after exhausting retries: {result!r}"
    assert "invalid JSON" in result["error"], (
        f"error must surface the invalid-JSON failure, got {result.get('error')!r}"
    )
    assert result["raw"] == EMPTY_FENCE, (
        f"raw must preserve the last backend text, got {result.get('raw')!r}"
    )
    assert len(stub.calls) == 2, (
        f"attempts must be capped at exactly 2 (1 retry, not unbounded), "
        f"got {len(stub.calls)}"
    )


def test_decompose_plan_does_not_retry_when_backend_returns_no_text_at_all(
    agents_dir, monkeypatch
):
    """NEGATIVE: text=None is the distinct backend/auth/network failure class
    ('decompose backend returned no output') -- it must NOT be retried."""
    stub = _ScriptedBackend([(None, "some backend error")])
    _patch_backend(monkeypatch, stub)

    result = p._service.decompose_plan("Build a CLI todo app.")

    assert result["ok"] is False, f"must fail without retrying: {result!r}"
    assert "decompose backend returned no output" in result["error"], (
        f"error must use the no-output failure class, got {result.get('error')!r}"
    )
    assert len(stub.calls) == 1, (
        f"no-text failure must not be retried (exactly 1 call), "
        f"got {len(stub.calls)}"
    )


def test_decompose_plan_succeeds_on_first_attempt_without_retrying(
    agents_dir, monkeypatch
):
    """A valid plan on the very first attempt must not trigger any retry."""
    stub = _ScriptedBackend([(f"```json\n{VALID_PLAN_JSON}\n```", None)])
    _patch_backend(monkeypatch, stub)

    result = p._service.decompose_plan("Build a CLI todo app.")

    assert result["ok"] is True, f"valid plan must succeed: {result!r}"
    assert result["plan"] == VALID_PLAN
    assert len(stub.calls) == 1, (
        f"valid first response must not be retried (exactly 1 call), "
        f"got {len(stub.calls)}"
    )


def test_decompose_plan_retry_constant_is_exactly_two():
    """The retry budget is a named class attribute capped at exactly 2 total
    attempts (1 retry) -- a live LLM call must be tightly bounded."""
    attempts = getattr(p.PipelineService, "_DECOMPOSE_JSON_RETRY_ATTEMPTS", None)
    assert attempts == 2, (
        f"_DECOMPOSE_JSON_RETRY_ATTEMPTS must be exactly 2 (1 retry), got {attempts!r}"
    )


def test_decompose_plan_retry_loop_does_not_wrap_the_first_backend_call():
    """The retry must be a loop AFTER the first unconditional backend call,
    not a loop wrapping it: the method's literal first statement must remain
    ``text, backend_error = _run_decompose_detailed(request)`` (constrained by
    the pre-existing AST test in test_decompose_plan_migration.py)."""
    import ast
    import inspect
    import textwrap

    method = getattr(p.PipelineService, "decompose_plan", None)
    if method is None:
        pytest.fail("PipelineService.decompose_plan does not exist yet")
    source = inspect.getsource(method)
    mod = ast.parse(textwrap.dedent(source))
    func = mod.body[0]
    assert isinstance(func, ast.FunctionDef)
    first = func.body[0]
    assert isinstance(first, ast.Assign), (
        f"first statement must be an assignment, got {type(first).__name__}"
    )
    assert isinstance(first.value, ast.Call)
    assert isinstance(first.value.func, ast.Name)
    assert first.value.func.id == "_run_decompose_detailed", (
        "first statement must call bare _run_decompose_detailed"
    )
    targets = (
        list(first.targets[0].elts)
        if isinstance(first.targets[0], ast.Tuple)
        else list(first.targets)
    )
    names = [t.id for t in targets]
    assert names == ["text", "backend_error"], (
        f"first statement must unpack (text, backend_error), got {names}"
    )
    # And the retry loop must exist after it (a while statement somewhere in
    # the body, never before/around the first call).
    loops = [
        s
        for s in func.body[1:]
        if isinstance(s, (ast.While, ast.For))
    ]
    assert loops, "retry loop must exist after the first backend call"