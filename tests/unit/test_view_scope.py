"""The view-scope seam: which namespaces a principal may see in plan views.

With ``PIPELINE_VIEW_SCOPE_PLUGIN`` unset the scope is the namespaces the
namespace resolver reports for the principal, which is the default namespace
today. A stub plugin may narrow or widen that answer. Every malformed answer
and every failure to load is refused, never read as an empty or open view.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.identity import LOCAL_OPERATOR, ROLE_AUTHOR, Principal
from app.plugin_loader import ALLOWLIST_ENV, PluginError
from pipeline import view_scope
from pipeline.namespace_resolver import NamespaceAccess
from pipeline.store_records import DEFAULT_NAMESPACE, InvalidRecordKeyError
from pipeline.view_scope import (
    ALL,
    VIEW_SCOPE_PLUGIN_ENV,
    DefaultViewScope,
    ViewScopeError,
    active_view_scope,
    can_view,
    checked_visible,
)

AUTHOR = Principal(
    subject="author-1", auth_method="test", roles=frozenset({ROLE_AUTHOR})
)


@pytest.fixture(autouse=True)
def clear_view_scope_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(VIEW_SCOPE_PLUGIN_ENV, raising=False)
    monkeypatch.delenv(ALLOWLIST_ENV, raising=False)
    view_scope._PLUGIN_SCOPES.clear()
    yield
    view_scope._PLUGIN_SCOPES.clear()


def _install_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> str:
    """Write a stub plugin module, allowlist it, and select it through the env."""
    name = f"view_scope_stub_{uuid.uuid4().hex}"
    (tmp_path / f"{name}.py").write_text(body)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv(VIEW_SCOPE_PLUGIN_ENV, f"{name}:build")
    monkeypatch.setenv(ALLOWLIST_ENV, name)
    return name


def _scope_returning(value_source: str) -> str:
    return (
        "from pipeline.view_scope import ALL\n\n\n"
        "class Scope:\n"
        "    def visible(self, principal):\n"
        f"        return {value_source}\n\n\n"
        "def build():\n"
        "    return Scope()\n"
    )


class TestDefaultWithoutPlugin:
    def test_active_scope_is_the_default_when_no_plugin_is_configured(self) -> None:
        assert isinstance(active_view_scope(), DefaultViewScope)

    def test_default_scope_is_the_default_namespace_for_the_local_operator(
        self,
    ) -> None:
        assert DefaultViewScope().visible(LOCAL_OPERATOR) == frozenset(
            {DEFAULT_NAMESPACE}
        )

    def test_default_scope_is_the_default_namespace_for_a_plain_principal(self) -> None:
        assert DefaultViewScope().visible(AUTHOR) == frozenset({DEFAULT_NAMESPACE})

    def test_default_scope_follows_the_namespaces_the_resolver_reports(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class TwoNamespaces:
            def accesses(self, principal):
                return (
                    NamespaceAccess("alpha", frozenset()),
                    NamespaceAccess("beta/gamma", frozenset()),
                )

        monkeypatch.setattr(
            view_scope, "active_namespace_resolver", lambda: TwoNamespaces()
        )

        assert DefaultViewScope().visible(AUTHOR) == frozenset({"alpha", "beta/gamma"})

    def test_can_view_the_default_namespace(self) -> None:
        assert can_view(AUTHOR, DEFAULT_NAMESPACE) is True

    def test_cannot_view_another_namespace_explicitly(self) -> None:
        assert can_view(AUTHOR, "alpha") is False

    def test_refuses_a_malformed_namespace_to_view(self) -> None:
        with pytest.raises(InvalidRecordKeyError):
            can_view(AUTHOR, "../escape")


class TestStubPluginScopes:
    def test_a_stub_scope_narrows_the_view_to_its_namespaces(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_plugin(tmp_path, monkeypatch, _scope_returning('frozenset({"alpha"})'))

        assert can_view(AUTHOR, "alpha") is True
        assert can_view(AUTHOR, "beta/gamma") is False

    def test_a_stub_scope_returning_all_sees_every_namespace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_plugin(tmp_path, monkeypatch, _scope_returning("ALL"))

        assert checked_visible(active_view_scope(), AUTHOR) is ALL
        assert can_view(AUTHOR, "beta/gamma") is True

    def test_a_stub_scope_hiding_the_default_namespace_denies_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_plugin(tmp_path, monkeypatch, _scope_returning('frozenset({"alpha"})'))

        assert can_view(AUTHOR, DEFAULT_NAMESPACE) is False


class TestFailClosed:
    def test_a_spec_without_the_allowlist_is_refused_at_activation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(VIEW_SCOPE_PLUGIN_ENV, "some_module:build")

        with pytest.raises(PluginError, match="PIPELINE_VIEW_SCOPE_PLUGIN requires"):
            active_view_scope()

    def test_an_object_without_visible_is_refused_at_activation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_plugin(tmp_path, monkeypatch, "def build():\n    return object()\n")

        with pytest.raises(PluginError):
            active_view_scope()
        assert view_scope._PLUGIN_SCOPES == {}

    def test_an_empty_answer_is_refused_not_read_as_an_empty_view(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_plugin(tmp_path, monkeypatch, _scope_returning("frozenset()"))

        with pytest.raises(ViewScopeError, match="no namespaces"):
            can_view(AUTHOR, "alpha")

    def test_a_non_frozenset_answer_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_plugin(tmp_path, monkeypatch, _scope_returning('{"alpha"}'))

        with pytest.raises(ViewScopeError, match="frozenset or ALL"):
            can_view(AUTHOR, "alpha")

    def test_an_invalid_namespace_in_the_answer_is_refused_without_echoing_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_plugin(
            tmp_path, monkeypatch, _scope_returning('frozenset({"../escape"})')
        )

        with pytest.raises(ViewScopeError) as raised:
            can_view(AUTHOR, "alpha")
        assert "../escape" not in str(raised.value)

    def test_a_plugin_that_raises_propagates_and_does_not_open_the_view(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_plugin(
            tmp_path,
            monkeypatch,
            "class Scope:\n"
            "    def visible(self, principal):\n"
            "        raise RuntimeError('backend down')\n\n\n"
            "def build():\n"
            "    return Scope()\n",
        )

        with pytest.raises(RuntimeError, match="backend down"):
            can_view(AUTHOR, "alpha")

    def test_a_bad_answer_is_refused_again_on_a_second_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_plugin(tmp_path, monkeypatch, _scope_returning("frozenset()"))

        with pytest.raises(ViewScopeError):
            can_view(AUTHOR, "alpha")
        with pytest.raises(ViewScopeError):
            can_view(AUTHOR, "alpha")
