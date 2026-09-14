"""OPSA-9: high-risk merge holds are adjudicated by the overlord in full autonomy.

Pre-OPSA-9, ``_merge_decision`` parked every story whose risk ranked >= high
with reason ``high risk held for human review`` regardless of autonomy. In
``PIPELINE_AUTONOMY == 'full'`` that hold now becomes an overlord adjudication:
the overlord is asked to rule ``proceed`` or ``park`` with a rationale in the
parseable format ``RULING: <proceed|park>`` / ``RATIONALE: <one line>``, the
ruling is recorded in the decisions log, and the merge gate follows it.
Unparseable or absent overlord output fails closed to the original hold.

Dry-run and gated behaviour is byte-identical to pre-OPSA-9: same park action,
same reason string, and the overlord is never invoked.

The overlord invocation is stubbed at the true boundary
(``pipeline.overlord._invoke_overlord``) because ``pipeline/merge.py`` resolves
it lazily inside the function body; the autonomy knobs are patched on
``pipeline.server`` for the same reason.
"""

import copy
import re
from pathlib import Path

import pytest

from pipeline import merge as merge_mod
from pipeline import overlord as overlord_mod
from pipeline import persistence as persistence_mod
from pipeline import server as server_mod

HOLD_REASON = "high risk held for human review"
POLICY_PATH = Path(__file__).resolve().parents[2] / "overlord-policy.md"

PROCEED_REPLY = "RULING: proceed\nRATIONALE: checks are green and the change is contained"
PARK_REPLY = "RULING: park\nRATIONALE: security review is too thin to merge unattended"


def _story(**overrides):
    story = {
        "key": "OPSA-9-STORY",
        "plan": "PLAN-1",
        "plan_name": "PLAN-1",
        "status": "pr_open",
        "parked_reason": None,
        "review_verdict": "APPROVE",
        "security_review_verdict": "APPROVE",
        "risk": "high",
        "summary": "Rewrite the auth token cache",
        "pr_checks": {"ci": "pass", "lint": "pass"},
    }
    story.update(overrides)
    return story


class _Harness:
    """Patches the server autonomy knobs and the overlord/decisions boundary."""

    def __init__(self, monkeypatch, autonomy, ruling=None, threshold="low", exc=None):
        self.invocations = []
        self.decisions = []
        self.role_config = {"role": "overlord", "model": "opus"}
        self.ruling = ruling
        self.exc = exc
        monkeypatch.setattr(server_mod, "PIPELINE_AUTONOMY", autonomy, raising=False)
        monkeypatch.setattr(server_mod, "PIPELINE_RISK_THRESHOLD", threshold, raising=False)

        def fake_invoke(prompt, plan_role_config=None):
            self.invocations.append(
                {"prompt": prompt, "plan_role_config": plan_role_config}
            )
            if self.exc is not None:
                raise self.exc
            return self.ruling

        monkeypatch.setattr(overlord_mod, "_invoke_overlord", fake_invoke)
        monkeypatch.setattr(
            persistence_mod, "_plan_role_config", lambda plan_name: self.role_config
        )
        monkeypatch.setattr(
            persistence_mod,
            "_append_decision",
            lambda plan_name, record: self.decisions.append((plan_name, record)),
        )

    def decide(self, story):
        return merge_mod._merge_decision(story)

    @property
    def prompt(self):
        assert self.invocations, "the overlord was never invoked"
        return self.invocations[0]["prompt"]


# --------------------------------------------------------------------------
# full autonomy: the hold becomes an overlord adjudication
# --------------------------------------------------------------------------


def test_full_proceed_ruling_merges(monkeypatch):
    h = _Harness(monkeypatch, "full", ruling=PROCEED_REPLY)
    decision = h.decide(_story())
    assert decision["action"] == "merge"
    assert decision["reason"] != HOLD_REASON
    assert len(h.invocations) == 1


def test_full_park_ruling_holds_with_todays_reason(monkeypatch):
    h = _Harness(monkeypatch, "full", ruling=PARK_REPLY)
    assert h.decide(_story()) == {"action": "park", "reason": HOLD_REASON}
    assert len(h.invocations) == 1


def test_full_park_ruling_is_recorded(monkeypatch):
    h = _Harness(monkeypatch, "full", ruling=PARK_REPLY)
    h.decide(_story())
    assert len(h.decisions) == 1
    plan_name, record = h.decisions[0]
    assert plan_name == "PLAN-1"
    assert record["decided_by"] == "overlord"
    assert record["ruling"] == "park"
    assert "security review is too thin" in record["rationale"]
    assert record["story_key"] == "OPSA-9-STORY"


def test_full_proceed_ruling_is_recorded_with_prior_state(monkeypatch):
    h = _Harness(monkeypatch, "full", ruling=PROCEED_REPLY)
    h.decide(_story(status="pr_open", parked_reason=None))
    assert len(h.decisions) == 1
    plan_name, record = h.decisions[0]
    assert plan_name == "PLAN-1"
    assert record["decided_by"] == "overlord"
    assert record["ruling"] == "proceed"
    assert "checks are green" in record["rationale"]
    assert record["prior_status"] == "pr_open"
    assert record["prior_parked_reason"] is None


def test_full_records_prior_state_before_any_mutation(monkeypatch):
    h = _Harness(monkeypatch, "full", ruling=PARK_REPLY)
    story = _story(status="parked", parked_reason="awaiting security review")
    snapshot = copy.deepcopy(story)
    h.decide(story)
    assert story == snapshot, "the merge decision must not mutate the story"
    _, record = h.decisions[0]
    assert record["prior_status"] == "parked"
    assert record["prior_parked_reason"] == "awaiting security review"


def test_full_invocation_carries_merge_context_and_instruction(monkeypatch):
    h = _Harness(monkeypatch, "full", ruling=PROCEED_REPLY)
    story = _story(
        summary="Rewrite the auth token cache",
        risk="high",
        review_verdict="APPROVE",
        security_review_verdict="APPROVE_WITH_NOTES",
        pr_checks={"ci": "pass", "lint": "fail"},
    )
    h.decide(story)
    prompt = h.prompt
    assert "Rewrite the auth token cache" in prompt
    assert "APPROVE_WITH_NOTES" in prompt
    assert "APPROVE" in prompt
    assert "high" in prompt
    assert "ci" in prompt and "pass" in prompt
    assert re.search(r"proceed", prompt, re.IGNORECASE)
    assert re.search(r"park", prompt, re.IGNORECASE)
    assert re.search(r"rationale", prompt, re.IGNORECASE)
    assert re.search(r"RULING", prompt, re.IGNORECASE)


def test_full_invocation_uses_the_plans_role_config(monkeypatch):
    h = _Harness(monkeypatch, "full", ruling=PROCEED_REPLY)
    h.decide(_story())
    assert h.invocations[0]["plan_role_config"] == h.role_config


def test_full_ruling_parses_with_surrounding_prose(monkeypatch):
    h = _Harness(
        monkeypatch,
        "full",
        ruling="Sure, here is my ruling.\nRULING: proceed\nRATIONALE: contained change\nHope that helps.",
    )
    assert h.decide(_story())["action"] == "merge"


@pytest.mark.parametrize(
    "reply",
    [
        None,
        "",
        "I would probably merge this one, but it is your call.",
        "RULING: maybe\nRATIONALE: unsure",
        "RATIONALE: no ruling line at all",
    ],
)
def test_full_unparseable_reply_fails_closed(monkeypatch, reply):
    h = _Harness(monkeypatch, "full", ruling=reply)
    assert h.decide(_story()) == {"action": "park", "reason": HOLD_REASON}
    assert len(h.invocations) == 1
    assert len(h.decisions) == 1
    _, record = h.decisions[0]
    assert record["decided_by"] == "overlord"
    assert record.get("ruling") != "proceed"
    assert record["prior_status"] == "pr_open"


def test_full_overlord_failure_fails_closed(monkeypatch):
    h = _Harness(monkeypatch, "full", exc=RuntimeError("overlord unavailable"))
    assert h.decide(_story()) == {"action": "park", "reason": HOLD_REASON}
    assert len(h.invocations) == 1


def test_full_low_risk_merges_without_adjudication(monkeypatch):
    h = _Harness(monkeypatch, "full", ruling=PROCEED_REPLY)
    assert h.decide(_story(risk="low")) == {"action": "merge", "reason": "autonomy=full"}
    assert h.invocations == []
    assert h.decisions == []


# --------------------------------------------------------------------------
# dry-run and gated: byte-identical to pre-OPSA-9, no overlord invocation
# --------------------------------------------------------------------------


def test_gated_high_risk_hold_is_byte_identical(monkeypatch):
    h = _Harness(monkeypatch, "gated", ruling=PROCEED_REPLY)
    assert h.decide(_story()) == {"action": "park", "reason": HOLD_REASON}
    assert h.invocations == []
    assert h.decisions == []


def test_dry_run_high_risk_hold_is_byte_identical(monkeypatch):
    h = _Harness(monkeypatch, "dry-run", ruling=PROCEED_REPLY)
    assert h.decide(_story()) == {"action": "park", "reason": "dry-run"}
    assert h.invocations == []
    assert h.decisions == []


def test_gated_high_risk_holds_even_when_threshold_is_high(monkeypatch):
    h = _Harness(monkeypatch, "gated", threshold="high", ruling=PROCEED_REPLY)
    assert h.decide(_story(risk="high")) == {"action": "park", "reason": HOLD_REASON}
    assert h.invocations == []


@pytest.mark.parametrize("autonomy", ["dry-run", "gated", "full"])
def test_unapproved_story_parks_before_any_adjudication(monkeypatch, autonomy):
    h = _Harness(monkeypatch, autonomy, ruling=PROCEED_REPLY)
    assert h.decide(_story(review_verdict="REQUEST_CHANGES")) == {
        "action": "park",
        "reason": "not approved",
    }
    assert h.invocations == []


# --------------------------------------------------------------------------
# preserved PIPELINE_RISK_THRESHOLD logic and risk-rank boundaries
# --------------------------------------------------------------------------


def test_gated_low_risk_merges_at_threshold(monkeypatch):
    h = _Harness(monkeypatch, "gated", threshold="medium")
    assert h.decide(_story(risk="low")) == {
        "action": "merge",
        "reason": "risk <= threshold medium",
    }
    assert h.invocations == []


def test_gated_risk_above_threshold_parks(monkeypatch):
    h = _Harness(monkeypatch, "gated", threshold="low")
    assert h.decide(_story(risk="medium")) == {
        "action": "park",
        "reason": "risk above threshold low",
    }
    assert h.invocations == []


def test_unknown_risk_rank_is_treated_as_high(monkeypatch):
    h = _Harness(monkeypatch, "gated", ruling=PROCEED_REPLY)
    assert h.decide(_story(risk="banana")) == {"action": "park", "reason": HOLD_REASON}
    assert h.invocations == []


def test_missing_risk_defaults_to_low(monkeypatch):
    h = _Harness(monkeypatch, "gated", threshold="low")
    story = _story()
    del story["risk"]
    assert h.decide(story) == {"action": "merge", "reason": "risk <= threshold low"}


def test_autonomy_is_read_lazily_from_server(monkeypatch):
    assert not hasattr(merge_mod, "PIPELINE_AUTONOMY"), (
        "PIPELINE_AUTONOMY must be read lazily from pipeline.server, not bound "
        "at import time"
    )
    h = _Harness(monkeypatch, "gated", ruling=PROCEED_REPLY)
    assert h.decide(_story()) == {"action": "park", "reason": HOLD_REASON}
    monkeypatch.setattr(server_mod, "PIPELINE_AUTONOMY", "full")
    assert h.decide(_story())["action"] == "merge"


# --------------------------------------------------------------------------
# overlord-policy.md: the park-and-ping tier is now mode-conditional
# --------------------------------------------------------------------------


def _policy_flat():
    text = POLICY_PATH.read_text(encoding="utf-8")
    text = text.replace("\u2011", "-").replace("\u2010", "-")
    return re.sub(r"\s+", " ", text)


def test_policy_hold_is_scoped_to_dry_run_and_gated():
    flat = _policy_flat()
    assert re.search(
        r"dry-run.{0,200}?gated.{0,200}?risk: high", flat, re.IGNORECASE | re.DOTALL
    ), (
        "policy must state that in dry-run and gated a risk: high hold stands"
    )
    old = "triage never overrides the park-and-ping tier"
    idx = flat.lower().find(old)
    if idx != -1:
        window = flat[max(0, idx - 200) : idx + 400]
        assert "dry-run" in window and "gated" in window, (
            "the park-and-ping hold sentence must be scoped to dry-run and gated"
        )


def test_policy_states_full_adjudicates_and_records_the_ruling():
    flat = _policy_flat()
    assert re.search(
        r"full.{0,300}?adjudicat.{0,300}?record", flat, re.IGNORECASE | re.DOTALL
    ), (
        "policy must state that in full the overlord adjudicates the high-risk "
        "merge and records the ruling"
    )


def test_policy_ladder_text_still_credits_full_with_overlord_adjudication():
    flat = _policy_flat()
    assert "overlord adjudication of" in flat
    assert "risk: high" in flat
    assert "held in dry-run and gated" in flat
