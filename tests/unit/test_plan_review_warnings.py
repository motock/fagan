"""Tests for pipeline.plan_review.review_warnings.

``review_warnings(manifest)`` reports, per story, the advisory warnings
ingest would raise today (story sizing, preflight gap, unenforced security
review, isolation-only acceptance) so an operator can preview them before
dispatch. Boundary inputs degrade to ``{}`` and one story's failing helper
must never abort the others.
"""

import logging

from pipeline import build_detect, ingest, plan_review


def _clean_story() -> dict:
    """A story that no ingest warning helper flags."""
    return {
        "summary": "Add a small CLI flag",
        "risk": "low",
        "backend": "claude",
        "agent_instructions": (
            "Add a --verbose flag to the CLI parser.\n"
            "Preflight: .venv/bin/python -m pytest -q tests/unit/test_cli.py\n"
        ),
    }


def _story_without_preflight() -> dict:
    """A non-Claude-dispatch story whose brief has no Preflight: line."""
    return {
        "summary": "Add a small CLI flag",
        "risk": "low",
        "agent_instructions": "Add a --verbose flag to the CLI parser.\n",
    }


def _isolation_story() -> dict:
    """Brief requires call-site wiring; fixture grades only the unit."""
    source = (
        "from nudge import _no_tool_nudge\n"
        "def test_generic():\n"
        "    assert _no_tool_nudge(0) == 'Call a tool now.'\n"
    )
    return {
        "summary": "wire the nudge at the call site",
        "risk": "low",
        "agent_instructions": (
            "Wire _no_tool_nudge at the call site (local_agent.py:1550) so the "
            "nudge can be targeted. Update the call site to pass the last "
            "user message.\n"
            "Preflight: .venv/bin/python -m pytest -q tests/test_nudge.py\n"
        ),
        "acceptance": [{"path": "tests/test_nudge.py", "source": source}],
    }


def test_security_review_warning_included(monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    brief = "security" + " " + "review"
    assert ingest._SECURITY_REVIEW_RE.search(brief)
    story = {
        "summary": brief,
        "risk": "medium",
        "agent_instructions": f"{brief} the new upload path.\nPreflight: run tests\n",
    }
    expected = ingest._security_review_unenforced_warning(story)
    assert expected is not None

    result = plan_review.review_warnings({"stories": {"A": story}})

    assert expected in result["A"]


def test_preflight_missing_for_non_claude_dispatch(monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "codex")

    result = plan_review.review_warnings({"stories": {"A": _story_without_preflight()}})

    assert "preflight: missing" in result["A"]


def test_preflight_warning_suppressed_for_claude_dispatch(monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")

    result = plan_review.review_warnings({"stories": {"A": _story_without_preflight()}})

    assert not [w for w in result["A"] if w.startswith("preflight:")]


def test_clean_story_has_no_warnings(monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")

    result = plan_review.review_warnings({"stories": {"A": _clean_story()}})

    assert result == {"A": []}


def test_story_sizing_warning_included(monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    story = {
        "summary": "Touch three production files",
        "risk": "low",
        "backend": "local",
        "files": ["pipeline/a.py", "pipeline/b.py", "pipeline/c.py"],
        "agent_instructions": "Touch pipeline/a.py and its friends.\nPreflight: run tests\n",
    }
    expected = ingest._story_sizing_warning(
        ingest._sizing_view(story, None), ""
    )
    assert expected is not None

    result = plan_review.review_warnings({"stories": {"A": story}})

    assert expected in result["A"]


def test_isolation_only_acceptance_warning_included(monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    story = _isolation_story()
    expected = build_detect._isolation_only_acceptance_warning(story)
    assert expected is not None

    result = plan_review.review_warnings({"stories": {"A": story}})

    assert expected in result["A"]


def test_result_keys_follow_story_insertion_order(monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    stories = {"zeta": _clean_story(), "alpha": _clean_story()}

    result = plan_review.review_warnings({"stories": stories})

    assert list(result) == ["zeta", "alpha"]
    assert result == {"zeta": [], "alpha": []}


def test_boundary_inputs_return_empty_dict():
    assert plan_review.review_warnings(None) == {}
    assert plan_review.review_warnings([]) == {}
    assert plan_review.review_warnings("not a manifest") == {}
    assert plan_review.review_warnings({}) == {}
    assert plan_review.review_warnings({"stories": None}) == {}
    assert plan_review.review_warnings({"stories": []}) == {}


def test_non_dict_story_entry_is_skipped(monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")

    result = plan_review.review_warnings(
        {"stories": {"A": None, "B": _clean_story(), "C": "nope"}}
    )

    assert result == {"B": []}


def test_failing_helper_records_placeholder_and_does_not_abort_others(
    monkeypatch, caplog
):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")

    def _boom(story, repo_root):
        if story.get("summary") == "boom story":
            raise RuntimeError("boom /secret/path")

    monkeypatch.setattr(ingest, "_story_sizing_warning", _boom)
    bad = dict(_clean_story(), summary="boom story")
    good = _clean_story()

    caplog.set_level(logging.WARNING, logger="pipeline.plan_review")
    result = plan_review.review_warnings({"stories": {"A": bad, "B": good}})

    assert result == {"A": ["warning check failed"], "B": []}
    assert "RuntimeError" in caplog.text
    assert "boom" not in caplog.text
    assert "/secret/path" not in caplog.text

    # No cached failure state: a later call with only the good story is clean.
    assert plan_review.review_warnings({"stories": {"B": _clean_story()}}) == {"B": []}
