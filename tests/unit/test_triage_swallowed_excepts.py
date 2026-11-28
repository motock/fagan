"""Swallowed-exception handlers in pipeline.triage: today's return value is kept,
and each fail-safe that hides a fault now leaves one constant log record."""

from __future__ import annotations

import logging

import pytest

from pipeline import triage

LOGGER = "pipeline"


def _boom(*_args, **_kwargs):
    raise RuntimeError("secret-token-in-message")


def _records(caplog, level):
    return [r for r in caplog.records if r.name == LOGGER and r.levelno == level]


def _assert_logged_without_text(records):
    assert len(records) == 1
    text = records[0].getMessage()
    assert "RuntimeError" in text
    assert "secret-token-in-message" not in text
    assert records[0].exc_info is None


@pytest.fixture
def story():
    return {"status": "failed", "correlation_id": "cid-1"}


@pytest.fixture(autouse=True)
def _debug_capture(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)


def test_park_returns_marker_and_logs_when_notify_fails(monkeypatch, caplog, story):
    monkeypatch.setattr(triage, "_notify_user", _boom)

    assert triage._park("p", "k", story, "why") == "park_for_human"

    assert story["status"] == "parked"
    _assert_logged_without_text(_records(caplog, logging.WARNING))


def test_notify_escalate_model_returns_none_and_logs_when_notify_fails(monkeypatch, caplog, story):
    monkeypatch.setattr(triage, "_notify_user", _boom)

    assert triage._notify_escalate_model("p", "k", story, "msg", "escalated") is None

    _assert_logged_without_text(_records(caplog, logging.WARNING))


def test_park_for_human_with_existing_reason_logs_when_notify_fails(monkeypatch, caplog, story):
    monkeypatch.setattr(triage, "_notify_user", _boom)
    story["parked_reason"] = "kept reason"

    result = triage.execute_ruling("p", "k", story, {"action": "park_for_human"}, {})

    assert result == "park_for_human"
    assert story["parked_reason"] == "kept reason"
    _assert_logged_without_text(_records(caplog, logging.WARNING))


def test_deferred_action_logs_when_ruling_notice_fails(monkeypatch, caplog, story):
    monkeypatch.setattr(triage, "DEFERRED_ACTIONS", frozenset({"later"}))
    calls = []

    def notify(*args, **kwargs):
        calls.append(kwargs.get("event"))
        if kwargs.get("event") == "triage_ruling":
            raise RuntimeError("secret-token-in-message")

    monkeypatch.setattr(triage, "_notify_user", notify)

    result = triage.execute_ruling("p", "k", story, {"action": "later", "rationale": "r"}, {})

    assert result == "park_for_human"
    assert story["triage_deferred_action"] == "later"
    _assert_logged_without_text(_records(caplog, logging.WARNING))


def test_escalation_failure_parks_with_exception_class_in_reason(monkeypatch, story):
    story.update(backend="ollama", model="m1")
    monkeypatch.setattr(triage, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(triage, "_escalate_to_local_fallback_model", _boom)

    result = triage.execute_ruling(
        "p", "k", story, {"action": "escalate_model"}, {"local_model_fallback": "m2"}
    )

    assert result == "park_for_human"
    assert "RuntimeError" in story["parked_reason"]


def test_sweep_returns_error_class_and_logs_when_manifest_read_fails(monkeypatch, caplog):
    monkeypatch.setattr(triage, "_auto_triage_enabled", lambda: True)
    monkeypatch.setattr(triage.bound_records, "get_manifest", _boom)

    assert triage.run_triage_sweep("p") == {"ok": False, "error": "RuntimeError"}

    _assert_logged_without_text(_records(caplog, logging.WARNING))


def test_sweep_continues_with_no_findings_when_health_probe_fails(monkeypatch):
    manifest = {"stories": {"a": {"status": "parked", "worktree": "w"}}}
    seen = {}

    def evidence(worktree, story, findings=None, limit=8000):
        seen["findings"] = findings
        return ""

    monkeypatch.setattr(triage, "_auto_triage_enabled", lambda: True)
    monkeypatch.setattr(triage.bound_records, "get_manifest", lambda store, name: manifest)
    monkeypatch.setattr(triage.bound_records, "put_manifest", lambda *a, **k: None)
    monkeypatch.setattr(triage, "classify_repo_health", _boom)
    monkeypatch.setattr(triage, "collect_triage_evidence", evidence)
    monkeypatch.setattr(triage, "rule_on_story", lambda *a: {"action": "park_for_human"})
    monkeypatch.setattr(triage, "_apply_ruling_for_mode", lambda *a: "parked")
    monkeypatch.setattr(triage, "_notify_user", lambda *a, **k: None)

    result = triage.run_triage_sweep("p")

    assert seen["findings"] == []
    assert result["triaged"] == ["a"]


def test_suite_state_is_silent_string_and_logs_when_detection_fails(monkeypatch, caplog):
    monkeypatch.setattr(triage, "detect_test_command", _boom)

    assert triage._current_suite_state("/wt") == ""

    _assert_logged_without_text(_records(caplog, logging.WARNING))


def test_git_state_reports_unavailable_and_logs_when_diff_fails(monkeypatch, caplog, story):
    monkeypatch.setattr(triage, "_current_git_state_impl", lambda w, s: "GIT STATE: HEAD x")
    monkeypatch.setattr(triage, "_resolve_base_branch", lambda w, hint: "main")
    monkeypatch.setitem(triage.__dict__, "subprocess.run", _boom)
    monkeypatch.setattr("pipeline.server._default_branch", lambda: "main")

    result = triage._current_git_state("/wt", story)

    assert result.endswith("CHANGED FILES vs main:\n(unavailable)")
    _assert_logged_without_text(_records(caplog, logging.WARNING))


def test_git_state_logs_debug_when_default_branch_hint_fails(monkeypatch, caplog, story):
    monkeypatch.setattr(triage, "_current_git_state_impl", lambda w, s: "GIT STATE: HEAD x")
    monkeypatch.setattr(triage, "_resolve_base_branch", lambda w, hint: "")
    monkeypatch.setattr("pipeline.server._default_branch", _boom)

    result = triage._current_git_state("/wt", story)

    assert result == "GIT STATE: HEAD x\nCHANGED FILES vs :\n(unavailable)"
    _assert_logged_without_text(_records(caplog, logging.DEBUG))


def test_git_state_impl_returns_empty_and_logs_when_git_fails(monkeypatch, caplog, story):
    monkeypatch.setitem(triage.__dict__, "subprocess.run", _boom)

    assert triage._current_git_state_impl("/wt", story) == ""

    _assert_logged_without_text(_records(caplog, logging.WARNING))


def test_git_state_impl_logs_debug_when_default_branch_fails(monkeypatch, caplog, story):
    class Done:
        stdout = "abc\n"

    monkeypatch.setitem(triage.__dict__, "subprocess.run", lambda *a, **k: Done())
    monkeypatch.setattr("pipeline.server._default_branch", _boom)

    result = triage._current_git_state_impl("/wt", story)

    assert result == "GIT STATE: HEAD abc\nbase branch unresolved; new-commits check skipped"
    _assert_logged_without_text(_records(caplog, logging.DEBUG))


@pytest.mark.parametrize(
    ("target", "findings"),
    [
        ("_current_suite_state", None),
        ("_current_git_state", None),
        ("format_findings", [object()]),
        ("collect_failure_evidence", None),
    ],
)
def test_collect_evidence_omits_section_and_logs_when_a_source_fails(
    monkeypatch, caplog, story, target, findings
):
    monkeypatch.setattr(triage, "_current_suite_state", lambda w, s: "")
    monkeypatch.setattr(triage, "_current_git_state", lambda w, s: "")
    monkeypatch.setattr(triage, "collect_failure_evidence", lambda w, s, limit: "")
    monkeypatch.setattr(triage, target, _boom)

    result = triage.collect_triage_evidence("/wt", story, findings)

    assert result.startswith("TRIAGE QUESTION:")
    _assert_logged_without_text(_records(caplog, logging.WARNING))


def test_park_does_not_swallow_keyboard_interrupt_from_notifier(monkeypatch, story):
    def interrupt(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(triage, "_notify_user", interrupt)

    with pytest.raises(KeyboardInterrupt):
        triage._park("p", "k", story, "why")
