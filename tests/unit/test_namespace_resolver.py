"""Tests for the namespace resolver seam with its single-namespace default.

A resolver maps a :class:`app.identity.Principal` to the namespaces it may
reach, each with the roles it holds there. With no plugin configured the
default returns exactly one entry: the store's default namespace with the
principal's own roles. An operator may replace it through
``PIPELINE_NAMESPACE_RESOLVER_PLUGIN=module:attr``; a bad spec, a disallowed
module or a wrong shape raises at activation rather than falling back, and any
malformed result from a resolver is refused rather than treated as an empty
answer.
"""

from __future__ import annotations

import dataclasses
import sys
import uuid
from pathlib import Path

import pytest

import pipeline.namespace_resolver as resolver_module
from app.identity import ROLE_ADMIN, ROLE_AUTHOR, ROLES, Principal
from app.plugin_loader import ALLOWLIST_ENV, PluginError
from pipeline.namespace_resolver import (
    NAMESPACE_RESOLVER_PLUGIN_ENV,
    DefaultNamespaceResolver,
    NamespaceAccess,
    NamespaceResolverError,
    active_namespace_resolver,
    checked_accesses,
)
from pipeline.store_records import DEFAULT_NAMESPACE

#: An ordinary caller holding one role, so every assertion has a known roles set.
AUTHOR = Principal(subject="alice", roles=frozenset({ROLE_AUTHOR}))


@pytest.fixture(autouse=True)
def clear_resolver_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate every test from the process-wide resolver plugin cache."""
    monkeypatch.delenv(NAMESPACE_RESOLVER_PLUGIN_ENV, raising=False)
    monkeypatch.delenv(ALLOWLIST_ENV, raising=False)
    resolver_module._PLUGIN_RESOLVERS.clear()
    yield
    resolver_module._PLUGIN_RESOLVERS.clear()


def _stub_source(accesses_result: str) -> str:
    """Return plugin source whose ``accesses`` returns ``accesses_result``.

    The expression is spliced into the source verbatim, so it may use braces
    freely: only the template text is subject to f-string rules.
    """
    return f"""
from pipeline.namespace_resolver import NamespaceAccess

BUILDS = []


class StubResolver:
    def accesses(self, principal):
        return {accesses_result}


def build():
    BUILDS.append(1)
    return StubResolver()
"""


def _install_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    *,
    allowlist: str | None = None,
) -> str:
    """Write ``source`` as a fresh module, select it, and return its name.

    The module name is unique per call so ``sys.modules`` never hands a later
    test an earlier test's stub. The allowlist names the module unless a test
    supplies its own value.
    """
    name = "nsr_" + uuid.uuid4().hex
    (tmp_path / f"{name}.py").write_text(source, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv(NAMESPACE_RESOLVER_PLUGIN_ENV, f"{name}:build")
    monkeypatch.setenv(ALLOWLIST_ENV, name if allowlist is None else allowlist)
    return name


class TestNamespaceAccess:
    @pytest.mark.parametrize("namespace", ["default", "alpha", "alpha/beta"])
    def test_accepts_a_valid_namespace(self, namespace: str) -> None:
        access = NamespaceAccess(namespace, frozenset({ROLE_AUTHOR}))

        assert access.namespace == namespace

    def test_accepts_an_empty_roles_set(self) -> None:
        access = NamespaceAccess("alpha", frozenset())

        assert access.roles == frozenset()

    def test_accepts_every_defined_role(self) -> None:
        access = NamespaceAccess("alpha", frozenset(ROLES))

        assert access.roles == ROLES

    @pytest.mark.parametrize(
        "namespace",
        ["", "Alpha", "../etc", "a/b/c", "a b", "alpha/", "/alpha", 7, None],
    )
    def test_refuses_an_invalid_namespace(self, namespace: object) -> None:
        with pytest.raises(NamespaceResolverError, match="invalid namespace"):
            NamespaceAccess(namespace, frozenset({ROLE_AUTHOR}))  # type: ignore[arg-type]

    def test_refuses_a_role_outside_roles(self) -> None:
        with pytest.raises(NamespaceResolverError, match="invalid role"):
            NamespaceAccess("alpha", frozenset({"wizard"}))

    def test_refuses_roles_that_are_not_a_frozenset(self) -> None:
        with pytest.raises(NamespaceResolverError, match="roles"):
            NamespaceAccess("alpha", [ROLE_AUTHOR])  # type: ignore[arg-type]

    def test_is_frozen(self) -> None:
        access = NamespaceAccess("alpha", frozenset({ROLE_AUTHOR}))

        with pytest.raises(dataclasses.FrozenInstanceError):
            access.namespace = "beta"  # type: ignore[misc]


class TestDefaultResolver:
    def test_returns_exactly_one_default_namespace_entry(self) -> None:
        accesses = DefaultNamespaceResolver().accesses(AUTHOR)

        assert accesses == (NamespaceAccess(DEFAULT_NAMESPACE, AUTHOR.roles),)

    def test_carries_the_principal_roles_unchanged(self) -> None:
        admin = Principal(subject="root", roles=frozenset({ROLE_ADMIN, ROLE_AUTHOR}))

        (access,) = DefaultNamespaceResolver().accesses(admin)

        assert access.roles == frozenset({ROLE_ADMIN, ROLE_AUTHOR})

    def test_default_namespace_is_the_store_default(self) -> None:
        (access,) = DefaultNamespaceResolver().accesses(AUTHOR)

        assert access.namespace == "default"

    def test_principal_with_no_roles_gets_the_default_with_no_roles(self) -> None:
        (access,) = DefaultNamespaceResolver().accesses(Principal(subject="bob"))

        assert access == NamespaceAccess(DEFAULT_NAMESPACE, frozenset())


class TestActiveResolverWithoutPlugin:
    def test_no_plugin_selects_the_default_resolver(self) -> None:
        resolver = active_namespace_resolver()

        assert isinstance(resolver, DefaultNamespaceResolver)

    def test_unchanged_path_yields_todays_single_namespace(self) -> None:
        # The no-plugin path must not change what a caller sees: one entry in
        # the store's default namespace, carrying the principal's own roles.
        accesses = checked_accesses(active_namespace_resolver(), AUTHOR)

        assert accesses == (NamespaceAccess("default", AUTHOR.roles),)

    def test_no_plugin_caches_nothing(self) -> None:
        active_namespace_resolver()

        assert resolver_module._PLUGIN_RESOLVERS == {}


class TestStubPluginIsUsed:
    def test_active_resolver_is_the_plugin_object(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        name = _install_plugin(
            tmp_path,
            monkeypatch,
            _stub_source('(NamespaceAccess("alpha/beta", frozenset({"author"})),)'),
        )

        resolver = active_namespace_resolver()

        assert type(resolver) is sys.modules[name].StubResolver

    def test_plugin_result_is_returned_to_the_caller(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_plugin(
            tmp_path,
            monkeypatch,
            _stub_source('(NamespaceAccess("alpha/beta", frozenset({"author"})),)'),
        )

        accesses = checked_accesses(active_namespace_resolver(), AUTHOR)

        assert accesses == (NamespaceAccess("alpha/beta", frozenset({"author"})),)

    def test_plugin_result_does_not_fall_back_to_the_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_plugin(
            tmp_path,
            monkeypatch,
            _stub_source('(NamespaceAccess("alpha", frozenset({"author"})),)'),
        )

        accesses = checked_accesses(active_namespace_resolver(), AUTHOR)

        assert DEFAULT_NAMESPACE not in {access.namespace for access in accesses}


class TestActivationRefusesBadPlugins:
    def test_refuses_a_spec_without_a_colon(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(NAMESPACE_RESOLVER_PLUGIN_ENV, "stub_module")
        monkeypatch.setenv(ALLOWLIST_ENV, "stub_module")

        with pytest.raises(PluginError, match="not a valid plugin spec"):
            active_namespace_resolver()

    def test_refuses_a_private_attribute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        name = _install_plugin(tmp_path, monkeypatch, _stub_source("()"))
        monkeypatch.setenv(NAMESPACE_RESOLVER_PLUGIN_ENV, f"{name}:_build")

        with pytest.raises(PluginError, match="must be public"):
            active_namespace_resolver()

    def test_refuses_a_plugin_when_the_allowlist_is_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_plugin(tmp_path, monkeypatch, _stub_source("()"))
        monkeypatch.delenv(ALLOWLIST_ENV)

        with pytest.raises(PluginError, match="requires PIPELINE_PLUGIN_MODULES"):
            active_namespace_resolver()

    def test_refuses_a_module_outside_the_allowlist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_plugin(
            tmp_path, monkeypatch, _stub_source("()"), allowlist="some_other_module"
        )

        with pytest.raises(PluginError, match="not in PIPELINE_PLUGIN_MODULES"):
            active_namespace_resolver()

    def test_refuses_a_standard_library_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(NAMESPACE_RESOLVER_PLUGIN_ENV, "json:dumps")
        monkeypatch.setenv(ALLOWLIST_ENV, "json")

        with pytest.raises(PluginError, match="standard-library"):
            active_namespace_resolver()

    def test_refuses_a_module_that_cannot_be_imported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        missing = "nsr_missing_" + uuid.uuid4().hex
        monkeypatch.setenv(NAMESPACE_RESOLVER_PLUGIN_ENV, f"{missing}:build")
        monkeypatch.setenv(ALLOWLIST_ENV, missing)

        with pytest.raises(PluginError, match="cannot import the plugin module"):
            active_namespace_resolver()

    def test_refuses_a_missing_attribute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        name = _install_plugin(tmp_path, monkeypatch, _stub_source("()"))
        monkeypatch.setenv(NAMESPACE_RESOLVER_PLUGIN_ENV, f"{name}:absent")

        with pytest.raises(PluginError, match="no such attribute"):
            active_namespace_resolver()

    def test_refuses_a_non_callable_attribute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        name = _install_plugin(
            tmp_path,
            monkeypatch,
            "from pipeline.namespace_resolver import NamespaceAccess\nbuild = 3\n",
        )
        monkeypatch.setenv(NAMESPACE_RESOLVER_PLUGIN_ENV, f"{name}:build")

        with pytest.raises(PluginError, match="not callable"):
            active_namespace_resolver()

    def test_refuses_a_factory_whose_object_lacks_accesses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_plugin(
            tmp_path,
            monkeypatch,
            "def build():\n    return object()\n",
        )

        with pytest.raises(PluginError, match="missing required methods: accesses"):
            active_namespace_resolver()

    def test_refuses_a_factory_whose_accesses_is_not_callable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_plugin(
            tmp_path,
            monkeypatch,
            "class Bad:\n    accesses = 3\n\n\ndef build():\n    return Bad()\n",
        )

        with pytest.raises(PluginError, match="missing required methods: accesses"):
            active_namespace_resolver()

    def test_a_refused_plugin_is_not_cached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_plugin(tmp_path, monkeypatch, "def build():\n    return object()\n")

        with pytest.raises(PluginError):
            active_namespace_resolver()

        assert resolver_module._PLUGIN_RESOLVERS == {}


class TestCheckedAccessesRefusesMalformedResults:
    def test_refuses_duplicate_namespaces_and_does_not_echo_them(self) -> None:
        class Duplicating:
            def accesses(self, principal):
                return (
                    NamespaceAccess("zeta-ns", frozenset({ROLE_AUTHOR})),
                    NamespaceAccess("zeta-ns", frozenset({ROLE_ADMIN})),
                )

        with pytest.raises(NamespaceResolverError, match="duplicate namespace") as info:
            checked_accesses(Duplicating(), AUTHOR)

        assert "zeta-ns" not in str(info.value)

    def test_accepts_distinct_namespaces(self) -> None:
        class Distinct:
            def accesses(self, principal):
                return (
                    NamespaceAccess("alpha", frozenset({ROLE_AUTHOR})),
                    NamespaceAccess("beta", frozenset({ROLE_AUTHOR})),
                )

        accesses = checked_accesses(Distinct(), AUTHOR)

        assert [access.namespace for access in accesses] == ["alpha", "beta"]

    def test_refuses_an_empty_result(self) -> None:
        class Empty:
            def accesses(self, principal):
                return ()

        with pytest.raises(NamespaceResolverError, match="no namespaces"):
            checked_accesses(Empty(), AUTHOR)

    def test_refuses_a_list_instead_of_a_tuple(self) -> None:
        class Listing:
            def accesses(self, principal):
                return [NamespaceAccess("alpha", frozenset({ROLE_AUTHOR}))]

        with pytest.raises(NamespaceResolverError, match="must be a tuple"):
            checked_accesses(Listing(), AUTHOR)

    def test_refuses_an_entry_that_is_not_a_namespace_access(self) -> None:
        class Mixed:
            def accesses(self, principal):
                return ("alpha",)

        with pytest.raises(NamespaceResolverError, match="not a NamespaceAccess"):
            checked_accesses(Mixed(), AUTHOR)

    def test_a_resolver_exception_is_not_turned_into_an_empty_result(self) -> None:
        class Failing:
            def accesses(self, principal):
                raise RuntimeError("resolver failure")

        with pytest.raises(RuntimeError, match="resolver failure"):
            checked_accesses(Failing(), AUTHOR)


class TestPluginCachedPerProcess:
    def test_plugin_is_built_once_and_reused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        name = _install_plugin(
            tmp_path,
            monkeypatch,
            _stub_source('(NamespaceAccess("alpha", frozenset({"author"})),)'),
        )

        first = active_namespace_resolver()
        second = active_namespace_resolver()

        assert first is second
        assert sys.modules[name].BUILDS == [1]

    def test_clearing_the_cache_rebuilds_the_plugin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        name = _install_plugin(
            tmp_path,
            monkeypatch,
            _stub_source('(NamespaceAccess("alpha", frozenset({"author"})),)'),
        )
        first = active_namespace_resolver()

        resolver_module._PLUGIN_RESOLVERS.clear()
        second = active_namespace_resolver()

        assert first is not second
        assert sys.modules[name].BUILDS == [1, 1]
