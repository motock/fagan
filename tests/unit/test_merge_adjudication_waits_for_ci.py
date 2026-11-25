"""A high-risk merge is not adjudicated while its PR checks are still pending.

The overlord rules on the evidence it is given. A ruling made while CI is
``pending`` is made on incomplete evidence, so ``_adjudicate_high_risk_merge``
defers: it keeps the standing hold, calls no overlord and writes no decision
record. The caller's park branch still snapshots the ``pending`` state into
``merge_park_evidence``, so ``_readjudicate_parked_merge_hold`` re-runs the gate
once the forge reports a finished state.

A cached ``pending`` snapshot is stale by definition, so the gather reads the
forge again instead of trusting it. A cached terminal state is kept as it was.

The overlord is stubbed at ``pipeline.overlord._invoke_overlord`` and the forge
CI read at ``_ci_status_once`` (both its ``pipeline.server`` and
``pipeline.ci`` bindings). The gate, the scoring and the decisions-log writer
run for real against the ``plan_dir`` tmp directory.
"""

import json
import logging

import pytest

from pipeline import ci as ci_mod
from pipeline import merge as merge_mod
from pipeline import merge_readjudication
from pipeline import overlord as overlord_mod
from pipeline import persistence as persistence_mod

# A new test file that imports a pipeline.* module must import pipeline.server
# first (pre-existing import cycle, see tests/unit/test_triage_cap_silent_skip.py).
from pipeline import server as server_mod

PLAN = "CIWAIT-PLAN"
HOLD_REASON = "high risk held for human review"
PROCEED_REPLY = "RULING: proceed\nRATIONALE: checks are green"
PARK_REPLY = "RULING: park\nRATIONALE: not yet"
DEFERRED_LOG = "high-risk merge adjudication deferred: PR checks pending"


@pytest.fixture(autouse=True)
def _triage_execute_mode(monkeypatch):
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "execute")


class _Overlord:
    """Records each overlord call and answers with a fixed reply."""

    def __init__(self, monkeypatch, reply):
        self.prompts = []

        def fake_invoke(prompt, plan_role_config=None):
            self.prompts.append(prompt)
            return reply

        monkeypatch.setattr(overlord_mod, "_invoke_overlord", fake_invoke)


class _Forge:
    """Stands in for the single-poll CI read; ``exc`` makes the read raise."""

    def __init__(self, monkeypatch, value=None, exc=None):
        self.calls = []
        self.value = value
        self.exc = exc

        def fake_ci_status_once(branch, sha="", settings=None):
            self.calls.append(branch)
            if self.exc is not None:
                raise self.exc
            return self.value

        monkeypatch.setattr(server_mod, "_ci_status_once", fake_ci_status_once)
        monkeypatch.setattr(ci_mod, "_ci_status_once", fake_ci_status_once)

    def answer(self, value):
        self.value = value


def _full_autonomy(monkeypatch):
    monkeypatch.setattr(server_mod, "PIPELINE_AUTONOMY", "full", raising=False)
    monkeypatch.setattr(server_mod, "PIPELINE_RISK_THRESHOLD", "low", raising=False)
    monkeypatch.setattr(
        persistence_mod,
        "_plan_role_config",
        lambda plan_name: {"role": "overlord", "model": "opus"},
    )


def _records(plan_dir):
    path = plan_dir / f"{PLAN}.decisions.json"
    return json.loads(path.read_text()) if path.exists() else []


def _snapshot(state):
    return {"state": state, "error": ""}


def _high_risk_story(**overrides):
    story = {
        "key": "CIWAIT-STORY",
        "status": "pr_open",
        "parked_reason": None,
        "review_verdict": "APPROVE",
        "security_review_verdict": "APPROVE",
        "risk": "high",
        "summary": "Rewrite the auth token cache",
        "pr_url": "https://github.com/example/repo/pull/7",
        "worktree": "",
    }
    story.update(overrides)
    return story


def _adjudicate(story):
    return merge_mod._adjudicate_high_risk_merge(story, PLAN)


# --- a pending check defers the ruling ----------------------------------------


def test_pending_checks_do_not_call_the_overlord(plan_dir, monkeypatch):
    _full_autonomy(monkeypatch)
    _Forge(monkeypatch, value=_snapshot("pending"))
    overlord = _Overlord(monkeypatch, PROCEED_REPLY)

    _adjudicate(_high_risk_story())

    assert overlord.prompts == []


def test_pending_checks_return_the_standing_hold(plan_dir, monkeypatch):
    _full_autonomy(monkeypatch)
    _Forge(monkeypatch, value=_snapshot("pending"))
    _Overlord(monkeypatch, PROCEED_REPLY)

    decision = _adjudicate(_high_risk_story())

    assert decision == {"action": "park", "reason": HOLD_REASON}


def test_pending_checks_write_no_decision_record(plan_dir, monkeypatch):
    _full_autonomy(monkeypatch)
    _Forge(monkeypatch, value=_snapshot("pending"))
    _Overlord(monkeypatch, PROCEED_REPLY)

    _adjudicate(_high_risk_story())

    assert _records(plan_dir) == []


def test_two_consecutive_pending_calls_write_no_record(plan_dir, monkeypatch):
    _full_autonomy(monkeypatch)
    _Forge(monkeypatch, value=_snapshot("pending"))
    overlord = _Overlord(monkeypatch, PARK_REPLY)

    _adjudicate(_high_risk_story())
    _adjudicate(_high_risk_story())

    assert overlord.prompts == []
    assert _records(plan_dir) == []


def test_a_deferred_ruling_logs_one_info_line_naming_plan_and_story(
    plan_dir, monkeypatch, caplog
):
    _full_autonomy(monkeypatch)
    _Forge(monkeypatch, value=_snapshot("pending"))
    _Overlord(monkeypatch, PARK_REPLY)

    with caplog.at_level(logging.INFO, logger="pipeline"):
        _adjudicate(_high_risk_story())

    deferred = [r for r in caplog.records if r.getMessage() == DEFERRED_LOG]
    assert len(deferred) == 1
    assert deferred[0].levelno == logging.INFO
    assert deferred[0].plan_name == PLAN
    assert deferred[0].story_key == "CIWAIT-STORY"


# --- the deferred hold is re-adjudicated once CI finishes ---------------------


def test_a_deferred_hold_is_re_adjudicated_when_checks_pass(plan_dir, monkeypatch):
    _full_autonomy(monkeypatch)
    story = _high_risk_story()
    forge = _Forge(monkeypatch, value=_snapshot("pending"))
    overlord = _Overlord(monkeypatch, PROCEED_REPLY)

    # The park branch runs the gate while CI is pending, then snapshots it.
    assert _adjudicate(story) == {"action": "park", "reason": HOLD_REASON}
    story.update(
        status="parked",
        parked_reason=HOLD_REASON,
        merge_park_evidence={"pr_checks": story["pr_checks"]},
    )
    assert overlord.prompts == []

    # CI finishes green on the forge; the next tick re-runs the gate.
    forge.answer(_snapshot("pass"))
    decision = merge_readjudication._readjudicate_parked_merge_hold(
        PLAN, "CIWAIT-STORY", story
    )

    # The authority tier still holds a high-risk merge for a human, so the
    # ruling parks it again; what this test pins is that the ruling was made.
    assert decision["action"] == "park"
    assert decision["reason"].startswith("overlord ruled proceed")
    assert len(overlord.prompts) == 1
    assert "PR CHECKS: {'state': 'pass', 'error': ''}" in overlord.prompts[0]
    assert story["merge_park_evidence"] == {"pr_checks": _snapshot("pass")}
    assert len(_records(plan_dir)) == 1


# --- every finished state is still adjudicated as before ----------------------


@pytest.mark.parametrize(
    "state, evidence_complete",
    [("pass", True), ("fail", True), ("none", False)],
)
def test_a_finished_state_goes_to_the_overlord_and_is_recorded(
    plan_dir, monkeypatch, state, evidence_complete
):
    _full_autonomy(monkeypatch)
    _Forge(monkeypatch, value=_snapshot(state))
    overlord = _Overlord(monkeypatch, PARK_REPLY)

    _adjudicate(_high_risk_story())

    assert len(overlord.prompts) == 1
    assert f"'state': '{state}'" in overlord.prompts[0]
    assert len(_records(plan_dir)) == 1
    assert _records(plan_dir)[0]["evidence_complete"] is evidence_complete


def test_an_unreadable_ci_read_defers_without_going_to_the_overlord(
    plan_dir, monkeypatch
):
    _full_autonomy(monkeypatch)
    _Forge(monkeypatch, exc=RuntimeError("gh unavailable"))
    overlord = _Overlord(monkeypatch, PARK_REPLY)

    decision = _adjudicate(_high_risk_story())

    assert decision == {"action": "park", "reason": HOLD_REASON}
    assert overlord.prompts == []
    assert _records(plan_dir) == []


# --- a cached snapshot is trusted only when it is terminal --------------------


def test_a_cached_pending_snapshot_is_read_again(plan_dir, monkeypatch):
    _full_autonomy(monkeypatch)
    forge = _Forge(monkeypatch, value=_snapshot("pass"))
    overlord = _Overlord(monkeypatch, PROCEED_REPLY)

    _adjudicate(_high_risk_story(pr_checks=_snapshot("pending")))

    assert len(forge.calls) == 1
    assert len(overlord.prompts) == 1
    assert "PR CHECKS: {'state': 'pass', 'error': ''}" in overlord.prompts[0]


def test_a_cached_terminal_snapshot_is_not_read_again(plan_dir, monkeypatch):
    _full_autonomy(monkeypatch)
    forge = _Forge(monkeypatch, value=_snapshot("fail"))
    overlord = _Overlord(monkeypatch, PARK_REPLY)

    _adjudicate(_high_risk_story(pr_checks=_snapshot("pass")))

    assert forge.calls == []
    assert len(overlord.prompts) == 1
    assert "PR CHECKS: {'state': 'pass', 'error': ''}" in overlord.prompts[0]


def test_a_failed_refresh_of_a_cached_pending_snapshot_reports_unreadable(
    plan_dir, monkeypatch
):
    _full_autonomy(monkeypatch)
    _Forge(monkeypatch, exc=RuntimeError("gh unavailable"))
    overlord = _Overlord(monkeypatch, PARK_REPLY)

    decision = _adjudicate(_high_risk_story(pr_checks=_snapshot("pending")))

    assert decision == {"action": "park", "reason": HOLD_REASON}
    assert overlord.prompts == []
    assert _records(plan_dir) == []
