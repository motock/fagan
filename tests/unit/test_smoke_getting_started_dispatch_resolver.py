"""Parity guard: the smoke's announced dispatch backend follows the registry.

Story SRR-1 (re-pin). This file used to pin the OPPOSITE premise: that
``model_registry.json``'s ``roles.dispatch`` entry is advisory only and must
NOT drive the smoke's announced backend, because real dispatch read
``PIPELINE_BACKEND_DISPATCH`` raw and never consulted
``app.role_registry.resolve_role``.

That premise was inverted by the ``registry-single-source-of-truth`` plan
(REG-1..REG-5). ``pipeline/dispatch.py``, ``pipeline/advance.py`` and
``pipeline/preflight.py`` now all resolve the dispatch role through
``app.role_registry.resolve_role``, and the registry OUTRANKS
``PIPELINE_BACKEND_<ROLE>`` (plan/story overrides sit above both). The smoke
was the last hand-rolled, env-only copy of that logic, so it announced a
backend real dispatch would never use - and printed an "advisory" note
reassuring the operator that the registry "only feeds the dashboard display
and decompose-time sizing, never real dispatch". That reassurance is now
false.

So this file is re-pinned to the new truth, and kept as a PARITY guard rather
than a tautology: the smoke's announced (provider, model) must equal
``role_registry.resolve_role("dispatch")`` for the same synthetic inputs, and
the advisory note must be gone.

The registry SOURCE (``app.role_registry.load_registry``) is stubbed and
``PIPELINE_MODEL_REGISTRY_PATH`` points at a temp file with the same contents,
so the real ``resolve_role`` precedence logic runs against synthetic inputs -
never against this machine's real ``model_registry.json``.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
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


def _install(monkeypatch, tmp_path, registry: dict, environ: dict) -> None:
    """Point the smoke's registry + env at SYNTHETIC inputs."""
    for key in [k for k in os.environ if k.startswith("PIPELINE_")]:
        monkeypatch.delenv(key, raising=False)
    for key, value in environ.items():
        monkeypatch.setenv(key, value)

    from app import role_registry

    monkeypatch.setattr(
        role_registry, "load_registry", lambda *a, **k: copy.deepcopy(registry)
    )
    registry_file = tmp_path / "synthetic_model_registry.json"
    registry_file.write_text(json.dumps(registry))
    monkeypatch.setenv("PIPELINE_MODEL_REGISTRY_PATH", str(registry_file))


def test_registry_dispatch_entry_drives_the_announced_backend(
    monkeypatch, capsys, tmp_path
):
    """The old worked example, inverted: env unset, roles.dispatch="local".

    Real dispatch now resolves "local" here (the registry outranks the env
    var), so the smoke must announce "local" - not "claude" - and must not
    print the obsolete advisory note.
    """
    registry = {"roles": {"dispatch": {"provider": "local"}}}
    _install(monkeypatch, tmp_path, registry, {})
    mod = _load_script()

    provider, _model, _source = mod._announce_dispatch_backend()

    assert provider == "local", (
        "with PIPELINE_BACKEND_DISPATCH unset and roles.dispatch pinned to "
        "'local', the registry is the source of truth for dispatch, so the "
        f"guard must announce 'local'; got {provider!r}"
    )

    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "local" in text, (
        f"the announce line must name the registry's provider; got {text!r}"
    )
    assert "advisory" not in text.lower(), (
        "the obsolete advisory registry note must be gone - the registry IS "
        f"the dispatch source now; got {text!r}"
    )
    assert "never real dispatch" not in text, (
        f"the guard must not claim the registry never drives dispatch; got {text!r}"
    )


def test_announced_backend_equals_resolve_role_for_synthetic_inputs(
    monkeypatch, capsys, tmp_path
):
    """PARITY: announced (provider, model) == resolve_role("dispatch")."""
    registry = {
        "providers": {
            "ollama": {
                "models": {"deepseek-v4.1-flash": {"tag": "deepseek-v4.1-flash:cloud"}}
            }
        },
        "roles": {"dispatch": {"provider": "ollama", "model": "deepseek-v4.1-flash"}},
    }
    environ = {"PIPELINE_BACKEND_DISPATCH": "lmstudio"}
    _install(monkeypatch, tmp_path, registry, environ)
    mod = _load_script()

    provider, model, _source = mod._announce_dispatch_backend()

    from app import role_registry

    expected = role_registry.resolve_role(
        "dispatch", registry=registry, environ=environ
    )
    assert (provider, model) == (expected.provider, expected.model), (
        "the smoke's announced backend must equal "
        "role_registry.resolve_role('dispatch') for the same synthetic inputs; "
        f"smoke={(provider, model)!r} "
        f"resolve_role={(expected.provider, expected.model)!r}"
    )
    assert provider == "ollama", (
        "the registry's roles.dispatch entry outranks "
        f"PIPELINE_BACKEND_DISPATCH; got {provider!r}"
    )
