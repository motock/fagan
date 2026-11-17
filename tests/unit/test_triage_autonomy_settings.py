"""The triage executors read the plan's ``triage_mode`` and ``autonomy`` keys.

``autonomy`` (``dry-run``|``gated``|``full``), ``auto_merge`` (bool) and
``triage_mode`` (``shadow``|``execute``) are tenant settings keys; the
environment variables stay as the deployment-layer stand-in.  At the point of
use the value in force is resolved for the plan:

* no plan context -> the environment value, exactly as before;
* the Store layers cannot be read -> the most restrictive value, with one
  warning;
* otherwise the environment value fills the deployment layer, so a lower layer
  may only tighten it.

These tests drive the public triage entry points -- ``pipeline.triage._triage_mode``
and the executors that consult it -- with ``pipeline.autonomy_settings.read_layers``
pinned, so no Store is touched.  The resolver's own rules are covered by
``tests/unit/test_autonomy_settings.py``.
"""

from __future__ import annotations

import logging

import pytest

from pipeline import autonomy_settings, server, triage

PLAN = "plan-a"
KEY = "HR-X"

MANIFEST = {"namespace": "default", "team": "t1"}

TRIAGE_MODE = "triage_mode"
AUTONOMY = "autonomy"


def _pin_layers(monkeypatch, layers):
    """Pin the Store layers the resolver reads; return the recorded calls."""
    calls = []

    def _read_layers(*args, **kwargs):
        calls.append((args, kwargs))
        return layers

    monkeypatch.setattr(autonomy_settings, "read_layers", _read_layers)
    return calls


def _warnings(caplog):
    return [record for record in caplog.records if record.levelno == logging.WARNING]


def _story() -> dict:
    return {
        "status": "parked",
        "parked_reason": "scope conflict",
        "files": ["pipeline/foo.py"],
    }


def _ruling() -> dict:
    return {"action": "amend_scope", "rationale": "needed"}


@pytest.fixture
def executor(monkeypatch):
    """Stub the notification and the decision log; record what was recorded."""
    calls = {"notify": [], "decisions": []}

    monkeypatch.setattr(
        triage,
        "_notify_user",
        lambda plan, message, **kwargs: calls["notify"].append(message),
    )
    monkeypatch.setattr(
        triage,
        "_record_execution",
        lambda plan, key, action, result, mode, *a, **k: calls["decisions"].append(
            (action, result, mode)
        ),
    )
    monkeypatch.setattr(
        triage,
        "_append_decision",
        lambda plan, record: calls["decisions"].append(record),
    )
    return calls


# --- no plan context: the environment value stands -----------------------------


def test_triage_mode_without_a_manifest_is_the_environment_value(monkeypatch):
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "execute")
    calls = _pin_layers(monkeypatch, {"team": {TRIAGE_MODE: "shadow"}})

    assert triage._triage_mode() == "execute"
    # No plan context -> the Store is never read.
    assert calls == []


def test_triage_mode_without_a_manifest_reads_no_store_even_when_the_read_fails(
    monkeypatch,
):
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "execute")

    def _boom(*args, **kwargs):
        raise AssertionError("the Store must not be read without a plan context")

    monkeypatch.setattr(autonomy_settings, "read_layers", _boom)

    assert triage._triage_mode() == "execute"


@pytest.mark.parametrize(
    "env_value,expected",
    [
        ("execute", "execute"),
        ("shadow", "shadow"),
        ("", "shadow"),
        ("bogus", "shadow"),
    ],
)
def test_triage_mode_without_a_manifest_mirrors_the_environment(
    monkeypatch, env_value, expected
):
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", env_value)
    _pin_layers(monkeypatch, {})

    assert triage._triage_mode() == expected


def test_triage_mode_without_a_manifest_ignores_the_environment_case(monkeypatch):
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "EXECUTE")
    _pin_layers(monkeypatch, {})

    assert triage._triage_mode() == "execute"


# --- a plan context: the resolved value wins -----------------------------------


def test_a_team_layer_may_tighten_the_environment_triage_mode(monkeypatch):
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "execute")
    _pin_layers(monkeypatch, {"team": {TRIAGE_MODE: "shadow"}})

    assert triage._triage_mode(MANIFEST) == "shadow"


def test_an_empty_layer_set_keeps_the_environment_triage_mode(monkeypatch):
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "execute")
    _pin_layers(monkeypatch, {})

    assert triage._triage_mode(MANIFEST) == "execute"


def test_a_team_layer_may_not_loosen_the_environment_triage_mode(monkeypatch):
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "shadow")
    _pin_layers(monkeypatch, {"team": {TRIAGE_MODE: "execute"}})

    assert triage._triage_mode(MANIFEST) == "shadow"


def test_an_unreadable_store_falls_back_to_the_restrictive_triage_mode(
    monkeypatch, caplog
):
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "execute")
    _pin_layers(monkeypatch, {"error": "settings_store_read_failed"})
    caplog.set_level(logging.WARNING)

    assert triage._triage_mode(MANIFEST) == "shadow"
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert warnings[0].setting_key == TRIAGE_MODE
    # The warning names the setting, never its value.
    assert "execute" not in warnings[0].getMessage()


def test_a_failing_store_read_falls_back_to_the_restrictive_triage_mode(
    monkeypatch, caplog
):
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "execute")

    def _boom(*args, **kwargs):
        raise RuntimeError("store is down")

    monkeypatch.setattr(autonomy_settings, "read_layers", _boom)
    caplog.set_level(logging.WARNING)

    assert triage._triage_mode(MANIFEST) == "shadow"
    assert len(_warnings(caplog)) == 1


def test_a_manifest_selects_the_plan_the_layers_are_read_for(monkeypatch):
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "execute")
    calls = _pin_layers(monkeypatch, {})

    triage._triage_mode(MANIFEST)

    assert len(calls) == 1
    args, kwargs = calls[0]
    supplied = list(args) + list(kwargs.values())
    assert "default" in supplied
    assert "t1" in supplied


def test_a_manifest_without_a_team_still_names_a_plan(monkeypatch):
    """A manifest that names no team still names a plan, so the Store is read."""
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "execute")
    calls = _pin_layers(monkeypatch, {"team": {TRIAGE_MODE: "shadow"}})

    assert triage._triage_mode({}) == "shadow"
    assert len(calls) == 1


# --- the executors read the plan's mode ---------------------------------------


def test_the_executor_shadows_a_new_action_when_the_plan_is_in_shadow(
    monkeypatch, executor
):
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "execute")
    monkeypatch.setattr(server, "PIPELINE_AUTONOMY", "full")
    _pin_layers(monkeypatch, {"team": {TRIAGE_MODE: "shadow"}})
    story = _story()

    result = triage._apply_ruling_for_mode(
        PLAN, KEY, story, {"action": "resolve_rebase", "rationale": "x"}, MANIFEST
    )

    assert result == "shadow"
    assert story["status"] == "parked"


def test_the_executor_dry_runs_when_the_plan_sets_dry_run_autonomy(
    monkeypatch, executor
):
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "execute")
    monkeypatch.setattr(server, "PIPELINE_AUTONOMY", "gated")
    _pin_layers(monkeypatch, {"team": {AUTONOMY: "dry-run"}})
    story = _story()

    result = triage._apply_ruling_for_mode(PLAN, KEY, story, _ruling(), MANIFEST)

    assert result == "dry-run"
    assert story["status"] == "parked"


def test_the_executor_runs_when_the_environment_autonomy_still_applies(
    monkeypatch, executor
):
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "execute")
    monkeypatch.setattr(server, "PIPELINE_AUTONOMY", "gated")
    _pin_layers(monkeypatch, {})
    monkeypatch.setattr(triage, "execute_ruling", lambda *a: "amend_scope")

    result = triage._apply_ruling_for_mode(PLAN, KEY, _story(), _ruling(), MANIFEST)

    assert result == "amend_scope"


def test_the_executor_dry_runs_when_the_store_cannot_be_read(monkeypatch, executor):
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "execute")
    monkeypatch.setattr(server, "PIPELINE_AUTONOMY", "full")
    _pin_layers(monkeypatch, {"error": "settings_store_read_failed"})

    result = triage._apply_ruling_for_mode(PLAN, KEY, _story(), _ruling(), MANIFEST)

    # Autonomy falls back to its most restrictive value: dry-run.
    assert result == "dry-run"


def test_the_executor_dry_runs_when_the_store_read_raises(monkeypatch, executor):
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "execute")
    monkeypatch.setattr(server, "PIPELINE_AUTONOMY", "full")

    def _boom(*args, **kwargs):
        raise RuntimeError("store is down")

    monkeypatch.setattr(autonomy_settings, "read_layers", _boom)

    result = triage._apply_ruling_for_mode(PLAN, KEY, _story(), _ruling(), MANIFEST)

    assert result == "dry-run"


# --- the other executors pass the manifest on ---------------------------------


@pytest.mark.parametrize(
    "module_name,function_name",
    [
        ("pipeline.triage_rebase", "execute_resolve_rebase"),
        ("pipeline.triage_resume_review", "execute_resume_review"),
        ("pipeline.triage_test_edit", "execute_approve_test_edit"),
    ],
)
def test_the_other_executors_read_the_plan_triage_mode(
    monkeypatch, module_name, function_name
):
    """Each executor asks for the mode with the plan's manifest in hand.

    With the plan in shadow the executor must return ``"shadow"`` even though
    the environment says ``execute``; with the plan in execute it must get past
    the mode check and fail on its own preconditions instead.
    """
    import importlib

    module = importlib.import_module(module_name)
    run = getattr(module, function_name)
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "execute")
    monkeypatch.setattr(triage, "_notify_user", lambda plan, message, **kw: None)

    _pin_layers(monkeypatch, {"team": {TRIAGE_MODE: "shadow"}})
    shadowed = run(PLAN, KEY, _story(), _ruling(), MANIFEST)
    assert shadowed == "shadow"

    _pin_layers(monkeypatch, {"team": {TRIAGE_MODE: "execute"}})
    executed = run(PLAN, KEY, _story(), _ruling(), MANIFEST)
    assert executed != "shadow"
