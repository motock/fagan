"""Tests for the operator-selected authorizer plugin (ADR 0009 seam).

The authorizer is the enforcement point of ADR 0009, so a plugin replaces
:class:`app.policy.LocalPolicy` only when the operator names it through
``PIPELINE_AUTHORIZER_PLUGIN=module:attr``, a bad spec raises instead of
silently falling back, and an exception inside the plugin lands denied.
"""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path

import pytest

import app.policy
from app.identity import Principal
from app.plugin_loader import PluginError
from app.policy import (
    LOCAL_OPERATOR,
    Action,
    LocalPolicy,
    Resource,
    default_authorizer,
    is_allowed,
)

#: The operator-controlled variable that selects the authorizer seam.
PLUGIN_ENV = "PIPELINE_AUTHORIZER_PLUGIN"

#: A non-operator principal, so only the plugin can be answering.
OIDC_ALICE = Principal(subject="oidc:alice", auth_method="oidc")


def _reset_authorizer() -> None:
    """Drop the process-lifetime resolved authorizer (the single cache slot)."""
    app.policy._AUTHORIZER = None


@pytest.fixture(autouse=True)
def clear_authorizer_cache() -> None:
    """Isolate every test from the process-wide resolved authorizer."""
    _reset_authorizer()
    yield
    _reset_authorizer()


def _install_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, source: str
) -> None:
    """Write ``source`` as module ``name`` in ``tmp_path`` and select it."""
    (tmp_path / f"{name}.py").write_text(source, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv(PLUGIN_ENV, f"{name}:build")


ALLOWING_PLUGIN = '''
FACTORY_CALLS = 0


class _Allowing:
    def allow(self, principal, action, resource=None):
        return True


def build():
    global FACTORY_CALLS
    FACTORY_CALLS += 1
    return _Allowing()
'''

DENYING_PLUGIN = '''
FACTORY_CALLS = 0


class _Denying:
    def allow(self, principal, action, resource=None):
        return False


def build():
    global FACTORY_CALLS
    FACTORY_CALLS += 1
    return _Denying()
'''

RAISING_PLUGIN = '''
FACTORY_CALLS = 0


class _Raising:
    def allow(self, principal, action, resource=None):
        raise RuntimeError("policy backend unavailable")


def build():
    global FACTORY_CALLS
    FACTORY_CALLS += 1
    return _Raising()
'''

ALLOWLESS_PLUGIN = '''
FACTORY_CALLS = 0


class _NoAllow:
    pass


def build():
    global FACTORY_CALLS
    FACTORY_CALLS += 1
    return _NoAllow()
'''


def test_unset_variable_returns_local_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no plugin selected the built-in local policy is unchanged."""
    monkeypatch.delenv(PLUGIN_ENV, raising=False)

    assert isinstance(default_authorizer(), LocalPolicy)


def test_selected_plugin_authorizer_replaces_local_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator-supplied authorizer is returned instead of LocalPolicy."""
    _install_plugin(tmp_path, monkeypatch, "policy_plugin_allowing", ALLOWING_PLUGIN)

    authorizer = default_authorizer()

    assert not isinstance(authorizer, LocalPolicy)
    assert callable(getattr(authorizer, "allow", None))


def test_plugin_authorizer_is_cached_across_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stateful authorizer is built once, not on every authorization check."""
    _install_plugin(tmp_path, monkeypatch, "policy_plugin_cached", ALLOWING_PLUGIN)

    first = default_authorizer()
    second = default_authorizer()

    assert first is second
    assert importlib.import_module("policy_plugin_cached").FACTORY_CALLS == 1


def test_is_allowed_honors_plugin_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``is_allowed`` asks the plugin, returning whatever it decides."""
    _install_plugin(tmp_path, monkeypatch, "policy_plugin_denier", DENYING_PLUGIN)
    resource = Resource(tenant="acme", plan="plan-1")

    assert is_allowed(default_authorizer(), OIDC_ALICE, Action.PLAN_APPROVE, resource) is False

    _install_plugin(tmp_path, monkeypatch, "policy_plugin_allower", ALLOWING_PLUGIN)
    # The authorizer is frozen per process, so drop the resolved one to model
    # a fresh start-up that selects the other plugin.
    _reset_authorizer()

    assert is_allowed(default_authorizer(), OIDC_ALICE, Action.PLAN_APPROVE, resource) is True


def test_plugin_exception_lands_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plugin whose ``allow`` raises is denied, and the instance stays cached."""
    _install_plugin(tmp_path, monkeypatch, "policy_plugin_raising", RAISING_PLUGIN)
    authorizer = default_authorizer()

    assert is_allowed(authorizer, OIDC_ALICE, Action.PLAN_APPROVE) is False
    assert default_authorizer() is authorizer
    assert importlib.import_module("policy_plugin_raising").FACTORY_CALLS == 1


def test_invalid_plugin_spec_raises_instead_of_falling_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spec naming a module that cannot be imported never yields LocalPolicy."""
    monkeypatch.setenv(PLUGIN_ENV, "no_such_module:build")

    with pytest.raises(PluginError):
        default_authorizer()

    assert app.policy._AUTHORIZER is None


def test_plugin_without_allow_raises_and_is_never_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A factory returning an object without ``allow`` raises on every call."""
    _install_plugin(tmp_path, monkeypatch, "policy_plugin_allowless", ALLOWLESS_PLUGIN)

    with pytest.raises(PluginError, match="allow"):
        default_authorizer()

    # The unvalidated object must never be cached: a later call re-runs the
    # factory and raises again rather than handing back a policy-less object.
    assert app.policy._AUTHORIZER is None
    # A failed build must not leave the process-wide lock held.
    assert app.policy._AUTHORIZER_LOCK.locked() is False
    with pytest.raises(PluginError, match="allow"):
        default_authorizer()
    assert importlib.import_module("policy_plugin_allowless").FACTORY_CALLS == 2


def test_blank_variable_returns_local_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blank variable is treated as unset, so the built-in policy stays."""
    monkeypatch.setenv(PLUGIN_ENV, "   ")

    assert isinstance(default_authorizer(), LocalPolicy)
    assert default_authorizer().allow(LOCAL_OPERATOR, Action.PLAN_APPROVE) is True


def test_resolution_is_frozen_after_the_first_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later environment write cannot swap the policy of a running process.

    The authorizer is resolved once per process: re-reading
    ``PIPELINE_AUTHORIZER_PLUGIN`` per decision would let any in-process writer
    redefine who is allowed, with no restart and no audit record of the swap.
    """
    _install_plugin(tmp_path, monkeypatch, "policy_plugin_frozen_allower", ALLOWING_PLUGIN)
    first = default_authorizer()
    resource = Resource(tenant="acme", plan="plan-1")

    assert is_allowed(first, OIDC_ALICE, Action.PLAN_APPROVE, resource) is True

    # Another part of the process rewrites the variable after start-up.
    _install_plugin(tmp_path, monkeypatch, "policy_plugin_frozen_denier", DENYING_PLUGIN)

    assert default_authorizer() is first
    assert is_allowed(default_authorizer(), OIDC_ALICE, Action.PLAN_APPROVE, resource) is True
    assert app.policy._AUTHORIZER is first


def test_missing_factory_raises_plugin_error_not_type_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured seam whose factory cannot be resolved fails fast, cleanly.

    ``load_plugin`` returns ``None`` when the variable is unset, so a
    configured-but-unresolved seam must raise ``PluginError`` rather than call
    ``None`` (which would be an unhandled ``TypeError`` on the decision path,
    outside ``is_allowed``'s fail-closed wrapper).
    """
    monkeypatch.setattr(app.policy, "plugin_spec", lambda seam: "no_such:build")
    monkeypatch.setattr(app.policy, "load_plugin", lambda seam: None)

    with pytest.raises(PluginError):
        default_authorizer()

    assert app.policy._AUTHORIZER is None


def test_unset_seam_warns_and_returns_local_policy(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Running the authorizer seam without its plugin is auditable."""
    monkeypatch.delenv(PLUGIN_ENV, raising=False)

    with caplog.at_level(logging.WARNING):
        authorizer = default_authorizer()

    assert isinstance(authorizer, LocalPolicy)
    assert "no plugin configured for a security-sensitive seam" in caplog.text


def test_plugin_denial_reaches_refusal_path_and_audit_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plugin denial becomes the clean refusal dict and an audit record."""
    _install_plugin(tmp_path, monkeypatch, "policy_plugin_audit_denier", DENYING_PLUGIN)

    # pipeline.server must be imported before pipeline.ingest (see
    # tests/unit/test_authorization.py) so the import cycle resolves.
    import pipeline.server as p
    from pipeline import concurrency as pcon
    from pipeline import persistence as ppers
    from pipeline.actor import LOCAL_OPERATOR as ACTOR_OPERATOR
    from pipeline.actor import bind_principal
    from pipeline.authorization import ENFORCE_ENV, authorize

    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", plan_dir)
    monkeypatch.setattr(ppers, "PLAN_DIR", plan_dir)
    monkeypatch.setattr(pcon, "PLAN_DIR", plan_dir)
    monkeypatch.delenv(ENFORCE_ENV, raising=False)

    # LocalPolicy would allow the local operator; only the plugin denies it.
    with bind_principal(ACTOR_OPERATOR):
        result = authorize("story:merge", plan_name="p", story_key="S", manifest={})

    assert result is not None
    assert result["ok"] is False
    assert result["forbidden"] is True
    records = [
        json.loads(line)
        for line in (plan_dir / "p.notifications.jsonl").read_text().splitlines()
    ]
    assert [record["event"] for record in records] == ["authorization_denied"]
