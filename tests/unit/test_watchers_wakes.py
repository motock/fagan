"""Tests for the scheduler's manifest-derived wakes (SQ-07).

Today only an ``agent_done`` marker starts a plan advance. Three other
finishing conditions are visible in the manifest alone and are therefore
detected here, inside the scan, without any cross-process producer:

* a detached grade wrote ``grading_result_path`` and the file now exists;
* a merge re-verify wrote ``story["merge_reverify"]["result_path"]``;
* a story another plan lists in ``blocked_by`` became ``done``.

A wake is a hint only: it publishes a ``reconcile`` event, and the advance
re-reads the manifest and decides everything from it.

``pipeline.server`` is imported first: the package has a pre-existing import
cycle and every new test module that imports ``pipeline.*`` must break it the
same way (see ``tests/unit/test_triage_cap_silent_skip.py``).
"""

from __future__ import annotations

import json
import logging
import os

import pytest

import pipeline.server  # noqa: F401 - breaks a pre-existing import cycle
from pipeline import advance_runtime, event_wiring, paths, watchers

# The wake helpers are reached through ``watchers`` (not imported by name) so
# this module always *collects*: an unwired helper must fail on an assertion,
# never on an import that hides every other test in the file.

# ---------------------------------------------------------------------------
# Fakes and helpers
# ---------------------------------------------------------------------------


class FakeBus:
    """Records published events; enough surface for ``scan_all_plans``."""

    def __init__(self) -> None:
        self.published: list[dict] = []

    def publish(self, event: dict) -> None:
        self.published.append(event)

    def reconcile_events(self) -> list[dict]:
        return [event for event in self.published if event["type"] == "reconcile"]


class RecordingBus:
    """Records subscriptions; ``build_runtime`` only needs ``subscribe``."""

    def __init__(self) -> None:
        self.subscriptions: list[tuple[str, object]] = []

    def subscribe(self, event_type, handler) -> None:
        self.subscriptions.append((event_type, handler))

    def publish(self, event) -> None:  # pragma: no cover - never used here
        raise AssertionError("build_runtime must not publish")


class FakeQueue:
    """The queue surface ``build_runtime`` registers as the wake hook."""

    def request(self, plan_name, reason=""):
        return True

    def last_advanced(self, plan_name):
        return None


class FakePool:
    """The pool surface ``build_runtime`` starts and stops."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self, timeout_s=15.0) -> None:
        pass

    def stats(self) -> dict:
        return {}


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Point ``pipeline.paths.PLAN_DIR`` at an empty directory."""
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(paths, "PLAN_DIR", directory)
    return directory


@pytest.fixture(autouse=True)
def fresh_wake_state(monkeypatch):
    """Give every test its own wake bookkeeping.

    The dedup set and the per-plan status map are process state by design (a
    daemon scans for weeks), so a test that inherited them would see another
    test's stamps.
    """
    monkeypatch.setattr(watchers, "_PUBLISHED_JOB_RESULTS", set())
    monkeypatch.setattr(watchers, "_PREVIOUS_STATUS", {})


def _write_manifest(plan_dir, plan: str, manifest: dict) -> None:
    (plan_dir / f"{plan}.manifest.json").write_text(json.dumps(manifest))


def _touch_new_mtime(path) -> None:
    """Move *path*'s mtime forward, so a same-size rewrite is a new stamp."""
    stamp = path.stat().st_mtime_ns + 1_000_000_000
    os.utime(path, ns=(stamp, stamp))


def _scan(bus: FakeBus) -> list[dict]:
    return watchers.scan_all_plans(bus)


# ---------------------------------------------------------------------------
# JOB_RESULT_FIELDS: cumulative, membership only
# ---------------------------------------------------------------------------


def test_job_result_fields_names_the_known_detached_jobs():
    # Membership only: later stories append their own paths, so asserting the
    # tuple's length or exact contents would break on their merge.
    assert ("grading_result_path",) in watchers.JOB_RESULT_FIELDS
    assert ("merge_reverify", "result_path") in watchers.JOB_RESULT_FIELDS


# ---------------------------------------------------------------------------
# A finished detached grade wakes the plan once per result stamp
# ---------------------------------------------------------------------------


def test_grading_result_wakes_once_per_result_stamp(plan_dir):
    result = plan_dir / "grade.json"
    result.write_text("{}")
    _write_manifest(
        plan_dir,
        "A",
        {"stories": {"A-1": {"status": "grading", "grading_result_path": str(result)}}},
    )
    bus = FakeBus()

    _scan(bus)

    published = bus.reconcile_events()
    assert len(published) == 1
    assert published[0]["plan"] == "A"
    assert published[0]["payload"] == {"reason": "job_result", "stories": ["A-1"]}

    # The file is still there and unchanged: nothing new to say.
    _scan(bus)
    assert len(bus.reconcile_events()) == 1

    # A rewritten result is a new stamp, so the plan is woken again.
    result.write_text("{}")
    _touch_new_mtime(result)
    _scan(bus)

    published = bus.reconcile_events()
    assert len(published) == 2
    assert published[-1]["payload"]["stories"] == ["A-1"]


def test_grading_result_missing_file_wakes_nothing(plan_dir):
    _write_manifest(
        plan_dir,
        "A",
        {
            "stories": {
                "A-1": {"status": "grading", "grading_result_path": str(plan_dir / "absent.json")}
            }
        },
    )
    bus = FakeBus()

    assert _scan(bus) == []
    assert bus.published == []


def test_job_results_ready_forgets_stories_the_manifest_dropped(plan_dir):
    result = plan_dir / "grade.json"
    result.write_text("{}")
    story = {"status": "grading", "grading_result_path": str(result)}

    assert watchers.job_results_ready("A", {"stories": {"A-1": story}}) == ["A-1"]
    # Same stamp: already woken.
    assert watchers.job_results_ready("A", {"stories": {"A-1": story}}) == []
    # The story is gone from the manifest, so its stamp is forgotten; if it
    # reappears the plan is woken again rather than staying silent forever.
    assert watchers.job_results_ready("A", {"stories": {}}) == []
    assert watchers.job_results_ready("A", {"stories": {"A-1": story}}) == ["A-1"]


# ---------------------------------------------------------------------------
# A finished merge re-verify wakes the plan the same way
# ---------------------------------------------------------------------------


def test_merge_reverify_result_wakes_once_per_result_stamp(plan_dir):
    result = plan_dir / "verify.json"
    _write_manifest(
        plan_dir,
        "A",
        {
            "stories": {
                "A-1": {
                    "status": "merge_reverify",
                    "merge_reverify": {"result_path": str(result)},
                }
            }
        },
    )
    bus = FakeBus()

    # The re-verify is still running: no result file, no wake.
    _scan(bus)
    assert bus.reconcile_events() == []

    result.write_text("{}")
    _scan(bus)

    published = bus.reconcile_events()
    assert len(published) == 1
    assert published[0]["payload"] == {"reason": "job_result", "stories": ["A-1"]}

    _scan(bus)
    assert len(bus.reconcile_events()) == 1

    result.write_text("{}")
    _touch_new_mtime(result)
    _scan(bus)
    assert len(bus.reconcile_events()) == 2


def test_non_dict_merge_reverify_is_ignored(plan_dir):
    _write_manifest(
        plan_dir,
        "A",
        {"stories": {"A-1": {"status": "merging", "merge_reverify": ["not", "a", "dict"]}}},
    )
    bus = FakeBus()

    # No exception, no wake.
    assert _scan(bus) == []
    assert bus.published == []
    assert watchers.job_results_ready("A", {"stories": {"A-1": {"merge_reverify": "oops"}}}) == []


# ---------------------------------------------------------------------------
# A blocking story that became done wakes the dependent plan
# ---------------------------------------------------------------------------


def test_blocker_became_done_wakes_the_dependent_plan(plan_dir):
    _write_manifest(plan_dir, "A", {"stories": {"A-1": {"status": "pr_open"}}})
    _write_manifest(
        plan_dir,
        "B",
        {"stories": {"B-1": {"status": "blocked", "blocked_by": ["A:A-1"]}}},
    )
    bus = FakeBus()
    _scan(bus)  # records A-1 as pr_open

    _write_manifest(plan_dir, "A", {"stories": {"A-1": {"status": "done"}}})
    _scan(bus)

    published = bus.reconcile_events()
    assert len(published) == 1
    assert published[0]["plan"] == "B"
    assert published[0]["payload"] == {"reason": "blocked_by_done"}


def test_first_sight_of_an_already_done_story_wakes_nothing(plan_dir):
    # A daemon restart must not wake every plan that depends on long-done
    # stories: with no previous status map there is no transition to report.
    _write_manifest(plan_dir, "A", {"stories": {"A-1": {"status": "done"}}})
    _write_manifest(
        plan_dir,
        "B",
        {"stories": {"B-1": {"status": "blocked", "blocked_by": ["A:A-1"]}}},
    )
    bus = FakeBus()

    _scan(bus)
    _scan(bus)

    assert bus.reconcile_events() == []


def test_newly_done_reports_only_transitions_into_done():
    current = {"stories": {"S1": {"status": "done"}, "S2": {"status": "done"}, "S3": {"status": "parked"}}}

    assert watchers.newly_done(None, current) == []
    assert watchers.newly_done({"S1": "done", "S2": "pr_open", "S3": "done"}, current) == ["S2"]


def test_newly_done_ignores_malformed_manifests_and_stories():
    assert watchers.newly_done({"S1": "pr_open"}, {}) == []
    assert watchers.newly_done({"S1": "pr_open"}, {"stories": ["not", "a", "map"]}) == []
    assert watchers.newly_done({"S1": "pr_open"}, {"stories": {"S1": "not-a-story"}}) == []


# ---------------------------------------------------------------------------
# A broken helper must never break the sweep
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("helper", ["job_results_ready", "newly_done"])
def test_a_raising_helper_logs_a_warning_and_the_scan_still_returns_events(
    plan_dir, monkeypatch, caplog, helper
):
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / ".agent_done").write_text(json.dumps({"status": "tests_passed"}))
    _write_manifest(
        plan_dir,
        "A",
        {"stories": {"A-1": {"status": "in_progress", "worktree": str(worktree)}}},
    )

    def boom(*args, **kwargs):
        raise RuntimeError("detail that must not be logged: /tmp/secret")

    monkeypatch.setattr(watchers, helper, boom)
    bus = FakeBus()

    with caplog.at_level(logging.WARNING, logger="pipeline.watchers"):
        events = _scan(bus)

    # The marker still became an event: one broken wake costs one wake.
    assert [event["type"] for event in events] == ["agent_done"]
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert warnings, "the failed wake pass must be logged at WARNING"
    text = "\n".join(record.getMessage() for record in warnings)
    assert "RuntimeError" in text
    assert "A" in text or any(getattr(r, "plan", None) == "A" for r in warnings)
    assert "detail that must not be logged" not in text
    assert "/tmp/secret" not in text


# ---------------------------------------------------------------------------
# reconcile_handler: a hint, never an inline advance
# ---------------------------------------------------------------------------


def test_reconcile_handler_hands_the_plan_to_the_wake_hook():
    woken: list[str] = []
    event_wiring.set_wake_hook(woken.append)
    try:
        assert event_wiring.reconcile_handler({"type": "reconcile", "plan": "A"}) is None
    finally:
        event_wiring.set_wake_hook(None)

    assert woken == ["A"]


def test_reconcile_handler_without_a_hook_neither_wakes_nor_advances(monkeypatch):
    advanced: list[tuple] = []
    monkeypatch.setattr(
        "pipeline.server.advance_pipeline", lambda *args, **kwargs: advanced.append((args, kwargs))
    )
    event_wiring.set_wake_hook(None)

    assert event_wiring.reconcile_handler({"type": "reconcile", "plan": "A"}) is None
    assert event_wiring.reconcile_handler({"type": "reconcile"}) is None
    assert event_wiring.reconcile_handler({}) is None

    assert advanced == []


# ---------------------------------------------------------------------------
# build_runtime subscribes the reconcile handler, in queue mode only
# ---------------------------------------------------------------------------


@pytest.fixture
def queue_mode(monkeypatch):
    """Queue mode with the queue, the pool and the worker count faked."""
    monkeypatch.setenv("PIPELINE_ADVANCE_MODE", "queue")
    from pipeline import advance_pool, advance_queue, advance_workers

    monkeypatch.setattr(advance_queue, "AdvanceQueue", FakeQueue)
    monkeypatch.setattr(advance_workers, "AdvanceWorkerPool", FakePool)
    monkeypatch.setattr(advance_pool, "_advance_workers", lambda: 1)
    monkeypatch.setattr(advance_pool, "_plan_timeout_seconds", lambda: 1.0)
    # Never leave a queue-backed hook installed for the next test.
    monkeypatch.setattr(event_wiring, "set_wake_hook", lambda hook: None)


def test_build_runtime_in_queue_mode_subscribes_reconcile(queue_mode):
    bus = RecordingBus()

    _reconcile_fn, stop_fn = advance_runtime.build_runtime(bus, lambda: {"ok": True})
    stop_fn()

    handlers = [handler for event_type, handler in bus.subscriptions if event_type == "reconcile"]
    assert handlers == [event_wiring.reconcile_handler]


def test_build_runtime_without_queue_mode_does_not_subscribe_reconcile(monkeypatch):
    monkeypatch.delenv("PIPELINE_ADVANCE_MODE", raising=False)
    bus = RecordingBus()

    reconcile_fn, _stop_fn = advance_runtime.build_runtime(bus, lambda: {"ok": True})

    assert callable(reconcile_fn)
    assert bus.subscriptions == []
