"""AUTH-11: ``set_role_default`` must require ``config:write``.

``PipelineService.set_role_default`` rewrites ``model_registry.json`` -- the
default provider/model for every role on this host -- and is reachable from
the dashboard route ``app/dashboard.py:618``, which authenticates the caller
but never asks the policy. A host-scoped write has no plan to scope a check
to, so the guard is ``pipeline.authorization.refusal_for_host_config``: it
decides against an empty resource (which ``LocalPolicy`` grants only to the
local operator) and records the decision in the host-scoped chain,
``<PLAN_DIR>/.audit.host.jsonl``, because no plan chain can own it.

Every case below asserts the observable contract, not the implementation:
allowed writes land, refusals leave the registry byte-identical, and the
decision is recorded once in the host chain rather than in a plan chain.

The ``plan_dir`` / ``registry_path`` / ``seeded_registry`` fixtures are copied
verbatim from ``tests/unit/test_config_write_security.py`` (the module that
owns them) rather than imported: ``tests/unit/`` has no ``__init__.py``, so
there is no package path to import them from, only a bare module name that
pytest re-imports under its own collection name.
"""
from __future__ import annotations

import json

import pytest

from app import dashboard as d
from app.identity import Principal
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import server as psrv
from pipeline.actor import bind_principal

REGISTRY_PATH_ENV = "PIPELINE_MODEL_REGISTRY_PATH"
ENFORCE_ENV = "PIPELINE_POLICY_ENFORCE"

#: The role seeded in the test registry that every case targets.
TARGET_ROLE = "review"

#: A role the registry and ``PIPELINE_ROLES`` both do not declare.
UNKNOWN_ROLE = "no-such-role"

#: An authenticated non-operator: what an OIDC bearer token resolves to. The
#: policy allows it nothing on an empty resource, so every write must refuse.
ALICE = Principal(subject="alice", auth_method="oidc")


# --------------------------------------------------------------------------- #
# Fixtures -- copied verbatim from tests/unit/test_config_write_security.py
# --------------------------------------------------------------------------- #
@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Redirect every PLAN_DIR binding the write paths touch to one tmp dir."""
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(d, "PLAN_DIR", directory)
    monkeypatch.setattr(psrv, "PLAN_DIR", directory)
    monkeypatch.setattr(ppers, "PLAN_DIR", directory)
    monkeypatch.setattr(pcon, "PLAN_DIR", directory)
    return directory


@pytest.fixture
def registry_path(tmp_path, monkeypatch):
    """Point the model registry at a tmp file so writes never touch the real
    repo model_registry.json."""
    path = tmp_path / "registry.json"
    monkeypatch.setenv(REGISTRY_PATH_ENV, str(path))
    return path


@pytest.fixture
def seeded_registry(registry_path):
    """A registry with two providers and a couple of declared roles."""
    registry = {
        "providers": {
            "claude": {
                "models": {
                    "opus": {"tag": "opus"},
                    "sonnet": {"tag": "sonnet"},
                    "haiku": {"tag": "haiku"},
                }
            },
            "ollama": {
                "models": {
                    "glm": {"tag": "glm-5.2:cloud"},
                    "devstral": {"tag": "devstral:24b"},
                }
            },
        },
        "roles": {
            "review": {"provider": "ollama", "model": "glm"},
            "planner": {"provider": "claude", "model": "sonnet"},
        },
    }
    registry_path.write_text(json.dumps(registry, indent=2))
    return registry


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _svc() -> psrv.PipelineService:
    return psrv.PipelineService()


def _bytes(path):
    return path.read_bytes()


def _role_entry(registry_path, role):
    return json.loads(registry_path.read_text()).get("roles", {}).get(role)


def _host_decisions(plan_dir):
    """Return ``(action, outcome)`` for every record in the host chain."""
    path = plan_dir / ".audit.host.jsonl"
    if not path.exists():
        return []
    return [
        (record.get("action"), record.get("outcome"))
        for record in (json.loads(line) for line in path.read_text().splitlines() if line)
    ]


def _plan_chains(plan_dir):
    """Return the plan-scoped audit streams that exist, if any."""
    return sorted(path.name for path in plan_dir.glob("*.audit.jsonl"))


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_local_operator_still_sets_a_role_default(
    seeded_registry, registry_path, plan_dir, monkeypatch
):
    """No-regression case: nothing bound resolves to the local operator."""
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    result = _svc().set_role_default(TARGET_ROLE, "ollama", "devstral")

    assert (result.get("ok"), _role_entry(registry_path, TARGET_ROLE)) == (
        True,
        {"provider": "ollama", "model": "devstral"},
    )


def test_denied_principal_gets_refusal_and_registry_bytes_unchanged(
    seeded_registry, registry_path, plan_dir, monkeypatch
):
    """A principal the policy denies cannot repoint a role; the file is intact."""
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    before = _bytes(registry_path)
    with bind_principal(ALICE):
        result = _svc().set_role_default(TARGET_ROLE, "ollama", "devstral")

    assert (result.get("ok"), _bytes(registry_path) == before) == (False, True)


def test_denial_appends_one_denied_record_to_the_host_audit_chain(
    seeded_registry, registry_path, plan_dir, monkeypatch
):
    """The refusal is recorded host-scoped -- and in no plan's chain."""
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    with bind_principal(ALICE):
        _svc().set_role_default(TARGET_ROLE, "ollama", "devstral")

    assert (_host_decisions(plan_dir), _plan_chains(plan_dir)) == (
        [("config:write", "denied")],
        [],
    )


def test_allowed_case_appends_one_allowed_record_to_the_host_audit_chain(
    seeded_registry, registry_path, plan_dir, monkeypatch
):
    """An allowed host-scoped write is recorded in the same host chain."""
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    _svc().set_role_default(TARGET_ROLE, "ollama", "devstral")

    assert _host_decisions(plan_dir) == [("config:write", "allowed")]


def test_enforcement_disabled_lets_the_denied_principal_write(
    seeded_registry, registry_path, plan_dir, monkeypatch
):
    """The documented kill switch still wins over the new guard."""
    monkeypatch.setenv(ENFORCE_ENV, "0")
    with bind_principal(ALICE):
        result = _svc().set_role_default(TARGET_ROLE, "ollama", "devstral")

    assert (result.get("ok"), _role_entry(registry_path, TARGET_ROLE)) == (
        True,
        {"provider": "ollama", "model": "devstral"},
    )


def test_refusal_names_the_action_and_subject_without_registry_contents(plan_dir, monkeypatch):
    """The refusal says who and what, and never echoes registry contents."""
    from pipeline.authorization import refusal_for_host_config

    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    with bind_principal(ALICE):
        refusal = refusal_for_host_config("config:write", "the global role defaults")

    error = refusal["error"] if refusal is not None else ""
    assert ("config:write" in error, "alice" in error, "providers" in error) == (
        True,
        True,
        False,
    )


def test_unknown_role_is_still_rejected_for_an_allowed_principal(
    seeded_registry, registry_path, plan_dir, monkeypatch
):
    """The pre-existing validation in ``_set_role_default_impl`` is unchanged."""
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    before = _bytes(registry_path)
    result = _svc().set_role_default(UNKNOWN_ROLE, "ollama", "devstral")

    assert (result.get("ok"), _bytes(registry_path) == before) == (False, True)
