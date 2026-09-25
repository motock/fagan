"""A reviewer bounce is visible in the plan summary next to the baseline verdict.

Two definitions of "first-pass clean" coexist: the baseline verdict on each story
line (no escalation, park, triage or brief patch) and ``first_pass_clean_rate`` in
the rollup, which also counts a reviewer change request as not clean. A story the
baseline passes but a reviewer sent back used to read ``[first-pass clean]`` right
above ``first_pass_clean_rate=0.0``. It is now annotated, and the rollup carries a
single note that names the difference.
"""

from pipeline.plan_summary import _story_line, format_plan_summary

LOCAL_STORY = {
    "backend": "ollama",
    "dispatched_model": "deepseek-v4.1-flash:cloud",
    "status": "done",
}
CLAUDE_STORY = {"backend": "claude", "status": "done"}

BOUNCE_SUFFIX = " [first-pass clean (baseline definition); reviewer requested changes]"
NOTE_PREFIX = "Note: first_pass_clean_rate also counts a reviewer change request"
SENTINEL = "SENTINEL_RAW_CI_STDERR_4c81d7"


def _bounced(key: str, correlation_id: str) -> list[dict]:
    return [
        {"story_key": key, "event": "review_changes_requested", "correlation_id": correlation_id},
        {"story_key": key, "event": "story_merged", "correlation_id": correlation_id},
    ]


def _merged(key: str, correlation_id: str) -> list[dict]:
    return [{"story_key": key, "event": "story_merged", "correlation_id": correlation_id}]


def _summary(stories: dict, records: list[dict]) -> str:
    return format_plan_summary("demo-plan", {"stories": stories}, records)


def _line(summary: str, key: str) -> str:
    matches = [ln for ln in summary.splitlines() if ln.startswith(f"- {key}")]
    assert len(matches) == 1, matches
    return matches[0]


def test_baseline_clean_story_that_a_reviewer_bounced_is_annotated():
    line = _story_line("S1", LOCAL_STORY, _bounced("S1", "c1"))
    assert line.endswith(BOUNCE_SUFFIX)


def test_annotated_story_still_reports_the_baseline_verdict_as_clean():
    line = _story_line("S1", LOCAL_STORY, _bounced("S1", "c1"))
    assert "[first-pass clean (baseline definition)" in line


def test_clean_story_without_a_bounce_keeps_the_plain_verdict():
    line = _story_line("S1", LOCAL_STORY, _merged("S1", "c1"))
    assert line == "- S1 [first-pass clean]"


def test_story_with_no_records_keeps_the_plain_verdict():
    assert _story_line("S1", LOCAL_STORY, []) == "- S1 [first-pass clean]"


def test_escalated_and_bounced_story_keeps_the_not_clean_verdict():
    records = _bounced("S1", "c1") + [{"story_key": "S1", "event": "escalated", "correlation_id": "c1"}]
    line = _story_line("S1", LOCAL_STORY, records)
    assert line.endswith(" [not first-pass clean: escalated]")


def test_claude_story_that_was_bounced_gets_no_verdict_suffix():
    assert _story_line("S1", CLAUDE_STORY, _bounced("S1", "c1")) == "- S1"


def test_bounce_on_one_story_does_not_annotate_another():
    records = _bounced("S1", "c1") + _merged("S2", "c2")
    summary = _summary({"S1": LOCAL_STORY, "S2": LOCAL_STORY}, records)
    assert _line(summary, "S2").endswith(" [first-pass clean]")


def test_rollup_note_appears_once_when_two_stories_were_bounced():
    records = _bounced("S1", "c1") + _bounced("S2", "c2")
    summary = _summary({"S1": LOCAL_STORY, "S2": LOCAL_STORY}, records)
    assert summary.count(NOTE_PREFIX) == 1


def test_rollup_note_is_absent_when_no_story_was_bounced():
    summary = _summary({"S1": LOCAL_STORY}, _merged("S1", "c1"))
    assert NOTE_PREFIX not in summary


def test_rollup_note_is_absent_when_a_bounced_story_is_not_baseline_clean():
    records = _bounced("S1", "c1") + [{"story_key": "S1", "event": "escalated", "correlation_id": "c1"}]
    assert NOTE_PREFIX not in _summary({"S1": LOCAL_STORY}, records)


def test_rollup_still_reports_first_pass_clean_rate_for_a_bounced_story():
    summary = _summary({"S1": LOCAL_STORY}, _bounced("S1", "c1"))
    assert "- first_pass_clean_rate=0.0" in summary


def test_rollup_note_is_the_last_line():
    summary = _summary({"S1": LOCAL_STORY}, _bounced("S1", "c1"))
    assert summary.splitlines()[-1].startswith(NOTE_PREFIX)


def test_record_message_text_is_never_embedded():
    records = _bounced("S1", "c1")
    records[0]["message"] = SENTINEL
    assert SENTINEL not in _summary({"S1": LOCAL_STORY}, records)
