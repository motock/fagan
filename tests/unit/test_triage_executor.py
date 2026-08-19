"""Tests for the triage executor slice (park path + autonomy-mode dispatch).

These tests target the functions added to pipeline/triage.py in the
"triage executor" story:

* ``_park`` - parks a story and notifies the user, fail-closed on
  notification failure.
* ``execute_ruling`` - in THIS slice every action (recognized or not) parks.
* ``_apply_ruling_for_mode`` - reads PIPELINE_AUTONOMY and either withholds
  action (dry-run) or delegates to execute_ruling (gated/full).

The implementation does not exist yet, so this file is expected to be RED
(import/attribute errors) until it lands.
"""

import pytest

from pipeline import triage as triage_mod
import pipeline.triage


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def story():
    return {
        "status": "failed",
        "parked_reason": "",
    }


@pytest.fixture
def ruling():
    return {
        "ruling": "park it",
        "tier": "tier1",
        "risk": "low",
        "rationale": "the suite is red and the fix is non-obvious",
        "notify_user": True,
        "action": "park_for_human",
    }


@pytest.fixture
def manifest():
    return {}


@pytest.fixture
def manifest_path(tmp_path):
    return tmp_path / "manifest.json"


@pytest.fixture(autouse=True)
def _patch_notify(monkeypatch):
    """Replace pipeline.triage._notify_user with a stub that records calls.

    Never make a real backend call from these tests.
    """
    calls = []

    def _fake_notify(plan_name, message, *args, **kwargs):
        calls.append({"plan_name": plan_name, "message": message, "args": args, "kwargs": kwargs})

    monkeypatch.setattr(triage_mod, "_notify_user", _fake_notify)
    return calls


# ---------------------------------------------------------------------------
# _park
# ---------------------------------------------------------------------------

class TestPark:
    def test_park_sets_status_reason_and_returns(self, story):
        result = pipeline.triage._park("cap1", "S1", story, "no path forward")

        assert result == "park_for_human"
        assert story["status"] == "parked"
        assert story["parked_reason"] == "no path forward"

    def test_park_notifies_user(self, story, _patch_notify):
        pipeline.triage._park("cap1", "S1", story, "no path forward")

        assert len(_patch_notify) == 1
        call = _patch_notify[0]
        assert call["plan_name"] == "cap1"
        assert "S1" in call["message"]
        assert "no path forward" in call["message"]

    def test_park_survives_notification_failure(self, story, monkeypatch):
        def _raising_notify(*args, **kwargs):
            raise RuntimeError("backend down")

        monkeypatch.setattr(triage_mod, "_notify_user", _raising_notify)

        # Must not propagate, and must still park.
        result = pipeline.triage._park("cap1", "S1", story, "boom")

        assert result == "park_for_human"
        assert story["status"] == "parked"
        assert story["parked_reason"] == "boom"

    def test_park_overwrites_existing_reason(self, story):
        story["parked_reason"] = "old reason"

        pipeline.triage._park("cap1", "S1", story, "new reason")

        assert story["parked_reason"] == "new reason"


# ---------------------------------------------------------------------------
# execute_ruling
# ---------------------------------------------------------------------------

class TestExecuteRuling:
    def test_escalate_model_action_parks(self, story, ruling, manifest, manifest_path):
        ruling["action"] = "escalate_model"

        result = pipeline.triage.execute_ruling(
            "cap1", "S1", story, ruling, manifest, manifest_path
        )

        assert result == "park_for_human"
        assert story["status"] == "parked"
        # parked_reason must name the ruled action.
        assert "escalate_model" in story["parked_reason"]

    def test_unrecognized_action_parks_fail_closed(self, story, ruling, manifest, manifest_path):
        ruling["action"] = "do_something_weird"

        result = pipeline.triage.execute_ruling(
            "cap1", "S1", story, ruling, manifest, manifest_path
        )

        assert result == "park_for_human"
        assert story["status"] == "parked"
        assert "do_something_weird" in story["parked_reason"]

    def test_park_for_human_action_parks(self, story, ruling, manifest, manifest_path):
        ruling["action"] = "park_for_human"

        result = pipeline.triage.execute_ruling(
            "cap1", "S1", story, ruling, manifest, manifest_path
        )

        assert result == "park_for_human"
        assert story["status"] == "parked"

    def test_rationale_truncated_to_300_chars(self, story, ruling, manifest, manifest_path):
        long_rationale = "x" * 1000
        ruling["rationale"] = long_rationale
        ruling["action"] = "escalate_model"

        pipeline.triage.execute_ruling(
            "cap1", "S1", story, ruling, manifest, manifest_path
        )

        reason = story["parked_reason"]
        # The rationale portion must be truncated to at most 300 characters.
        # The reason names the action plus the (truncated) rationale.
        assert "escalate_model" in reason
        assert "x" * 300 in reason
        assert "x" * 301 not in reason

    def test_parked_reason_names_ruled_action_and_rationale(self, story, ruling, manifest, manifest_path):
        ruling["action"] = "escalate_model"
        ruling["rationale"] = "the build is broken"

        pipeline.triage.execute_ruling(
            "cap1", "S1", story, ruling, manifest, manifest_path
        )

        reason = story["parked_reason"]
        assert "escalate_model" in reason
        assert "the build is broken" in reason


# ---------------------------------------------------------------------------
# _apply_ruling_for_mode
# ---------------------------------------------------------------------------

class TestApplyRulingForMode:
    def test_dry_run_returns_dry_run_and_leaves_status(
        self, monkeypatch, story, ruling, manifest, manifest_path
    ):
        import pipeline.server as server

        monkeypatch.setattr(server, "PIPELINE_AUTONOMY", "dry-run")
        original_status = story["status"]

        # execute_ruling must NEVER be called in dry-run.
        def _must_not_call(*args, **kwargs):
            pytest.fail("execute_ruling must not be called in dry-run mode")

        monkeypatch.setattr(triage_mod, "execute_ruling", _must_not_call)

        result = pipeline.triage._apply_ruling_for_mode(
            "cap1", "S1", story, ruling, manifest, manifest_path
        )

        assert result == "dry-run"
        assert story["status"] == original_status

    def test_dry_run_notifies_user_with_recommendation(
        self, monkeypatch, story, ruling, manifest, manifest_path, _patch_notify
    ):
        import pipeline.server as server

        monkeypatch.setattr(server, "PIPELINE_AUTONOMY", "dry-run")
        ruling["action"] = "escalate_model"
        ruling["rationale"] = "the suite is red"

        pipeline.triage._apply_ruling_for_mode(
            "cap1", "S1", story, ruling, manifest, manifest_path
        )

        # At least one notification carrying the recommended action + rationale.
        assert len(_patch_notify) >= 1
        msg = _patch_notify[-1]["message"]
        assert "escalate_model" in msg
        assert "the suite is red" in msg

    def test_full_calls_execute_ruling_once_and_returns_its_value(
        self, monkeypatch, story, ruling, manifest, manifest_path
    ):
        import pipeline.server as server

        monkeypatch.setattr(server, "PIPELINE_AUTONOMY", "full")

        calls = []

        def _stub_execute(plan_name, story_key, s, r, m, mp):
            calls.append((plan_name, story_key, s, r, m, mp))
            return "park_for_human"

        monkeypatch.setattr(triage_mod, "execute_ruling", _stub_execute)

        result = pipeline.triage._apply_ruling_for_mode(
            "cap1", "S1", story, ruling, manifest, manifest_path
        )

        assert len(calls) == 1
        assert calls[0][0] == "cap1"
        assert calls[0][1] == "S1"
        assert result == "park_for_human"

    def test_gated_calls_execute_ruling(
        self, monkeypatch, story, ruling, manifest, manifest_path
    ):
        import pipeline.server as server

        monkeypatch.setattr(server, "PIPELINE_AUTONOMY", "gated")

        calls = []

        def _stub_execute(plan_name, story_key, s, r, m, mp):
            calls.append((plan_name, story_key))
            return "park_for_human"

        monkeypatch.setattr(triage_mod, "execute_ruling", _stub_execute)

        result = pipeline.triage._apply_ruling_for_mode(
            "cap1", "S1", story, ruling, manifest, manifest_path
        )

        assert len(calls) == 1
        assert result == "park_for_human"

    def test_dry_run_does_not_change_status_when_already_parked(
        self, monkeypatch, story, ruling, manifest, manifest_path
    ):
        import pipeline.server as server

        monkeypatch.setattr(server, "PIPELINE_AUTONOMY", "dry-run")
        story["status"] = "parked"
        story["parked_reason"] = "pre-existing"

        def _must_not_call(*args, **kwargs):
            pytest.fail("execute_ruling must not be called in dry-run mode")

        monkeypatch.setattr(triage_mod, "execute_ruling", _must_not_call)

        result = pipeline.triage._apply_ruling_for_mode(
            "cap1", "S1", story, ruling, manifest, manifest_path
        )

        assert result == "dry-run"
        assert story["status"] == "parked"
        assert story["parked_reason"] == "pre-existing"


# ---------------------------------------------------------------------------
# __all__ membership
# ---------------------------------------------------------------------------

class TestAllMembership:
    def test_execute_ruling_in_all(self):
        assert "execute_ruling" in pipeline.triage.__all__

    def test_apply_ruling_for_mode_in_all(self):
        assert "_apply_ruling_for_mode" in pipeline.triage.__all__