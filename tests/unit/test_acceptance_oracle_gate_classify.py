"""Acceptance oracle: classify_oracle_outcome must tell a USEFUL oracle failure
(the deliverable is missing) apart from a BROKEN oracle (the fixture itself
errors), so a fixture that can never go green is rejected before dispatch.

The distinction that matters: a fixture importing the not-yet-written module
raises ModuleNotFoundError and pytest reports it as a collection ERROR - that
is the CORRECT pre-dispatch state and must NOT be classified as broken.
"""
import pytest

from pipeline.oracle_gate import classify_oracle_outcome


def test_zero_returncode_is_passes():
    assert classify_oracle_outcome(0, "5 passed")["state"] == "passes"


def test_no_tests_collected_is_empty():
    assert classify_oracle_outcome(5, "no tests ran")["state"] == "empty"


def test_plain_assertion_failure_is_fails_correctly():
    out = "E       assert 3 == 4\n1 failed"
    assert classify_oracle_outcome(1, out)["state"] == "fails_correctly"


def test_missing_deliverable_module_is_fails_correctly():
    out = (
        "ERROR collecting tests/unit/test_x.py\n"
        "E   ModuleNotFoundError: No module named 'pipeline.not_yet'"
    )
    assert classify_oracle_outcome(2, out)["state"] == "fails_correctly"


def test_expat_error_in_a_helper_is_errors():
    out = (
        "ERROR collecting tests/unit/test_x.py\n"
        "E   xml.parsers.expat.ExpatError: not well-formed (invalid token)"
    )
    assert classify_oracle_outcome(2, out)["state"] == "errors"


def test_pytest_internalerror_is_errors():
    assert classify_oracle_outcome(3, "INTERNALERROR> Traceback")["state"] == "errors"


def test_bad_invocation_is_errors():
    out = "usage: pytest [options]\npytest: error: unrecognized arguments: --nope"
    assert classify_oracle_outcome(4, out)["state"] == "errors"


@pytest.mark.parametrize("state", ["passes", "empty", "errors", "fails_correctly"])
def test_every_state_carries_a_detail_string(state):
    samples = {
        "passes": (0, "5 passed"),
        "empty": (5, "no tests ran"),
        "errors": (3, "INTERNALERROR> Traceback"),
        "fails_correctly": (1, "E       assert 3 == 4"),
    }
    result = classify_oracle_outcome(*samples[state])
    assert result["state"] == state
    assert isinstance(result["detail"], str)
