"""A failed-open triage ruling must say where it failed and why, without leaking.

PRH-2 (2026-09-24) was parked with ``triage failed open: RuntimeError`` and the
scheduler log held one bare line, ``RuntimeError``: no story, no plan, no
stage, no cause. The rationale (user-visible) now names the stage that failed;
the server-side log line also names the story, plan, correlation id and the
exception-type chain. Neither ever carries the exception message.
"""

import logging

import pytest

from pipeline import triage

EVIDENCE = "=== FAILURE EVIDENCE ===\nsomething broke\n"


class _ReadTimeout(Exception):
    """Stand-in for an HTTP client's timeout type (e.g. httpx.ReadTimeout)."""


@pytest.fixture(autouse=True)
def _stub_backends(monkeypatch):
    monkeypatch.setattr(triage, "_load_policy", lambda: "POLICY")
    monkeypatch.setattr(triage, "_plan_role_config", lambda plan_name: {})
    monkeypatch.setattr(triage, "_append_decision", lambda plan_name, record: None)


def _raise_wrapped(prompt, plan_role_config=None):
    try:
        raise _ReadTimeout("secret-token-abc")
    except _ReadTimeout as inner:
        raise RuntimeError("backend said secret-token-abc") from inner


def test_overlord_failure_rationale_names_the_overlord_stage(monkeypatch):
    monkeypatch.setattr(triage, "_invoke_overlord", _raise_wrapped)

    ruling = triage.rule_on_story("plan-x", "S-1", {}, EVIDENCE)

    assert ruling["rationale"] == "triage failed open at overlord: RuntimeError"


def test_policy_failure_rationale_names_the_policy_stage(monkeypatch):
    def boom():
        raise OSError("disk")

    monkeypatch.setattr(triage, "_load_policy", boom)

    ruling = triage.rule_on_story("plan-x", "S-1", {}, EVIDENCE)

    assert ruling["rationale"] == "triage failed open at policy: OSError"


def test_parse_failure_rationale_names_the_parse_stage(monkeypatch):
    monkeypatch.setattr(triage, "_invoke_overlord", lambda prompt, plan_role_config=None: "x")

    def bad_parse(raw):
        raise ValueError("unparseable")

    monkeypatch.setattr(triage, "_parse_ruling", bad_parse)

    ruling = triage.rule_on_story("plan-x", "S-1", {}, EVIDENCE)

    assert ruling["rationale"] == "triage failed open at parse: ValueError"


def test_failed_open_ruling_records_the_stage_as_a_field(monkeypatch):
    monkeypatch.setattr(triage, "_invoke_overlord", _raise_wrapped)

    ruling = triage.rule_on_story("plan-x", "S-1", {}, EVIDENCE)

    assert ruling["failed_stage"] == "overlord"


def test_log_line_names_story_plan_stage_and_correlation_id(monkeypatch, caplog):
    monkeypatch.setattr(triage, "_invoke_overlord", _raise_wrapped)

    with caplog.at_level(logging.WARNING, logger="pipeline"):
        triage.rule_on_story("plan-x", "S-1", {"correlation_id": "abc123def456"}, EVIDENCE)

    text = caplog.text
    assert "S-1" in text
    assert "plan-x" in text
    assert "overlord" in text
    assert "abc123def456" in text


def test_log_line_carries_the_exception_type_chain(monkeypatch, caplog):
    monkeypatch.setattr(triage, "_invoke_overlord", _raise_wrapped)

    with caplog.at_level(logging.WARNING, logger="pipeline"):
        triage.rule_on_story("plan-x", "S-1", {}, EVIDENCE)

    assert "RuntimeError <- _ReadTimeout" in caplog.text


def test_neither_rationale_nor_log_carries_the_exception_message(monkeypatch, caplog):
    monkeypatch.setattr(triage, "_invoke_overlord", _raise_wrapped)

    with caplog.at_level(logging.WARNING, logger="pipeline"):
        ruling = triage.rule_on_story("plan-x", "S-1", {}, EVIDENCE)

    assert "secret-token-abc" not in ruling["rationale"]
    assert "secret-token-abc" not in caplog.text


def test_type_chain_of_an_unchained_exception_is_its_own_name():
    assert triage._exception_type_chain(KeyError("k")) == "KeyError"


def test_type_chain_stops_at_five_links():
    exc = None
    for _ in range(8):
        try:
            raise ValueError("v") from exc
        except ValueError as new:
            exc = new

    assert triage._exception_type_chain(exc).count("ValueError") == 5


def test_happy_path_ruling_has_no_failed_stage(monkeypatch):
    monkeypatch.setattr(
        triage,
        "_invoke_overlord",
        lambda prompt, plan_role_config=None: (
            "RULING: r\nTIER: t\nRISK: low\nRATIONALE: ok\nNOTIFY_USER: no\nACTION: park_for_human\n"
        ),
    )

    ruling = triage.rule_on_story("plan-x", "S-1", {}, EVIDENCE)

    assert ruling["failed_open"] is False
    assert "failed_stage" not in ruling
