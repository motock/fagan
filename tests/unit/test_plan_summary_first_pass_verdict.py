"""First-pass verdict suffix on per-story lines of ``format_plan_summary``.

The plan summary must annotate each story that belongs to the local-dispatch
population with the local first-pass verdict produced by
``pipeline.local_success.classify_story``:

* `` [first-pass clean]`` when the story is in the population and clean;
* `` [not first-pass clean: <reason>, ...]`` otherwise, where the reasons are
  the fixed reason identifiers (never free-form message text).

Stories outside the population (Claude-dispatched) get no suffix at all, and
the returned summary must never embed a record's ``message`` text.

These tests are self-contained: they build synthetic manifests/records only.
"""

from __future__ import annotations

import copy
import inspect
from pathlib import Path

import pytest

from pipeline.plan_summary import _story_line, format_plan_summary

REPO_ROOT = Path(__file__).resolve().parents[2]
PLAN_SUMMARY_PATH = REPO_ROOT / "pipeline" / "plan_summary.py"

STEP_CAP_HEADER = "=== PRIOR-ATTEMPT DIAGNOSIS (read this FIRST) ==="
SENTINEL = "SENTINEL_RAW_CI_STDERR_9f3a2b"

LOCAL_STORY = {
    "backend": "ollama",
    "dispatched_model": "gpt-oss-20b-high:latest",
    "status": "done",
}


def _manifest(stories: dict) -> dict:
    return {"plan_name": "demo-plan", "stories": stories}


def _story_lines(summary: str, key: str) -> list[str]:
    """Return the rendered per-story lines for ``key``."""
    prefix = f"- {key}"
    return [
        line
        for line in summary.splitlines()
        if line == prefix or line.startswith((prefix + ":", prefix + " "))
    ]


def _story_line_for(summary: str, key: str) -> str:
    lines = _story_lines(summary, key)
    assert len(lines) == 1, f"expected exactly one line for {key!r}, got {lines!r}"
    return lines[0]


# --------------------------------------------------------------------------
# Behaviour: the verdict suffix
# --------------------------------------------------------------------------


def test_local_story_with_no_records_is_first_pass_clean() -> None:
    summary = format_plan_summary("demo-plan", _manifest({"S1": dict(LOCAL_STORY)}), [])
    line = _story_line_for(summary, "S1")
    assert line.endswith(" [first-pass clean]")


def test_local_story_with_summary_keeps_summary_before_verdict() -> None:
    story = dict(LOCAL_STORY, summary="do the thing")
    summary = format_plan_summary("demo-plan", _manifest({"S1": story}), [])
    line = _story_line_for(summary, "S1")
    assert line == "- S1: do the thing [first-pass clean]"


def test_escalated_story_is_not_first_pass_clean() -> None:
    story = dict(LOCAL_STORY, escalated=True)
    summary = format_plan_summary("demo-plan", _manifest({"S1": story}), [])
    line = _story_line_for(summary, "S1")
    assert line.endswith(" [not first-pass clean: escalated]")


def test_step_cap_rebrief_header_yields_step_cap_rebrief_reason() -> None:
    story = dict(
        LOCAL_STORY,
        agent_instructions=f"do the work\n\n{STEP_CAP_HEADER}\nprior attempt failed\n",
    )
    summary = format_plan_summary("demo-plan", _manifest({"S1": story}), [])
    line = _story_line_for(summary, "S1")
    assert line.endswith("]")
    assert "not first-pass clean" in line
    assert "step_cap_rebrief" in line


def test_not_done_story_reports_not_done_reason() -> None:
    story = dict(LOCAL_STORY, status="in_progress")
    summary = format_plan_summary("demo-plan", _manifest({"S1": story}), [])
    line = _story_line_for(summary, "S1")
    assert line.endswith(" [not first-pass clean: not_done]")


def test_claude_story_gets_no_verdict_suffix() -> None:
    story = {"backend": "claude", "status": "done", "summary": "claude work"}
    summary = format_plan_summary("demo-plan", _manifest({"S1": story}), [])
    line = _story_line_for(summary, "S1")
    assert line == "- S1: claude work"
    assert "[" not in line


def test_claude_story_gets_no_suffix_even_with_benign_records() -> None:
    story = {"backend": "claude", "status": "done"}
    records = [{"story_key": "S1", "message": "claude finished"}]
    summary = format_plan_summary("demo-plan", _manifest({"S1": story}), records)
    line = _story_line_for(summary, "S1")
    assert "[" not in line


def test_pr_url_is_rendered_before_the_verdict() -> None:
    story = dict(LOCAL_STORY, pr_url="https://example.invalid/pr/7")
    summary = format_plan_summary("demo-plan", _manifest({"S1": story}), [])
    line = _story_line_for(summary, "S1")
    assert " (pr: https://example.invalid/pr/7)" in line
    assert line.index("(pr: ") < line.index("[first-pass clean]")
    assert line.endswith(" [first-pass clean]")


def test_pr_url_and_summary_and_verdict_order() -> None:
    story = dict(
        LOCAL_STORY,
        summary="do the thing",
        pr_url="https://example.invalid/pr/7",
    )
    summary = format_plan_summary("demo-plan", _manifest({"S1": story}), [])
    line = _story_line_for(summary, "S1")
    assert line == "- S1: do the thing (pr: https://example.invalid/pr/7) [first-pass clean]"


def test_verdict_is_per_story() -> None:
    stories = {
        "S1": dict(LOCAL_STORY),
        "S2": dict(LOCAL_STORY, escalated=True),
        "S3": {"backend": "claude", "status": "done"},
    }
    summary = format_plan_summary("demo-plan", _manifest(stories), [])
    assert _story_line_for(summary, "S1").endswith(" [first-pass clean]")
    assert _story_line_for(summary, "S2").endswith(" [not first-pass clean: escalated]")
    assert "[" not in _story_line_for(summary, "S3")


# --------------------------------------------------------------------------
# Data minimisation: no record message text, no paths
# --------------------------------------------------------------------------


def test_summary_never_contains_record_message_text() -> None:
    records = [
        {"story_key": "S1", "message": f"gate failed: {SENTINEL}"},
        {"story_key": "S2", "message": f"boom {SENTINEL}"},
    ]
    stories = {"S1": dict(LOCAL_STORY), "S2": dict(LOCAL_STORY)}
    summary = format_plan_summary("demo-plan", _manifest(stories), records)
    assert SENTINEL not in summary
    assert "gate failed" not in summary


def test_summary_never_contains_repo_root_path() -> None:
    manifest = _manifest({"S1": dict(LOCAL_STORY)})
    manifest["repo_root"] = "/abs/path/to/worktree/secret"
    summary = format_plan_summary("demo-plan", manifest, [])
    assert "/abs/path/to/worktree/secret" not in summary


def test_reasons_are_identifiers_not_message_text() -> None:
    # A record whose message carries a legacy keyword contributes the fixed
    # reason identifier, never the message text itself.
    records = [{"story_key": "S1", "message": "story escalated to the cloud tier"}]
    summary = format_plan_summary("demo-plan", _manifest({"S1": dict(LOCAL_STORY)}), records)
    line = _story_line_for(summary, "S1")
    assert line.endswith(" [not first-pass clean: legacy_message]")
    assert "to the cloud tier" not in summary


# --------------------------------------------------------------------------
# Boundaries / malformed input
# --------------------------------------------------------------------------


def test_empty_stories_renders_no_per_story_section() -> None:
    summary = format_plan_summary("demo-plan", _manifest({}), [])
    assert "Per story:" not in summary
    assert "Plan: demo-plan" in summary


def test_missing_stories_key_is_tolerated() -> None:
    summary = format_plan_summary("demo-plan", {}, [])
    assert "Plan: demo-plan" in summary
    assert "Per story:" not in summary


def test_non_dict_story_gets_no_verdict_suffix() -> None:
    summary = format_plan_summary("demo-plan", _manifest({"S1": "not-a-dict"}), [])
    line = _story_line_for(summary, "S1")
    assert line == "- S1"
    assert "[" not in line


def test_story_without_backend_or_model_gets_no_verdict_suffix() -> None:
    summary = format_plan_summary("demo-plan", _manifest({"S1": {"status": "done"}}), [])
    line = _story_line_for(summary, "S1")
    assert "[" not in line


def test_empty_records_list_is_tolerated() -> None:
    summary = format_plan_summary("demo-plan", _manifest({"S1": dict(LOCAL_STORY)}), [])
    assert _story_line_for(summary, "S1").endswith(" [first-pass clean]")


def test_manifest_is_not_mutated() -> None:
    manifest = _manifest({"S1": dict(LOCAL_STORY)})
    before = copy.deepcopy(manifest)
    format_plan_summary("demo-plan", manifest, [])
    assert manifest == before


def test_records_are_not_mutated() -> None:
    records = [{"story_key": "S1", "message": "ok"}]
    before = copy.deepcopy(records)
    format_plan_summary("demo-plan", _manifest({"S1": dict(LOCAL_STORY)}), records)
    assert records == before


# --------------------------------------------------------------------------
# _story_line signature and direct behaviour
# --------------------------------------------------------------------------


def test_story_line_signature_takes_records() -> None:
    params = list(inspect.signature(_story_line).parameters)
    assert params == ["key", "story", "records"]


def test_story_line_direct_call_appends_verdict() -> None:
    line = _story_line("S1", dict(LOCAL_STORY), [])
    assert line.endswith(" [first-pass clean]")


def test_story_line_direct_call_escalated() -> None:
    line = _story_line("S1", dict(LOCAL_STORY, escalated=True), [])
    assert line.endswith(" [not first-pass clean: escalated]")


def test_story_line_direct_call_claude_has_no_suffix() -> None:
    line = _story_line("S1", {"backend": "claude", "status": "done"}, [])
    assert line == "- S1"


def test_story_line_docstring_first_line_updated() -> None:
    doc = inspect.getdoc(_story_line) or ""
    first = doc.splitlines()[0] if doc else ""
    assert first == (
        "Render one manifest story: key, summary, pr_url when present, "
        "and the local first-pass verdict for local-tier stories."
    )


# --------------------------------------------------------------------------
# Source-level requirements (import wiring, call site, dead name removal)
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def source() -> str:
    return PLAN_SUMMARY_PATH.read_text(encoding="utf-8")


def test_imports_classify_story(source: str) -> None:
    assert "from pipeline.local_success import classify_story" in source


def test_local_success_import_sorts_before_story_metrics(source: str) -> None:
    local_idx = source.index("from pipeline.local_success import classify_story")
    metrics_idx = source.index("from pipeline.story_metrics import")
    assert local_idx < metrics_idx


def test_format_plan_summary_passes_records_to_story_line(source: str) -> None:
    assert "_story_line(key, story, records)" in source


def test_old_two_arg_call_is_gone(source: str) -> None:
    assert "_story_line(key, story)" not in source


def test_old_docstring_first_line_is_gone(source: str) -> None:
    assert "Render one manifest story: key, summary, and pr_url when present." not in source


def test_story_line_uses_classify_story(source: str) -> None:
    assert "classify_story(key, story, records)" in source


def test_verdict_suffixes_are_literal_in_source(source: str) -> None:
    assert "[first-pass clean]" in source
    assert "[not first-pass clean: " in source


def test_no_record_message_text_is_embedded(source: str) -> None:
    # The verdict must be built from reason identifiers only.
    assert "verdict['reasons']" in source or 'verdict["reasons"]' in source
    assert "rec.get(\"message\")" not in source
    assert "record.get(\"message\")" not in source
