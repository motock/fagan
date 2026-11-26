"""Acceptance oracle: a reviewer backend exception must leave a diagnosable
trail.

The handler logs only type(e).__name__ for secret hygiene, so PR #210's review
RuntimeError can never be root-caused. Record a traceback to the review log
while keeping the exception TEXT out of the user-facing notification.
"""
import inspect

from pipeline import review_scope_gate


def _source():
    # The reviewer exception handler moved out of review_story into
    # pipeline/review_scope_gate.py (ohc-07).
    return inspect.getsource(review_scope_gate)


def test_the_handler_records_a_traceback():
    src = _source()
    assert "format_exc" in src or "exc_info" in src, (
        "the reviewer exception handler must persist a traceback, not just the "
        "exception type name"
    )


def test_the_notification_still_omits_the_exception_text():
    src = _source()
    marker = "treating as inconclusive"
    idx = src.index(marker)
    window = src[max(0, idx - 400):idx]
    assert "type(e).__name__" in window, (
        "the user-facing notification must keep naming only the exception TYPE"
    )
    assert "{e}" not in window and "str(e)" not in window, (
        "the exception text must not reach the notification"
    )


def test_the_failure_still_falls_closed_to_inconclusive():
    src = _source()
    idx = src.index("treating as inconclusive")
    assert 'reviewer_output = ""' in src[idx:idx + 400], (
        "a reviewer exception must still fall closed into the UNKNOWN/"
        "inconclusive path, never an APPROVE"
    )
