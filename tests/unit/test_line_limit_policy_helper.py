"""Tests for the policy-aware line-limit helper."""

import warnings

import pytest

from tests.unit._line_limit_policy import assert_within_line_limit

POLICY_ENV = "PIPELINE_FILE_SIZE_POLICY"


def test_should_fail_when_enforced_and_over_limit(monkeypatch):
    monkeypatch.setenv(POLICY_ENV, "enforced")

    with pytest.raises(AssertionError) as excinfo:
        assert_within_line_limit(1001, 1000, "pipeline/foo.py")

    assert "pipeline/foo.py" in str(excinfo.value)
    assert "1001" in str(excinfo.value)
    assert "1000" in str(excinfo.value)


def test_should_pass_without_warning_when_enforced_and_within_limit(monkeypatch):
    monkeypatch.setenv(POLICY_ENV, "enforced")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert_within_line_limit(999, 1000, "pipeline/foo.py")

    assert not [w for w in caught if issubclass(w.category, UserWarning)]


def test_should_warn_naming_label_when_unset_and_over_limit(monkeypatch):
    monkeypatch.delenv(POLICY_ENV, raising=False)

    with pytest.warns(UserWarning, match="pipeline/foo.py"):
        assert_within_line_limit(1001, 1000, "pipeline/foo.py")


def test_should_emit_no_warning_when_unset_and_within_limit(monkeypatch):
    monkeypatch.delenv(POLICY_ENV, raising=False)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert_within_line_limit(999, 1000, "pipeline/foo.py")

    assert not [w for w in caught if issubclass(w.category, UserWarning)]


def test_should_treat_unknown_policy_value_as_advisory(monkeypatch):
    monkeypatch.setenv(POLICY_ENV, "enforce")

    with pytest.warns(UserWarning, match="pipeline/foo.py"):
        assert_within_line_limit(1001, 1000, "pipeline/foo.py")


def test_should_treat_blank_policy_value_as_advisory(monkeypatch):
    monkeypatch.setenv(POLICY_ENV, "   ")

    with pytest.warns(UserWarning, match="pipeline/foo.py"):
        assert_within_line_limit(1001, 1000, "pipeline/foo.py")


@pytest.mark.parametrize("policy", [None, "enforced"])
def test_should_pass_at_exact_limit_in_both_modes(monkeypatch, policy):
    if policy is None:
        monkeypatch.delenv(POLICY_ENV, raising=False)
    else:
        monkeypatch.setenv(POLICY_ENV, policy)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert_within_line_limit(1000, 1000, "pipeline/foo.py")

    assert not [w for w in caught if issubclass(w.category, UserWarning)]
