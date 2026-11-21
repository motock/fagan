"""The merge gate applies the plan's stored settings when it is called plan-less.

``pipeline/advance_merge.py`` calls the gate as ``_merge_decision(story)`` with
no plan argument; the plan reaches it only through the adjudication context
bound by ``merge_adjudication_plan``. ``_merge_decision`` must resolve the plan
from that context before it builds the settings context, so the stored
autonomy and auto_merge values apply in production. An explicit ``plan_name``
still wins, and with neither the environment values stand and the Store is not
read.

The Store seam (``read_layers``) and ``settings_for_plan`` are pinned, as in
``test_merge_autonomy_settings.py``. The overlord is stubbed at its own
boundary (``_invoke_overlord``) for the adjudication cases only.
"""

from __future__ import annotations

import pytest

from pipeline import autonomy_settings
from pipeline import ci as ci_mod
from pipeline import config as config_mod
from pipeline import merge as merge_mod
from pipeline import overlord as overlord_mod
from pipeline import persistence as persistence_mod
from pipeline import server as server_mod
from pipeline.ci import SettingsContext

APPROVED = {"review_verdict": "APPROVE", "risk": "low"}
HIGH_RISK_APPROVED = {"key": "JCC-02", "review_verdict": "APPROVE", "risk": "high"}
HOLD_REASON = "high risk held for human review"
PARK_REPLY = "RULING: park\nRATIONALE: security review is too thin to merge unattended"


@pytest.fixture(autouse=True)
def _environment_baseline(monkeypatch):
    """Baseline: gated autonomy at the ``low`` threshold, like production."""
    monkeypatch.setattr(server_mod, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(server_mod, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(config_mod, "PIPELINE_AUTO_MERGE", True)


def _context() -> SettingsContext:
    """A fixed plan context: one namespace, one team, one repo."""
    return SettingsContext(namespace="default", team="alpha", repo="github.com/acme/app")


def _pin_plan(monkeypatch, context):
    """Fix the plan's settings context; return the plan names it was asked for."""
    seen: list[str] = []

    def _settings_for_plan(plan_name):
        seen.append(plan_name)
        return context

    monkeypatch.setattr(merge_mod, "settings_for_plan", _settings_for_plan)
    monkeypatch.setattr(ci_mod, "settings_for_plan", _settings_for_plan)
    return seen


def _pin_layers(monkeypatch, layers):
    """Pin the Store layers the settings resolution reads."""
    calls: list[tuple] = []

    def _read_layers(*args, **kwargs):
        calls.append((args, kwargs))
        return layers

    monkeypatch.setattr(autonomy_settings, "read_layers", _read_layers)
    return calls


def _forbid_layers(monkeypatch):
    """Make any Store read fail; return the list of attempted reads."""
    attempts: list[tuple] = []

    def _boom(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("the Store must not be read without a plan")

    monkeypatch.setattr(autonomy_settings, "read_layers", _boom)
    return attempts


class _Overlord:
    """Stubs the overlord and the decisions writer; records each overlord call."""

    def __init__(self, monkeypatch, ruling=PARK_REPLY):
        self.invocations: list[dict] = []
        self.decisions: list[tuple] = []

        def fake_invoke(prompt, plan_role_config=None):
            self.invocations.append({"prompt": prompt, "plan_role_config": plan_role_config})
            return ruling

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


# --- the bound adjudication plan supplies the settings ---------------------------


def test_should_read_the_plan_settings_from_the_bound_adjudication_plan(monkeypatch):
    seen = _pin_plan(monkeypatch, _context())
    _pin_layers(monkeypatch, {})

    with merge_mod.merge_adjudication_plan("plan-7"):
        merge_mod._merge_decision(dict(APPROVED))

    assert seen
    assert set(seen) == {"plan-7"}


def test_should_adjudicate_a_high_risk_merge_when_the_bound_plan_resolves_full(monkeypatch):
    _pin_plan(monkeypatch, _context())
    _pin_layers(monkeypatch, {"deployment": {"autonomy": "full"}})
    overlord = _Overlord(monkeypatch)

    with merge_mod.merge_adjudication_plan("plan-7"):
        decision = merge_mod._merge_decision(dict(HIGH_RISK_APPROVED))

    assert len(overlord.invocations) == 1
    assert decision == {"action": "park", "reason": HOLD_REASON}


def test_should_park_without_adjudication_when_the_bound_plan_resolves_gated(monkeypatch):
    _pin_plan(monkeypatch, _context())
    _pin_layers(
        monkeypatch,
        {"deployment": {"autonomy": "full"}, "team_repo": {"autonomy": "gated"}},
    )
    overlord = _Overlord(monkeypatch)

    with merge_mod.merge_adjudication_plan("plan-7"):
        decision = merge_mod._merge_decision(dict(HIGH_RISK_APPROVED))

    assert overlord.invocations == []
    assert decision == {"action": "park", "reason": HOLD_REASON}


def test_should_turn_auto_merge_off_from_the_bound_plan_settings(monkeypatch):
    _pin_plan(monkeypatch, _context())
    _pin_layers(monkeypatch, {"team": {"auto_merge": False}})

    with merge_mod.merge_adjudication_plan("plan-7"):
        decision = merge_mod._merge_decision(dict(APPROVED))

    assert decision == {
        "action": "park",
        "reason": "auto-merge disabled (auto_merge=off)",
    }


# --- an explicit plan_name still wins -------------------------------------------


def test_should_prefer_an_explicit_plan_name_over_the_bound_one(monkeypatch):
    seen = _pin_plan(monkeypatch, _context())
    _pin_layers(monkeypatch, {})

    with merge_mod.merge_adjudication_plan("plan-b"):
        merge_mod._merge_decision(dict(APPROVED), "plan-a")

    assert seen
    assert set(seen) == {"plan-a"}


# --- no plan bound and none passed: the environment stands ----------------------


def test_should_keep_environment_values_when_no_plan_is_bound_or_passed(monkeypatch):
    attempts = _forbid_layers(monkeypatch)

    assert merge_mod._merge_decision(dict(APPROVED)) == {
        "action": "merge",
        "reason": "risk <= threshold low",
    }
    assert attempts == []
