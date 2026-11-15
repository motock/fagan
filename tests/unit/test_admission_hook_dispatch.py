"""The admission hook consulted before a story takes a dispatch slot.

A refusal or a hook exception defers the dispatch for this tick through the
real advance tick: the story stays dispatch-eligible, is not parked, claims no
lease and starts no worker, and the refusal is audited once. With no plugin
selected the tick dispatches exactly as before.
"""

from __future__ import annotations

import importlib
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.plugin_loader import ALLOWLIST_ENV
from pipeline import access_hooks
from pipeline.access_hooks import ADMISSION_HOOK_PLUGIN_ENV
from tests.unit.test_advance_dispatch_per_tick_cap import (
    _PLAN,
    _pin_tick_environment,
    _read_stories,
    _run_tick,
    _seed_tick,
)

_TEMPLATE = """\
from pipeline.access_hooks import AdmissionDecision

CALLS = []


class StubHook:
    def admit(self, kind, namespace, context):
        CALLS.append((kind, dict(context)))
@@BODY@@


def build():
    return StubHook()
"""

REFUSE_DISPATCH = """
        if kind == "dispatch":
            return AdmissionDecision(admitted=False, reason="policy_block")
        return AdmissionDecision(admitted=True, reason="ok")"""

RAISE_ON_DISPATCH = """
        if kind == "dispatch":
            raise RuntimeError("boom")
        return AdmissionDecision(admitted=True, reason="ok")"""

REFUSE_FIRST_TICK = """
        if kind == "dispatch" and len(CALLS) == 1:
            return AdmissionDecision(admitted=False, reason="policy_block")
        return AdmissionDecision(admitted=True, reason="ok")"""

ALLOW_ALL = """
        return AdmissionDecision(admitted=True, reason="ok")"""


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


def _install_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> str:
    """Write a stub admission hook as a fresh module and select it."""
    name = "admission_dispatch_" + uuid.uuid4().hex
    (tmp_path / f"{name}.py").write_text(
        _TEMPLATE.replace("@@BODY@@", body), encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv(ADMISSION_HOOK_PLUGIN_ENV, f"{name}:build")
    monkeypatch.setenv(ALLOWLIST_ENV, name)
    return name


def _seed_one_ready_story(monkeypatch, tmp_path):
    _pin_tick_environment(monkeypatch)
    return _seed_tick(monkeypatch, tmp_path, ["s1"])


class TestDefaultDispatchUnchanged:
    def test_ready_story_dispatches_with_no_plugin(self, monkeypatch, tmp_path, audits):
        rec, path = _seed_one_ready_story(monkeypatch, tmp_path)
        summary = _run_tick()
        assert rec.calls == [(_PLAN, "s1")]
        assert summary["dispatched"] == ["s1"]
        assert _read_stories(path)["s1"]["status"] == "in_progress"
        assert audits == []


class TestRefusedDispatchDefers:
    def test_refusal_starts_no_worker_and_leaves_the_story_eligible(
        self, monkeypatch, tmp_path, audits
    ):
        _install_plugin(tmp_path, monkeypatch, REFUSE_DISPATCH)
        rec, path = _seed_one_ready_story(monkeypatch, tmp_path)
        summary = _run_tick()
        assert rec.calls == []
        assert summary["dispatched"] == []
        story = _read_stories(path)["s1"]
        assert story["status"] == "todo"
        assert "dispatch_lease_expires_at" not in story
        assert "parked_reason" not in story

    def test_refusal_is_audited_once_with_its_reason_token(
        self, monkeypatch, tmp_path, audits
    ):
        _install_plugin(tmp_path, monkeypatch, REFUSE_DISPATCH)
        _seed_one_ready_story(monkeypatch, tmp_path)
        _run_tick()
        assert audits == [
            {
                "action": "admission_refused",
                "outcome": "refused",
                "detail": {"admission_kind": "dispatch", "reason": "policy_block"},
            }
        ]

    def test_refused_story_dispatches_on_a_later_tick_once_admitted(
        self, monkeypatch, tmp_path, audits
    ):
        _install_plugin(tmp_path, monkeypatch, REFUSE_FIRST_TICK)
        rec, _path = _seed_one_ready_story(monkeypatch, tmp_path)
        assert _run_tick()["dispatched"] == []
        assert rec.calls == []
        assert _run_tick()["dispatched"] == ["s1"]
        assert rec.calls == [(_PLAN, "s1")]
        assert len(audits) == 1


class TestDispatchHookFailureDefers:
    def test_raising_hook_starts_no_worker_and_leaves_the_story_eligible(
        self, monkeypatch, tmp_path, audits
    ):
        _install_plugin(tmp_path, monkeypatch, RAISE_ON_DISPATCH)
        rec, path = _seed_one_ready_story(monkeypatch, tmp_path)
        summary = _run_tick()
        assert rec.calls == []
        assert summary["dispatched"] == []
        story = _read_stories(path)["s1"]
        assert story["status"] == "todo"
        assert "dispatch_lease_expires_at" not in story
        assert "parked_reason" not in story

    def test_raising_hook_failure_is_audited_once_as_hook_error(
        self, monkeypatch, tmp_path, audits
    ):
        _install_plugin(tmp_path, monkeypatch, RAISE_ON_DISPATCH)
        _seed_one_ready_story(monkeypatch, tmp_path)
        _run_tick()
        assert audits == [
            {
                "action": "admission_hook_failed",
                "outcome": "refused",
                "detail": {"admission_kind": "dispatch", "reason": "hook_error"},
            }
        ]


class TestAdmitDispatchDelegate:
    def test_admit_dispatch_passes_only_counts_as_context(
        self, tmp_path, monkeypatch, audits
    ):
        name = _install_plugin(tmp_path, monkeypatch, ALLOW_ALL)
        assert access_hooks.admit_dispatch({"dispatch_attempts": 2}) is True
        assert importlib.import_module(name).CALLS == [
            ("dispatch", {"dispatch_attempts": 2}),
        ]

    def test_missing_attempt_count_is_sent_as_zero(self, tmp_path, monkeypatch, audits):
        name = _install_plugin(tmp_path, monkeypatch, ALLOW_ALL)
        access_hooks.admit_dispatch({})
        assert importlib.import_module(name).CALLS == [
            ("dispatch", {"dispatch_attempts": 0}),
        ]
