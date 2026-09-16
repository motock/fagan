"""Tests for pipeline.preflight check c: the dispatch backend check (PP-02).

Contract (re-pinned by REG-3): check c reports the backend that real
per-story dispatch execution will actually run. Since REG-1/REG-2 that is
`app.role_registry.resolve_role("dispatch", ...)` -- see
pipeline/dispatch.py's `_resolve_dispatch_target` -- which consults
model_registry.json's roles.dispatch entry when PIPELINE_BACKEND_DISPATCH is
unset. This file previously asserted the opposite (that check c must read
PIPELINE_BACKEND_DISPATCH raw and never consult the registry), because at the
time real dispatch did exactly that; the prerequisite stories removed that
divergence, so the premise is inverted here.

The tests below therefore pin the env-var priority and the per-backend CLI
status rules against a registry that has NO roles.dispatch entry -- the
boundary where resolve_role falls through to its "claude" default, which is
the pre-registry behaviour these tests describe. Registry-pinned resolution
is covered by tests/unit/test_preflight_reports_registry_backend.py.

Repo rule (`.claude/rules/testing-config-gates.md`): test the resolution
logic, never today's configured values. Every test injects `which`, and the
autouse fixture stubs `role_registry.load_registry` with a synthetic payload,
so the live model_registry.json / PIPELINE_MODEL_REGISTRY_PATH is never
consulted.
"""

from __future__ import annotations

import sys

import pytest

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


def _registry_without_a_dispatch_entry():
    """Synthetic registry: no roles.dispatch entry anywhere.

    This is the boundary the tests below describe -- resolve_role falls
    through to its default provider ("claude"), exactly the pre-registry
    behaviour. It is deliberately NOT the live model_registry.json.
    """
    return {
        "providers": {
            "claude": {"models": {"sonnet": {"tag": "claude-sonnet-4"}}},
            "ollama": {
                "models": {"glm-5.3-flash:cloud": {"tag": "glm-5.3-flash:cloud"}}
            },
        },
        "roles": {"overlord": {"provider": "claude", "model": "sonnet"}},
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


@pytest.fixture(autouse=True)
def _stub_registry(monkeypatch):
    """Give check c a deterministic registry with no roles.dispatch entry.

    Check c resolves through role_registry.resolve_role, whose registry=None
    path calls load_registry(); stubbing it keeps these tests off the live
    model_registry.json (and off PIPELINE_MODEL_REGISTRY_PATH). With no
    roles.dispatch entry, resolve_role falls through to its "claude" default
    -- the pre-registry behaviour these tests describe.
    """
    from app import role_registry

    monkeypatch.setattr(
        role_registry, "load_registry", _registry_without_a_dispatch_entry
    )
    yield


# --------------------------------------------------------------------------- #
# Default: env unset -> "claude" (what pipeline/dispatch.py will actually run).
# --------------------------------------------------------------------------- #
def test_env_unset_defaults_to_claude_and_probes_the_claude_cli(
    tmp_path, monkeypatch
):
    probed = []

    def recording_which(name):
        probed.append(name)
        return f"/fake/bin/{name}"

    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)

    results = preflight.run_preflight(
        plan_dir=tmp_path, which=recording_which
    )
    check = _find_dispatch(results)
    assert check["status"] == "ok"
    assert "claude" in check["message"].lower()
    assert "claude" in probed


def test_env_unset_with_claude_cli_absent_is_fail(tmp_path, monkeypatch):
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)

    results = preflight.run_preflight(plan_dir=tmp_path, which=_none_which)
    check = _find_dispatch(results)
    assert check["status"] == "fail"
    lowered = check["message"].lower()
    assert "claude" in lowered
    assert "install" in lowered or "cli" in lowered


# --------------------------------------------------------------------------- #
# Boundary: empty / whitespace env value counts as unset -> "claude" default.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw", ["", "   "])
def test_empty_or_whitespace_env_value_falls_back_to_claude(
    raw, tmp_path, monkeypatch
):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", raw)

    results = preflight.run_preflight(plan_dir=tmp_path, which=_path_which)
    check = _find_dispatch(results)
    assert check["status"] == "ok"
    assert "claude" in check["message"].lower()


# --------------------------------------------------------------------------- #
# Env value wins over any registry content (registry must not be consulted).
# --------------------------------------------------------------------------- #
def test_env_ollama_selects_ollama_and_probes_its_cli(tmp_path, monkeypatch):
    probed = []

    def recording_which(name):
        probed.append(name)
        return f"/fake/bin/{name}"

    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "ollama")

    results = preflight.run_preflight(
        plan_dir=tmp_path, which=recording_which
    )
    check = _find_dispatch(results)
    assert check["status"] == "ok"
    assert "ollama" in check["message"].lower()
    assert "ollama" in probed
    assert "claude" not in probed


def test_env_value_is_stripped_and_lowercased(tmp_path, monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "  CLAUDE  ")

    results = preflight.run_preflight(plan_dir=tmp_path, which=_path_which)
    check = _find_dispatch(results)
    assert check["status"] == "ok"
    assert "claude" in check["message"].lower()

    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", " Ollama ")
    results = preflight.run_preflight(plan_dir=tmp_path, which=_none_which)
    check = _find_dispatch(results)
    assert check["status"] == "warn"
    assert "ollama" in check["message"].lower()


# --------------------------------------------------------------------------- #
# Status rules per selected backend.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("backend", ["ollama", "lmstudio", "mlx", "local", "auto"])
def test_local_family_backend_absent_is_warn_never_fail(
    backend, tmp_path, monkeypatch
):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", backend)

    results = preflight.run_preflight(plan_dir=tmp_path, which=_none_which)
    # Graceful degradation: warn, never fail, never raise.
    assert _find_dispatch(results)["status"] == "warn"


def test_local_family_absent_warn_message_names_provider_and_fix(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "ollama")

    results = preflight.run_preflight(plan_dir=tmp_path, which=_none_which)
    check = _find_dispatch(results)
    assert check["status"] == "warn"
    lowered = check["message"].lower()
    assert "ollama" in lowered
    assert "install" in lowered or "configur" in lowered


def test_lmstudio_maps_to_the_lms_cli(tmp_path, monkeypatch):
    probed = []

    def recording_which(name):
        probed.append(name)
        return f"/fake/bin/{name}"

    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "lmstudio")

    results = preflight.run_preflight(
        plan_dir=tmp_path, which=recording_which
    )
    check = _find_dispatch(results)
    assert check["status"] == "ok"
    assert "lms" in probed


# --------------------------------------------------------------------------- #
# Unrecognized value: never crash, never pass silently.
# --------------------------------------------------------------------------- #
def test_unknown_backend_value_warns_naming_the_known_set(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "carrier-pigeon")

    results = preflight.run_preflight(plan_dir=tmp_path, which=_path_which)
    check = _find_dispatch(results)
    assert check["status"] == "warn"
    lowered = check["message"].lower()
    assert "carrier-pigeon" in lowered
    assert "ollama" in lowered  # one of the known backends is named


# --------------------------------------------------------------------------- #
# Check c resolves through app.role_registry, so a broken registry import must
# degrade gracefully: no crash at call time, and the check still reports.
# --------------------------------------------------------------------------- #
def test_check_c_survives_a_broken_registry_import(tmp_path, monkeypatch):
    """REG-3 re-pin: check c now resolves through app.role_registry, so this
    test no longer asserts that the module is never imported (it previously
    did, because the check was env-var-only). What survives is the guarantee
    that a broken registry import never crashes preflight: the check still
    reports a status instead of raising.
    """
    import app

    # Block the submodule in sys.modules AND drop the attribute the
    # `from app import role_registry` fallback would otherwise satisfy.
    monkeypatch.setitem(sys.modules, "app.role_registry", None)
    monkeypatch.delattr(app, "role_registry")
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)

    results = preflight.run_preflight(plan_dir=tmp_path, which=_path_which)
    check = _find_dispatch(results)
    assert check["status"] in ("ok", "warn", "fail"), check
    assert check["message"], check
    assert "claude" in check["message"].lower()