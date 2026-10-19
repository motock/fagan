"""RGN-1: the plan-level "review backend gated" notice fires once per gated
episode, not once per scheduler tick.

Before this change ``pipeline/advance.py``'s ``else:`` branch called
``_notify_user`` on every tick in which the review backend was gated and any
story was ``tests_passed``. ``_notify_user`` records a ``dedup_key`` but does
not dedupe (dedupe is caller-side, as in ``pipeline/wedge_io.py``'s
``_WEDGE_LAST_EMIT``), so the identical message repeated every tick:
overlord-failure-triage accumulated 159 identical records in 3.2 hours (51 in
one hour) and M56-1 emitted 4 in three minutes on 2026-10-19.

The transition state lives in ``pipeline.review_gate_notice`` and is keyed on
the PLAN only - never on the reason, because a gate reason can embed a
measured value (free memory, a usage percentage) that changes every tick.

The integration tests below drive the REAL tick body
(``_advance_pipeline_locked_impl``) under a REAL plan lock, following the
harness in ``tests/unit/test_advance_review_releases_plan_lock.py`` - only the
model-call-heavy collaborators are stubbed.
"""

from __future__ import annotations

import json

import pytest

import pipeline.advance as advance_module
import pipeline.config as config_module
from pipeline import concurrency, review_gate_notice
from pipeline.review_gate_notice import (
    clear_review_gate_notice,
    should_notify_review_gate,
)

# Deliberately unique: no other test uses this plan name, so the module-level
# transition state cannot leak between test modules.
_PLAN = "review-gate-transition-plan"

_GATED_PREFIX = "Review backend gated"


@pytest.fixture(autouse=True)
def _reset_review_gate_notice_state():
    """The transition state is module-level (in-process, like wedge_io's
    cooldown). Clear it before and after every test so a failure in one test
    cannot arm or disarm another."""
    review_gate_notice._NOTIFIED.clear()
    yield
    review_gate_notice._NOTIFIED.clear()


# ---------------------------------------------------------------------------
# Unit tests: the transition primitive


def test_first_call_notifies_second_call_does_not():
    assert should_notify_review_gate(_PLAN) is True
    assert should_notify_review_gate(_PLAN) is False


def test_clear_rearms_the_notice():
    assert should_notify_review_gate(_PLAN) is True
    assert should_notify_review_gate(_PLAN) is False

    clear_review_gate_notice(_PLAN)

    assert should_notify_review_gate(_PLAN) is True


def test_plans_are_independent():
    other = f"{_PLAN}-other"

    assert should_notify_review_gate(_PLAN) is True
    assert should_notify_review_gate(other) is True
    assert should_notify_review_gate(_PLAN) is False
    assert should_notify_review_gate(other) is False


def test_clearing_one_plan_does_not_rearm_another():
    other = f"{_PLAN}-other"

    assert should_notify_review_gate(_PLAN) is True
    assert should_notify_review_gate(other) is True

    clear_review_gate_notice(_PLAN)

    assert should_notify_review_gate(other) is False
    assert should_notify_review_gate(_PLAN) is True


def test_clear_on_unknown_plan_is_a_noop():
    clear_review_gate_notice("never-seen-plan")
    assert should_notify_review_gate("never-seen-plan") is True


# ---------------------------------------------------------------------------
# Integration harness (mirrors tests/unit/test_advance_review_releases_plan_lock.py)


@pytest.fixture(autouse=True)
def _isolate_plan_lock(tmp_path, monkeypatch):
    """Point the real flock at tmp_path and give every test a clean held-set."""
    monkeypatch.setattr(concurrency, "PLAN_DIR", tmp_path)
    concurrency._held_plan_locks().clear()
    yield
    concurrency._held_plan_locks().clear()


class _FakeBackend:
    def get_backend(self, *_args, **_kwargs):
        return self

    def resource_status(self, **_kwargs):
        return {"ok": True}


class _FakeAutonomy:
    def __init__(self, value="gated"):
        self._value_ = value

    def _value(self):
        return self._value_


class _FakeStore:
    def __init__(self, path):
        self.path = path

    def manifest_path(self, plan_name):
        return self.path

    def get_manifest(self, plan_name):
        return json.loads(self.path.read_text())


def _story(status="tests_passed", **extra):
    story = {
        "key": "s1",
        "title": "story s1",
        "status": status,
        "risk": "low",
        "dispatch_attempts": 0,
    }
    story.update(extra)
    return story


def _write_manifest(tmp_path, stories):
    manifest = {"name": _PLAN, "paused": False, "stories": stories}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return path


def _seed(monkeypatch, path, notifications, review_ok, review_reason=""):
    """Wire every seam the tick body touches, matching the conventions in
    tests/unit/test_advance_review_releases_plan_lock.py. ``notifications``
    accumulates every ``_notify_user`` call as ``(message, kwargs)``."""
    monkeypatch.setattr(advance_module, "_store", _FakeStore(path))
    monkeypatch.setattr(advance_module, "backend", _FakeBackend())

    def _role_resource_ok(role, **_kwargs):
        if role == "dispatch":
            return True, ""
        return review_ok, review_reason

    monkeypatch.setattr(advance_module, "_role_resource_ok", _role_resource_ok)
    monkeypatch.setattr(
        advance_module, "_count_on_device_in_progress_agents", lambda: 0
    )
    monkeypatch.setattr(
        advance_module, "PIPELINE_AUTONOMY", _FakeAutonomy("gated"), raising=True
    )
    monkeypatch.setattr(advance_module, "_adjudicate_merges", lambda *a, **k: None)

    def _notify_user(_plan_name, message, **kwargs):
        notifications.append((message, kwargs))

    monkeypatch.setattr(advance_module, "_notify_user", _notify_user)
    monkeypatch.setattr(advance_module, "interrupt_story", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(
        advance_module, "check_story_status", lambda *a, **k: {"ok": True}
    )
    monkeypatch.setattr(advance_module, "dispatch_story", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(
        config_module, "PIPELINE_MAX_DISPATCH_PER_TICK", 1, raising=False
    )
    monkeypatch.setattr(
        advance_module, "PIPELINE_MAX_DISPATCH_PER_TICK", 1, raising=False
    )
    monkeypatch.setattr(config_module, "MAX_CONCURRENT_AGENTS", 8, raising=False)
    monkeypatch.setattr(advance_module, "MAX_CONCURRENT_AGENTS", 8, raising=False)
    monkeypatch.setattr(config_module, "DISPATCH_MAX_ATTEMPTS", 3, raising=False)
    monkeypatch.setattr(advance_module, "DISPATCH_MAX_ATTEMPTS", 3, raising=False)


def _run_tick_holding_lock(plan_name=_PLAN):
    """Simulate the production entry point: hold the real plan lock for the
    whole tick, exactly as PipelineService.advance_pipeline does via
    _store.transaction(plan_name)."""
    with concurrency._plan_lock(plan_name) as acquired:
        assert acquired, "test setup: failed to acquire the plan lock"
        return advance_module._advance_pipeline_locked_impl(plan_name)


def _gate_notices(notifications):
    return [n for n in notifications if n[0].startswith(_GATED_PREFIX)]


def _forbid_review_story(monkeypatch):
    def _must_not_be_called(plan_name, key):
        raise AssertionError("review_story must not be called in this scenario")

    monkeypatch.setattr(advance_module, "review_story", _must_not_be_called)


# ---------------------------------------------------------------------------
# Integration tests: one notice per gated episode


def test_two_consecutive_gated_ticks_emit_one_notice(monkeypatch, tmp_path):
    """The defect: every gated tick re-notified. The second tick's reason
    DIFFERS (a real reason embeds a measured value), so an implementation
    keyed on plan+reason would still emit twice and fail here."""
    path = _write_manifest(tmp_path, {"s1": _story()})
    notifications = []
    _forbid_review_story(monkeypatch)

    _seed(monkeypatch, path, notifications, review_ok=False, review_reason="free_mem=512MB")
    first = _run_tick_holding_lock()

    _seed(monkeypatch, path, notifications, review_ok=False, review_reason="free_mem=480MB")
    second = _run_tick_holding_lock()

    assert len(_gate_notices(notifications)) == 1
    # The tick summary still records the pause on BOTH ticks.
    assert "review_paused" in first["notify"]
    assert "review_paused" in second["notify"]


def test_notice_rearms_after_review_proceeds(monkeypatch, tmp_path):
    """gated -> review available (nothing to review) -> gated again is two
    separate episodes, so exactly two notices."""
    path = _write_manifest(tmp_path, {"s1": _story()})
    notifications = []
    _forbid_review_story(monkeypatch)

    _seed(monkeypatch, path, notifications, review_ok=False, review_reason="free_mem=512MB")
    _run_tick_holding_lock()

    # Review is available this tick and there is no tests_passed work, so the
    # ``if review_ok:`` branch runs (clearing the episode) but calls no
    # review_story.
    path = _write_manifest(tmp_path, {"s1": _story(status="pr_open")})
    _seed(monkeypatch, path, notifications, review_ok=True)
    _run_tick_holding_lock()

    path = _write_manifest(tmp_path, {"s1": _story()})
    _seed(monkeypatch, path, notifications, review_ok=False, review_reason="free_mem=500MB")
    _run_tick_holding_lock()

    assert len(_gate_notices(notifications)) == 2


def test_gate_notice_carries_plan_and_reason_dedup_key(monkeypatch, tmp_path):
    path = _write_manifest(tmp_path, {"s1": _story()})
    notifications = []
    _forbid_review_story(monkeypatch)

    reason = "free_mem=512MB"
    _seed(monkeypatch, path, notifications, review_ok=False, review_reason=reason)
    _run_tick_holding_lock()

    notices = _gate_notices(notifications)
    assert len(notices) == 1
    assert notices[0][1].get("dedup_key") == f"review_gate:{_PLAN}:{reason}"
