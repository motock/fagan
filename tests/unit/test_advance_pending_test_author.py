"""Scheduler tick handling of a story pending on a detached test-author phase.

A story holding a dict ``test_author_phase`` with a live int ``pid`` still has
a local model loaded, so it occupies an on-device slot even though its status
is not ``in_progress``. dispatch_story returning ``{"pending": "test_author"}``
is reported under ``summary["pending_phase"]``, not ``"dispatched"``.
"""

# ruff: noqa: F811  (pytest fixture imported then used as a test parameter)

import os

import pytest

from pipeline import advance as _advance
from pipeline import server as p
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _write_manifest,
    plan_dir,
)
from tests.unit.test_advance_cloud_slot_exempt import (
    _CLOUD_TAG,
    _DEVICE_TAG,
    _stub_tick_seams,
)

_PENDING = {"ok": True, "pending": "test_author"}


def _stub_dispatch(monkeypatch, result=_PENDING, per_tick_cap=0):
    # Default 0 (no per-tick cap) so slot accounting, not the per-tick cap,
    # decides whether a second story dispatches.
    monkeypatch.setattr(_advance, "PIPELINE_MAX_DISPATCH_PER_TICK", per_tick_cap)
    calls = []
    monkeypatch.setattr(
        p, "dispatch_story", lambda plan, key: calls.append(key) or result
    )
    return calls


def _dead_pid():
    pid = 2**22 + 12345
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        except PermissionError:
            pass
        pid += 1


def _pending_story(pid, **extra):
    return {
        "summary": "pending", "status": "todo", "model": _DEVICE_TAG,
        "dependencies": [], "test_author_phase": {"pid": pid}, **extra,
    }


def test_pending_result_is_reported_under_pending_phase_not_dispatched(
    plan_dir, monkeypatch,
):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _stub_tick_seams(monkeypatch, max_agents=0)
    _stub_dispatch(monkeypatch)
    _write_manifest(plan_dir, "pta_report", {
        "T1": {"summary": "one", "status": "todo", "dependencies": []},
    })

    result = p.advance_pipeline("pta_report")

    assert "T1" in result["pending_phase"]
    assert "T1" not in result["dispatched"]


def test_pending_result_still_counts_toward_per_tick_cap(plan_dir, monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _stub_tick_seams(monkeypatch, max_agents=0)
    calls = _stub_dispatch(monkeypatch, per_tick_cap=1)
    _write_manifest(plan_dir, "pta_cap", {
        "T1": {"summary": "one", "status": "todo", "dependencies": []},
        "T2": {"summary": "two", "status": "todo", "dependencies": []},
    })

    p.advance_pipeline("pta_cap")

    assert len(calls) == 1


def test_live_pending_story_blocks_second_on_device_dispatch(
    plan_dir, monkeypatch,
):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _stub_tick_seams(monkeypatch, max_agents=1)
    calls = _stub_dispatch(monkeypatch)
    _write_manifest(plan_dir, "pta_block", {
        "P1": {**_pending_story(os.getpid()), "status": "blocked_for_test"},
        "L1": {"summary": "device", "status": "todo", "model": _DEVICE_TAG,
               "dependencies": []},
    })

    p.advance_pipeline("pta_block")

    assert "L1" not in calls


def test_pending_story_is_redispatched_even_though_it_fills_the_only_slot(
    plan_dir, monkeypatch,
):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _stub_tick_seams(monkeypatch, max_agents=1)
    calls = _stub_dispatch(monkeypatch)
    _write_manifest(plan_dir, "pta_redispatch", {
        "P1": _pending_story(os.getpid()),
    })

    p.advance_pipeline("pta_redispatch")

    assert "P1" in calls


def test_pending_story_does_not_consume_a_second_slot_when_redispatched(
    plan_dir, monkeypatch,
):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _stub_tick_seams(monkeypatch, max_agents=2)
    calls = _stub_dispatch(monkeypatch)
    _write_manifest(plan_dir, "pta_noddouble", {
        "P1": _pending_story(os.getpid()),
        "L1": {"summary": "device", "status": "todo", "model": _DEVICE_TAG,
               "dependencies": []},
    })

    p.advance_pipeline("pta_noddouble")

    assert "L1" in calls


def test_dead_pid_test_author_phase_frees_the_slot(plan_dir, monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _stub_tick_seams(monkeypatch, max_agents=1)
    calls = _stub_dispatch(monkeypatch)
    _write_manifest(plan_dir, "pta_dead", {
        "P1": {**_pending_story(_dead_pid()), "status": "blocked_for_test"},
        "L1": {"summary": "device", "status": "todo", "model": _DEVICE_TAG,
               "dependencies": []},
    })

    p.advance_pipeline("pta_dead")

    assert "L1" in calls


@pytest.mark.parametrize(
    "phase",
    ["junk", ["pid"], None, {}, {"pid": "123"}, {"pid": True}, {"pid": 0}],
)
def test_malformed_phase_is_ignored_and_frees_the_slot(
    plan_dir, monkeypatch, phase,
):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _stub_tick_seams(monkeypatch, max_agents=1)
    calls = _stub_dispatch(monkeypatch)
    _write_manifest(plan_dir, "pta_malformed", {
        "P1": {"summary": "p", "status": "blocked_for_test", "model": _DEVICE_TAG,
               "dependencies": [], "test_author_phase": phase},
        "L1": {"summary": "device", "status": "todo", "model": _DEVICE_TAG,
               "dependencies": []},
    })

    p.advance_pipeline("pta_malformed")

    assert "L1" in calls


def test_cloud_pending_story_does_not_consume_a_slot(plan_dir, monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _stub_tick_seams(monkeypatch, max_agents=1)
    calls = _stub_dispatch(monkeypatch)
    _write_manifest(plan_dir, "pta_cloud", {
        "P1": {**_pending_story(os.getpid(), model=_CLOUD_TAG),
               "status": "blocked_for_test"},
        "L1": {"summary": "device", "status": "todo", "model": _DEVICE_TAG,
               "dependencies": []},
    })

    p.advance_pipeline("pta_cloud")

    assert "L1" in calls


def test_backend_blind_counter_counts_live_phase_once(plan_dir):
    _write_manifest(plan_dir, "pta_count", {
        "P1": {**_pending_story(os.getpid()), "status": "in_progress",
               "pid": os.getpid()},
        "P2": _pending_story(os.getpid()),
        "P3": _pending_story(_dead_pid()),
    })

    assert p._count_in_progress_agents() == 2
