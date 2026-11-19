"""A ``proceed`` ruling on a high-risk merge is held unless triage mode is execute.

Under ``PIPELINE_AUTONOMY=full`` a high-risk story is adjudicated by the
overlord. Until the triage rulings are promoted, the harness records the
``proceed`` ruling but holds the merge for a human: the merge only goes ahead
when the plan's effective ``triage_mode`` is ``execute``. A ``park`` ruling
still parks, and gated autonomy never asks the overlord at all.

The overlord is stubbed at the true boundary
(``pipeline.overlord._invoke_overlord``) as in
``tests/unit/test_merge_overlord_adjudication.py``; the plan's settings are
pinned at the Store seam as in ``tests/unit/test_merge_autonomy_settings.py``.
"""

import logging
import re

import pytest

from pipeline import autonomy_settings
from pipeline import config as config_mod
from pipeline import merge as merge_mod
from pipeline import overlord as overlord_mod
from pipeline import persistence as persistence_mod
from pipeline import server as server_mod
from pipeline.ci import SettingsContext

HOLD_REASON = "high risk held for human review"
SHADOW_REASON = "overlord ruled proceed (shadow: held for a human)"

PROCEED_REPLY = (
    "RULING: proceed\nRATIONALE: checks are green and the change is contained"
)
PARK_REPLY = "RULING: park\nRATIONALE: security review is too thin to merge unattended"


@pytest.fixture(autouse=True)
def _no_triage_mode_by_default(monkeypatch):
    """Baseline: no environment triage mode and no plan settings layers."""
    monkeypatch.delenv("PIPELINE_TRIAGE_MODE", raising=False)
    monkeypatch.setattr(config_mod, "PIPELINE_AUTO_MERGE", True)


def _story(**overrides):
    story = {
        "key": "SHADOW-STORY",
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

    def __init__(self, monkeypatch, autonomy, ruling=None):
        self.invocations = []
        self.decisions = []
        self.ruling = ruling
        monkeypatch.setattr(server_mod, "PIPELINE_AUTONOMY", autonomy, raising=False)
        monkeypatch.setattr(server_mod, "PIPELINE_RISK_THRESHOLD", "low", raising=False)

        def fake_invoke(prompt, plan_role_config=None):
            self.invocations.append(prompt)
            return self.ruling

        monkeypatch.setattr(overlord_mod, "_invoke_overlord", fake_invoke)
        monkeypatch.setattr(
            persistence_mod,
            "_plan_role_config",
            lambda plan_name: {"role": "overlord", "model": "opus"},
        )
        monkeypatch.setattr(
            persistence_mod,
            "_append_decision",
            lambda plan_name, record: self.decisions.append((plan_name, record)),
        )

    def decide(self, story=None):
        with merge_mod.merge_adjudication_plan("PLAN-1"):
            return merge_mod._merge_decision(story or _story(), "PLAN-1")


def _pin_plan_settings(monkeypatch, layers):
    """Pin the plan's settings context and the Store layers it resolves from."""
    context = SettingsContext(
        namespace="default", team="alpha", repo="github.com/acme/app"
    )
    monkeypatch.setattr(merge_mod, "settings_for_plan", lambda plan_name: context)
    monkeypatch.setattr(autonomy_settings, "read_layers", lambda *a, **k: layers)


# --- environment triage mode decides in full autonomy -------------------------


def test_proceed_ruling_is_held_when_triage_mode_is_unset(monkeypatch):
    h = _Harness(monkeypatch, "full", ruling=PROCEED_REPLY)

    assert h.decide() == {"action": "park", "reason": SHADOW_REASON}


def test_proceed_ruling_is_still_recorded_when_held(monkeypatch):
    h = _Harness(monkeypatch, "full", ruling=PROCEED_REPLY)
    h.decide()

    assert len(h.decisions) == 1
    plan_name, record = h.decisions[0]
    assert plan_name == "PLAN-1"
    assert record["ruling"] == "proceed"
    assert record["story_key"] == "SHADOW-STORY"


def test_proceed_ruling_merges_when_environment_triage_mode_is_execute(monkeypatch):
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "execute")
    h = _Harness(monkeypatch, "full", ruling=PROCEED_REPLY)

    decision = h.decide()

    assert decision["action"] == "merge"
    assert decision["reason"] == "overlord ruled proceed"


def test_environment_execute_with_plan_shadow_setting_still_parks(monkeypatch):
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "execute")
    _pin_plan_settings(monkeypatch, {"team": {"triage_mode": "shadow"}})
    h = _Harness(monkeypatch, "full", ruling=PROCEED_REPLY)

    assert h.decide() == {"action": "park", "reason": SHADOW_REASON}


@pytest.mark.xfail(
    strict=True,
    reason=(
        "brief expects a plan-level execute to merge with the environment unset, "
        "but triage_mode is a ceiling setting: the environment shadow value is "
        "the deployment layer and a team layer may not loosen it without "
        "allow_loosen. Decision pending; see the PR description."
    ),
)
def test_plan_execute_setting_with_environment_unset_merges(monkeypatch):
    _pin_plan_settings(monkeypatch, {"team": {"triage_mode": "execute"}})
    h = _Harness(monkeypatch, "full", ruling=PROCEED_REPLY)

    assert h.decide()["action"] == "merge"


def test_park_ruling_parks_when_triage_mode_is_unset(monkeypatch):
    h = _Harness(monkeypatch, "full", ruling=PARK_REPLY)

    assert h.decide() == {"action": "park", "reason": HOLD_REASON}


def test_park_ruling_parks_when_triage_mode_is_execute(monkeypatch):
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "execute")
    h = _Harness(monkeypatch, "full", ruling=PARK_REPLY)

    assert h.decide() == {"action": "park", "reason": HOLD_REASON}


# --- environment value normalisation -------------------------------------------


def test_triage_mode_environment_value_is_case_and_whitespace_insensitive(monkeypatch):
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "Execute ")
    h = _Harness(monkeypatch, "full", ruling=PROCEED_REPLY)

    assert h.decide()["action"] == "merge"


def test_unrecognised_triage_mode_environment_value_holds(monkeypatch):
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "bogus")
    h = _Harness(monkeypatch, "full", ruling=PROCEED_REPLY)

    assert h.decide() == {"action": "park", "reason": SHADOW_REASON}


# --- gated autonomy never reaches the overlord ---------------------------------


def test_gated_autonomy_parks_high_risk_without_asking_the_overlord(monkeypatch):
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "execute")
    h = _Harness(monkeypatch, "gated", ruling=PROCEED_REPLY)

    assert h.decide() == {"action": "park", "reason": HOLD_REASON}
    assert h.invocations == []


# --- the shadow hold is logged with identifiers only ---------------------------


def test_shadow_hold_is_logged_at_info_with_identifiers(monkeypatch, caplog):
    h = _Harness(monkeypatch, "full", ruling=PROCEED_REPLY)

    with caplog.at_level(logging.INFO, logger="pipeline"):
        h.decide()

    records = [r for r in caplog.records if r.getMessage().startswith("high-risk")]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.INFO
    assert record.plan_name == "PLAN-1"
    assert record.story_key == "SHADOW-STORY"
    assert record.triage_mode == "shadow"


def test_shadow_hold_log_carries_no_prompt_or_rationale(monkeypatch, caplog):
    h = _Harness(monkeypatch, "full", ruling=PROCEED_REPLY)

    with caplog.at_level(logging.DEBUG, logger="pipeline"):
        h.decide()

    text = " ".join(r.getMessage() for r in caplog.records)
    assert not re.search(r"checks are green|Rewrite the auth token cache", text)
