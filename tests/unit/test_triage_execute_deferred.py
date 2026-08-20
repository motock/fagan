"""Tests for the deferred-action branch of ``execute_ruling``.

These tests target the work added to ``pipeline/triage.py`` in the
"triage execute deferred actions" story (E6/E7 of
docs/plans/OVERLORD_FAILURE_TRIAGE_PLAN.md):

* a module-level constant ``DEFERRED_ACTIONS = frozenset({"split_story",
  "repo_issue"})`` with a comment naming E6/E7 as where they will be
  implemented;
* a branch in ``execute_ruling`` for an action in ``DEFERRED_ACTIONS``,
  evaluated BEFORE the fall-through park, that:
    - sets ``story["triage_deferred_action"] = action``;
    - parks via ``_park`` with a reason naming the action as not implemented
      yet and parked for a human;
    - additionally calls ``_notify_user(plan_name, ...)`` with a message
      containing the story key, the action name, and the ruling's rationale
      truncated to 300 characters;
    - returns ``"park_for_human"``;
* an unrecognized action still takes the plain park path with a reason naming
  it as unrecognized and must NOT set ``triage_deferred_action``;
* ``DEFERRED_ACTIONS`` is added to ``__all__``.

The implementation does not exist yet, so this file is expected to be RED
(import/attribute errors) until it lands. No real backend: ``_notify_user``
and the escalation helpers are monkeypatched on ``pipeline.triage``.
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
def split_ruling():
    return {
        "action": "split_story",
        "rationale": "the story is too large for one implementer at this tier",
        "ruling": "split",
        "tier": "tier1",
        "risk": "low",
        "notify_user": True,
    }


@pytest.fixture
def repo_ruling():
    return {
        "action": "repo_issue",
        "rationale": "the failure is environmental, not the story's fault",
        "ruling": "file issue",
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
        state["notify_calls"].append(
            {"plan_name": plan_name, "message": message, "args": args, "kwargs": kwargs}
        )

    # These attributes must exist on pipeline.triage for the patch to succeed.
    monkeypatch.setattr(triage_mod, "_escalate_to_claude", _fake_escalate_to_claude)
    monkeypatch.setattr(
        triage_mod, "_escalate_to_local_fallback_model", _fake_escalate_to_local_fallback_model
    )
    monkeypatch.setattr(triage_mod, "_auto_escalation_enabled", _fake_auto_escalation_enabled)
    monkeypatch.setattr(triage_mod, "_notify_user", _fake_notify)
    return state


# ---------------------------------------------------------------------------
# Module-level constant DEFERRED_ACTIONS
# ---------------------------------------------------------------------------

class TestDeferredActionsConstant:
    def test_deferred_actions_exists(self):
        assert hasattr(triage_mod, "DEFERRED_ACTIONS")

    def test_deferred_actions_is_frozenset(self):
        assert isinstance(triage_mod.DEFERRED_ACTIONS, frozenset)

    def test_deferred_actions_contains_split_and_repo(self):
        assert "split_story" in triage_mod.DEFERRED_ACTIONS
        assert "repo_issue" in triage_mod.DEFERRED_ACTIONS

    def test_deferred_actions_contains_only_split_and_repo(self):
        assert triage_mod.DEFERRED_ACTIONS == frozenset({"split_story", "repo_issue"})

    def test_deferred_actions_in_all(self):
        assert "DEFERRED_ACTIONS" in pipeline.triage.__all__

    def test_deferred_actions_comment_names_e6_e7(self):
        """The constant must carry a comment naming E6/E7 of the triage plan
        as where split_story / repo_issue will be implemented."""
        source = pipeline.triage.__file__
        with open(source, encoding="utf-8") as f:
            text = f.read()
        # The comment must reference both E6 and E7 and the plan doc.
        assert "E6" in text
        assert "E7" in text
        assert "OVERLORD_FAILURE_TRIAGE_PLAN" in text


# ---------------------------------------------------------------------------
# split_story branch
# ---------------------------------------------------------------------------

class TestSplitStoryBranch:
    def test_split_story_parks_and_marks_deferred(
        self, base_story, split_ruling, manifest, manifest_path, patched
    ):
        result = pipeline.triage.execute_ruling(
            "cap1", "S1", base_story, split_ruling, manifest, manifest_path
        )

        assert result == "park_for_human"
        assert base_story["status"] == "parked"
        assert base_story["triage_deferred_action"] == "split_story"

    def test_split_story_notifies_with_key_and_action(
        self, base_story, split_ruling, manifest, manifest_path, patched
    ):
        pipeline.triage.execute_ruling(
            "cap1", "S1", base_story, split_ruling, manifest, manifest_path
        )

        # At least one notification message contains both the story key and
        # the action name.
        assert len(patched["notify_calls"]) >= 1
        msgs = [c["message"] for c in patched["notify_calls"]]
        assert any("S1" in m and "split_story" in m for m in msgs)

    def test_split_story_parked_reason_names_action_not_implemented(
        self, base_story, split_ruling, manifest, manifest_path, patched
    ):
        pipeline.triage.execute_ruling(
            "cap1", "S1", base_story, split_ruling, manifest, manifest_path
        )

        reason = base_story["parked_reason"]
        assert "split_story" in reason
        assert "not implemented" in reason.lower()
        assert "human" in reason.lower()

    def test_split_story_does_not_escalate(
        self, base_story, split_ruling, manifest, manifest_path, patched
    ):
        pipeline.triage.execute_ruling(
            "cap1", "S1", base_story, split_ruling, manifest, manifest_path
        )

        assert patched["claude_calls"] == []
        assert patched["fallback_calls"] == []


# ---------------------------------------------------------------------------
# repo_issue branch
# ---------------------------------------------------------------------------

class TestRepoIssueBranch:
    def test_repo_issue_parks_and_marks_deferred(
        self, base_story, repo_ruling, manifest, manifest_path, patched
    ):
        result = pipeline.triage.execute_ruling(
            "cap1", "S2", base_story, repo_ruling, manifest, manifest_path
        )

        assert result == "park_for_human"
        assert base_story["status"] == "parked"
        assert base_story["triage_deferred_action"] == "repo_issue"

    def test_repo_issue_notifies_with_key_and_action(
        self, base_story, repo_ruling, manifest, manifest_path, patched
    ):
        pipeline.triage.execute_ruling(
            "cap1", "S2", base_story, repo_ruling, manifest, manifest_path
        )

        assert len(patched["notify_calls"]) >= 1
        msgs = [c["message"] for c in patched["notify_calls"]]
        assert any("S2" in m and "repo_issue" in m for m in msgs)

    def test_repo_issue_parked_reason_names_action_not_implemented(
        self, base_story, repo_ruling, manifest, manifest_path, patched
    ):
        pipeline.triage.execute_ruling(
            "cap1", "S2", base_story, repo_ruling, manifest, manifest_path
        )

        reason = base_story["parked_reason"]
        assert "repo_issue" in reason
        assert "not implemented" in reason.lower()
        assert "human" in reason.lower()

    def test_repo_issue_does_not_escalate(
        self, base_story, repo_ruling, manifest, manifest_path, patched
    ):
        pipeline.triage.execute_ruling(
            "cap1", "S2", base_story, repo_ruling, manifest, manifest_path
        )

        assert patched["claude_calls"] == []
        assert patched["fallback_calls"] == []


# ---------------------------------------------------------------------------
# Rationale truncation in the notification
# ---------------------------------------------------------------------------

class TestRationaleTruncation:
    def test_notification_contains_first_part_of_rationale(
        self, base_story, split_ruling, manifest, manifest_path, patched
    ):
        split_ruling["rationale"] = "the story is too large for one implementer"
        pipeline.triage.execute_ruling(
            "cap1", "S1", base_story, split_ruling, manifest, manifest_path
        )

        msgs = [c["message"] for c in patched["notify_calls"]]
        # At least one notification carries the leading text of the rationale.
        assert any("the story is too large" in m for m in msgs)

    def test_5000_char_rationale_truncated_to_300_in_notification(
        self, base_story, split_ruling, manifest, manifest_path, patched
    ):
        long_rationale = "A" * 5000
        split_ruling["rationale"] = long_rationale
        pipeline.triage.execute_ruling(
            "cap1", "S1", base_story, split_ruling, manifest, manifest_path
        )

        msgs = [c["message"] for c in patched["notify_calls"]]
        # The deferred-action notification must contain the truncated
        # rationale: 300 chars present, the 301st absent.
        assert any(("A" * 300) in m for m in msgs)
        assert all(("A" * 301) not in m for m in msgs)


# ---------------------------------------------------------------------------
# park_for_human action: no deferred-action marker
# ---------------------------------------------------------------------------

class TestParkForHumanNoDeferred:
    def test_park_for_human_does_not_set_deferred_action(
        self, base_story, park_ruling, manifest, manifest_path, patched
    ):
        result = pipeline.triage.execute_ruling(
            "cap1", "S1", base_story, park_ruling, manifest, manifest_path
        )

        assert result == "park_for_human"
        assert base_story["status"] == "parked"
        assert "triage_deferred_action" not in base_story


# ---------------------------------------------------------------------------
# escalate_model regression: still takes the escalation branch
# ---------------------------------------------------------------------------

class TestEscalateModelRegression:
    def test_escalate_model_still_escalates(
        self, base_story, escalate_ruling, manifest, manifest_path, patched
    ):
        # No fallback configured; auto escalation on -> Claude branch taken.
        result = pipeline.triage.execute_ruling(
            "cap1", "S1", base_story, escalate_ruling, manifest, manifest_path
        )

        assert result == "escalate_model"
        assert len(patched["claude_calls"]) == 1
        assert patched["fallback_calls"] == []
        # No deferred-action marker set on an escalation.
        assert "triage_deferred_action" not in base_story


# ---------------------------------------------------------------------------
# Unrecognized action: plain park, no deferred-action marker, no exception
# ---------------------------------------------------------------------------

class TestUnrecognizedAction:
    def test_bogus_action_parks_without_exception(
        self, base_story, manifest, manifest_path, patched
    ):
        bogus_ruling = {
            "action": "launch_missiles",
            "rationale": "because we can",
            "ruling": "boom",
            "tier": "tier1",
            "risk": "low",
            "notify_user": True,
        }

        # Must not raise.
        result = pipeline.triage.execute_ruling(
            "cap1", "S1", base_story, bogus_ruling, manifest, manifest_path
        )

        assert result == "park_for_human"
        assert base_story["status"] == "parked"
        assert "triage_deferred_action" not in base_story

    def test_bogus_action_reason_names_it_unrecognized(
        self, base_story, manifest, manifest_path, patched
    ):
        bogus_ruling = {
            "action": "launch_missiles",
            "rationale": "because we can",
            "ruling": "boom",
            "tier": "tier1",
            "risk": "low",
            "notify_user": True,
        }

        pipeline.triage.execute_ruling(
            "cap1", "S1", base_story, bogus_ruling, manifest, manifest_path
        )

        reason = base_story["parked_reason"]
        assert "launch_missiles" in reason


# ---------------------------------------------------------------------------
# Notification failure does not propagate
# ---------------------------------------------------------------------------

class TestNotificationFailure:
    def test_notify_user_raising_oserror_still_parks(
        self, base_story, split_ruling, manifest, manifest_path, monkeypatch
    ):
        # Escalation helpers stubbed so they are never reached.
        monkeypatch.setattr(triage_mod, "_auto_escalation_enabled", lambda: True)
        monkeypatch.setattr(triage_mod, "_escalate_to_claude", lambda *a, **k: None)
        monkeypatch.setattr(
            triage_mod, "_escalate_to_local_fallback_model", lambda *a, **k: None
        )

        def _raising_notify(*args, **kwargs):
            raise OSError("notification backend down")

        monkeypatch.setattr(triage_mod, "_notify_user", _raising_notify)

        # Must not propagate; story still parked and marked deferred.
        result = pipeline.triage.execute_ruling(
            "cap1", "S1", base_story, split_ruling, manifest, manifest_path
        )

        assert result == "park_for_human"
        assert base_story["status"] == "parked"
        assert base_story["triage_deferred_action"] == "split_story"