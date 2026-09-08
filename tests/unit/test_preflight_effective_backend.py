"""Tests for pipeline.preflight check c: the EFFECTIVE dispatch backend (PP-02).

check c used to read PIPELINE_BACKEND_DISPATCH (defaulting to "claude") and
stop there, so on a host where the env var is unset but model_registry.json
routes the dispatch role to ollama, preflight reported "claude" — a false
green on the exact surface a first-time operator trusts. These tests pin the
rewritten contract: check c resolves the dispatch role the way production
does (app.role_registry.resolve_role's plan -> env -> registry -> default
chain) and reports the resolved provider AND model.

Repo rule (`.claude/rules/testing-config-gates.md`): test the resolution
logic, never today's configured values. Every test stubs the registry via
app.role_registry.load_registry / resolve_role (monkeypatch) and injects
`which`, so no assertion depends on the live model_registry.json or the
host's installed CLIs. run_preflight is called WITHOUT registry_loader so
check c takes its production path (app.role_registry), which is the surface
these tests stub.
"""

from __future__ import annotations

import sys

import pytest

from app import role_registry
from pipeline import preflight


# --------------------------------------------------------------------------- #
# Stubs.
# --------------------------------------------------------------------------- #
def _path_which(name):
    """`which` stub: every CLI present at a fabricated (never stat'd) path."""
    return f"/fake/bin/{name}"


def _none_which(name):
    """`which` stub: every CLI absent."""
    return


def _registry_with_dispatch(dispatch=None):
    """Synthetic registry payload; `dispatch` is the roles.dispatch entry."""
    return {
        "providers": {
            "claude": {"models": {"sonnet": {"tag": "claude-sonnet-4"}}},
            "ollama": {
                "models": {"glm-5.3-flash:cloud": {"tag": "glm-5.3-flash:cloud"}}
            },
        },
        "roles": {
            "overlord": {"provider": "claude", "model": "sonnet"},
            **({"dispatch": dispatch} if dispatch is not None else {}),
        },
    }


def _find_dispatch(results):
    matches = [
        check
        for check in results
        if "dispatch" in str(check.get("name", "")).lower()
    ]
    assert matches, (
        f"no dispatch check in names {[check.get('name') for check in results]}"
    )
    return matches[0]


# --------------------------------------------------------------------------- #
# Effective resolution: registry wins when the env var is unset.
# --------------------------------------------------------------------------- #
def test_env_unset_registry_routes_dispatch_to_ollama_message_names_ollama(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        role_registry,
        "load_registry",
        lambda: _registry_with_dispatch(
            {"provider": "ollama", "model": "glm-5.3-flash:cloud"}
        ),
    )
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)

    results = preflight.run_preflight(plan_dir=tmp_path, which=_path_which)
    check = _find_dispatch(results)
    assert check["status"] == "ok"
    lowered = check["message"].lower()
    assert "ollama" in lowered
    assert "glm-5.3-flash:cloud" in lowered
    # The false green this story fixes: the raw-env default must not appear.
    assert "claude" not in lowered


def test_resolved_provider_probes_its_own_cli_not_claudes(tmp_path, monkeypatch):
    probed = []

    def _recording_which(name):
        probed.append(name)
        return f"/fake/bin/{name}"

    monkeypatch.setattr(
        role_registry,
        "load_registry",
        lambda: _registry_with_dispatch(
            {"provider": "ollama", "model": "glm-5.3-flash:cloud"}
        ),
    )
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)

    results = preflight.run_preflight(plan_dir=tmp_path, which=_recording_which)
    check = _find_dispatch(results)
    assert check["status"] == "ok"
    assert "ollama" in probed
    assert "claude" not in probed


# --------------------------------------------------------------------------- #
# Priority: the env var outranks the registry.
# --------------------------------------------------------------------------- #
def test_env_claude_outranks_registry_ollama(tmp_path, monkeypatch):
    monkeypatch.setattr(
        role_registry,
        "load_registry",
        lambda: _registry_with_dispatch(
            {"provider": "ollama", "model": "glm-5.3-flash:cloud"}
        ),
    )
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")

    results = preflight.run_preflight(plan_dir=tmp_path, which=_path_which)
    check = _find_dispatch(results)
    assert check["status"] == "ok"
    lowered = check["message"].lower()
    assert "claude" in lowered
    assert "ollama" not in lowered


# --------------------------------------------------------------------------- #
# Status rules per resolved provider.
# --------------------------------------------------------------------------- #
def test_resolved_local_family_provider_absent_is_warn_and_actionable(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        role_registry,
        "load_registry",
        lambda: _registry_with_dispatch(
            {"provider": "ollama", "model": "glm-5.3-flash:cloud"}
        ),
    )
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)

    results = preflight.run_preflight(plan_dir=tmp_path, which=_none_which)
    check = _find_dispatch(results)
    # Graceful degradation, matching the existing local-family behavior.
    assert check["status"] == "warn"
    lowered = check["message"].lower()
    assert "ollama" in lowered
    assert "install" in lowered or "configur" in lowered


def test_resolved_claude_provider_absent_is_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(
        role_registry,
        "load_registry",
        lambda: _registry_with_dispatch({"provider": "claude", "model": "sonnet"}),
    )
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)

    results = preflight.run_preflight(plan_dir=tmp_path, which=_none_which)
    check = _find_dispatch(results)
    assert check["status"] == "fail"
    assert "claude" in check["message"].lower()


# --------------------------------------------------------------------------- #
# NEW warn case: nothing configured from any source.
# --------------------------------------------------------------------------- #
def test_nothing_configured_anywhere_warns_naming_both_ways_to_fix(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        role_registry, "load_registry", lambda: _registry_with_dispatch(None)
    )
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)

    results = preflight.run_preflight(plan_dir=tmp_path, which=_path_which)
    check = _find_dispatch(results)
    # Even with every CLI present, an unconfigured dispatch role is a warn:
    # the built-in default must never be presented as the operator's choice.
    assert check["status"] == "warn"
    assert "PIPELINE_BACKEND_DISPATCH" in check["message"]
    assert "model_registry.json" in check["message"]
    assert "claude" not in check["message"].lower()


# --------------------------------------------------------------------------- #
# Negative: resolution raises -> fail, class name only, nothing leaks.
# --------------------------------------------------------------------------- #
def test_resolution_failure_is_fail_naming_only_the_exception_class(
    tmp_path, monkeypatch
):
    leak_path = str(tmp_path / "model_registry.json")
    leak_token = "sk-fake-preflight-secret-xyz"
    leak_env_value = "sk-fake-env-backend-value"

    class _FakeResolutionError(RuntimeError):
        pass

    def _boom_resolve(*args, **kwargs):
        raise _FakeResolutionError(
            f"role 'dispatch' unreadable at {leak_path} token={leak_token}"
        )

    monkeypatch.setattr(role_registry, "resolve_role", _boom_resolve)
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", leak_env_value)

    results = preflight.run_preflight(plan_dir=tmp_path, which=_path_which)
    check = _find_dispatch(results)
    assert check["status"] == "fail"
    message = check["message"]
    assert "_FakeResolutionError" in message  # the exception CLASS name
    assert leak_token not in message  # never str(exc)
    assert leak_path not in message  # never a path carried by the exception
    assert leak_env_value not in message  # never an env value
    assert str(tmp_path) not in message


# --------------------------------------------------------------------------- #
# Boundary: empty / whitespace env value is treated as unset.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw", ["", "   "])
def test_empty_or_whitespace_env_value_is_treated_as_unset(
    raw, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        role_registry,
        "load_registry",
        lambda: _registry_with_dispatch(
            {"provider": "ollama", "model": "glm-5.3-flash:cloud"}
        ),
    )
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", raw)

    results = preflight.run_preflight(plan_dir=tmp_path, which=_path_which)
    check = _find_dispatch(results)
    assert check["status"] == "ok"
    lowered = check["message"].lower()
    assert "ollama" in lowered  # the registry was consulted
    assert "claude" not in lowered


# --------------------------------------------------------------------------- #
# The lazy import itself must never crash preflight.
# --------------------------------------------------------------------------- #
def test_registry_module_import_failure_is_fail_never_crash(tmp_path, monkeypatch):
    # Simulate a broken registry module: block the submodule in sys.modules
    # AND drop the attribute the `from app import role_registry` fallback
    # would otherwise satisfy (the package was already imported by conftest).
    import app

    monkeypatch.setitem(sys.modules, "app.role_registry", None)
    monkeypatch.delattr(app, "role_registry")

    results = preflight.run_preflight(plan_dir=tmp_path, which=_path_which)
    check = _find_dispatch(results)
    assert check["status"] == "fail"
    # The exception CLASS name only (ModuleNotFoundError subclasses
    # ImportError); never str(exc), which would carry the module path.
    assert "ModuleNotFoundError" in check["message"]
    assert "app/role_registry.py" not in check["message"]