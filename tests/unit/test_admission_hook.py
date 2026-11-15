"""The admission hook consulted at spend and notification send.

With ``PIPELINE_ADMISSION_PLUGIN`` unset the hook is absent and every entry
point behaves exactly as before. A refusal of a spend pauses the usage poll's
decision; a refused notification is dropped and counted. A failure to consult
the hook is handled on the safe side per kind: a spend is paused, while a
notification is still sent because losing an alert is worse than sending one.
"""

from __future__ import annotations

import importlib
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.plugin_loader import ALLOWLIST_ENV
from pipeline import access_hooks, notification_sinks, usage
from pipeline.access_hooks import ADMISSION_HOOK_PLUGIN_ENV, admit
from pipeline.actor import current_namespace

PROBE_STATE = {"session_pct": 10, "week_pct": 5, "checked_at": "2026-11-15T00:00:00+00:00"}

_TEMPLATE = """\
from pipeline.access_hooks import AdmissionDecision

CALLS = []


class StubHook:
    def admit(self, kind, namespace, context):
        CALLS.append((kind, namespace, dict(context)))
@@BODY@@


def build():
    return StubHook()
"""

REFUSE_ALL = """
        return AdmissionDecision(admitted=False, reason="policy_block")"""

REFUSE_SPEND_ONLY = """
        if kind == "spend":
            return AdmissionDecision(admitted=False, reason="policy_block")
        return AdmissionDecision(admitted=True, reason="ok")"""

REFUSE_NOTIFICATION_ONLY = """
        if kind == "notification":
            return AdmissionDecision(admitted=False, reason="policy_block")
        return AdmissionDecision(admitted=True, reason="ok")"""

ALLOW_ALL = """
        return AdmissionDecision(admitted=True, reason="ok")"""

FREE_TEXT_REFUSAL = """
        return AdmissionDecision(admitted=False, reason="Carol may not touch acme")"""

RAISE = """
        raise RuntimeError("boom")"""

NOT_A_DECISION = """
        return {"admitted": True}"""


@pytest.fixture(autouse=True)
def _no_plugin_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ADMISSION_HOOK_PLUGIN_ENV, raising=False)
    monkeypatch.delenv(ALLOWLIST_ENV, raising=False)


@pytest.fixture
def audits(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture host audit appends instead of writing the real chain."""
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(
        access_hooks, "append_host_audit", lambda **kwargs: recorded.append(kwargs)
    )
    return recorded


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture notification writes instead of touching the plan directory."""
    written: list[dict[str, Any]] = []
    monkeypatch.setattr(
        notification_sinks.persistence,
        "_notification_record",
        lambda **kwargs: {"record": kwargs.get("plan_name")},
    )
    monkeypatch.setattr(
        notification_sinks.persistence,
        "_append_notification",
        lambda plan, record: written.append(record),
    )
    return written


@pytest.fixture
def usage_io(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the usage probe and the state file so the poll runs in memory."""
    from pipeline import server

    written: dict[str, Any] = {}
    monkeypatch.setattr(server, "_run_usage_probe", lambda: dict(PROBE_STATE))
    monkeypatch.setattr(usage, "_read_usage_state", dict)
    monkeypatch.setattr(usage, "_write_usage_state", lambda state: written.update(state))
    monkeypatch.setattr(usage, "announce", lambda paused, state: None)
    return written


def _install_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: str,
    *,
    allowlist: str | None = None,
) -> str:
    """Write a stub hook as a fresh module, select it, and return its name."""
    name = "admission_" + uuid.uuid4().hex
    source = _TEMPLATE.replace("@@BODY@@", body)
    (tmp_path / f"{name}.py").write_text(source, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv(ADMISSION_HOOK_PLUGIN_ENV, f"{name}:build")
    monkeypatch.setenv(ALLOWLIST_ENV, name if allowlist is None else allowlist)
    return name


def _event(plan: str = "plan-x") -> dict[str, Any]:
    return {"type": "notification", "plan": plan, "payload": {"message": "hello"}}


class TestDefaultPathUnchanged:
    @pytest.mark.parametrize("kind", ["dispatch", "spend", "notification"])
    def test_admit_admits_every_kind_with_no_plugin(self, kind, audits) -> None:
        assert admit(kind, {}) is True
        assert audits == []

    def test_notification_is_written_with_no_plugin(self, sent, audits) -> None:
        notification_sinks.file_log_sink(_event())
        assert len(sent) == 1
        assert audits == []

    def test_spend_pause_is_the_gate_result_with_no_plugin(
        self, usage_io, audits
    ) -> None:
        state = usage._check_usage_impl()
        assert state["paused"] is usage._usage_gate(
            False, PROBE_STATE["session_pct"], PROBE_STATE["week_pct"]
        )
        assert audits == []

    def test_spend_pause_is_set_by_the_gate_when_over_threshold(
        self, usage_io, monkeypatch
    ) -> None:
        from pipeline import server

        monkeypatch.setattr(
            server,
            "_run_usage_probe",
            lambda: {**PROBE_STATE, "session_pct": 100, "week_pct": 100},
        )
        assert usage._check_usage_impl()["paused"] is True


class TestRefusalPerKind:
    def test_refused_dispatch_is_denied_and_audited_with_its_reason(
        self, tmp_path, monkeypatch, audits
    ) -> None:
        _install_plugin(tmp_path, monkeypatch, REFUSE_ALL)
        assert admit("dispatch", {"in_progress": 1}) is False
        assert audits == [
            {
                "action": "admission_refused",
                "outcome": "refused",
                "detail": {"admission_kind": "dispatch", "reason": "policy_block"},
            }
        ]

    def test_refused_spend_pauses_the_poll(
        self, tmp_path, monkeypatch, usage_io, audits
    ) -> None:
        _install_plugin(tmp_path, monkeypatch, REFUSE_SPEND_ONLY)
        state = usage._check_usage_impl()
        assert state["paused"] is True
        assert audits[0]["detail"] == {
            "admission_kind": "spend",
            "reason": "policy_block",
        }

    def test_refused_spend_is_not_applied_to_other_kinds(
        self, tmp_path, monkeypatch, audits
    ) -> None:
        _install_plugin(tmp_path, monkeypatch, REFUSE_SPEND_ONLY)
        assert admit("notification", {}) is True
        assert audits == []

    def test_refused_notification_is_dropped_counted_and_audited(
        self, tmp_path, monkeypatch, sent, audits
    ) -> None:
        _install_plugin(tmp_path, monkeypatch, REFUSE_NOTIFICATION_ONLY)
        before = notification_sinks.dropped_notification_count()
        notification_sinks.file_log_sink(_event())
        assert sent == []
        assert notification_sinks.dropped_notification_count() == before + 1
        assert audits[0]["detail"] == {
            "admission_kind": "notification",
            "reason": "policy_block",
        }

    def test_concurrent_refusals_are_all_counted(
        self, tmp_path, monkeypatch, sent, audits
    ) -> None:
        import sys
        import threading

        _install_plugin(tmp_path, monkeypatch, REFUSE_NOTIFICATION_ONLY)
        workers, per_worker = 8, 250
        before = notification_sinks.dropped_notification_count()
        start = threading.Barrier(workers)

        def refuse_many() -> None:
            start.wait()
            for _ in range(per_worker):
                notification_sinks.file_log_sink(_event())

        # A tiny switch interval makes a lost increment likely if the counter is unguarded.
        previous = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            threads = [threading.Thread(target=refuse_many) for _ in range(workers)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        finally:
            sys.setswitchinterval(previous)
        assert sent == []
        assert notification_sinks.dropped_notification_count() == before + workers * per_worker

    def test_free_text_reason_is_not_echoed_into_the_audit(
        self, tmp_path, monkeypatch, audits
    ) -> None:
        _install_plugin(tmp_path, monkeypatch, FREE_TEXT_REFUSAL)
        assert admit("spend", {}) is False
        assert audits[0]["detail"]["reason"] == "unspecified"
        assert "Carol" not in json.dumps(audits)
        assert "acme" not in json.dumps(audits)

    def test_hook_receives_namespace_and_only_the_caller_counts(
        self, tmp_path, monkeypatch, usage_io
    ) -> None:
        name = _install_plugin(tmp_path, monkeypatch, ALLOW_ALL)
        usage._check_usage_impl()
        assert importlib.import_module(name).CALLS == [
            ("spend", current_namespace(), {"session_pct": 10, "week_pct": 5}),
        ]

    def test_unknown_kind_is_a_programming_error(self, tmp_path, monkeypatch) -> None:
        _install_plugin(tmp_path, monkeypatch, ALLOW_ALL)
        with pytest.raises(ValueError):
            admit("bogus_kind", {})


class TestHookFailurePerKind:
    @pytest.mark.parametrize("body", [RAISE, NOT_A_DECISION], ids=["raises", "malformed"])
    def test_failed_dispatch_consult_is_denied_and_audited(
        self, tmp_path, monkeypatch, audits, body
    ) -> None:
        _install_plugin(tmp_path, monkeypatch, body)
        assert admit("dispatch", {}) is False
        assert audits[0]["detail"] == {
            "admission_kind": "dispatch",
            "reason": "hook_error",
        }

    @pytest.mark.parametrize("body", [RAISE, NOT_A_DECISION], ids=["raises", "malformed"])
    def test_failed_spend_consult_pauses_the_poll(
        self, tmp_path, monkeypatch, usage_io, audits, body
    ) -> None:
        _install_plugin(tmp_path, monkeypatch, body)
        assert usage._check_usage_impl()["paused"] is True
        assert audits[0]["detail"]["reason"] == "hook_error"

    def test_failed_notification_consult_still_sends_the_alert(
        self, tmp_path, monkeypatch, sent, audits
    ) -> None:
        _install_plugin(tmp_path, monkeypatch, RAISE)
        before = notification_sinks.dropped_notification_count()
        notification_sinks.file_log_sink(_event())
        assert len(sent) == 1
        assert notification_sinks.dropped_notification_count() == before
        assert audits[0]["detail"]["reason"] == "hook_error"


class TestFailClosedLoading:
    def test_plugin_without_allowlist_is_refused_before_import(
        self, tmp_path, monkeypatch, audits
    ) -> None:
        _install_plugin(tmp_path, monkeypatch, ALLOW_ALL, allowlist="")
        monkeypatch.delenv(ALLOWLIST_ENV, raising=False)
        assert admit("dispatch", {}) is False
        assert audits[0]["detail"]["reason"] == "hook_error"

    def test_standard_library_spec_is_refused(self, monkeypatch, sent, audits) -> None:
        monkeypatch.setenv(ADMISSION_HOOK_PLUGIN_ENV, "json:dumps")
        monkeypatch.setenv(ALLOWLIST_ENV, "json")
        assert admit("dispatch", {}) is False
        notification_sinks.file_log_sink(_event())
        assert len(sent) == 1

    def test_malformed_spec_is_refused(self, monkeypatch, audits) -> None:
        monkeypatch.setenv(ADMISSION_HOOK_PLUGIN_ENV, "not a spec")
        monkeypatch.setenv(ALLOWLIST_ENV, "not")
        assert admit("spend", {}) is False
        assert audits[0]["detail"]["reason"] == "hook_error"
