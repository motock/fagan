"""Acceptance (PLD90-W2-WIRE): review_story hands the story brief, sibling
status and Deferred lines to the reviewer through the REAL _run_reviewer.

The backend is mocked only at its transport boundary (backend.get_backend);
review_story and _run_reviewer run for real, so this fails if the call site in
pipeline/review_orchestrator.py does not pass story_context.
"""

import json

import pytest

import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import persistence as ppers

DEFERRED = (
    "Deferred to Document the dry-run flag in REFERENCE.md \u2014 not a finding: "
    "the doc row lands in the sibling story."
)


def _scope_rule():
    # Imported after pipeline.server (module top) to avoid import-order cycles.
    import pipeline.review as review_mod

    return review_mod.REVIEW_CONTEXT_SCOPE_RULE


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


def _write_manifest(plan_dir, stories):
    manifest = {"epics": {}, "stories": stories}
    (plan_dir / "ctx.manifest.json").write_text(json.dumps(manifest))


def _main_review_prompts(monkeypatch):
    prompts = []

    class _FakeDriver:
        def complete(self, prompt, **kwargs):
            prompts.append(prompt)
            return "The error path is untested; add coverage.\nVERDICT: REQUEST_CHANGES"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())
    monkeypatch.setattr(p, "_notify_user", lambda *args, **kwargs: None)
    monkeypatch.setattr(p, "_auto_escalation_enabled", lambda: False)
    return prompts


def _story(plan_dir, **extra):
    story = {
        "summary": "Add the dry-run flag",
        "status": "tests_passed",
        "worktree": str(plan_dir / "wt"),
        "risk": "low",
    }
    story.update(extra)
    return story


def _main(prompts):
    return [pr for pr in prompts if "Review the changes on branch" in pr]


def test_review_story_passes_brief_siblings_and_deferred_line_to_reviewer(plan_dir, monkeypatch):
    prompts = _main_review_prompts(monkeypatch)
    brief = "GOAL: add the --dry-run flag to the CLI.\n" + DEFERRED + "\n"
    _write_manifest(plan_dir, {
        "S1": _story(plan_dir, agent_instructions=brief),
        "S2": {"summary": "Document the dry-run flag in REFERENCE.md", "status": "todo"},
    })

    p.review_story("ctx", "S1")

    main = _main(prompts)
    assert main, f"the real reviewer was never invoked: {prompts!r}"
    prompt = main[0]
    assert "GOAL: add the --dry-run flag to the CLI." in prompt
    assert DEFERRED in prompt
    assert "- Document the dry-run flag in REFERENCE.md \u2014 todo" in prompt
    assert _scope_rule() in prompt


def test_story_without_a_brief_adds_no_context_block(plan_dir, monkeypatch):
    prompts = _main_review_prompts(monkeypatch)
    _write_manifest(plan_dir, {
        "S1": _story(plan_dir),
        "S2": {"summary": "Document the dry-run flag in REFERENCE.md", "status": "todo"},
    })

    p.review_story("ctx", "S1")

    main = _main(prompts)
    assert main, f"the real reviewer was never invoked: {prompts!r}"
    assert _scope_rule() not in main[0]
