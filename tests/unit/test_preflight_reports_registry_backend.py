"""REG-3: preflight's dispatch-backend check must report the backend that
real per-story dispatch will actually run.

`run_preflight`'s check c (pipeline/preflight.py) reports the dispatch
role's resolution. Since REG-1/REG-2, real dispatch resolves that role
through `app.role_registry.resolve_role("dispatch", ...)` - see
pipeline/dispatch.py's `_resolve_dispatch_target` - which consults
model_registry.json's `roles.dispatch` entry when PIPELINE_BACKEND_DISPATCH
is unset. Check c must resolve the SAME way. Reporting the raw env var (the
PP-02 review's "fix") now names a backend that will never run on a
registry-pinned host: the same false-green defect class, pointing the other
way.

These tests grade observable behaviour, not message wording: they assert
(a) the provider name the check reports and (b) which CLI the check probed.
Parity is the real guard - `test_reported_backend_matches_dispatch_resolver`
compares check c's reported backend against pipeline/dispatch.py's own
resolver for the same inputs, so the two can never drift apart again
without this file going red.

Every test stubs `role_registry.load_registry` (so the live
model_registry.json / PIPELINE_MODEL_REGISTRY_PATH is never consulted) and
sets or deletes PIPELINE_BACKEND_DISPATCH explicitly (the ambient
environment may have it set).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app import role_registry
from pipeline import dispatch, preflight

CLAUDE = "claude"
OLLAMA = "ollama"
LMSTUDIO = "lmstudio"

# lmstudio's CLI is not named after the provider.
_CLI_FOR = {LMSTUDIO: "lms"}

# Every provider this file can resolve to, so a parity assertion can name
# the CLIs that must NOT have been probed.
_ALL_PROVIDERS = (CLAUDE, OLLAMA, LMSTUDIO)


def _cli_for(provider: str) -> str:
    return _CLI_FOR.get(provider, provider)


def _registry(dispatch_entry=None, *, roles=None) -> dict:
    """Synthetic registry payload.

    `dispatch_entry=None` means "no roles.dispatch entry at all" - the
    boundary case, where resolve_role falls through to its default provider.
    """
    payload_roles = {"overlord": {"provider": CLAUDE, "model": "sonnet"}}
    if roles is not None:
        payload_roles = dict(roles)
    if dispatch_entry is not None:
        payload_roles["dispatch"] = dispatch_entry
    return {
        "providers": {
            CLAUDE: {"models": {"sonnet": {"tag": "claude-sonnet-4"}}},
            OLLAMA: {
                "models": {"glm-5.3-flash:cloud": {"tag": "glm-5.3-flash:cloud"}}
            },
            LMSTUDIO: {"models": {"qwen": {"tag": "qwen-local"}}},
        },
        "roles": payload_roles,
    }


def _patch_registry(monkeypatch, payload) -> None:
    """Make resolve_role (and dispatch's own fallback) see `payload`.

    resolve_role's registry=None path calls role_registry.load_registry(),
    so patching that module attribute covers both preflight and
    pipeline/dispatch.py's resolver.
    """
    monkeypatch.setattr(role_registry, "load_registry", lambda: payload)


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


def _recording_which(present):
    """`which` stub: only the named CLIs exist; records every probe."""
    probed = []

    def which(name):
        probed.append(name)
        return f"/fake/bin/{name}" if name in present else None

    return which, probed


def _dispatch_resolved_backend() -> str:
    """The backend pipeline/dispatch.py will actually run for a plain story.

    This is the parity anchor: whatever dispatch's own resolver says for
    these inputs is what check c must report.
    """
    provider, _model = dispatch._resolve_dispatch_target({}, None)
    return provider


# --------------------------------------------------------------------------- #
# 1. POSITIVE: the registry's provider is the one reported.
# --------------------------------------------------------------------------- #
def test_env_unset_registry_pins_dispatch_to_ollama(tmp_path, monkeypatch):
    """Env unset + registry routes dispatch to ollama -> check c names
    ollama (the backend that will actually run) and PASSES its CLI check
    when the ollama CLI is present.

    On the pre-REG-3 code this reports claude and fails (claude's CLI is
    absent here), which is the false-green defect this story removes.
    """
    _patch_registry(
        monkeypatch,
        _registry({"provider": OLLAMA, "model": "glm-5.3-flash:cloud"}),
    )
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    which, probed = _recording_which({OLLAMA})

    check = _find_dispatch(
        preflight.run_preflight(plan_dir=tmp_path, which=which)
    )

    assert check["status"] == "ok", check
    assert OLLAMA in check["message"].lower(), check
    assert OLLAMA in probed, probed
    assert CLAUDE not in probed, probed


def test_registry_provider_is_reported_even_when_its_cli_is_absent(
    tmp_path, monkeypatch
):
    """The reported provider follows the registry, not the env default, in
    the failing direction too: claude's CLI is present, ollama's is not, and
    the check must still name ollama (warn, local family) - never green-light
    claude, which will not be dispatched to."""
    _patch_registry(
        monkeypatch,
        _registry({"provider": OLLAMA, "model": "glm-5.3-flash:cloud"}),
    )
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    which, probed = _recording_which({CLAUDE})

    check = _find_dispatch(
        preflight.run_preflight(plan_dir=tmp_path, which=which)
    )

    assert check["status"] == "warn", check
    assert OLLAMA in check["message"].lower(), check
    assert OLLAMA in probed, probed
    assert CLAUDE not in probed, probed


# --------------------------------------------------------------------------- #
# 2. PARITY: check c's reported backend == dispatch's own resolver.
# --------------------------------------------------------------------------- #
_PARITY_CONFIGS = [
    pytest.param(
        None,
        {"provider": OLLAMA, "model": "glm-5.3-flash:cloud"},
        id="registry-ollama-env-unset",
    ),
    pytest.param(
        None, {"provider": CLAUDE, "model": "sonnet"}, id="registry-claude-env-unset"
    ),
    pytest.param(
        None, {"provider": OLLAMA}, id="registry-ollama-provider-only"
    ),
    pytest.param(
        OLLAMA,
        {"provider": CLAUDE, "model": "sonnet"},
        id="env-ollama-beats-registry-claude",
    ),
    pytest.param(
        CLAUDE,
        {"provider": OLLAMA, "model": "glm-5.3-flash:cloud"},
        id="env-claude-beats-registry-ollama",
    ),
    pytest.param(
        LMSTUDIO,
        {"provider": CLAUDE, "model": "sonnet"},
        id="env-lmstudio-registry-model-mismatch",
    ),
    pytest.param(None, None, id="no-registry-dispatch-entry"),
]


@pytest.mark.parametrize("env_backend,dispatch_entry", _PARITY_CONFIGS)
def test_reported_backend_matches_dispatch_resolver(
    env_backend, dispatch_entry, tmp_path, monkeypatch
):
    """The real guard: for each synthetic config, check c's reported backend
    must equal what pipeline/dispatch.py's own resolver returns for the same
    inputs. If preflight and real dispatch ever drift apart again, this goes
    red - no provider name is hardcoded here.
    """
    if env_backend is None:
        monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    else:
        monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", env_backend)
    _patch_registry(monkeypatch, _registry(dispatch_entry))

    expected = _dispatch_resolved_backend()
    which, probed = _recording_which({_cli_for(expected)})

    check = _find_dispatch(
        preflight.run_preflight(plan_dir=tmp_path, which=which)
    )

    assert expected in check["message"].lower(), (
        f"preflight reported {check['message']!r} but pipeline/dispatch.py "
        f"resolves this story to {expected!r}"
    )
    assert _cli_for(expected) in probed, (
        f"preflight did not probe {_cli_for(expected)!r} (probed {probed!r})"
    )
    for other in _ALL_PROVIDERS:
        if _cli_for(other) == _cli_for(expected):
            continue
        assert _cli_for(other) not in probed, (
            f"preflight probed {_cli_for(other)!r} as well as "
            f"{_cli_for(expected)!r} (probed {probed!r})"
        )


# --------------------------------------------------------------------------- #
# 3. NEGATIVE: a missing CLI still fails closed, naming the resolved backend.
# --------------------------------------------------------------------------- #
def test_registry_pinned_claude_with_cli_absent_fails_closed(
    tmp_path, monkeypatch
):
    """The resolved backend's CLI is missing -> check c still FAILS closed,
    naming the backend it resolved."""
    _patch_registry(monkeypatch, _registry({"provider": CLAUDE, "model": "sonnet"}))
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    which, probed = _recording_which(set())

    check = _find_dispatch(
        preflight.run_preflight(plan_dir=tmp_path, which=which)
    )

    assert check["status"] == "fail", check
    assert CLAUDE in check["message"].lower(), check
    assert CLAUDE in probed, probed


def test_registry_pinned_ollama_with_cli_absent_is_never_green(
    tmp_path, monkeypatch
):
    """Registry pins dispatch to ollama and ollama's CLI is absent: the local
    family degrades to a warn - never a silent ok - and the message names
    ollama, the backend that will actually be attempted."""
    _patch_registry(
        monkeypatch,
        _registry({"provider": OLLAMA, "model": "glm-5.3-flash:cloud"}),
    )
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    which, probed = _recording_which(set())

    check = _find_dispatch(
        preflight.run_preflight(plan_dir=tmp_path, which=which)
    )

    assert check["status"] == "warn", check
    assert OLLAMA in check["message"].lower(), check
    assert OLLAMA in probed, probed


def test_registry_pinned_ollama_checks_ollama_even_when_env_names_claude(
    tmp_path, monkeypatch
):
    """Since REG-4 the registry outranks the env var, so the CLI that gets
    checked is the registry's ollama: its absence fails closed naming
    ollama, never the env-pinned claude (which will not run)."""
    _patch_registry(
        monkeypatch,
        _registry({"provider": OLLAMA, "model": "glm-5.3-flash:cloud"}),
    )
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", CLAUDE)
    which, probed = _recording_which(set())

    check = _find_dispatch(
        preflight.run_preflight(plan_dir=tmp_path, which=which)
    )

    assert check["status"] == "warn", check
    assert OLLAMA in check["message"].lower(), check
    assert OLLAMA in probed, probed
    assert CLAUDE not in probed, probed


# --------------------------------------------------------------------------- #
# 4. BOUNDARY: no registry roles entry -> same behaviour as today.
# --------------------------------------------------------------------------- #
def test_no_registry_dispatch_entry_behaves_as_today(tmp_path, monkeypatch):
    """No roles.dispatch entry anywhere -> the pre-registry behaviour (env
    unset -> claude), and claude's CLI is the one checked."""
    _patch_registry(monkeypatch, _registry(None))
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    which, probed = _recording_which({CLAUDE})

    check = _find_dispatch(
        preflight.run_preflight(plan_dir=tmp_path, which=which)
    )

    assert check["status"] == "ok", check
    assert CLAUDE in check["message"].lower(), check
    assert CLAUDE in probed, probed
    assert OLLAMA not in probed, probed


def test_no_registry_dispatch_entry_with_cli_absent_is_fail(
    tmp_path, monkeypatch
):
    """Same boundary, failing direction: env unset, no roles.dispatch entry,
    claude's CLI absent -> fail naming claude."""
    _patch_registry(monkeypatch, _registry(None))
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    which, probed = _recording_which(set())

    check = _find_dispatch(
        preflight.run_preflight(plan_dir=tmp_path, which=which)
    )

    assert check["status"] == "fail", check
    assert CLAUDE in check["message"].lower(), check
    assert CLAUDE in probed, probed


def test_registry_without_a_roles_block_behaves_as_today(tmp_path, monkeypatch):
    """A registry with no `roles` key at all (the shape of this repo's own
    model_registry.json) must not crash the check: env unset -> claude."""
    _patch_registry(
        monkeypatch,
        {"providers": {CLAUDE: {"models": {"sonnet": {"tag": "sonnet"}}}}},
    )
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    which, probed = _recording_which({CLAUDE})

    check = _find_dispatch(
        preflight.run_preflight(plan_dir=tmp_path, which=which)
    )

    assert check["status"] == "ok", check
    assert CLAUDE in check["message"].lower(), check
    assert CLAUDE in probed, probed


def test_empty_registry_payload_behaves_as_today(tmp_path, monkeypatch):
    """An empty registry payload is the degenerate boundary: still claude."""
    _patch_registry(monkeypatch, {})
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    which, probed = _recording_which({CLAUDE})

    check = _find_dispatch(
        preflight.run_preflight(plan_dir=tmp_path, which=which)
    )

    assert check["status"] == "ok", check
    assert CLAUDE in check["message"].lower(), check
    assert CLAUDE in probed, probed


# --------------------------------------------------------------------------- #
# 5. The authorized existing-test edits themselves (REG-3).
#
# The story inverts the premise of two existing test modules, so their
# scenario/docstrings had to be re-pinned rather than deleted. These
# assertions grade that the re-pin happened and that the now-dead names are
# gone, so a later edit cannot quietly restore the inverted premise.
# --------------------------------------------------------------------------- #
_UNIT_DIR = Path(__file__).resolve().parent
_PARITY_TEST = _UNIT_DIR / "test_preflight_dispatch_matches_real_resolution.py"
_EFFECTIVE_BACKEND_TEST = _UNIT_DIR / "test_preflight_effective_backend.py"

_OLD_PARITY_TEST_NAME = (
    "test_env_unset_registry_routes_to_ollama_but_real_dispatch_uses_claude"
)
_NEW_PARITY_TEST_NAME = (
    "test_env_unset_registry_routes_to_ollama_reports_ollama_and_passes"
)


def _module_ast(path):
    return ast.parse(path.read_text(encoding="utf-8"))


def _function_names(tree):
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_parity_test_module_records_the_inversion():
    """The parity module's docstring must state the new truth and record that
    it previously pinned the opposite, and why (dispatch now resolves through
    the registry)."""
    tree = _module_ast(_PARITY_TEST)
    doc = (ast.get_docstring(tree) or "").lower()

    assert doc, "the parity test module lost its docstring"
    assert "opposite" in doc, (
        "the parity module's docstring no longer records that it previously "
        "pinned the opposite of what it now asserts"
    )
    assert "resolve_role" in doc, (
        "the parity module's docstring no longer explains that dispatch now "
        "resolves through role_registry.resolve_role"
    )
    assert "registry" in doc, (
        "the parity module's docstring no longer mentions the registry"
    )


def test_parity_scenario_test_was_re_pinned_not_deleted():
    """The review's scenario must survive as a test (re-pinned), not be
    deleted: the old inverted name is gone, the re-pinned one is present."""
    names = _function_names(_module_ast(_PARITY_TEST))

    assert _NEW_PARITY_TEST_NAME in names, (
        f"{_NEW_PARITY_TEST_NAME} is missing; the review's scenario must be "
        "re-pinned, not deleted"
    )
    assert _OLD_PARITY_TEST_NAME not in names, (
        f"{_OLD_PARITY_TEST_NAME} still exists; its premise (preflight must "
        "report claude and fail) is inverted by REG-3"
    )


def test_effective_backend_module_no_longer_forbids_registry_consultation():
    """The now-dead names from the env-only premise must be gone: the
    'registry must not be consulted' stub and the test that asserted check c
    never imports the registry module."""
    text = _EFFECTIVE_BACKEND_TEST.read_text(encoding="utf-8")

    assert "_registry_that_must_not_be_consulted" not in text, (
        "the dead 'registry must not be consulted' stub is still present"
    )
    assert "test_check_c_never_imports_the_registry_module" not in text, (
        "the dead 'check c never imports the registry module' test is still "
        "present; check c now resolves through app.role_registry"
    )
    assert "test_check_c_survives_a_broken_registry_import" in text, (
        "the re-pinned broken-registry-import test is missing"
    )
