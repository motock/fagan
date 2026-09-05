"""Tests for deduping done-bar rejection notices.

Measured live (2026-09-04, real .agent_transcript.json): the identical
874-char "full test suite still fails" notice was appended 5+ times to one
resumed transcript — once per rework cycle where the same failure tripped
the done-bar again. A byte-identical notice that is already present in the
agent's messages adds zero information; skip the duplicate append. A notice
with a DIFFERENT failure tail must still append (the failure changed).
"""
from tests.unit._local_agent_test_helpers import la

TAIL = "tests/unit/test_example.py::test_one FAILED"


def test_identical_suite_notice_is_not_appended_twice():
    messages = []
    la._reject_done_for_suite(messages, 3, TAIL, "test")
    la._reject_done_for_suite(messages, 9, TAIL, "test")
    assert len(messages) == 1


def test_changed_failure_tail_still_appends():
    messages = []
    la._reject_done_for_suite(messages, 3, TAIL, "test")
    la._reject_done_for_suite(messages, 9, "tests/unit/test_other.py::test_two FAILED", "test")
    assert len(messages) == 2


def test_identical_lint_notice_is_not_appended_twice():
    messages = []
    la._reject_done_for_suite(messages, 3, "F401 unused import", "lint")
    la._reject_done_for_suite(messages, 9, "F401 unused import", "lint")
    assert len(messages) == 1


def test_notice_appends_when_a_earlier_notice_was_trimmed_away():
    # Context compaction can drop the earlier notice; the re-append must
    # still fire when the message is genuinely gone from context.
    messages = [{"role": "user", "content": "unrelated nudge"}]
    la._reject_done_for_suite(messages, 3, TAIL, "test")
    assert len(messages) == 2


def test_empty_messages_still_gets_the_notice():
    messages = []
    la._reject_done_for_suite(messages, 3, TAIL, "test")
    assert len(messages) == 1
    assert "full test suite still fails" in messages[0]["content"]