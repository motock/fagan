"""Tests for the deployment telemetry profile switch (``pipeline/telemetry_profile``).

The telemetry profile is a DEPLOYMENT setting read from the environment once at
configure time. It is never request data, a header, a plan field or a story
field, and no caller may choose it. Exactly one value selects the ``harness``
profile; every unset, blank, malformed or unknown value resolves to the
restrictive ``customer`` profile (docs/TELEMETRY_CONTRACT.md, ``## Profiles``
and ``## Data classification``).

``pipeline.server`` is imported explicitly first: the pipeline package has a
pre-existing import cycle and every test that imports a ``pipeline.*`` module
imports the server module first (see tests/unit/test_triage_cap_silent_skip.py).
"""

from __future__ import annotations

import logging

import pytest

import pipeline.server  # noqa: F401
from pipeline import telemetry_profile


def _env(value):
    """A one-variable environment mapping for ``profile()``."""
    return {telemetry_profile.PROFILE_ENV: value}


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_profile_env_name_is_stable():
    assert telemetry_profile.PROFILE_ENV == "PIPELINE_TELEMETRY_PROFILE"


def test_profile_values_are_lowercase_enum_strings():
    assert telemetry_profile.CUSTOMER == "customer"
    assert telemetry_profile.HARNESS == "harness"


# ---------------------------------------------------------------------------
# Positive: only the exact harness value selects harness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "environ",
    [
        pytest.param(_env("harness"), id="exact"),
        pytest.param(_env(" HARNESS "), id="spaces-upper-case"),
        pytest.param(_env("\tHarness\n"), id="tabs-newlines"),
    ],
)
def test_profile_resolves_harness(environ):
    assert telemetry_profile.profile(environ) == "harness"


@pytest.mark.parametrize(
    "environ",
    [
        pytest.param({}, id="unset"),
        pytest.param({telemetry_profile.PROFILE_ENV: None}, id="none-value"),
        pytest.param(_env("customer"), id="explicit-customer"),
        pytest.param(_env(" CUSTOMER "), id="spaces-upper-case-customer"),
    ],
)
def test_profile_resolves_customer(environ):
    assert telemetry_profile.profile(environ) == "customer"


def test_profile_defaults_to_customer_when_unset(monkeypatch):
    monkeypatch.delenv(telemetry_profile.PROFILE_ENV, raising=False)
    assert telemetry_profile.profile() == "customer"


# ---------------------------------------------------------------------------
# Negative: malformed and unknown values must never reach harness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("Harness-Plus", id="near-miss-suffix"),
        pytest.param("1", id="digit"),
        pytest.param("true", id="boolean"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="blank"),
        pytest.param("x" * 10_000, id="huge"),
    ],
)
def test_unknown_or_malformed_values_resolve_to_customer(value):
    assert telemetry_profile.profile(_env(value)) == "customer"


def test_is_harness_is_true_only_for_harness(monkeypatch):
    monkeypatch.setenv(telemetry_profile.PROFILE_ENV, "harness")
    assert telemetry_profile.is_harness() is True

    monkeypatch.setenv(telemetry_profile.PROFILE_ENV, " HARNESS ")
    assert telemetry_profile.is_harness() is True


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(None, id="unset"),
        pytest.param("customer", id="customer"),
        pytest.param("Harness-Plus", id="unknown"),
        pytest.param("", id="empty"),
    ],
)
def test_is_harness_is_false_for_everything_else(monkeypatch, value):
    if value is None:
        monkeypatch.delenv(telemetry_profile.PROFILE_ENV, raising=False)
    else:
        monkeypatch.setenv(telemetry_profile.PROFILE_ENV, value)
    assert telemetry_profile.is_harness() is False


# ---------------------------------------------------------------------------
# Warning: one per process, never the value
# ---------------------------------------------------------------------------


def test_unknown_value_warns_exactly_once_per_process(monkeypatch, caplog):
    monkeypatch.setattr(telemetry_profile, "_warned_unknown", False)

    with caplog.at_level(logging.WARNING):
        assert telemetry_profile.profile(_env("Harness-Plus")) == "customer"
        assert telemetry_profile.profile(_env("another-unknown")) == "customer"

    records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(records) == 1
    # The operator needs to know which variable to fix ...
    assert telemetry_profile.PROFILE_ENV in caplog.text
    # ... but the value may be arbitrary/untrusted, so it is never logged.
    assert "Harness-Plus" not in caplog.text
    assert "another-unknown" not in caplog.text


def test_blank_value_does_not_warn(monkeypatch, caplog):
    monkeypatch.setattr(telemetry_profile, "_warned_unknown", False)

    with caplog.at_level(logging.WARNING):
        assert telemetry_profile.profile(_env("   ")) == "customer"

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert telemetry_profile._warned_unknown is False


# ---------------------------------------------------------------------------
# The environment is read on every call; nothing is cached
# ---------------------------------------------------------------------------


def test_profile_reads_the_environment_each_call(monkeypatch):
    monkeypatch.setenv(telemetry_profile.PROFILE_ENV, "harness")
    assert telemetry_profile.profile() == "harness"

    monkeypatch.setenv(telemetry_profile.PROFILE_ENV, "customer")
    assert telemetry_profile.profile() == "customer"

    monkeypatch.setenv(telemetry_profile.PROFILE_ENV, "harness")
    assert telemetry_profile.profile() == "harness"


def test_profile_does_not_import_opentelemetry():
    source = __import__("inspect").getsource(telemetry_profile)
    assert "opentelemetry" not in source
