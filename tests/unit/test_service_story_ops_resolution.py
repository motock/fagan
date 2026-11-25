"""The rebrief resolution is written after the plan lock is released.

``patch_story`` answers a parked story's ruling with a ``rebrief`` resolution.
That write is an audit record, not part of the manifest change, so it must not
hold the plan's transaction while it runs: ``set_story_status`` already writes
its resolution after leaving the lock, and a rebrief should do the same.
"""

from __future__ import annotations

import threading

import pytest

from pipeline import server as p
from pipeline import service_story_ops
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _write_manifest,
    plan_dir,
)

PLAN = "ssor-plan"
KEY = "S1"


def _parked() -> dict:
    return {
        "summary": "s",
        "status": "parked",
        "agent_instructions": "Old.",
        "dispatched_at": "2026-11-19T00:00:00+00:00",
    }


def _lock_free_in_another_thread(plan_name: str) -> bool:
    """True when a different thread can take the plan's transaction right now.

    The lock is thread-reentrant, so the probe must run on its own thread: the
    holding thread would always re-acquire it.
    """
    acquired: list[bool] = []

    def probe() -> None:
        with p._store.transaction(plan_name, 0.0) as got:
            acquired.append(bool(got))

    thread = threading.Thread(target=probe)
    thread.start()
    thread.join()
    return acquired == [True]


def test_rebrief_resolution_is_written_after_the_plan_lock_is_released(
    plan_dir, monkeypatch  # noqa: F811
) -> None:
    _write_manifest(plan_dir, PLAN, {KEY: _parked()})
    lock_free_when_recorded: list[bool] = []
    real_record = service_story_ops.record_resolution_if_ruled

    def recording(plan_name, story_key, **kwargs):
        lock_free_when_recorded.append(_lock_free_in_another_thread(plan_name))
        return real_record(plan_name, story_key, **kwargs)

    monkeypatch.setattr(service_story_ops, "record_resolution_if_ruled", recording)

    result = p.patch_story(PLAN, KEY, {"agent_instructions": "New."})

    assert result["ok"] is True
    assert lock_free_when_recorded == [True]


@pytest.mark.parametrize("fields", [{"agent_instructions": "Old."}, {"model": "sonnet"}])
def test_patch_that_does_not_rewrite_the_brief_records_no_rebrief(
    plan_dir, monkeypatch, fields  # noqa: F811
) -> None:
    _write_manifest(plan_dir, PLAN, {KEY: _parked()})
    calls: list[str] = []
    monkeypatch.setattr(
        service_story_ops,
        "record_resolution_if_ruled",
        lambda plan_name, story_key, **kwargs: calls.append(kwargs["human_action"]),
    )

    result = p.patch_story(PLAN, KEY, fields)

    assert result["ok"] is True
    assert calls == []
