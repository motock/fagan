"""Tests for pipeline.preflight check c: the dispatch backend check (PP-02).

Contract (corrected after review): check c reports the backend that real
per-story dispatch execution will actually run -- PIPELINE_BACKEND_DISPATCH
(default "claude"), exactly as pipeline/dispatch.py:291-294 resolves it via
_resolve_dispatch_backend. Real dispatch NEVER consults model_registry.json's
roles.dispatch key (the only registry path into real routing is the separate
"auto" -> routing.dispatch lookup), so this check must not either: resolving
via role_registry.resolve_role would green-light a provider real dispatch
will never invoke -- the same false-green defect class this check exists to
prevent, just on another provider.

Repo rule (`.claude/rules/testing-config-gates.md`): test the resolution
logic, never today's configured values. Every test injects `which`, and the
registry stub RAISES if consulted -- proving check c never touches the
registry (including resolve_role's own registry=None -> load_registry()
internal path) and never depends on the live model_registry.json.
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


def _registry_that_must_not_be_consulted():
    raise AssertionError(
        "check c consulted the model registry; real dispatch execution "
        "(pipeline/dispatch.py) resolves PIPELINE_BACKEND_DISPATCH directly "
        "and never reads roles.dispatch"
    )


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
def _forbid_registry_consultation(monkeypatch):
    """Fail loudly if check c reads the registry through any path.

    Covers both a direct `from app import role_registry` import and
    resolve_role's own registry=None -> load_registry() internal fallback.
    """
    from app import role_registry

    monkeypatch.setattr(
        role_registry, "load_registry", _registry_that_must_not_be_consulted
    )
    monkeypatch.setattr(
        role_registry,
        "resolve_role",
        _registry_that_must_not_be_consulted,
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
# The check must stay read-only w.r.t. the registry even if app.role_registry
# itself is broken (no import at module top, no crash at call time).
# --------------------------------------------------------------------------- #
def test_check_c_never_imports_the_registry_module(tmp_path, monkeypatch):
    import app

    # Block the submodule in sys.modules AND drop the attribute the
    # `from app import role_registry` fallback would otherwise satisfy.
    monkeypatch.setitem(sys.modules, "app.role_registry", None)
    monkeypatch.delattr(app, "role_registry")
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)

    results = preflight.run_preflight(plan_dir=tmp_path, which=_path_which)
    check = _find_dispatch(results)
    # The env-var check does not care: it still reports the claude default.
    assert check["status"] == "ok"
    assert "claude" in check["message"].lower()