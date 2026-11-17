"""``advance_pipeline`` must read the plan's own ``autonomy`` setting.

``autonomy`` used to be read straight from the environment, so every plan
shared one value. It is now a tenant settings key: at the point of use the
value in force is the one the registry resolves for the plan, with the
environment value standing in for the deployment layer, so a lower layer may
only tighten it.

The rules under test, driven through the public ``advance_pipeline`` entrypoint:

* a team layer that tightens the environment value wins -> the tick runs in
  dry-run and says so in its result;
* no layer sets the key -> the environment value stands, exactly as before, so
  the result carries no ``dry_run`` key at all;
* the Store layers cannot be read -> the most restrictive value, so the tick
  runs in dry-run, and exactly one warning names the setting (never a value);
* an environment value the key does not accept is not injected, so it survives
  only when no layer sets the key either;
* whatever the result is, it stays JSON-serializable.
"""

from __future__ import annotations

import json
import logging

import pytest

from pipeline import autonomy_settings
from pipeline import persistence as ppers
from pipeline import server as p
from pipeline.policy_settings import SETTINGS_ERROR_KEY
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _clear_caches,
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _plane_configured,
    _write_manifest,
)

AUTONOMY = "autonomy"
SETTINGS_LOGGER = "pipeline.autonomy_settings"


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Point the plan store at a scratch directory for this test."""
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    return d


@pytest.fixture
def env_autonomy(monkeypatch):
    """Set the environment value of ``autonomy`` (the server's binding)."""

    def _set(value: str) -> str:
        monkeypatch.setattr(p, "PIPELINE_AUTONOMY", value)
        return value

    return _set


def _pin_layers(monkeypatch, layers):
    """Pin the Store layers the autonomy resolver reads."""
    monkeypatch.setattr(autonomy_settings, "read_layers", lambda *a, **k: layers)


def _settings_warnings(caplog):
    return [
        r for r in caplog.records if r.name == SETTINGS_LOGGER and r.levelno >= logging.WARNING
    ]


def test_a_team_layer_may_tighten_the_environment_value(plan_dir, monkeypatch, env_autonomy):
    """A team layer that tightens autonomy to dry-run wins over the env."""
    env_autonomy("gated")
    _write_manifest(plan_dir, "tightened", {})
    _pin_layers(monkeypatch, {"team": {AUTONOMY: "dry-run"}})

    result = p.advance_pipeline("tightened")

    assert result["dry_run"] is True
    assert result["autonomy"] == "dry-run"


def test_a_team_layer_may_not_loosen_the_environment_value(plan_dir, monkeypatch, env_autonomy):
    """A layer may only tighten: the env ``dry-run`` stays in force."""
    env_autonomy("dry-run")
    _write_manifest(plan_dir, "not_loosened", {})
    _pin_layers(monkeypatch, {"team": {AUTONOMY: "full"}})

    result = p.advance_pipeline("not_loosened")

    assert result["dry_run"] is True
    assert result["autonomy"] == "dry-run"


def test_no_layer_leaves_the_environment_value_in_force(plan_dir, monkeypatch, env_autonomy):
    """With no layer setting the key the env value stands, as it did before."""
    env_autonomy("gated")
    _write_manifest(plan_dir, "unset", {})
    _pin_layers(monkeypatch, {})

    result = p.advance_pipeline("unset")

    assert "dry_run" not in result
    assert result["autonomy"] == "gated"


def test_an_unreadable_store_falls_back_to_the_most_restrictive_value(
    plan_dir, monkeypatch, env_autonomy, caplog
):
    """The Store layers cannot be read -> the most restrictive value."""
    env_autonomy("gated")
    _write_manifest(plan_dir, "unreadable", {})
    _pin_layers(monkeypatch, {SETTINGS_ERROR_KEY: "settings_provider_error"})
    caplog.set_level(logging.WARNING)

    result = p.advance_pipeline("unreadable")

    assert result["dry_run"] is True
    assert result["autonomy"] == "dry-run"
    warnings = _settings_warnings(caplog)
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "gated" not in message
    assert "dry-run" not in message


def test_an_unaccepted_environment_value_is_kept_when_no_layer_sets_the_key(
    plan_dir, monkeypatch, env_autonomy
):
    """A value the key does not accept is not injected and survives as-is."""
    env_autonomy("bogus")
    _write_manifest(plan_dir, "bogus", {})
    _pin_layers(monkeypatch, {})

    result = p.advance_pipeline("bogus")

    assert result["autonomy"] == "bogus"
    assert "dry_run" not in result


def test_an_unaccepted_environment_value_does_not_hide_a_layer(plan_dir, monkeypatch, env_autonomy):
    """A layer still decides when the env value is not acceptable."""
    env_autonomy("bogus")
    _write_manifest(plan_dir, "bogus_layer", {})
    _pin_layers(monkeypatch, {"team": {AUTONOMY: "dry-run"}})

    result = p.advance_pipeline("bogus_layer")

    assert result["dry_run"] is True
    assert result["autonomy"] == "dry-run"


def test_the_plan_context_comes_from_the_manifest(plan_dir, monkeypatch, env_autonomy):
    """The plan's own namespace and team are the context that is resolved."""
    env_autonomy("gated")
    (plan_dir / "contextual.manifest.json").write_text(
        json.dumps(
            {
                "epics": {},
                "stories": {},
                "namespace": "acme",
                "team": "core",
                "repo_root": "/tmp/acme/core",
            }
        )
    )
    seen = {}

    def _read_layers(namespace, **kwargs):
        seen["namespace"] = namespace
        seen.update(kwargs)
        return {"team": {AUTONOMY: "dry-run"}}

    monkeypatch.setattr(autonomy_settings, "read_layers", _read_layers)

    result = p.advance_pipeline("contextual")

    assert result["dry_run"] is True
    assert seen["namespace"] == "acme"
    assert seen["team"] == "core"


def test_the_result_stays_json_serializable(plan_dir, monkeypatch, env_autonomy):
    """The dry-run result must survive the JSON round-trip the MCP tool does."""
    env_autonomy("gated")
    _write_manifest(plan_dir, "serializable", {})
    _pin_layers(monkeypatch, {"team": {AUTONOMY: "dry-run"}})

    result = p.advance_pipeline("serializable")

    assert json.dumps(result)


def test_a_plan_named_by_its_repo_root_is_not_forced_into_dry_run(
    plan_dir, monkeypatch, env_autonomy
):
    """A plan whose manifest names a local checkout must still tick.

    The plan's repo identity comes from its ``repo_root``, which is a
    filesystem path rather than a ``host/org/name`` identity. Reading the
    settings for such a plan must not fail and leave the tick stuck in the
    restrictive dry-run mode: the environment value stands.

    The layers are read for real here (nothing is pinned), because the point
    is that the shape of the plan's own repo identity must not break the read.
    """
    env_autonomy("gated")
    _write_manifest(plan_dir, "localcheckout", {})
    manifest_path = plan_dir / "localcheckout.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["repo_root"] = str(plan_dir.parent / "checkout")
    manifest_path.write_text(json.dumps(manifest))

    result = p.advance_pipeline("localcheckout")

    assert "dry_run" not in result
    assert result["autonomy"] == "gated"
