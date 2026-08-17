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


def test_last_test_failure_falls_back_to_stdout_stderr_tail_when_no_error_key(tmp_path):
    wt = _worktree(tmp_path, "boom")
    story = {
        "summary": "s",
        "last_test_check": {
            "returncode": 1,
            "stdout_tail": "FAILED tests/test_x.py::test_y - AssertionError: expected 3 got 4",
            "stderr_tail": "",
        },
    }
    assert "AssertionError: expected 3 got 4" in collect_failure_evidence(wt, story)


def test_passing_last_test_check_is_not_reported_as_a_failure(tmp_path):
    wt = _worktree(tmp_path, "boom")
    story = {
        "summary": "s",
        "last_test_check": {"returncode": 0, "stdout_tail": "5 passed", "stderr_tail": ""},
    }
    evidence = collect_failure_evidence(wt, story)
    assert "5 passed" not in evidence
    assert "LAST TEST FAILURE" not in evidence


def test_last_test_check_with_no_returncode_does_not_raise(tmp_path):
    wt = _worktree(tmp_path, "boom")
    story = {"summary": "s", "last_test_check": {"returncode": None}}
    evidence = collect_failure_evidence(wt, story)
    assert isinstance(evidence, str)
    assert "LAST TEST FAILURE" not in evidence


def test_last_test_check_missing_entirely_does_not_raise(tmp_path):
    wt = _worktree(tmp_path, "boom")
    story = {"summary": "s"}
    evidence = collect_failure_evidence(wt, story)
    assert isinstance(evidence, str)
    assert "LAST TEST FAILURE" not in evidence


def test_error_key_takes_priority_over_stdout_stderr_tail(tmp_path):
    wt = _worktree(tmp_path, "boom")
    story = {
        "summary": "s",
        "last_test_check": {
            "error": "explicit-error-text",
            "returncode": 1,
            "stdout_tail": "other-text",
        },
    }
    evidence = collect_failure_evidence(wt, story)
    assert "explicit-error-text" in evidence
    assert "other-text" not in evidence


def test_stale_last_test_check_sha_is_omitted_from_evidence(tmp_path, monkeypatch):
    """A last_test_check recorded at a PAST commit (stale sha) must NOT be fed
    into the diagnosis - the worktree HEAD has since moved, so the recorded
    failure may no longer exist. Only when the recorded sha matches the
    worktree's current HEAD should the LAST TEST FAILURE section appear."""
    wt = _worktree(tmp_path, "boom")
    # Worktree HEAD is now bbb222.
    monkeypatch.setattr(
        "pipeline.rebrief._git",
        lambda worktree, args, timeout=15: "bbb222" if args == ["rev-parse", "HEAD"] else None,
    )
    stale = {
        "summary": "s",
        "last_test_check": {
            "returncode": 1,
            "sha": "aaa111",
            "stdout_tail": "FAILED tests/test_x.py::test_y - AssertionError: expected 3 got 4",
            "stderr_tail": "",
        },
    }
    evidence = collect_failure_evidence(wt, stale)
    assert "LAST TEST FAILURE" not in evidence
    assert "expected 3 got 4" not in evidence

    # Fresh: recorded sha matches the current HEAD -> section included.
    fresh = {
        "summary": "s",
        "last_test_check": {
            "returncode": 1,
            "sha": "bbb222",
            "stdout_tail": "FAILED tests/test_x.py::test_y - AssertionError: expected 3 got 4",
            "stderr_tail": "",
        },
    }
    evidence = collect_failure_evidence(wt, fresh)
    assert "LAST TEST FAILURE" in evidence
    assert "expected 3 got 4" in evidence
