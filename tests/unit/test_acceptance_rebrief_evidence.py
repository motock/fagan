"""Acceptance oracle: collect_failure_evidence must produce a BOUNDED,
information-dense summary of why an attempt failed, from the artifacts a
dispatched run actually leaves behind.

Bounded matters: this text is prepended to the next dispatch's prompt, so an
unbounded agent.log tail would blow the context budget it is meant to save.
"""
from pipeline.rebrief import collect_failure_evidence


def _worktree(tmp_path, log_text):
    (tmp_path / "agent.log").write_text(log_text)
    return tmp_path


def test_includes_the_tail_of_the_agent_log(tmp_path):
    wt = _worktree(tmp_path, "early noise\n" + "\n".join(f"line {i}" for i in range(200)))
    evidence = collect_failure_evidence(wt, {"summary": "s"})
    assert "line 199" in evidence


def test_output_is_bounded(tmp_path):
    wt = _worktree(tmp_path, "x" * 500_000)
    evidence = collect_failure_evidence(wt, {"summary": "s"}, limit=4000)
    assert len(evidence) <= 4000


def test_includes_the_story_summary_for_context(tmp_path):
    wt = _worktree(tmp_path, "boom")
    evidence = collect_failure_evidence(wt, {"summary": "make widgets portable"})
    assert "make widgets portable" in evidence


def test_missing_agent_log_does_not_raise(tmp_path):
    evidence = collect_failure_evidence(tmp_path, {"summary": "s"})
    assert isinstance(evidence, str)


def test_missing_worktree_does_not_raise(tmp_path):
    evidence = collect_failure_evidence(tmp_path / "gone", {"summary": "s"})
    assert isinstance(evidence, str)


def test_last_test_output_is_included_when_present(tmp_path):
    wt = _worktree(tmp_path, "boom")
    story = {"summary": "s", "last_test_check": {"error": "E   assert 3 == 4"}}
    assert "assert 3 == 4" in collect_failure_evidence(wt, story)
