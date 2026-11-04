"""Tests for app.plugin_loader — operator-selected plugin seams.

The loader is code execution chosen by an environment variable, so it must be
off unless the variable is set, validate the spec strictly, and fail closed
(a bad spec raises; it never silently falls back to the built-in).
"""

from __future__ import annotations

import logging
import sys
import textwrap

import pytest

from app.plugin_loader import (
    PluginError,
    check_no_conflict,
    load_plugin,
    load_plugin_list,
    plugin_env_name,
    plugin_spec,
    require_methods,
)

ENV_EVENT_BUS = "PIPELINE_EVENT_BUS_PLUGIN"
ENV_EVENT_BUS_LIST = "PIPELINE_EVENT_BUS_PLUGINS"
ENV_AUTH = "PIPELINE_AUTH_PLUGIN"

PLUGIN_OK_MOD = '''\
VALUE = "plugin-value"


def factory():
    return VALUE


NOT_CALLABLE = 42
'''

PLUGIN_BOOM_MOD = '''\
raise RuntimeError("SECRET-TOKEN")
'''

PLUGIN_LIST_FIRST = '''\
def factory():
    return "first"
'''

PLUGIN_LIST_SECOND = '''\
def factory():
    return "second"
'''

PLUGIN_NEVER_IMPORTED = '''\
def factory():
    return "never"
'''


def write_module(tmp_path, name, source):
    """Write a small importable plugin module into tmp_path."""
    (tmp_path / f"{name}.py").write_text(textwrap.dedent(source), encoding="utf-8")


@pytest.fixture
def plugin_env(tmp_path, monkeypatch):
    """Put tmp_path on sys.path and populate it with plugin modules."""
    monkeypatch.syspath_prepend(str(tmp_path))
    write_module(tmp_path, "plugin_ok_mod", PLUGIN_OK_MOD)
    write_module(tmp_path, "plugin_boom_mod", PLUGIN_BOOM_MOD)
    write_module(tmp_path, "plugin_list_first", PLUGIN_LIST_FIRST)
    write_module(tmp_path, "plugin_list_second", PLUGIN_LIST_SECOND)
    write_module(tmp_path, "plugin_never_imported", PLUGIN_NEVER_IMPORTED)
    return tmp_path


class AuthorizerOnly:
    def authorize(self):
        return True


class NonCallableMethod:
    authorize = "not-a-method"


# (1) unset -> None
def test_load_plugin_unset_returns_none(plugin_env):
    assert load_plugin("event_bus", {}) is None


# (2) blank and whitespace-only -> None
@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_load_plugin_blank_returns_none(plugin_env, value):
    assert load_plugin("event_bus", {ENV_EVENT_BUS: value}) is None


# (3) valid spec returns the factory; calling it returns the module's value
def test_load_plugin_valid_spec_returns_factory(plugin_env):
    environ = {ENV_EVENT_BUS: "  plugin_ok_mod:factory  "}

    assert plugin_spec("event_bus", environ) == "plugin_ok_mod:factory"
    # plugin_spec must not mutate the caller's dict.
    assert environ == {ENV_EVENT_BUS: "  plugin_ok_mod:factory  "}

    factory = load_plugin("event_bus", environ)

    assert callable(factory)
    assert factory() == "plugin-value"
    # A second load with the same dict still succeeds.
    assert load_plugin("event_bus", environ) is factory


# (4) invalid specs raise without echoing the offending spec text.
# ``:attr`` is the one exception to the "never echo the spec" rule: the
# documented error text ends with the shape hint ``expected 'module:attr'``,
# which necessarily contains that substring.
@pytest.mark.parametrize(
    ("spec", "spec_may_be_echoed"),
    [
        ("nocolon", False),
        ("a:b:c", False),
        ("a.b", False),
        (":attr", True),
        ("mod:", False),
        ("mod:1x", False),
        ("mod :attr", False),
        ("../x:y", False),
        ("a-b:c", False),
    ],
)
def test_load_plugin_invalid_spec_raises_without_echoing_spec(
    plugin_env, spec, spec_may_be_echoed
):
    with pytest.raises(PluginError) as excinfo:
        load_plugin("event_bus", {ENV_EVENT_BUS: spec})

    message = str(excinfo.value)
    assert ENV_EVENT_BUS in message
    if not spec_may_be_echoed:
        assert spec not in message

    # Invalid is not unset: a retry with the same dict raises again.
    with pytest.raises(PluginError):
        load_plugin("event_bus", {ENV_EVENT_BUS: spec})


# (5) missing module -> PluginError naming only the exception type
def test_load_plugin_missing_module_raises_without_module_name(plugin_env):
    module_name = "plugin_module_that_does_not_exist_xyz"

    with pytest.raises(PluginError) as excinfo:
        load_plugin("event_bus", {ENV_EVENT_BUS: f"{module_name}:factory"})

    message = str(excinfo.value)
    assert "ModuleNotFoundError" in message
    assert module_name not in message


# (6) a plugin module that raises at import -> PluginError without the secret
def test_load_plugin_import_error_hides_exception_text(plugin_env):
    with pytest.raises(PluginError) as excinfo:
        load_plugin("event_bus", {ENV_EVENT_BUS: "plugin_boom_mod:factory"})

    message = str(excinfo.value)
    assert "RuntimeError" in message
    assert "SECRET-TOKEN" not in message


# (7) missing attribute and non-callable attribute both raise
def test_load_plugin_missing_attribute_raises(plugin_env):
    with pytest.raises(PluginError) as excinfo:
        load_plugin("event_bus", {ENV_EVENT_BUS: "plugin_ok_mod:missing_attr"})

    message = str(excinfo.value)
    assert ENV_EVENT_BUS in message
    assert "no such attribute" in message


def test_load_plugin_non_callable_attribute_raises(plugin_env):
    with pytest.raises(PluginError) as excinfo:
        load_plugin("event_bus", {ENV_EVENT_BUS: "plugin_ok_mod:NOT_CALLABLE"})

    message = str(excinfo.value)
    assert ENV_EVENT_BUS in message
    assert "not callable" in message


# (8) selector conflicts, through load_plugin and check_no_conflict directly
def test_load_plugin_selector_conflict_raises(plugin_env):
    environ = {ENV_AUTH: "plugin_ok_mod:factory", "AUTH_MODE": "oidc"}

    with pytest.raises(PluginError) as excinfo:
        load_plugin(
            "auth",
            environ,
            selector="AUTH_MODE",
            selector_default="shared-secret",
        )

    message = str(excinfo.value)
    assert "PIPELINE_AUTH_PLUGIN" in message
    assert "AUTH_MODE" in message


def test_load_plugin_selector_conflict_ignored_without_plugin(plugin_env):
    # The selector only matters when a plugin variable is set.
    environ = {"AUTH_MODE": "oidc"}

    assert (
        load_plugin(
            "auth",
            environ,
            selector="AUTH_MODE",
            selector_default="shared-secret",
        )
        is None
    )


def test_check_no_conflict_raises_even_when_plugin_unset(plugin_env):
    with pytest.raises(PluginError) as excinfo:
        check_no_conflict(
            "auth",
            {"AUTH_MODE": "oidc"},
            selector="AUTH_MODE",
            selector_default="shared-secret",
        )

    message = str(excinfo.value)
    assert "PIPELINE_AUTH_PLUGIN" in message
    assert "AUTH_MODE" in message


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"AUTH_MODE": ""},
        {"AUTH_MODE": "   "},
        {"AUTH_MODE": "shared-secret"},
    ],
)
def test_check_no_conflict_allows_default_blank_or_unset(plugin_env, environ):
    check_no_conflict(
        "auth",
        environ,
        selector="AUTH_MODE",
        selector_default="shared-secret",
    )


# (9) list parsing: order preserved, unset -> [], blank entry raises
def test_load_plugin_list_parses_in_order(plugin_env):
    environ = {ENV_EVENT_BUS_LIST: "plugin_list_first:factory, plugin_list_second:factory"}

    factories = load_plugin_list("event_bus", environ)

    assert [factory() for factory in factories] == ["first", "second"]


def test_load_plugin_list_unset_returns_empty(plugin_env):
    assert load_plugin_list("event_bus", {}) == []
    assert load_plugin_list("event_bus", {ENV_EVENT_BUS_LIST: "   "}) == []


def test_load_plugin_list_blank_entry_raises_before_importing(plugin_env):
    environ = {
        ENV_EVENT_BUS_LIST: (
            "plugin_never_imported:factory,,plugin_list_second:factory"
        )
    }

    with pytest.raises(PluginError) as excinfo:
        load_plugin_list("event_bus", environ)

    message = str(excinfo.value)
    assert ENV_EVENT_BUS_LIST in message
    assert "empty entry" in message
    # Nothing is imported before the whole list validates.
    assert "plugin_never_imported" not in sys.modules

    environ[ENV_EVENT_BUS_LIST] = (
        "plugin_never_imported:factory, plugin_list_second:factory"
    )
    factories = load_plugin_list("event_bus", environ)
    assert [factory() for factory in factories] == ["never", "second"]


# (10) require_methods passes and names every missing method
def test_require_methods_passes_for_complete_object():
    require_methods(AuthorizerOnly(), ["authorize"], "authorizer")


def test_require_methods_names_missing_methods():
    with pytest.raises(PluginError) as excinfo:
        require_methods(AuthorizerOnly(), ["authorize", "filter"], "authorizer")

    assert str(excinfo.value) == (
        "authorizer plugin is missing required methods: filter"
    )


def test_require_methods_rejects_non_callable_attribute():
    with pytest.raises(PluginError) as excinfo:
        require_methods(NonCallableMethod(), ["authorize"], "authorizer")

    assert str(excinfo.value) == (
        "authorizer plugin is missing required methods: authorize"
    )


# (11) plugin_env_name validates the seam and derives the variable name
def test_plugin_env_name_rejects_invalid_seam():
    with pytest.raises(ValueError):
        plugin_env_name("Bad-Seam")


def test_plugin_env_name_maps_seam():
    assert plugin_env_name("event_bus") == "PIPELINE_EVENT_BUS_PLUGIN"


# (12) a successful load logs exactly one INFO record with the seam
def test_load_plugin_logs_one_info_record(plugin_env, caplog):
    caplog.set_level(logging.INFO, logger="app.plugin_loader")

    with caplog.at_level(logging.INFO, logger="app.plugin_loader"):
        factory = load_plugin("event_bus", {ENV_EVENT_BUS: "plugin_ok_mod:factory"})

    assert callable(factory)
    records = [r for r in caplog.records if r.name == "app.plugin_loader"]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.INFO
    assert record.plugin_seam == "event_bus"
    assert record.plugin_module == "plugin_ok_mod"
    assert record.plugin_attr == "factory"
