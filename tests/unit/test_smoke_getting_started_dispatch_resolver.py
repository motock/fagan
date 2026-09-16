"""Regression test for the reviewer's Blocking finding #1 on scripts/smoke_getting_started.py.

`_announce_dispatch_backend()` (no `value=` argument) is supposed to guard the
same thing real dispatch actually decides. But real dispatch NEVER consults
`app.role_registry.resolve_role("dispatch")` for provider selection -
`pipeline/dispatch.py`, `pipeline/advance.py`, and `pipeline/preflight.py`
all read the raw `PIPELINE_BACKEND_DISPATCH` env var directly
(`.strip().lower()`, default `"claude"`) and never call `resolve_role`.

The current implementation's `_default_dispatch_resolver` (nested inside
`_announce_dispatch_backend`, scripts/smoke_getting_started.py:128-176) calls
`resolve_role("dispatch", ...)`, whose precedence chain falls through to
`model_registry.json`'s `roles.dispatch` entry when the env var is unset.
That entry is a legitimate, real-world configuration for two OTHER,
unrelated call sites (`pipeline/service.py`'s dashboard display and
`pipeline/planner.py`'s decompose-time sizing guidance) - but it does not
govern what backend a story actually dispatches on.

Concrete failure (from the review): PIPELINE_BACKEND_DISPATCH unset,
model_registry.json has roles.dispatch = "local" (legitimate for the
dashboard/sizing use cases). A real dispatched story in this state resolves
to "claude" (env unset -> "claude" default). The smoke guard, as written
today, resolves to "local" via the registry fallback and refuses to run
(exit 2) - a false negative that would block every legitimate use of
roles.dispatch for its actual (reporting/sizing) purpose.

Per testing-config-gates.md, this stubs the registry SOURCE
(`app.role_registry.load_registry`, the thing that reads
model_registry.json) rather than asserting against whatever
model_registry.json currently contains on disk, and lets the real
`resolve_role` precedence logic run against that stub - so the test
exercises production's actual resolution logic, not a hand-rolled
replica of it.

This test is expected to be RED against today's implementation (the guard
raises SystemExit(2) when it should pass) until `_default_dispatch_resolver`
is fixed to mirror pipeline/preflight.py's raw env-var-with-claude-default
logic and stop consulting the registry.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "smoke_getting_started.py"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_script():
    if not SCRIPT_PATH.exists():
        pytest.fail(f"scripts/smoke_getting_started.py not found at {SCRIPT_PATH}.")
    mod_name = "smoke_getting_started_under_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def test_guard_ignores_registry_dispatch_entry_when_env_var_unset(monkeypatch, capsys):
    """Worked example from the review: env unset, roles.dispatch="local".

    Real dispatch (pipeline/dispatch.py, pipeline/advance.py,
    pipeline/preflight.py) would resolve to "claude" in this exact state -
    none of them ever consult app.role_registry.resolve_role. The guard
    must agree and pass, not exit 2.
    """
    mod = _load_script()
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)

    from app import role_registry

    # Stub the config SOURCE only (load_registry), not resolve_role itself -
    # resolve_role's real precedence logic still runs against this stub.
    monkeypatch.setattr(
        role_registry,
        "load_registry",
        lambda *a, **k: {"roles": {"dispatch": {"provider": "local"}}},
    )

    try:
        mod._announce_dispatch_backend()
    except SystemExit as exc:
        pytest.fail(
            "with PIPELINE_BACKEND_DISPATCH unset, the guard must resolve "
            "the same way real dispatch does (raw env var, default "
            "'claude') and IGNORE model_registry.json's roles.dispatch "
            "entry - that entry only feeds the dashboard/sizing call "
            "sites, never actual dispatch. Got SystemExit"
            f"({exc.code!r}); stderr: {capsys.readouterr().err!r}"
        )

    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert any(
        "claude" in line and "PIPELINE_BACKEND_DISPATCH" in line
        for line in text.splitlines()
    ), (
        "the guard must announce the resolved provider 'claude' (the raw "
        "env-var default), not the registry's roles.dispatch entry; "
        f"got: {text!r}"
    )
