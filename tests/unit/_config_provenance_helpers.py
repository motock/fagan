"""Shared fixtures/helpers for the pipeline.config_provenance test suite,
split across test_config_provenance_*.py files (originally one 3,117-line
test_config_provenance.py) to keep each file under the project's line-count
target.
"""
import json
import plistlib
from pathlib import Path

import pytest

# Lazily imported inside tests so collection does not hard-fail before the
# first test runs (the implementation does not exist yet on this branch).
RoleResolution = None
RoleRegistryError = None


def _ensure_role_registry_imports():
    """Import RoleResolution / RoleRegistryError from app.role_registry.

    Done lazily so the suite stays RED (per-test import errors) rather than
    failing at collection time before the implementation exists.
    """
    global RoleResolution, RoleRegistryError
    from app import role_registry

    RoleResolution = role_registry.RoleResolution
    RoleRegistryError = role_registry.RoleRegistryError
    return role_registry


@pytest.fixture(autouse=True)
def _load_role_registry_imports(request):
    """Ensure RoleResolution / RoleRegistryError are set on the CALLING test
    module's namespace before any test in it runs, so that module's bare-name
    references resolve. Set via request.module (not a bare `global` in this
    helper module) because this fixture is imported into several
    test_config_provenance_*.py split files - a bare `global` here would only
    update THIS module's own copy, leaving each importing split file's
    already-bound `RoleResolution = None` (captured at import time) stale.
    If the import fails (implementation not present yet), leave the names as
    None so the test surfaces the failure itself rather than erroring at
    collection time."""
    try:
        role_registry = _ensure_role_registry_imports()
        request.module.RoleResolution = role_registry.RoleResolution
        request.module.RoleRegistryError = role_registry.RoleRegistryError
    except ImportError:
        pass

# The module is imported lazily inside fixtures/tests so that collection of
# this file itself does not hard-fail before the first test runs: we want the
# RED state to surface as per-test import errors, not a collection error that
# hides which assertion is unmet.


def _import_module():
    import pipeline.config_provenance as mod

    return mod


# ---------------------------------------------------------------------------
# Helpers to build plist payloads.
# ---------------------------------------------------------------------------

_PLIST_HEADER = b"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
_PLIST_DOCTYPE = b"<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"


def _write_plist(tmp_path: Path, env: dict) -> Path:
    """Write a minimal plist whose top-level dict has an EnvironmentVariables key."""
    payload = {"EnvironmentVariables": env}
    data = plistlib.dumps(payload, fmt=plistlib.FMT_XML)
    p = tmp_path / "scheduler.plist"
    p.write_bytes(data)
    return p


def _write_plist_raw(tmp_path: Path, raw: bytes, name: str = "scheduler.plist") -> Path:
    p = tmp_path / name
    p.write_bytes(raw)
    return p


def _write_json(tmp_path: Path, payload, name: str = "claude.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _build_registry(roles=None, providers=None):
    """Build a minimal model_registry.json-shaped dict.

    ``roles`` maps role_name -> {"provider": ..., "model": ...}.
    ``providers`` maps provider_name -> {"models": {friendly: {"tag": tag}}}.
    """
    roles = roles or {}
    providers = providers or {}
    return {"roles": roles, "providers": providers}


def _registry_with_claude_sonnet():
    """A registry where role 'overlord' uses claude/sonnet (friendly -> tag)."""
    return _build_registry(
        roles={"overlord": {"provider": "claude", "model": "sonnet"}},
        providers={
            "claude": {
                "models": {
                    "sonnet": {"tag": "claude-3-5-sonnet"},
                    "haiku": {"tag": "claude-3-5-haiku"},
                    "opus": {"tag": "claude-3-opus"},
                }
            },
            "openai": {
                "models": {
                    "gpt4": {"tag": "gpt-4o"},
                }
            },
        },
    )


__all__ = [
    "_PLIST_DOCTYPE",
    "_PLIST_HEADER",
    "RoleRegistryError",
    "RoleResolution",
    "_build_registry",
    "_ensure_role_registry_imports",
    "_import_module",
    "_load_role_registry_imports",
    "_registry_with_claude_sonnet",
    "_write_json",
    "_write_plist",
    "_write_plist_raw",
]
