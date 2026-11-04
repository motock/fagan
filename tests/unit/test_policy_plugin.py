"""Tests for the operator-selected authorizer plugin (ADR 0009 seam).

The authorizer is the enforcement point of ADR 0009, so a plugin replaces
:class:`app.policy.LocalPolicy` only when the operator names it through
``PIPELINE_AUTHORIZER_PLUGIN=module:attr`` (and only when
``PIPELINE_PLUGIN_MODULES`` allows that module), a bad spec raises instead of
silently falling back, an exception inside the plugin lands denied, and only
the literal ``True`` returned by ``allow()`` authorizes.
"""

from __future__ import annotations

import importlib
import json
import logging
import sys
from pathlib import Path

import pytest

import app.policy
from app.identity import Principal
from app.plugin_loader import ALLOWLIST_ENV, PluginError
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

#: A plan scope chain for the decision calls.
RESOURCE = Resource(tenant="acme", team="core", repo="fagan", plan="plan-42")


@pytest.fixture(autouse=True)
def clear_authorizer_cache() -> None:
    """Isolate every test from the process-wide authorizer plugin cache."""
    app.policy._PLUGIN_AUTHORIZERS.clear()
    yield
    app.policy._PLUGIN_AUTHORIZERS.clear()


def _install_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    source: str,
    *,
    allowlist: str | None = None,
) -> None:
    """Write ``source`` as module ``name`` in ``tmp_path`` and select it.

    The authorizer seam requires ``PIPELINE_PLUGIN_MODULES``, so the module name
    is allowlisted unless a test asks for something else (``"<unset>"`` leaves
    the variable unset).
    """
    (tmp_path / f"{name}.py").write_text(source, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv(PLUGIN_ENV, f"{name}:build")
    if allowlist is None:
        allowlist = name
    if allowlist == "<unset>":
        monkeypatch.delenv(ALLOWLIST_ENV, raising=False)
    else:
        monkeypatch.setenv(ALLOWLIST_ENV, allowlist)


def _returning_plugin(result_expression: str) -> str:
    """Return plugin source whose ``allow`` returns ``result_expression``."""
    return f'''
FACTORY_CALLS = 0


class _Authorizer:
    def allow(self, principal, action, resource=None):
        return {result_expression}


def build():
    global FACTORY_CALLS
    FACTORY_CALLS += 1
    return _Authorizer()
'''


ALLOWING_PLUGIN = _returning_plugin("True")
DENYING_PLUGIN = _returning_plugin("False")

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
    monkeypatch.delenv(ALLOWLIST_ENV, raising=False)

    assert isinstance(default_authorizer(), LocalPolicy)
    assert is_allowed(default_authorizer(), LOCAL_OPERATOR, Action.PLAN_APPROVE) is True


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
    """``is_allowed`` asks the plugin and returns a real bool either way."""
    _install_plugin(tmp_path, monkeypatch, "policy_plugin_denier", DENYING_PLUGIN)

    assert is_allowed(default_authorizer(), OIDC_ALICE, Action.PLAN_APPROVE, RESOURCE) is False

    _install_plugin(tmp_path, monkeypatch, "policy_plugin_allower", ALLOWING_PLUGIN)

    assert is_allowed(default_authorizer(), OIDC_ALICE, Action.PLAN_APPROVE, RESOURCE) is True


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
    monkeypatch.setenv(ALLOWLIST_ENV, "no_such_module")

    with pytest.raises(PluginError):
        default_authorizer()

    assert app.policy._PLUGIN_AUTHORIZERS == {}


def test_plugin_without_allow_raises_and_is_never_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A factory returning an object without ``allow`` raises on every call."""
    _install_plugin(tmp_path, monkeypatch, "policy_plugin_allowless", ALLOWLESS_PLUGIN)

    with pytest.raises(PluginError, match="allow"):
        default_authorizer()

    # The unvalidated object must never be cached: a later call re-runs the
    # factory and raises again rather than handing back a policy-less object.
    assert app.policy._PLUGIN_AUTHORIZERS == {}
    with pytest.raises(PluginError, match="allow"):
        default_authorizer()
    assert importlib.import_module("policy_plugin_allowless").FACTORY_CALLS == 2


def test_blank_variable_returns_local_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blank variable is treated as unset, so the built-in policy stays."""
    monkeypatch.setenv(PLUGIN_ENV, "   ")
    monkeypatch.delenv(ALLOWLIST_ENV, raising=False)

    assert isinstance(default_authorizer(), LocalPolicy)
    assert default_authorizer().allow(LOCAL_OPERATOR, Action.PLAN_APPROVE) is True


def test_missing_factory_raises_plugin_error_not_type_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured seam whose factory cannot be resolved fails cleanly.

    ``load_plugin`` returns ``None`` when the variable is unset, so a
    configured-but-unresolved seam must raise ``PluginError`` rather than call
    ``None`` (which would be an unhandled ``TypeError`` on the decision path,
    outside ``is_allowed``'s fail-closed wrapper).
    """
    monkeypatch.setenv(PLUGIN_ENV, "no_such_module:build")
    monkeypatch.setenv(ALLOWLIST_ENV, "no_such_module")
    monkeypatch.setattr(app.policy, "load_plugin", lambda seam, *a, **k: None)

    with pytest.raises(PluginError):
        default_authorizer()

    assert app.policy._PLUGIN_AUTHORIZERS == {}


@pytest.mark.parametrize(
    "result_expression",
    ['"denied"', '"yes"', '{"ok": False}', "1", "None", "[]"],
)
def test_non_bool_allow_result_is_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result_expression: str
) -> None:
    """Only the literal ``True`` authorizes; every other value denies.

    ``1`` is denied deliberately (``1 is True`` is ``False``): the plugin has
    to return a real bool, not merely something ``bool()`` accepts.
    """
    _install_plugin(
        tmp_path,
        monkeypatch,
        "policy_plugin_nonbool",
        _returning_plugin(result_expression),
    )

    assert is_allowed(default_authorizer(), OIDC_ALICE, Action.PLAN_APPROVE, RESOURCE) is False


def test_non_bool_warning_names_decision_but_not_the_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The denial warning is auditable and never carries the plugin's value."""
    sentinel = "sk-sentinel-value-do-not-log"
    _install_plugin(
        tmp_path, monkeypatch, "policy_plugin_warn", _returning_plugin(f'"{sentinel}"')
    )

    with caplog.at_level(logging.WARNING, logger="app.policy"):
        denied = is_allowed(default_authorizer(), OIDC_ALICE, Action.PLAN_APPROVE, RESOURCE)

    assert denied is False
    assert "non-boolean result" in caplog.text
    assert "plan:approve" in caplog.text
    assert "oidc:alice" in caplog.text
    assert sentinel not in caplog.text


@pytest.mark.parametrize("allowlist", ["<unset>", "", "   "])
def test_plugin_seam_requires_the_module_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allowlist: str
) -> None:
    """No allowlist means no authorizer plugin, and the module is never imported."""
    name = "policy_plugin_no_allowlist"
    _install_plugin(tmp_path, monkeypatch, name, ALLOWING_PLUGIN, allowlist=allowlist)
    monkeypatch.delitem(sys.modules, name, raising=False)

    with pytest.raises(PluginError) as excinfo:
        default_authorizer()

    message = str(excinfo.value)
    assert PLUGIN_ENV in message
    assert ALLOWLIST_ENV in message
    assert name not in sys.modules
    assert app.policy._PLUGIN_AUTHORIZERS == {}


def test_allowlist_that_does_not_cover_the_module_still_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loader's own allowlist check keeps its veto for this seam."""
    _install_plugin(
        tmp_path,
        monkeypatch,
        "policy_plugin_offlist",
        ALLOWING_PLUGIN,
        allowlist="some_other_package",
    )

    with pytest.raises(PluginError):
        default_authorizer()

    assert app.policy._PLUGIN_AUTHORIZERS == {}


def test_stdlib_cannot_become_the_authorizer(monkeypatch: pytest.MonkeyPatch) -> None:
    """An allowlisted stdlib module is still refused by the loader."""
    monkeypatch.setenv(PLUGIN_ENV, "os:system")
    monkeypatch.setenv(ALLOWLIST_ENV, "os")

    with pytest.raises(PluginError):
        default_authorizer()

    assert app.policy._PLUGIN_AUTHORIZERS == {}


def test_plugin_module_loaded_record_names_seam_and_module_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """First build logs one record naming the module; cached calls log nothing."""
    name = "policy_plugin_audited"
    _install_plugin(tmp_path, monkeypatch, name, ALLOWING_PLUGIN)

    with caplog.at_level(logging.INFO, logger="app.policy"):
        first = default_authorizer()
        records = [record for record in caplog.records if record.name == "app.policy"]

    assert first is not None
    assert len(records) == 1
    record = records[0]
    assert record.getMessage() == "authorizer plugin loaded"
    assert record.plugin_seam == "authorizer"
    assert record.plugin_module == name
    assert not hasattr(record, "plugin_attr")

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.policy"):
        assert default_authorizer() is first
        assert [r for r in caplog.records if r.name == "app.policy"] == []


def test_plugin_denial_reaches_refusal_path_and_audit_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plugin denial becomes the clean refusal dict and a denied audit record."""
    _install_plugin(tmp_path, monkeypatch, "policy_plugin_audit_denier", DENYING_PLUGIN)

    plan_dir = _bind_plan_dir(tmp_path, monkeypatch)
    from pipeline.actor import LOCAL_OPERATOR as ACTOR_OPERATOR

    with _operating_as(ACTOR_OPERATOR):
        result = _authorize("story:merge", plan_name="p", story_key="S")

    assert result is not None
    assert result["ok"] is False
    assert result["forbidden"] is True
    assert _audit_events(plan_dir) == ["authorization_denied"]


def test_truthy_non_bool_plugin_result_is_refused_by_the_refusal_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A truthy non-``True`` result must NOT audit an allowed action.

    This is the audit-side reproduction of the fail-open bug: a plugin whose
    ``allow`` returns a denial string used to satisfy ``if allowed:`` and write
    ``outcome="allowed"``.
    """
    _install_plugin(
        tmp_path, monkeypatch, "policy_plugin_audit_truthy", _returning_plugin('"denied"')
    )

    plan_dir = _bind_plan_dir(tmp_path, monkeypatch)
    from pipeline.actor import LOCAL_OPERATOR as ACTOR_OPERATOR

    with _operating_as(ACTOR_OPERATOR):
        result = _authorize("story:merge", plan_name="p", story_key="S")

    assert result is not None
    assert result["ok"] is False
    assert result["forbidden"] is True
    assert _audit_events(plan_dir) == ["authorization_denied"]


def _bind_plan_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every ``PLAN_DIR`` binding at a throwaway directory."""
    # pipeline.server must be imported before pipeline.ingest (see
    # tests/unit/test_authorization.py) so the import cycle resolves.
    import pipeline.server as p
    from pipeline import concurrency as pcon
    from pipeline import persistence as ppers
    from pipeline.authorization import ENFORCE_ENV

    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", plan_dir)
    monkeypatch.setattr(ppers, "PLAN_DIR", plan_dir)
    monkeypatch.setattr(pcon, "PLAN_DIR", plan_dir)
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    return plan_dir


def _operating_as(principal: object):
    """Bind ``principal`` for the duration of one authorization call."""
    from pipeline.actor import bind_principal

    return bind_principal(principal)


def _authorize(action: str, *, plan_name: str, story_key: str) -> dict | None:
    """Run the service-layer gate with an empty manifest."""
    from pipeline.authorization import authorize

    return authorize(action, plan_name=plan_name, story_key=story_key, manifest={})


def _audit_events(plan_dir: Path) -> list[str]:
    """Return the ``event`` field of every notification the plan sidecar holds."""
    path = plan_dir / "p.notifications.jsonl"
    return [json.loads(line)["event"] for line in path.read_text().splitlines()]
