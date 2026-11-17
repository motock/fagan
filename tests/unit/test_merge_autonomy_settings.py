"""The merge gate reads autonomy and auto_merge from the plan's settings.

``pipeline/merge.py::_merge_decision`` used to read ``PIPELINE_AUTONOMY`` and
``PIPELINE_AUTO_MERGE`` straight from the environment. Those two behaviours are
now tenant settings keys, so the value in force for a story depends on the plan
the story belongs to:

* with ``plan_name`` the values come from the plan's settings, resolved through
  ``pipeline.autonomy_settings``, with the environment value standing in for the
  deployment layer, so a lower layer may only tighten the environment value;
* without ``plan_name`` there is no plan context and the environment value
  stands, exactly as before - the Store is not even read.

These tests drive the gate's public behaviour: the decision it returns and the
reason it gives. The Store seam is pinned (``read_layers``) so the resolution
is deterministic and hermetic.
"""

from __future__ import annotations

import inspect
import logging

import pytest

from pipeline import autonomy_settings
from pipeline import ci as ci_mod
from pipeline import config as config_mod
from pipeline import merge as merge_mod
from pipeline import server as server_mod
from pipeline.ci import SettingsContext
from pipeline.policy_settings import SETTINGS_ERROR_KEY

APPROVED = {"review_verdict": "APPROVE", "risk": "low"}


@pytest.fixture(autouse=True)
def _environment_baseline(monkeypatch):
    """Baseline: gated autonomy at the ``low`` threshold, like production."""
    monkeypatch.setattr(server_mod, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(server_mod, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(config_mod, "PIPELINE_AUTO_MERGE", True)


def _context() -> SettingsContext:
    """A fixed plan context: one namespace, one team, one repo."""
    return SettingsContext(namespace="default", team="alpha", repo="acme/app")


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
    """Make any Store read fail: the gate must not read without a plan."""

    def _boom(*args, **kwargs):
        raise AssertionError("the Store must not be read without a plan")

    monkeypatch.setattr(autonomy_settings, "read_layers", _boom)


def _decision(story=None, plan_name="plan-x"):
    return merge_mod._merge_decision(dict(story or APPROVED), plan_name)


# --- the plan's settings decide ------------------------------------------------


def test_a_team_layer_can_turn_auto_merge_off(monkeypatch):
    _pin_plan(monkeypatch, _context())
    _pin_layers(monkeypatch, {"team": {"auto_merge": False}})

    assert _decision() == {
        "action": "park",
        "reason": "auto-merge disabled (auto_merge=off)",
    }


def test_a_team_layer_can_turn_autonomy_to_dry_run(monkeypatch):
    _pin_plan(monkeypatch, _context())
    _pin_layers(monkeypatch, {"team": {"autonomy": "dry-run"}})

    assert _decision() == {"action": "park", "reason": "dry-run"}


def test_empty_layers_keep_the_environment_values(monkeypatch):
    _pin_plan(monkeypatch, _context())
    _pin_layers(monkeypatch, {})

    assert _decision() == {"action": "merge", "reason": "risk <= threshold low"}


def test_the_settings_are_read_for_the_plan_being_decided(monkeypatch):
    seen = _pin_plan(monkeypatch, _context())
    _pin_layers(monkeypatch, {})

    _decision(plan_name="plan-42")

    assert seen
    assert set(seen) == {"plan-42"}


def test_a_deployment_layer_may_set_autonomy_above_the_environment(monkeypatch):
    """The stored deployment layer replaces the environment value."""
    _pin_plan(monkeypatch, _context())
    _pin_layers(monkeypatch, {"deployment": {"autonomy": "full"}})

    assert _decision() == {"action": "merge", "reason": "autonomy=full"}


def test_a_lower_layer_may_not_loosen_the_environment_autonomy(monkeypatch):
    _pin_plan(monkeypatch, _context())
    _pin_layers(monkeypatch, {"team": {"autonomy": "full"}})

    assert _decision() == {"action": "merge", "reason": "risk <= threshold low"}


def test_a_lower_layer_may_not_turn_the_environment_auto_merge_back_on(monkeypatch):
    monkeypatch.setattr(config_mod, "PIPELINE_AUTO_MERGE", False)
    _pin_plan(monkeypatch, _context())
    _pin_layers(monkeypatch, {"team": {"auto_merge": True}})

    assert _decision() == {
        "action": "park",
        "reason": "auto-merge disabled (PIPELINE_AUTO_MERGE=0)",
    }


def test_an_unaccepted_environment_autonomy_is_not_turned_into_dry_run(monkeypatch):
    monkeypatch.setattr(server_mod, "PIPELINE_AUTONOMY", "bogus")
    _pin_plan(monkeypatch, _context())
    _pin_layers(monkeypatch, {})

    assert _decision()["action"] == "merge"


def test_an_unaccepted_environment_autonomy_does_not_hide_a_layer(monkeypatch):
    monkeypatch.setattr(server_mod, "PIPELINE_AUTONOMY", "bogus")
    _pin_plan(monkeypatch, _context())
    _pin_layers(monkeypatch, {"team": {"autonomy": "dry-run"}})

    assert _decision() == {"action": "park", "reason": "dry-run"}


# --- the Store cannot be read: the restrictive value ----------------------------


def test_an_unreadable_store_parks_with_the_restrictive_value(monkeypatch, caplog):
    _pin_plan(monkeypatch, _context())
    _pin_layers(monkeypatch, {SETTINGS_ERROR_KEY: "settings_provider_error"})

    caplog.set_level(logging.WARNING)
    decision = _decision()

    assert decision["action"] == "park"
    assert decision["reason"] == "dry-run"
    assert any(record.levelno == logging.WARNING for record in caplog.records)


# --- no plan context: the environment value stands ------------------------------


def test_without_a_plan_the_store_is_not_read(monkeypatch):
    _forbid_layers(monkeypatch)

    assert _decision(plan_name=None) == {
        "action": "merge",
        "reason": "risk <= threshold low",
    }


def test_without_a_plan_the_environment_dry_run_still_parks(monkeypatch):
    monkeypatch.setattr(server_mod, "PIPELINE_AUTONOMY", "dry-run")
    _forbid_layers(monkeypatch)

    assert _decision(plan_name=None) == {"action": "park", "reason": "dry-run"}


def test_without_a_plan_the_environment_auto_merge_off_still_parks(monkeypatch):
    monkeypatch.setattr(config_mod, "PIPELINE_AUTO_MERGE", False)
    _forbid_layers(monkeypatch)

    assert _decision(plan_name=None) == {
        "action": "park",
        "reason": "auto-merge disabled (PIPELINE_AUTO_MERGE=0)",
    }


# --- the environment flag off keeps its own reason -----------------------------


def test_the_environment_auto_merge_off_keeps_its_own_reason(monkeypatch):
    monkeypatch.setattr(config_mod, "PIPELINE_AUTO_MERGE", False)
    _pin_plan(monkeypatch, _context())
    _pin_layers(monkeypatch, {})

    assert _decision() == {
        "action": "park",
        "reason": "auto-merge disabled (PIPELINE_AUTO_MERGE=0)",
    }


# --- what the plan's settings do not change ------------------------------------


def test_the_risk_threshold_still_comes_from_the_environment(monkeypatch):
    _pin_plan(monkeypatch, _context())
    _pin_layers(monkeypatch, {"team": {"autonomy": "gated"}})
    story = {"review_verdict": "APPROVE", "risk": "medium"}

    assert _decision(story) == {"action": "park", "reason": "risk above threshold low"}


def test_the_risk_threshold_still_comes_from_the_environment_when_raised(monkeypatch):
    monkeypatch.setattr(server_mod, "PIPELINE_RISK_THRESHOLD", "medium")
    _pin_plan(monkeypatch, _context())
    _pin_layers(monkeypatch, {})
    story = {"review_verdict": "APPROVE", "risk": "medium"}

    assert _decision(story) == {"action": "merge", "reason": "risk <= threshold medium"}


def test_an_unapproved_story_still_parks_as_not_approved(monkeypatch):
    _pin_plan(monkeypatch, _context())
    _pin_layers(monkeypatch, {"team": {"auto_merge": False}})

    decision = _decision({"review_verdict": "REJECT", "risk": "low"})

    assert decision == {"action": "park", "reason": "not approved"}


# --- the gate documents where its values come from ------------------------------


def test_the_docstring_names_the_plan_and_its_settings():
    doc = inspect.getdoc(merge_mod._merge_decision) or ""

    assert "plan_name" in doc
    assert "setting" in doc.lower()
