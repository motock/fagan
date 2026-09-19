"""Tests for the structured notification emitted by ``escalate_model`` rulings.

Story LD90-W0-03.  ``execute_ruling``'s ``escalate_model`` branch calls
``_escalate_to_local_fallback_model`` / ``_escalate_to_claude`` and returns
without notifying anyone, even though every other caller of those helpers
notifies itself.  This file pins the two notices that close that gap:

* the local-fallback rung emits ``event="model_fallback"``;
* the Claude rung emits ``event="escalated"``.

Both notices are attributable to their story (``story_key`` plus the story's
``correlation_id`` when it has one), and a notifier failure must never turn a
completed escalation into a park.

The same concern covers triage park notices: ``_park`` and the
``park_for_human`` re-park of an existing reason notify directly, so both are
stamped with the story attribution too.

No real backend: the escalation helpers, ``_auto_escalation_enabled``,
``_escalation_label`` and ``_notify_user`` are monkeypatched on
``pipeline.triage``.
"""

import pytest

# ``pipeline.triage`` imports ``pipeline.build_detect``, which imports
# ``pipeline.server``, which imports ``run_triage_sweep`` back out of
# ``pipeline.triage``.  Importing ``pipeline.triage`` first therefore trips a
# pre-existing circular import.  Importing ``pipeline.server`` first resolves
# the cycle, so this module can be run standalone as well as as part of the
# whole suite.
import pipeline.server  # noqa: F401  (imported for its side effect: breaks the cycle)
from pipeline import triage as triage_mod

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def manifest_path(tmp_path):
    return tmp_path / "plan.manifest.json"


@pytest.fixture
def escalate_ruling():
    return {"action": "escalate_model", "rationale": "needs a bigger model"}


@pytest.fixture
def patched(monkeypatch):
    """Monkeypatch the escalation helpers and ``_notify_user`` on
    ``pipeline.triage``.

    Returns a dict of recording lists so each test can assert on the calls
    made (or not made) without touching real git or a real backend.
    """
    state = {
        "claude_calls": [],
        "fallback_calls": [],
        "notify_calls": [],
        "auto_escalation": True,
        "notify_raises": None,
    }

    def _fake_escalate_to_claude(manifest, plan_name, story_key, manifest_path):
        state["claude_calls"].append(
            {
                "manifest": manifest,
                "plan_name": plan_name,
                "story_key": story_key,
                "manifest_path": manifest_path,
            }
        )

    def _fake_escalate_to_local_fallback_model(
        manifest, plan_name, story_key, manifest_path, fallback_model
    ):
        state["fallback_calls"].append(
            {
                "manifest": manifest,
                "plan_name": plan_name,
                "story_key": story_key,
                "manifest_path": manifest_path,
                "fallback_model": fallback_model,
            }
        )

    def _fake_auto_escalation_enabled():
        return state["auto_escalation"]

    def _fake_escalation_label():
        return "Claude"

    def _fake_notify(plan_name, message, *args, **kwargs):
        state["notify_calls"].append(
            {"plan_name": plan_name, "message": message, "args": args, "kwargs": kwargs}
        )
        if state["notify_raises"] is not None:
            raise state["notify_raises"]

    monkeypatch.setattr(triage_mod, "_escalate_to_claude", _fake_escalate_to_claude)
    monkeypatch.setattr(
        triage_mod, "_escalate_to_local_fallback_model", _fake_escalate_to_local_fallback_model
    )
    monkeypatch.setattr(triage_mod, "_auto_escalation_enabled", _fake_auto_escalation_enabled)
    monkeypatch.setattr(triage_mod, "_escalation_label", _fake_escalation_label)
    monkeypatch.setattr(triage_mod, "_notify_user", _fake_notify)
    return state


def _events(state):
    return [call["kwargs"].get("event") for call in state["notify_calls"]]


def _calls_with_event(state, event):
    return [call for call in state["notify_calls"] if call["kwargs"].get("event") == event]


# ---------------------------------------------------------------------------
# escalate_model notices
# ---------------------------------------------------------------------------

class TestEscalateModelNotice:
    def test_fallback_rung_notifies_model_fallback(
        self, patched, manifest_path, escalate_ruling
    ):
        manifest = {"local_model_fallback": "m2", "stories": {}}
        story = {
            "status": "failed",
            "backend": "local",
            "model": "m1",
            "correlation_id": "cid-3",
        }

        result = triage_mod.execute_ruling(
            "plan-x", "story-1", story, escalate_ruling, manifest, manifest_path
        )

        assert result == "escalate_model"
        assert len(patched["fallback_calls"]) == 1
        assert len(patched["notify_calls"]) == 1
        call = patched["notify_calls"][0]
        assert call["kwargs"]["event"] == "model_fallback"
        assert call["kwargs"]["story_key"] == "story-1"
        assert call["kwargs"]["correlation_id"] == "cid-3"
        assert "fallback model m2" in call["message"]

    def test_claude_rung_notifies_escalated(
        self, patched, manifest_path, escalate_ruling
    ):
        manifest = {"stories": {}}
        story = {"status": "failed", "backend": "local", "model": "m1"}

        result = triage_mod.execute_ruling(
            "plan-x", "story-1", story, escalate_ruling, manifest, manifest_path
        )

        assert result == "escalate_model"
        assert len(patched["claude_calls"]) == 1
        assert len(patched["notify_calls"]) == 1
        call = patched["notify_calls"][0]
        assert call["kwargs"]["event"] == "escalated"
        assert call["kwargs"]["story_key"] == "story-1"
        assert "correlation_id" not in call["kwargs"]
        assert "escalating to Claude" in call["message"]

    def test_notifier_failure_does_not_park(
        self, patched, manifest_path, escalate_ruling
    ):
        patched["notify_raises"] = RuntimeError("backend down")
        manifest = {"local_model_fallback": "m2", "stories": {}}
        story = {
            "status": "failed",
            "backend": "local",
            "model": "m1",
            "correlation_id": "cid-3",
        }

        result = triage_mod.execute_ruling(
            "plan-x", "story-1", story, escalate_ruling, manifest, manifest_path
        )

        assert result == "escalate_model"
        assert story["status"] != "parked"

    def test_ladder_exhausted_parks_without_escalation_event(
        self, patched, manifest_path, escalate_ruling
    ):
        patched["auto_escalation"] = False
        manifest = {"stories": {}}
        story = {"status": "failed", "backend": "local", "model": "m1"}

        result = triage_mod.execute_ruling(
            "plan-x", "story-1", story, escalate_ruling, manifest, manifest_path
        )

        assert result == "park_for_human"
        assert "escalated" not in _events(patched)
        assert "model_fallback" not in _events(patched)
        assert len(_calls_with_event(patched, "story_parked")) == 1


# ---------------------------------------------------------------------------
# park attribution
# ---------------------------------------------------------------------------

class TestParkAttribution:
    def test_park_stamps_story_key_and_correlation_id(self, patched):
        story = {"status": "failed", "correlation_id": "cid-5"}

        result = triage_mod._park("plan-x", "story-1", story, "reason")

        assert result == "park_for_human"
        assert len(patched["notify_calls"]) == 1
        call = patched["notify_calls"][0]
        assert call["kwargs"]["event"] == "story_parked"
        assert call["kwargs"]["story_key"] == "story-1"
        assert call["kwargs"]["correlation_id"] == "cid-5"

    def test_park_omits_correlation_id_when_absent(self, patched):
        story = {"status": "failed"}

        triage_mod._park("plan-x", "story-1", story, "reason")

        assert len(patched["notify_calls"]) == 1
        call = patched["notify_calls"][0]
        assert call["kwargs"]["story_key"] == "story-1"
        assert "correlation_id" not in call["kwargs"]

    def test_repark_stamps_story_key_and_correlation_id(
        self, patched, manifest_path
    ):
        story = {"status": "parked", "parked_reason": "held", "correlation_id": "cid-6"}
        ruling = {"action": "park_for_human", "rationale": "r"}

        result = triage_mod.execute_ruling(
            "plan-x", "story-1", story, ruling, {"stories": {}}, manifest_path
        )

        assert result == "park_for_human"
        assert story["parked_reason"] == "held"
        assert len(patched["notify_calls"]) == 1
        call = patched["notify_calls"][0]
        assert call["kwargs"]["event"] == "story_parked"
        assert call["kwargs"]["story_key"] == "story-1"
        assert call["kwargs"]["correlation_id"] == "cid-6"
