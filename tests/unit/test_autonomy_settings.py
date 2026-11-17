"""Resolve the autonomy, auto_merge and triage_mode keys for one plan.

The three behaviours that used to live only in environment variables become
tenant settings keys. At the point of use the value in force is the one the
registry resolves for the plan, with the environment value standing in for the
deployment layer, so a lower layer may only tighten it.

The rules under test:

* no plan context -> the environment value, exactly as before;
* the Store layers cannot be read -> the most restrictive value, and one
  warning that names no value;
* otherwise the environment value fills the deployment layer when the layer
  does not set the key, so the lower layers act as ceilings on it;
* an environment value the key does not accept is not injected, and when no
  layer sets the key either, that raw value is returned as before.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from pipeline.autonomy_settings import (
    RESTRICTIVE,
    effective,
    effective_auto_merge,
    effective_autonomy,
    effective_triage_mode,
)

from pipeline import autonomy_settings
from pipeline.policy_settings import SETTINGS_ERROR_KEY

REPO_ROOT = Path(__file__).resolve().parents[2]

AUTONOMY = "autonomy"
AUTO_MERGE = "auto_merge"
TRIAGE_MODE = "triage_mode"


@dataclass(frozen=True)
class _Context:
    """A stand-in for ``pipeline.ci.SettingsContext``.

    Any object carrying ``namespace``, ``team`` and ``repo`` is a context, so
    this frozen dataclass is one.
    """

    namespace: str = "default"
    team: str = "alpha"
    repo: str = "alpha/repo"


def _pin_layers(monkeypatch, layers):
    """Pin the Store layers the module reads; return the recorded calls."""
    calls = []

    def _read_layers(*args, **kwargs):
        calls.append((args, kwargs))
        return layers

    monkeypatch.setattr(autonomy_settings, "read_layers", _read_layers)
    return calls


def _warnings(caplog):
    return [record for record in caplog.records if record.levelno == logging.WARNING]


def _assert_hides(message, *values):
    for value in values:
        assert value not in message, f"the warning leaks the value {value!r}"


# --- the restrictive values ---------------------------------------------------


def test_restrictive_values_are_the_most_restrictive_ones():
    assert RESTRICTIVE[AUTONOMY] == "dry-run"
    assert RESTRICTIVE[AUTO_MERGE] is False
    assert RESTRICTIVE[TRIAGE_MODE] == "shadow"


# --- no plan context: the environment value stands ----------------------------


def test_no_context_keeps_the_environment_value(monkeypatch):
    calls = _pin_layers(monkeypatch, {})
    assert effective(AUTONOMY, "full", None) == "full"
    assert effective(AUTO_MERGE, False, None) is False
    assert effective(TRIAGE_MODE, "execute", None) == "execute"
    assert calls == []


def test_no_context_keeps_the_environment_value_through_the_wrappers(monkeypatch):
    _pin_layers(monkeypatch, {})
    assert effective_autonomy("full", None) == "full"
    assert effective_auto_merge(False, None) is False
    assert effective_triage_mode("execute", None) == "execute"


# --- the environment value fills the deployment layer --------------------------


def test_empty_layers_keep_the_environment_value(monkeypatch):
    _pin_layers(monkeypatch, {})
    assert effective(AUTONOMY, "full", _Context()) == "full"
    assert effective(AUTO_MERGE, False, _Context()) is False
    assert effective(TRIAGE_MODE, "execute", _Context()) == "execute"


def test_the_context_is_read_for_its_namespace_team_and_repo(monkeypatch):
    calls = _pin_layers(monkeypatch, {})
    context = _Context(namespace="acme", team="core", repo="acme/core")
    effective(AUTONOMY, "full", context)
    assert len(calls) == 1
    args, kwargs = calls[0]
    supplied = list(args) + list(kwargs.values())
    assert "acme" in supplied
    assert "core" in supplied
    assert "acme/core" in supplied


def test_a_team_layer_may_tighten_the_environment_value(monkeypatch):
    _pin_layers(monkeypatch, {"team": {AUTONOMY: "dry-run"}})
    assert effective(AUTONOMY, "full", _Context()) == "dry-run"


def test_a_team_layer_may_not_loosen_the_environment_value(monkeypatch):
    _pin_layers(monkeypatch, {"team": {AUTONOMY: "full"}})
    assert effective(AUTONOMY, "gated", _Context()) == "gated"


def test_the_stored_deployment_layer_wins_over_the_environment_value(monkeypatch):
    _pin_layers(monkeypatch, {"deployment": {AUTONOMY: "full"}})
    assert effective(AUTONOMY, "gated", _Context()) == "full"


def test_auto_merge_team_layer_may_turn_the_environment_off(monkeypatch):
    _pin_layers(monkeypatch, {"team": {AUTO_MERGE: False}})
    assert effective(AUTO_MERGE, True, _Context()) is False


def test_auto_merge_team_layer_may_not_turn_the_environment_on(monkeypatch):
    _pin_layers(monkeypatch, {"team": {AUTO_MERGE: True}})
    assert effective(AUTO_MERGE, False, _Context()) is False


def test_triage_mode_team_layer_may_tighten_the_environment_value(monkeypatch):
    _pin_layers(monkeypatch, {"team": {TRIAGE_MODE: "shadow"}})
    assert effective(TRIAGE_MODE, "execute", _Context()) == "shadow"


def test_triage_mode_team_layer_may_not_loosen_the_environment_value(monkeypatch):
    _pin_layers(monkeypatch, {"team": {TRIAGE_MODE: "execute"}})
    assert effective(TRIAGE_MODE, "shadow", _Context()) == "shadow"


# --- an environment value the key does not accept -----------------------------


def test_an_unaccepted_environment_value_is_returned_as_is(monkeypatch):
    _pin_layers(monkeypatch, {})
    assert effective(AUTONOMY, "bogus", _Context()) == "bogus"


def test_an_unaccepted_environment_value_does_not_hide_a_layer(monkeypatch):
    _pin_layers(monkeypatch, {"team": {AUTONOMY: "dry-run"}})
    assert effective(AUTONOMY, "bogus", _Context()) == "dry-run"


def test_a_layer_value_the_key_rejects_leaves_the_environment_value(monkeypatch):
    _pin_layers(monkeypatch, {"deployment": {AUTONOMY: "bogus"}})
    assert effective(AUTONOMY, "full", _Context()) == "full"


# --- the Store layers cannot be read -----------------------------------------


def test_an_unreadable_store_falls_back_to_the_restrictive_value(monkeypatch, caplog):
    _pin_layers(monkeypatch, {SETTINGS_ERROR_KEY: "settings_provider_error"})
    caplog.set_level(logging.WARNING)
    assert effective(AUTONOMY, "full", _Context()) == "dry-run"
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert warnings[0].setting_key == AUTONOMY
    _assert_hides(warnings[0].getMessage(), "full", "dry-run")


def test_an_unreadable_store_falls_back_for_every_key(monkeypatch, caplog):
    _pin_layers(monkeypatch, {SETTINGS_ERROR_KEY: "settings_provider_error"})
    caplog.set_level(logging.WARNING)
    assert effective(AUTO_MERGE, True, _Context()) is False
    assert effective(TRIAGE_MODE, "execute", _Context()) == "shadow"
    warnings = _warnings(caplog)
    assert len(warnings) == 2
    assert [record.setting_key for record in warnings] == [AUTO_MERGE, TRIAGE_MODE]
    for record in warnings:
        _assert_hides(record.getMessage(), "True", "False", "execute", "shadow")


def test_the_error_marker_wins_over_any_layer(monkeypatch, caplog):
    _pin_layers(monkeypatch, {"team": {AUTONOMY: "full"}, SETTINGS_ERROR_KEY: "boom"})
    caplog.set_level(logging.WARNING)
    assert effective(AUTONOMY, "full", _Context()) == "dry-run"
    assert len(_warnings(caplog)) == 1


def test_a_failing_store_read_falls_back_to_the_restrictive_value(
    monkeypatch, caplog
):
    def _boom(*args, **kwargs):
        raise RuntimeError("store is down")

    monkeypatch.setattr(autonomy_settings, "read_layers", _boom)
    caplog.set_level(logging.WARNING)
    assert effective(AUTONOMY, "full", _Context()) == "dry-run"
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert warnings[0].error_type == "RuntimeError"
    assert warnings[0].setting_key == AUTONOMY
    _assert_hides(warnings[0].getMessage(), "full", "dry-run", "store is down")


def test_a_failing_store_read_falls_back_for_every_key(monkeypatch, caplog):
    def _boom(*args, **kwargs):
        raise RuntimeError("store is down")

    monkeypatch.setattr(autonomy_settings, "read_layers", _boom)
    caplog.set_level(logging.WARNING)
    assert effective(AUTO_MERGE, True, _Context()) is False
    assert effective(TRIAGE_MODE, "execute", _Context()) == "shadow"
    warnings = _warnings(caplog)
    assert len(warnings) == 2
    assert [record.error_type for record in warnings] == ["RuntimeError"] * 2


# --- the thin wrappers --------------------------------------------------------


def test_effective_autonomy_lower_cases_the_environment_value(monkeypatch):
    _pin_layers(monkeypatch, {})
    assert effective_autonomy("GATED", _Context()) == "gated"


def test_effective_auto_merge_returns_a_boolean(monkeypatch):
    _pin_layers(monkeypatch, {})
    assert effective_auto_merge(True, _Context()) is True
    assert effective_auto_merge(False, _Context()) is False


def test_effective_triage_mode_reads_the_team_layer(monkeypatch):
    _pin_layers(monkeypatch, {"team": {TRIAGE_MODE: "shadow"}})
    assert effective_triage_mode("execute", _Context()) == "shadow"


# --- the module stays importable on its own -----------------------------------


def test_importing_the_module_does_not_import_pipeline_ci():
    code = (
        "import sys\n"
        "import pipeline.autonomy_settings\n"
        "assert 'pipeline.ci' not in sys.modules\n"
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PIPELINE_", "LOCAL_AGENT_"))
    }
    # The host environment selects operator plugins that this checkout does
    # not ship; the module must import on its own.
    env["PIPELINE_SKIP_ENV_FILE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
