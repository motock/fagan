"""Tests for the ``escalate_model`` branch of ``execute_ruling``.

These tests target the work added to ``pipeline/triage.py`` in the
"triage execute escalate_model" story:

* module-level imports of the three escalation helpers from
  ``pipeline.escalation`` (``_auto_escalation_enabled``,
  ``_escalate_to_claude``, ``_escalate_to_local_fallback_model``);
* an ``escalate_model`` branch in ``execute_ruling`` that picks the rung the
  EXISTING ladder would already pick, in this order:

  1. local-model fallback (plan-scoped, never spends Claude);
  2. Claude escalation (gated on the operator's existing
     ``_auto_escalation_enabled()`` switch);
  3. park for human (ladder exhausted).

The implementation does not exist yet, so this file is expected to be RED
(import/attribute errors) until it lands. No real git, no real backend: the
escalation helpers and ``_notify_user`` are monkeypatched on
``pipeline.triage``.
"""

import pytest

import pipeline.triage
from pipeline import triage as triage_mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def manifest():
    return {"stories": {}}


@pytest.fixture
def manifest_path(tmp_path):
    return tmp_path / "plan.manifest.json"


@pytest.fixture
def base_story():
    """A story in the state triage hands to execute_ruling."""
    return {
        "status": "failed",
        "model": "qwen2.5-coder",
        "backend": "local",
    }


@pytest.fixture
def escalate_ruling():
    return {
        "action": "escalate_model",
        "rationale": "local model failed; try the next rung",
        "ruling": "escalate",
        "tier": "tier1",
        "risk": "low",
        "notify_user": True,
    }


@pytest.fixture
def park_ruling():
    return {
        "action": "park_for_human",
        "rationale": "no path forward",
        "ruling": "park",
        "tier": "tier1",
        "risk": "low",
        "notify_user": True,
    }


@pytest.fixture
def patched(monkeypatch):
    """Monkeypatch the four module-level names on pipeline.triage.

    Returns a dict of recording lists / flags so each test can assert on the
    calls made (or not made) without touching real git or a real backend.
    """
    state = {
        "claude_calls": [],
        "fallback_calls": [],
        "notify_calls": [],
        "auto_escalation": True,
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

    def _fake_notify(plan_name, message, *args, **kwargs):
        state["notify_calls"].append({"plan_name": plan_name, "message": message})

    # These attributes must exist on pipeline.triage for the patch to succeed.
    # Until the implementation lands they will raise AttributeError, which is
    # the correct RED state.
    monkeypatch.setattr(triage_mod, "_escalate_to_claude", _fake_escalate_to_claude)
    monkeypatch.setattr(
        triage_mod, "_escalate_to_local_fallback_model", _fake_escalate_to_local_fallback_model
    )
    monkeypatch.setattr(triage_mod, "_auto_escalation_enabled", _fake_auto_escalation_enabled)
    monkeypatch.setattr(triage_mod, "_notify_user", _fake_notify)
    return state


# ---------------------------------------------------------------------------
# Module-level imports exist on pipeline.triage
# ---------------------------------------------------------------------------

class TestImports:
    def test_escalation_helpers_imported_into_triage(self):
        """The three names must be importable from pipeline.triage."""
        assert hasattr(triage_mod, "_auto_escalation_enabled")
        assert hasattr(triage_mod, "_escalate_to_claude")
        assert hasattr(triage_mod, "_escalate_to_local_fallback_model")


# ---------------------------------------------------------------------------
# Branch 1: local-model fallback
# ---------------------------------------------------------------------------

class TestLocalFallbackBranch:
    def test_fallback_taken_when_conditions_met(
        self, base_story, escalate_ruling, manifest, manifest_path, patched
    ):
        manifest["local_model_fallback"] = "devstral"
        # model != fallback, tried_fallback_model absent, backend local.

        result = pipeline.triage.execute_ruling(
            "cap1", "S1", base_story, escalate_ruling, manifest, manifest_path
        )

        assert result == "escalate_model"
        # Exactly one fallback call, no Claude call.
        assert len(patched["fallback_calls"]) == 1
        assert patched["claude_calls"] == []
        call = patched["fallback_calls"][0]
        assert call["fallback_model"] == "devstral"
        assert call["plan_name"] == "cap1"
        assert call["story_key"] == "S1"
        assert call["manifest_path"] == manifest_path
        # The SAME manifest dict object is passed through (no stale copy).
        assert call["manifest"] is manifest

    def test_fallback_not_taken_when_model_already_is_fallback(
        self, base_story, escalate_ruling, manifest, manifest_path, patched
    ):
        manifest["local_model_fallback"] = "devstral"
        base_story["model"] = "devstral"

        result = pipeline.triage.execute_ruling(
            "cap1", "S1", base_story, escalate_ruling, manifest, manifest_path
        )

        # Falls through to Claude (auto escalation on by default in fixture).
        assert result == "escalate_model"
        assert patched["fallback_calls"] == []
        assert len(patched["claude_calls"]) == 1

    def test_fallback_not_taken_when_tried_fallback_model_true(
        self, base_story, escalate_ruling, manifest, manifest_path, patched
    ):
        manifest["local_model_fallback"] = "devstral"
        base_story["tried_fallback_model"] = True

        result = pipeline.triage.execute_ruling(
            "cap1", "S1", base_story, escalate_ruling, manifest, manifest_path
        )

        assert result == "escalate_model"
        assert patched["fallback_calls"] == []
        assert len(patched["claude_calls"]) == 1

    def test_fallback_not_taken_when_backend_is_claude(
        self, base_story, escalate_ruling, manifest, manifest_path, patched
    ):
        """A Claude story's model field must not trigger the local-fallback
        branch even when a fallback is configured."""
        manifest["local_model_fallback"] = "devstral"
        base_story["backend"] = "claude"

        result = pipeline.triage.execute_ruling(
            "cap1", "S1", base_story, escalate_ruling, manifest, manifest_path
        )

        # Local-fallback branch is NOT taken.
        assert patched["fallback_calls"] == []
        # Falls through to Claude escalation (auto on).
        assert result == "escalate_model"
        assert len(patched["claude_calls"]) == 1

    def test_fallback_not_taken_when_fallback_unset(
        self, base_story, escalate_ruling, manifest, manifest_path, patched
    ):
        # No local_model_fallback key at all.
        result = pipeline.triage.execute_ruling(
            "cap1", "S1", base_story, escalate_ruling, manifest, manifest_path
        )

        assert patched["fallback_calls"] == []
        assert len(patched["claude_calls"]) == 1


# ---------------------------------------------------------------------------
# Branch 2: Claude escalation
# ---------------------------------------------------------------------------

class TestClaudeBranch:
    def test_claude_taken_when_fallback_already_tried(
        self, base_story, escalate_ruling, manifest, manifest_path, patched
    ):
        base_story["tried_fallback_model"] = True
        patched["auto_escalation"] = True

        result = pipeline.truling = None  # noqa: F841 (guard against typo)
        result = pipeline.triage.execute_ruling(
            "cap1", "S1", base_story, escalate_ruling, manifest, manifest_path
        )

        assert result == "escalate_model"
        assert len(patched["claude_calls"]) == 1
        assert patched["fallback_calls"] == []
        call = patched["claude_calls"][0]
        assert call["plan_name"] == "cap1"
        assert call["story_key"] == "S1"
        assert call["manifest_path"] == manifest_path
        assert call["manifest"] is manifest

    def test_claude_not_taken_when_auto_escalation_disabled(
        self, base_story, escalate_ruling, manifest, manifest_path, patched
    ):
        patched["auto_escalation"] = False
        # No fallback configured.

        result = pipeline.triage.execute_ruling(
            "cap1", "S1", base_story, escalate_ruling, manifest, manifest_path
        )

        assert result == "park_for_human"
        assert patched["claude_calls"] == []
        assert patched["fallback_calls"] == []
        assert base_story["status"] == "parked"

    def test_claude_not_taken_when_already_escalated(
        self, base_story, escalate_ruling, manifest, manifest_path, patched
    ):
        base_story["escalated"] = True
        patched["auto_escalation"] = True
        # No fallback configured.

        result = pipeline.triage.execute_ruling(
            "cap1", "S1", base_story, escalate_ruling, manifest, manifest_path
        )

        assert result == "park_for_human"
        assert patched["claude_calls"] == []
        assert patched["fallback_calls"] == []
        assert base_story["status"] == "parked"


# ---------------------------------------------------------------------------
# Branch 3: park for human (ladder exhausted)
# ---------------------------------------------------------------------------

class TestParkBranch:
    def test_no_fallback_and_auto_disabled_parks(
        self, base_story, escalate_ruling, manifest, manifest_path, patched
    ):
        patched["auto_escalation"] = False

        result = pipeline.triage.execute_ruling(
            "cap1", "S1", base_story, escalate_ruling, manifest, manifest_path
        )

        assert result == "park_for_human"
        assert patched["claude_calls"] == []
        assert patched["fallback_calls"] == []
        assert base_story["status"] == "parked"
        # The reason must mention escalate_model was ruled.
        assert "escalate_model" in base_story["parked_reason"]

    def test_already_escalated_no_fallback_parks(
        self, base_story, escalate_ruling, manifest, manifest_path, patched
    ):
        base_story["escalated"] = True
        patched["auto_escalation"] = True

        result = pipeline.triage.execute_ruling(
            "cap1", "S1", base_story, escalate_ruling, manifest, manifest_path
        )

        assert result == "park_for_human"
        assert patched["claude_calls"] == []
        assert patched["fallback_calls"] == []
        assert base_story["status"] == "parked"

    def test_park_reason_names_exhausted_ladder(
        self, base_story, escalate_ruling, manifest, manifest_path, patched
    ):
        base_story["escalated"] = True
        base_story["tried_fallback_model"] = True
        manifest["local_model_fallback"] = "devstral"
        patched["auto_escalation"] = True

        result = pipeline.triage.execute_ruling(
            "cap1", "S1", base_story, escalate_ruling, manifest, manifest_path
        )

        assert result == "park_for_human"
        assert base_story["status"] == "parked"
        reason = base_story["parked_reason"]
        assert "escalate_model" in reason
        assert "exhausted" in reason.lower()


# ---------------------------------------------------------------------------
# Error handling: escalation helper raises -> park, never propagate
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_claude_raising_oserror_parks_without_propagating(
        self, base_story, escalate_ruling, manifest, manifest_path, monkeypatch
    ):
        # Patch notify so _park doesn't hit a real backend.
        monkeypatch.setattr(triage_mod, "_notify_user", lambda *a, **k: None)
        monkeypatch.setattr(
            triage_mod, "_auto_escalation_enabled", lambda: True
        )
        monkeypatch.setattr(
            triage_mod, "_escalate_to_local_fallback_model", lambda *a, **k: None
        )

        def _raising_claude(manifest, plan_name, story_key, manifest_path):
            raise OSError("git worktree remove failed")

        monkeypatch.setattr(triage_mod, "_escalate_to_claude", _raising_claude)

        # No fallback configured so we reach the Claude branch.
        result = pipeline.triage.execute_ruling(
            "cap1", "S1", base_story, escalate_ruling, manifest, manifest_path
        )

        assert result == "park_for_human"
        assert base_story["status"] == "parked"
        # The reason must name the exception TYPE.
        assert "OSError" in base_story["parked_reason"]

    def test_fallback_raising_oserror_parks_without_propagating(
        self, base_story, escalate_ruling, manifest, manifest_path, monkeypatch
    ):
        monkeypatch.setattr(triage_mod, "_notify_user", lambda *a, **k: None)
        monkeypatch.setattr(
            triage_mod, "_auto_escalation_enabled", lambda: True
        )
        monkeypatch.setattr(
            triage_mod, "_escalate_to_claude", lambda *a, **k: None
        )

        def _raising_fallback(
            manifest, plan_name, story_key, manifest_path, fallback_model
        ):
            raise OSError("git branch -D failed")

        monkeypatch.setattr(
            triage_mod, "_escalate_to_local_fallback_model", _raising_fallback
        )

        manifest["local_model_fallback"] = "devstral"
        result = pipeline.triage.execute_ruling(
            "cap1", "S1", base_story, escalate_ruling, manifest, manifest_path
        )

        assert result == "park_for_human"
        assert base_story["status"] == "parked"
        assert "OSError" in base_story["parked_reason"]


# ---------------------------------------------------------------------------
# Regression: park_for_human action still parks (no escalation)
# ---------------------------------------------------------------------------

class TestParkForHumanRegression:
    def test_park_for_human_action_takes_plain_park_path(
        self, base_story, park_ruling, manifest, manifest_path, patched
    ):
        manifest["local_model_fallback"] = "devstral"
        patched["auto_escalation"] = True

        result = pipeline.triage.execute_ruling(
            "cap1", "S1", base_story, park_ruling, manifest, manifest_path
        )

        assert result == "park_for_human"
        assert base_story["status"] == "parked"
        assert patched["claude_calls"] == []
        assert patched["fallback_calls"] == []
        # The plain park reason should not claim an escalate_model ruling.
        assert "escalate_model" not in base_story.get("parked_reason", "")