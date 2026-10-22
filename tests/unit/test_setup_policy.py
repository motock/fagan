"""Unit tests for ``pipeline.setup.policy``.

The module under test is a pure projection: it turns a mapping of raw answers
into the exact set of ``.pipeline.env`` entries the guided setup may write. The
tests below pin the allowed key set, the cross-field rules (hysteresis and the
escalation backend/model pairing), the autonomy prose and the module's purity.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pipeline.setup import policy

ALLOWED_KEYS = frozenset(
    {
        "PIPELINE_AUTONOMY",
        "PIPELINE_RISK_THRESHOLD",
        "PIPELINE_MAX_CONCURRENT_AGENTS",
        "PIPELINE_DAILY_REQUEST_THRESHOLD",
        "PIPELINE_WEEKLY_REQUEST_THRESHOLD",
        "PIPELINE_PAUSE_THRESHOLD",
        "PIPELINE_RESUME_THRESHOLD",
        "PIPELINE_ESCALATION_BACKEND",
        "PIPELINE_ESCALATION_MODEL",
        "PIPELINE_SECURITY_AUDIT_EVERY_DAYS",
        "PIPELINE_SECURITY_AUDIT_EVERY_COMMITS",
    }
)

POLICY_MODULE = Path(policy.__file__)


def _complete_answers() -> dict[str, object]:
    """A full, valid answer set covering every allowed key."""
    return {
        "PIPELINE_AUTONOMY": "gated",
        "PIPELINE_RISK_THRESHOLD": "medium",
        "PIPELINE_MAX_CONCURRENT_AGENTS": "3",
        "PIPELINE_DAILY_REQUEST_THRESHOLD": "500",
        "PIPELINE_WEEKLY_REQUEST_THRESHOLD": "2500",
        "PIPELINE_PAUSE_THRESHOLD": "90",
        "PIPELINE_RESUME_THRESHOLD": "70",
        "PIPELINE_ESCALATION_BACKEND": "claude",
        "PIPELINE_ESCALATION_MODEL": "opus",
        "PIPELINE_SECURITY_AUDIT_EVERY_DAYS": "7",
        "PIPELINE_SECURITY_AUDIT_EVERY_COMMITS": "50",
    }


# ---------------------------------------------------------------------------
# Positive
# ---------------------------------------------------------------------------


def test_complete_answers_return_every_allowed_key_normalized() -> None:
    settings = policy.build_policy_settings(_complete_answers())

    assert set(settings) == ALLOWED_KEYS
    assert settings["PIPELINE_AUTONOMY"] == "gated"
    assert settings["PIPELINE_RISK_THRESHOLD"] == "medium"
    assert settings["PIPELINE_MAX_CONCURRENT_AGENTS"] == "3"
    assert settings["PIPELINE_DAILY_REQUEST_THRESHOLD"] == "500"
    assert settings["PIPELINE_WEEKLY_REQUEST_THRESHOLD"] == "2500"
    assert settings["PIPELINE_PAUSE_THRESHOLD"] == "90"
    assert settings["PIPELINE_RESUME_THRESHOLD"] == "70"
    assert settings["PIPELINE_ESCALATION_BACKEND"] == "claude"
    assert settings["PIPELINE_ESCALATION_MODEL"] == "opus"
    assert settings["PIPELINE_SECURITY_AUDIT_EVERY_DAYS"] == "7"
    assert settings["PIPELINE_SECURITY_AUDIT_EVERY_COMMITS"] == "50"


def test_every_returned_value_is_a_string() -> None:
    settings = policy.build_policy_settings(_complete_answers())

    assert all(isinstance(value, str) for value in settings.values())


def test_values_are_normalized_by_the_schema() -> None:
    settings = policy.build_policy_settings(
        {
            "PIPELINE_AUTONOMY": "  GATED  ",
            "PIPELINE_RISK_THRESHOLD": "HIGH",
            "PIPELINE_MAX_CONCURRENT_AGENTS": " 4 ",
        }
    )

    assert settings["PIPELINE_AUTONOMY"] == "gated"
    assert settings["PIPELINE_RISK_THRESHOLD"] == "high"
    assert settings["PIPELINE_MAX_CONCURRENT_AGENTS"] == "4"


def test_omitting_escalation_keys_leaves_them_unset() -> None:
    answers = _complete_answers()
    del answers["PIPELINE_ESCALATION_BACKEND"]
    del answers["PIPELINE_ESCALATION_MODEL"]

    settings = policy.build_policy_settings(answers)

    assert "PIPELINE_ESCALATION_BACKEND" not in settings
    assert "PIPELINE_ESCALATION_MODEL" not in settings
    assert set(settings) == ALLOWED_KEYS - {
        "PIPELINE_ESCALATION_BACKEND",
        "PIPELINE_ESCALATION_MODEL",
    }


def test_none_values_are_omitted() -> None:
    settings = policy.build_policy_settings(
        {"PIPELINE_AUTONOMY": "full", "PIPELINE_ESCALATION_MODEL": None}
    )

    assert settings == {"PIPELINE_AUTONOMY": "full"}


def test_empty_answers_return_empty_mapping() -> None:
    assert policy.build_policy_settings({}) == {}


def test_backend_without_model_is_valid() -> None:
    settings = policy.build_policy_settings(
        {"PIPELINE_ESCALATION_BACKEND": "local"}
    )

    assert settings == {"PIPELINE_ESCALATION_BACKEND": "local"}


def test_returned_keys_are_a_subset_of_the_allowed_set() -> None:
    settings = policy.build_policy_settings(_complete_answers())

    assert set(settings) <= ALLOWED_KEYS


def test_describe_autonomy_gated_mentions_threshold_and_approval() -> None:
    text = policy.describe_autonomy("gated")

    assert "PIPELINE_RISK_THRESHOLD" in text
    assert "approval" in text.lower()
    assert "unattended" in text.lower()


def test_describe_autonomy_full_says_threshold_not_consulted() -> None:
    text = policy.describe_autonomy("full")

    assert "threshold" in text.lower()
    assert "not consulted" in text.lower()


def test_describe_autonomy_dry_run_points_at_gated() -> None:
    text = policy.describe_autonomy("dry-run")

    assert "no story" in text.lower()
    assert "dry run" in text.lower()
    assert "--autonomy gated" in text
    assert "PIPELINE_AUTONOMY=gated" in text


def test_describe_autonomy_is_deterministic() -> None:
    assert policy.describe_autonomy("gated") == policy.describe_autonomy("gated")


# ---------------------------------------------------------------------------
# Negative
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["yolo", "", "GATED!", "gated "])
def test_describe_autonomy_rejects_unknown_modes(mode: str) -> None:
    with pytest.raises(ValueError):
        policy.describe_autonomy(mode)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("PIPELINE_AUTONOMY", "yolo"),
        ("PIPELINE_RISK_THRESHOLD", "critical"),
        ("PIPELINE_MAX_CONCURRENT_AGENTS", "0"),
        ("PIPELINE_MAX_CONCURRENT_AGENTS", "two"),
        ("PIPELINE_DAILY_REQUEST_THRESHOLD", "0"),
        ("PIPELINE_SECURITY_AUDIT_EVERY_DAYS", "-1"),
        ("PIPELINE_ESCALATION_BACKEND", "gpt"),
    ],
)
def test_invalid_values_raise_value_error(key: str, value: str) -> None:
    with pytest.raises(ValueError):
        policy.build_policy_settings({key: value})


def test_pause_greater_than_resume_is_required() -> None:
    with pytest.raises(ValueError) as excinfo:
        policy.build_policy_settings(
            {"PIPELINE_PAUSE_THRESHOLD": "50", "PIPELINE_RESUME_THRESHOLD": "60"}
        )

    message = str(excinfo.value)
    assert "PIPELINE_PAUSE_THRESHOLD" in message
    assert "PIPELINE_RESUME_THRESHOLD" in message


def test_pause_equal_to_resume_is_rejected() -> None:
    with pytest.raises(ValueError) as excinfo:
        policy.build_policy_settings(
            {"PIPELINE_PAUSE_THRESHOLD": "50", "PIPELINE_RESUME_THRESHOLD": "50"}
        )

    message = str(excinfo.value)
    assert "PIPELINE_PAUSE_THRESHOLD" in message
    assert "PIPELINE_RESUME_THRESHOLD" in message


def test_escalation_model_without_backend_is_rejected() -> None:
    with pytest.raises(ValueError) as excinfo:
        policy.build_policy_settings({"PIPELINE_ESCALATION_MODEL": "opus"})

    message = str(excinfo.value)
    assert "PIPELINE_ESCALATION_MODEL" in message
    assert "PIPELINE_ESCALATION_BACKEND" in message


def test_unknown_key_is_rejected_and_named() -> None:
    with pytest.raises(ValueError) as excinfo:
        policy.build_policy_settings({"PIPELINE_NOT_A_REAL_KEY": "1"})

    assert "PIPELINE_NOT_A_REAL_KEY" in str(excinfo.value)


def test_transport_and_local_agent_keys_are_rejected() -> None:
    for key in ("PIPELINE_TRANSPORT_MODE", "LOCAL_AGENT_MODEL"):
        with pytest.raises(ValueError) as excinfo:
            policy.build_policy_settings({key: "x"})
        assert key in str(excinfo.value)


def test_cross_field_errors_are_collected_into_one_message() -> None:
    with pytest.raises(ValueError) as excinfo:
        policy.build_policy_settings(
            {
                "PIPELINE_PAUSE_THRESHOLD": "10",
                "PIPELINE_RESUME_THRESHOLD": "20",
                "PIPELINE_ESCALATION_MODEL": "opus",
            }
        )

    message = str(excinfo.value)
    assert "PIPELINE_PAUSE_THRESHOLD" in message
    assert "PIPELINE_ESCALATION_MODEL" in message


# ---------------------------------------------------------------------------
# Boundary
# ---------------------------------------------------------------------------


def test_pause_100_resume_0_is_accepted() -> None:
    settings = policy.build_policy_settings(
        {"PIPELINE_PAUSE_THRESHOLD": "100", "PIPELINE_RESUME_THRESHOLD": "0"}
    )

    assert settings == {
        "PIPELINE_PAUSE_THRESHOLD": "100",
        "PIPELINE_RESUME_THRESHOLD": "0",
    }


def test_concurrency_of_one_is_accepted() -> None:
    assert policy.build_policy_settings(
        {"PIPELINE_MAX_CONCURRENT_AGENTS": "1"}
    ) == {"PIPELINE_MAX_CONCURRENT_AGENTS": "1"}


def test_audit_zero_is_accepted_as_disabled() -> None:
    settings = policy.build_policy_settings(
        {
            "PIPELINE_SECURITY_AUDIT_EVERY_DAYS": "0",
            "PIPELINE_SECURITY_AUDIT_EVERY_COMMITS": "0",
        }
    )

    assert settings == {
        "PIPELINE_SECURITY_AUDIT_EVERY_DAYS": "0",
        "PIPELINE_SECURITY_AUDIT_EVERY_COMMITS": "0",
    }


# ---------------------------------------------------------------------------
# Guard: no transport-only or local-agent keys can leak into the output
# ---------------------------------------------------------------------------


def test_no_transport_or_local_agent_key_can_appear() -> None:
    settings = policy.build_policy_settings(_complete_answers())

    for key in settings:
        assert not key.startswith("PIPELINE_TRANSPORT_")
        assert not key.startswith("LOCAL_AGENT_")


# ---------------------------------------------------------------------------
# Purity
# ---------------------------------------------------------------------------


def _policy_tree() -> ast.Module:
    return ast.parse(POLICY_MODULE.read_text(encoding="utf-8"))


def test_module_never_reads_the_environment() -> None:
    source = POLICY_MODULE.read_text(encoding="utf-8")

    assert "os.environ" not in source
    assert "getenv" not in source
    for node in ast.walk(_policy_tree()):
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"environ", "getenv", "putenv"}


def test_module_has_no_io_or_subprocess_side_effects() -> None:
    forbidden_modules = {"subprocess", "socket", "shutil", "tempfile", "pathlib"}
    for node in ast.walk(_policy_tree()):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in forbidden_modules
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in forbidden_modules
        elif isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            assert name != "open", "policy module must not open files"


def test_public_api_is_exactly_two_functions() -> None:
    assert set(policy.__all__) == {"build_policy_settings", "describe_autonomy"}
