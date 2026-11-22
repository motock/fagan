"""line_limit_gate_mode() picks the checker mode from PIPELINE_FILE_SIZE_POLICY."""

import pytest

from tests.unit._line_limit_policy import FILE_SIZE_POLICY_ENV, line_limit_gate_mode


def test_should_gate_in_warn_mode_when_policy_is_unset(monkeypatch):
    monkeypatch.delenv(FILE_SIZE_POLICY_ENV, raising=False)

    assert line_limit_gate_mode() == "warn"


@pytest.mark.parametrize("policy", ["", "advisory", "ENFORCED"])
def test_should_gate_in_warn_mode_when_policy_is_not_exactly_enforced(
    monkeypatch, policy
):
    monkeypatch.setenv(FILE_SIZE_POLICY_ENV, policy)

    assert line_limit_gate_mode() == "warn"


@pytest.mark.parametrize("policy", ["enforced", " enforced "])
def test_should_gate_in_block_mode_when_policy_is_enforced(monkeypatch, policy):
    monkeypatch.setenv(FILE_SIZE_POLICY_ENV, policy)

    assert line_limit_gate_mode() == "block"
