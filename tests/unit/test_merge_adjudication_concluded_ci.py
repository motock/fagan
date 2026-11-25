"""A high-risk merge is ruled on only when its CI read says so.

``success`` (``pass``) and ``failure`` (``fail``) conclude: the ruling is made
and its record carries ``evidence_complete: true``. ``none`` and ``cancelled``
will not change by waiting, so they rule too, with ``evidence_complete: false``.
Every other read - pending, unreadable, a snapshot with no state, or a failed
gather that left no state at all - defers the ruling: the standing hold stands,
no overlord call is made and no decision record is written.
``merge_readjudication`` re-rules once the state changes.

The overlord is stubbed at ``pipeline.overlord._invoke_overlord`` and the forge
CI read at ``_ci_status_once`` (both its ``pipeline.server`` and ``pipeline.ci``
bindings). The gate, the scoring and the decisions-log writer run for real
against the ``plan_dir`` tmp directory.
"""

import json
import logging

import pytest

from pipeline import ci as ci_mod
from pipeline import merge as merge_mod
from pipeline import merge_adjudication_evidence as evidence
from pipeline import merge_local_checks, merge_readjudication
from pipeline import overlord as overlord_mod
from pipeline import persistence as persistence_mod

# A new test file that imports a pipeline.* module must import pipeline.server
# first (pre-existing import cycle, see tests/unit/test_triage_cap_silent_skip.py).
from pipeline import server as server_mod

PLAN = "CICONCL-PLAN"
HOLD_REASON = "high risk held for human review"
PROCEED_REPLY = "RULING: proceed\nRATIONALE: checks are green"
PARK_REPLY = "RULING: park\nRATIONALE: not yet"
DEFERRED_LOG = "high-risk merge adjudication deferred: PR checks pending"
SKIPPED_WORDING = (
    "host tests are skipped by deployment policy; PR CI is the test evidence"
)


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
        "key": "CICONCL-STORY",
        "status": "pr_open",
        "parked_reason": None,
        "review_verdict": "APPROVE",
        "security_review_verdict": "APPROVE",
        "risk": "high",
        "summary": "Rewrite the auth token cache",
        "pr_url": "https://github.com/example/repo/pull/9",
        "worktree": "",
    }
    story.update(overrides)
    return story


def _adjudicate(story):
    return merge_mod._adjudicate_high_risk_merge(story, PLAN)


# --- the one "can the overlord rule" rule and the concluded flag -------------


@pytest.mark.parametrize("state", ["pass", "fail"])
def test_success_and_failure_are_concluded(state):
    assert evidence.ci_evidence_complete(_snapshot(state)) is True
    assert evidence.ci_defers_ruling(_snapshot(state)) is False


@pytest.mark.parametrize("state", ["none", "cancelled"])
def test_none_and_cancelled_rule_but_are_not_concluded(state):
    assert evidence.ci_evidence_complete(_snapshot(state)) is False
    assert evidence.ci_defers_ruling(_snapshot(state)) is False


@pytest.mark.parametrize(
    "checks",
    [
        _snapshot("pending"),
        _snapshot("unreadable"),
        _snapshot("queued"),
        {"error": "no state reported"},
        {"state": None, "error": ""},
        {},
        None,
        "pass",
    ],
)
def test_any_other_state_defers_the_ruling(checks):
    assert evidence.ci_defers_ruling(checks) is True
    assert evidence.ci_evidence_complete(checks) is False


def test_story_evidence_is_concluded_only_from_its_pr_checks():
    assert evidence.evidence_complete(_high_risk_story(pr_checks=_snapshot("pass")))
    assert not evidence.evidence_complete(_high_risk_story())


def test_gate_disabled_pass_is_named_as_disabled_in_the_evidence_note():
    checks = {"state": "pass", "error": "CI gate disabled"}

    assert evidence.ci_evidence_complete(checks) is True
    assert evidence.ci_evidence_note(checks) == (
        "CI gate disabled for this repository; no CI result was checked"
    )


def test_a_plain_pass_has_no_evidence_note():
    assert evidence.ci_evidence_note(_snapshot("pass")) == ""


@pytest.mark.parametrize(
    "state, note",
    [("none", "no CI configured for this repository"), ("cancelled", "CI was cancelled")],
)
def test_none_and_cancelled_carry_their_evidence_note(state, note):
    assert evidence.ci_evidence_note(_snapshot(state)) == note


# --- reproduction: non-concluded CI shapes must defer, not rule ---------------
# Before the fix each of these reached the overlord and wrote a record.


@pytest.mark.parametrize(
    "value",
    [
        _snapshot("unreadable"),
        _snapshot("pending"),
        {"error": "state missing"},
        {"state": "queued", "error": ""},
    ],
    ids=["unreadable", "pending", "missing-state", "queued"],
)
def test_non_concluded_read_defers_without_a_record(plan_dir, monkeypatch, value):
    _full_autonomy(monkeypatch)
    _Forge(monkeypatch, value=value)
    overlord = _Overlord(monkeypatch, PROCEED_REPLY)

    decision = _adjudicate(_high_risk_story())

    assert decision == {"action": "park", "reason": HOLD_REASON}
    assert overlord.prompts == []
    assert _records(plan_dir) == []


def test_a_failed_gather_defers_without_a_record(plan_dir, monkeypatch):
    _full_autonomy(monkeypatch)
    _Forge(monkeypatch, exc=RuntimeError("gh unavailable"))
    overlord = _Overlord(monkeypatch, PROCEED_REPLY)

    decision = _adjudicate(_high_risk_story())

    assert decision == {"action": "park", "reason": HOLD_REASON}
    assert overlord.prompts == []
    assert _records(plan_dir) == []


def test_a_cached_deferred_snapshot_is_read_again(plan_dir, monkeypatch):
    _full_autonomy(monkeypatch)
    forge = _Forge(monkeypatch, value=_snapshot("pass"))
    overlord = _Overlord(monkeypatch, PARK_REPLY)

    _adjudicate(_high_risk_story(pr_checks=_snapshot("unreadable")))

    assert len(forge.calls) == 1
    assert len(overlord.prompts) == 1
    assert len(_records(plan_dir)) == 1


def test_a_deferred_ruling_logs_the_deferral_and_the_ci_state(
    plan_dir, monkeypatch, caplog
):
    _full_autonomy(monkeypatch)
    _Forge(monkeypatch, value=_snapshot("pending"))
    _Overlord(monkeypatch, PROCEED_REPLY)

    with caplog.at_level(logging.INFO, logger="pipeline"):
        _adjudicate(_high_risk_story())

    deferred = [r for r in caplog.records if r.getMessage() == DEFERRED_LOG]
    assert len(deferred) == 1
    assert deferred[0].ci_state == "pending"


# --- success and failure both rule --------------------------------------------


@pytest.mark.parametrize("state", ["pass", "fail"])
def test_concluded_ci_is_ruled_on_and_recorded(plan_dir, monkeypatch, state):
    _full_autonomy(monkeypatch)
    _Forge(monkeypatch, value=_snapshot(state))
    overlord = _Overlord(monkeypatch, PARK_REPLY)

    _adjudicate(_high_risk_story())

    assert len(overlord.prompts) == 1
    assert f"'state': '{state}'" in overlord.prompts[0]
    records = _records(plan_dir)
    assert len(records) == 1
    assert records[0]["ruling"] == "park"


@pytest.mark.parametrize("state", ["pass", "fail"])
def test_a_concluded_record_carries_evidence_complete_true(
    plan_dir, monkeypatch, state
):
    _full_autonomy(monkeypatch)
    _Forge(monkeypatch, value=_snapshot(state))
    _Overlord(monkeypatch, PARK_REPLY)

    _adjudicate(_high_risk_story())

    assert _records(plan_dir)[0]["evidence_complete"] is True


# --- a deferred hold is re-adjudicated once CI concludes ----------------------


def _park_as_the_gate_does(story, value):
    story.update(
        status="parked",
        parked_reason=HOLD_REASON,
        merge_park_evidence={"pr_checks": value},
    )


def test_an_unreadable_hold_is_re_adjudicated_once_ci_concludes(plan_dir, monkeypatch):
    _full_autonomy(monkeypatch)
    story = _high_risk_story()
    forge = _Forge(monkeypatch, value=_snapshot("unreadable"))
    overlord = _Overlord(monkeypatch, PROCEED_REPLY)
    assert _adjudicate(story) == {"action": "park", "reason": HOLD_REASON}
    _park_as_the_gate_does(story, story["pr_checks"])
    assert overlord.prompts == []

    forge.answer(_snapshot("fail"))
    merge_readjudication._readjudicate_parked_merge_hold(
        PLAN, "CICONCL-STORY", story
    )

    assert len(overlord.prompts) == 1
    assert "PR CHECKS: {'state': 'fail', 'error': ''}" in overlord.prompts[0]
    assert len(_records(plan_dir)) == 1


def test_an_unchanged_unreadable_hold_does_not_re_invoke_the_overlord(
    plan_dir, monkeypatch
):
    _full_autonomy(monkeypatch)
    story = _high_risk_story()
    _Forge(monkeypatch, value=_snapshot("unreadable"))
    overlord = _Overlord(monkeypatch, PROCEED_REPLY)
    _adjudicate(story)
    _park_as_the_gate_does(story, story["pr_checks"])

    merge_readjudication._readjudicate_parked_merge_hold(
        PLAN, "CICONCL-STORY", story
    )

    assert overlord.prompts == []
    assert _records(plan_dir) == []


def test_a_gather_failure_hold_is_re_adjudicated_once_ci_concludes(
    plan_dir, monkeypatch
):
    _full_autonomy(monkeypatch)
    story = _high_risk_story()
    forge = _Forge(monkeypatch, exc=RuntimeError("gh unavailable"))
    overlord = _Overlord(monkeypatch, PARK_REPLY)
    assert _adjudicate(story) == {"action": "park", "reason": HOLD_REASON}
    _park_as_the_gate_does(story, story.get("pr_checks"))

    forge.exc = None
    forge.answer(_snapshot("pass"))
    merge_readjudication._readjudicate_parked_merge_hold(
        PLAN, "CICONCL-STORY", story
    )

    assert len(overlord.prompts) == 1
    assert len(_records(plan_dir)) == 1
    assert _records(plan_dir)[0]["evidence_complete"] is True


# --- skipped host tests are said to be skipped by policy ----------------------

_SCOPED_HOST_SKIPPED = {"test_scope": "scoped", "host_tests": "skipped"}


def test_policy_skipped_host_tests_are_named_in_the_local_checks_line():
    story = {
        "test_scope_resolution": _SCOPED_HOST_SKIPPED,
        "local_checks": {
            "sha": "abcdef1234567",
            "state": "pass",
            "checks": {"tests": "skipped", "lint": "pass", "build": "pass"},
        },
    }

    line = merge_local_checks.render_local_checks_line(story)

    assert SKIPPED_WORDING in line
    assert line.startswith("LOCAL CHECKS: tests skipped (")


def test_policy_skipped_host_tests_are_named_in_the_overlord_prompt(
    plan_dir, monkeypatch
):
    _full_autonomy(monkeypatch)
    _Forge(monkeypatch, value=_snapshot("pass"))
    overlord = _Overlord(monkeypatch, PARK_REPLY)
    story = _high_risk_story(
        test_scope_resolution=_SCOPED_HOST_SKIPPED,
        local_checks={
            "sha": "abcdef1234567",
            "state": "pass",
            "checks": {"tests": "skipped", "lint": "pass", "build": "pass"},
        },
    )

    _adjudicate(story)

    assert SKIPPED_WORDING in overlord.prompts[0]


@pytest.mark.parametrize(
    "resolution",
    [
        None,
        {"test_scope": "full", "host_tests": "run"},
        {"test_scope": "scoped", "host_tests": "run"},
    ],
    ids=["no-resolution", "full-scope", "scoped-host-tests-run"],
)
def test_skipped_wording_is_absent_when_policy_did_not_skip_host_tests(resolution):
    story = {
        "local_checks": {
            "sha": "abcdef1234567",
            "state": "pass",
            "checks": {"tests": "skipped", "lint": "pass", "build": "pass"},
        },
    }
    if resolution is not None:
        story["test_scope_resolution"] = resolution

    line = merge_local_checks.render_local_checks_line(story)

    assert SKIPPED_WORDING not in line
    assert line.startswith("LOCAL CHECKS: tests skipped / lint pass")


# --- no-CI and cancelled rule now, with the evidence saying why -----------------


@pytest.mark.parametrize(
    "state, note",
    [("none", "no CI configured for this repository"), ("cancelled", "CI was cancelled")],
)
def test_none_and_cancelled_are_ruled_and_recorded_as_incomplete(
    plan_dir, monkeypatch, state, note
):
    _full_autonomy(monkeypatch)
    _Forge(monkeypatch, value=_snapshot(state))
    overlord = _Overlord(monkeypatch, PARK_REPLY)

    _adjudicate(_high_risk_story())

    assert len(overlord.prompts) == 1
    assert f"CI NOTE: {note}" in overlord.prompts[0]
    records = _records(plan_dir)
    assert len(records) == 1
    assert records[0]["evidence_complete"] is False


def test_gate_disabled_pass_is_ruled_with_the_disabled_note(plan_dir, monkeypatch):
    _full_autonomy(monkeypatch)
    _Forge(monkeypatch, value={"state": "pass", "error": "CI gate disabled"})
    overlord = _Overlord(monkeypatch, PARK_REPLY)

    _adjudicate(_high_risk_story())

    assert "CI NOTE: CI gate disabled for this repository" in overlord.prompts[0]
    assert _records(plan_dir)[0]["evidence_complete"] is True


def test_a_plain_concluded_pass_has_no_ci_note_line(plan_dir, monkeypatch):
    _full_autonomy(monkeypatch)
    _Forge(monkeypatch, value=_snapshot("pass"))
    overlord = _Overlord(monkeypatch, PARK_REPLY)

    _adjudicate(_high_risk_story())

    assert "CI NOTE:" not in overlord.prompts[0]
