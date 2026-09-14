"""OPSA-9: high-risk merge holds are adjudicated by the overlord in full autonomy.

Pre-OPSA-9, ``_merge_decision`` parked every story whose risk ranked >= high
with reason ``high risk held for human review`` regardless of autonomy. In
``PIPELINE_AUTONOMY == 'full'`` that hold now becomes an overlord
adjudication: the overlord is asked to rule ``proceed`` or ``park`` with a
rationale, the ruling is recorded in the decisions log, and the merge gate
follows it. Unparseable overlord output fails closed to the original hold.

Dry-run and gated behaviour is byte-identical to pre-OPSA-9: same park
action, same reason string, and the overlord is never invoked.

The overlord invocation is stubbed at the true boundary
(``pipeline.overlord._invoke_overlord``) because ``pipeline/merge.py``
resolves it lazily inside the function body.
"""

from pipeline import merge as merge_mod
from pipeline import overlord as overlord_mod
from pipeline import persistence as persistence_mod
from pipeline import server as server_mod

HOLD_REASON = "high risk held for human review"


def _story(**overrides):
    story = {
        "key": "S1",
        "plan": "PLAN-1",
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

    def __init__(self, monkeypatch, autonomy, ruling=None):
        self.invocations = []
        self.decisions = []
        self.ruling = ruling
        monkeypatch.setattr(server_mod, "PIPELINE_AUTONOMY", autonomy, raising=False)
        monkeypatch.setattr(
            server_mod, "PIPELINE_RISK_THRESHOLD", "low", raising=False
        )

        def fake_invoke(prompt, plan_role_config=None):
            self.invocations.append(
                {"prompt": prompt, "plan_role_config": plan_role_config}
            )
            return self.ruling

        monkeypatch.setattr(overlord_mod, "_invoke_overlord", fake_invoke)
        monkeypatch.setattr(
            persistence_mod,
            "_plan_role_config",
            lambda plan_name: {"role": "overlord"},
        )
        monkeypatch.setattr(
            persistence_mod,
            "_append_decision",
            lambda plan_name, record: self.decisions.append((plan_name, record)),
        )

    def decide(self, story):
        return merge_mod._merge_decision(story)


def test_full_mode_proceed_ruling_merges(monkeypatch):
    h = _Harness(monkeypatch, "full", ruling="RULING: proceed\nRATIONALE: all gates green")
    decision = h.decide(_story())
    assert decision["action"] == "merge"
    assert "overlord" in decision["reason"]
    # The overlord was asked with the merge context, not the default hold.
    assert len(h.invocations) == 1
    prompt = h.invocations[0]["prompt"]
    assert "high" in prompt
    assert "Rewrite the auth token cache" in prompt
    assert "APPROVE" in prompt
    assert h.invocations[0]["plan_role_config"] == {"role": "overlord"}
    # The ruling is recorded in the decisions log.
    assert len(h.decisions) == 1
    plan_name, record = h.decisions[0]
    assert plan_name == "PLAN-1"
    assert record["decided_by"] == "overlord"


def test_full_mode_park_ruling_parks_and_records_decision(monkeypatch):
    h = _Harness(
        monkeypatch,
        "full",
        ruling="RULING: park\nRATIONALE: security review flagged a secret in the diff",
    )
    decision = h.decide(_story())
    assert decision == {"action": "park", "reason": HOLD_REASON}
    assert len(h.invocations) == 1
    assert len(h.decisions) == 1
    plan_name, record = h.decisions[0]
    assert plan_name == "PLAN-1"
    assert record["decided_by"] == "overlord"
    assert record["ruling"] == "park"


def test_full_mode_unparseable_ruling_parks_fail_closed(monkeypatch):
    h = _Harness(monkeypatch, "full", ruling="I think this looks fine, ship it!")
    decision = h.decide(_story())
    assert decision == {"action": "park", "reason": HOLD_REASON}
    assert len(h.invocations) == 1
    # The failed adjudication is still recorded, marked as fail-closed.
    assert len(h.decisions) == 1
    plan_name, record = h.decisions[0]
    assert plan_name == "PLAN-1"
    assert record["decided_by"] == "overlord"


def test_gated_mode_high_risk_holds_without_overlord(monkeypatch):
    h = _Harness(monkeypatch, "gated", ruling="RULING: proceed\nRATIONALE: unused")
    decision = h.decide(_story())
    assert decision == {"action": "park", "reason": HOLD_REASON}
    assert h.invocations == []
    assert h.decisions == []


def test_dry_run_mode_parks_without_overlord(monkeypatch):
    h = _Harness(monkeypatch, "dry-run", ruling="RULING: proceed\nRATIONALE: unused")
    decision = h.decide(_story())
    assert decision == {"action": "park", "reason": "dry-run"}
    assert h.invocations == []
    assert h.decisions == []


def test_full_mode_low_risk_still_merges_without_overlord(monkeypatch):
    """The existing full-mode merge path for non-high risk is untouched."""
    h = _Harness(monkeypatch, "full", ruling="RULING: proceed\nRATIONALE: unused")
    decision = h.decide(_story(risk="low"))
    assert decision == {"action": "merge", "reason": "autonomy=full"}
    assert h.invocations == []
    assert h.decisions == []