"""Fail-safe handlers that used to swallow everything now leave a trace (bex-04).

Each L site keeps today's return value and gains one constant-message log record
carrying the exception class name only. Each N site lets an unexpected exception
propagate.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import types
from unittest import mock

import pytest

from pipeline import (
    scheduler_namespaces,
    sizing_by_class,
    sizing_class,
    stall_scan,
    standing_approval,
    story_fields,
    strength_class,
    telemetry_durations,
    telemetry_test_scope,
    test_author,
    test_edit_judge,
    trace_context,
    usage_cooldown,
    wedge_io,
    workspace,
)

SECRET = "s3cret-token-value"


class _Boom(RuntimeError):
    pass


def _boom(*_a, **_k):
    raise _Boom(SECRET)


def _records_with(caplog, level=logging.DEBUG):
    caplog.set_level(level, logger="pipeline")
    return caplog


def _assert_one_class_only_record(caplog, level):
    matching = [r for r in caplog.records if r.levelno == level and "_Boom" in r.getMessage() + str(getattr(r, "error_class", ""))]
    assert len(matching) == 1
    assert SECRET not in caplog.text
    assert matching[0].exc_info is None


def test_advance_namespace_logs_class_when_a_plan_raises(caplog):
    _records_with(caplog)
    records = mock.Mock()
    records.list_plans.return_value = ["p1"]
    records.get_manifest.side_effect = _boom
    plans, refusals = scheduler_namespaces._advance_namespace(mock.Mock(), records, "ns")
    assert plans == {"p1": {"ok": False, "error": "advance raised _Boom"}}
    assert refusals == []
    _assert_one_class_only_record(caplog, logging.WARNING)


def test_sizing_by_class_logs_when_class_lookup_fails(caplog, monkeypatch):
    _records_with(caplog)
    monkeypatch.setattr(sizing_by_class, "dispatch_strength_class", _boom)
    out = sizing_by_class.class_aware_sizing_warning({}, "/r", None, lambda s, r: "warn")
    assert out == "warn"
    _assert_one_class_only_record(caplog, logging.WARNING)


def test_sizing_applies_logs_when_provider_lookup_fails(caplog, monkeypatch):
    _records_with(caplog)
    monkeypatch.delenv(sizing_class._PROVIDER_ENV, raising=False)
    monkeypatch.setattr(sizing_class, "_effective_provider", _boom)
    assert sizing_class.sizing_applies({}, {}) is False
    _assert_one_class_only_record(caplog, logging.WARNING)


def test_needs_small_story_caps_logs_when_decision_fails(caplog, monkeypatch):
    _records_with(caplog)
    monkeypatch.delenv(sizing_class._PROVIDER_ENV, raising=False)
    monkeypatch.setattr(sizing_class, "_needs_caps", _boom)
    assert sizing_class.needs_small_story_caps({}, {}) is False
    _assert_one_class_only_record(caplog, logging.WARNING)


def test_gate_reason_logs_when_backend_is_broken(caplog, monkeypatch):
    _records_with(caplog)
    monkeypatch.setattr(stall_scan.backend, "get_backend", _boom)
    assert stall_scan._gate_reason() == "unknown"
    _assert_one_class_only_record(caplog, logging.WARNING)


def test_still_valid_refuses_and_logs_when_recheck_raises(caplog):
    _records_with(caplog)
    assert standing_approval._still_valid(_boom) is False
    _assert_one_class_only_record(caplog, logging.WARNING)


def test_still_valid_true_is_silent(caplog):
    _records_with(caplog)
    assert standing_approval._still_valid(lambda: True) is True
    assert caplog.records == []


def test_record_dispatch_baseline_logs_when_snapshot_raises(caplog, tmp_path):
    _records_with(caplog)
    marker = tmp_path / "marker.json"
    story = {}
    out = story_fields.record_dispatch_baseline(
        mock.Mock(), "plan", "key", story, tmp_path, marker=marker, snapshot=_boom, rerun=True
    )
    assert out is story
    _assert_one_class_only_record(caplog, logging.WARNING)


def test_load_registry_logs_when_registry_load_fails(caplog, monkeypatch):
    _records_with(caplog)
    from app import role_registry

    monkeypatch.setattr(role_registry, "load_registry", _boom)
    assert strength_class._load_registry() == {}
    _assert_one_class_only_record(caplog, logging.WARNING)


@pytest.mark.parametrize("module", [telemetry_durations, telemetry_test_scope])
def test_observe_logs_when_instrument_body_raises(caplog, monkeypatch, module):
    _records_with(caplog)
    monkeypatch.setattr(module, "_observe", _boom)
    assert module.observe({"type": "x"}) is None
    _assert_one_class_only_record(caplog, logging.WARNING)


def test_judge_logs_when_diff_judgement_raises(caplog, monkeypatch):
    _records_with(caplog)
    monkeypatch.setattr(test_edit_judge, "_judge", _boom)
    verdict = test_edit_judge.judge("a", "b", ".", name="t")
    assert verdict.outcome == test_edit_judge.FAIL
    assert verdict.reasons == ["could not judge the diff (_Boom)"]
    _assert_one_class_only_record(caplog, logging.WARNING)


def test_current_traceparent_logs_when_tracing_lookup_fails(caplog, monkeypatch):
    _records_with(caplog)
    fake_trace = types.SimpleNamespace(get_current_span=_boom)
    fake_otel = types.ModuleType("opentelemetry")
    fake_otel.trace = fake_trace
    monkeypatch.setitem(sys.modules, "opentelemetry", fake_otel)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", fake_trace)
    assert trace_context.current_traceparent() is None
    _assert_one_class_only_record(caplog, logging.DEBUG)


def test_trip_logs_when_latch_cannot_be_written(caplog, monkeypatch, tmp_path):
    _records_with(caplog)
    monkeypatch.setattr(usage_cooldown, "USAGE_STATE_PATH", tmp_path / "usage.json", raising=False)
    monkeypatch.setattr(usage_cooldown, "_utcnow", _boom)
    assert usage_cooldown.trip(5) is None
    _assert_one_class_only_record(caplog, logging.WARNING)


def test_active_is_false_for_unreadable_state(tmp_path, monkeypatch):
    path = tmp_path / "latch.json"
    monkeypatch.setattr(usage_cooldown, "_cooldown_path", lambda credential=None: path)
    assert usage_cooldown.active() is False
    path.write_text("{not json")
    assert usage_cooldown.active() is False
    path.write_text('{"until": "garbage"}')
    assert usage_cooldown.active() is False


def test_active_propagates_unexpected_exception(tmp_path, monkeypatch):
    path = tmp_path / "latch.json"
    path.write_text('{"until": "2999-01-01T00:00:00+00:00"}')
    monkeypatch.setattr(usage_cooldown, "_cooldown_path", lambda credential=None: path)
    monkeypatch.setattr(usage_cooldown, "_utcnow", _boom)
    with pytest.raises(_Boom):
        usage_cooldown.active()


def test_journal_key_listing_logs_when_store_cannot_answer(caplog, monkeypatch):
    _records_with(caplog)
    monkeypatch.setattr(wedge_io.bound_records, "list_journal_keys", _boom)
    assert wedge_io._list_journal_keys("plan") == []
    _assert_one_class_only_record(caplog, logging.WARNING)


def test_journal_updated_at_logs_when_store_cannot_answer(caplog, monkeypatch):
    _records_with(caplog)
    store = mock.Mock()
    store.records.side_effect = _boom
    monkeypatch.setattr(wedge_io, "_store", store)
    assert wedge_io._journal_updated_at("plan", "k") is None
    _assert_one_class_only_record(caplog, logging.DEBUG)


def test_sanitize_logs_when_repo_root_is_junk(caplog, monkeypatch):
    _records_with(caplog)

    class Junk:
        def __str__(self):
            raise _Boom(SECRET)

    monkeypatch.setattr(workspace, "REPO_ROOT", Junk())
    assert workspace.sanitize_error_message("plain") == "[redacted]"
    _assert_one_class_only_record(caplog, logging.WARNING)


def test_sanitize_logs_when_path_regex_fails(caplog, monkeypatch):
    _records_with(caplog)
    broken = types.SimpleNamespace(sub=_boom)
    monkeypatch.setattr(workspace, "_PATH_TOKEN_RE", broken)
    assert workspace.sanitize_error_message("plain") == "[redacted error]"
    _assert_one_class_only_record(caplog, logging.WARNING)


def test_agent_has_exited_treats_missing_ps_as_not_exited(monkeypatch):
    monkeypatch.setattr(test_author.os, "waitpid", mock.Mock(side_effect=ChildProcessError))
    monkeypatch.setattr(test_author.os, "kill", mock.Mock(return_value=None))
    monkeypatch.setattr(test_author.subprocess, "run", mock.Mock(side_effect=FileNotFoundError))
    assert test_author._agent_has_exited(os.getpid()) is False


def test_agent_has_exited_treats_subprocess_error_as_not_exited(monkeypatch):
    monkeypatch.setattr(test_author.os, "waitpid", mock.Mock(side_effect=ChildProcessError))
    monkeypatch.setattr(test_author.os, "kill", mock.Mock(return_value=None))
    err = subprocess.SubprocessError("x")
    monkeypatch.setattr(test_author.subprocess, "run", mock.Mock(side_effect=err))
    assert test_author._agent_has_exited(os.getpid()) is False


def test_agent_has_exited_propagates_unexpected_exception(monkeypatch):
    monkeypatch.setattr(test_author.os, "waitpid", mock.Mock(side_effect=ChildProcessError))
    monkeypatch.setattr(test_author.os, "kill", mock.Mock(return_value=None))
    monkeypatch.setattr(test_author.subprocess, "run", mock.Mock(side_effect=_Boom))
    with pytest.raises(_Boom):
        test_author._agent_has_exited(os.getpid())
