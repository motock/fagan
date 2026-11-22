"""Operator-supplied intake plugins, run once per scheduler tick.

``PIPELINE_INTAKE_PLUGINS`` lists ``module:attr`` callables, each called as
``plugin(now)``. The plugin modules are small fakes registered in
``sys.modules``; the approval gate is stubbed at the module's own binding.
"""

from __future__ import annotations

import importlib
import logging
import sys
import types
from pathlib import Path

import pytest

from app import plugin_loader
from app.plugin_loader import PluginError
from pipeline import intake_plugins

ENV = intake_plugins.PLUGINS_ENV
ALLOWLIST = plugin_loader.ALLOWLIST_ENV
SCHEDULER = Path(__file__).resolve().parents[2] / "pipeline" / "scheduler_daemon.py"
TICK_NOW = 1_700_000_000.5

CALLS: list[tuple[str, float]] = []


def _fake_module(monkeypatch, name: str, behaviour) -> None:
    """Register ``name`` with one ``run`` callable whose behaviour is ``behaviour``.

    The callable's ``__module__`` names the fake module, because the loader
    refuses an attribute that the plugin module does not itself define.
    """

    def run(now):
        CALLS.append((name, now))
        return behaviour(now)

    run.__module__ = name
    module = types.ModuleType(name)
    module.run = run
    monkeypatch.setitem(sys.modules, name, module)


def _ok(now):
    return None


def _boom(now):
    raise RuntimeError("secret-token-in-message")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    CALLS.clear()
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.delenv(ALLOWLIST, raising=False)
    monkeypatch.setattr(intake_plugins, "approval_enabled", lambda: True)


def _configure(monkeypatch, specs: str, allowlist: str = "fake_intake_a,fake_intake_b") -> None:
    monkeypatch.setenv(ENV, specs)
    monkeypatch.setenv(ALLOWLIST, allowlist)


def test_should_do_nothing_when_no_plugin_is_configured(monkeypatch):
    calls = []
    monkeypatch.setattr(
        intake_plugins, "approval_enabled", lambda: calls.append(1) or True
    )

    assert intake_plugins.run_intake_plugins(TICK_NOW) == {}
    assert calls == []


@pytest.mark.parametrize("blank", ["", "   ", " \t "])
def test_should_treat_a_blank_value_as_unset(monkeypatch, blank):
    monkeypatch.setenv(ENV, blank)
    calls = []
    monkeypatch.setattr(
        intake_plugins, "approval_enabled", lambda: calls.append(1) or True
    )

    assert intake_plugins.run_intake_plugins(TICK_NOW) == {}
    assert calls == []


def test_should_call_each_plugin_once_with_the_tick_time_in_listed_order(monkeypatch):
    _fake_module(monkeypatch, "fake_intake_a", _ok)
    _fake_module(monkeypatch, "fake_intake_b", _ok)
    _configure(monkeypatch, "fake_intake_b:run,fake_intake_a:run")

    result = intake_plugins.run_intake_plugins(TICK_NOW)

    assert CALLS == [("fake_intake_b", TICK_NOW), ("fake_intake_a", TICK_NOW)]
    assert result == {"ran": 2, "failed": 0}


def test_should_refuse_when_the_module_allowlist_is_unset(monkeypatch):
    imported = []
    real_import = importlib.import_module

    def spy(name, *args, **kwargs):
        imported.append(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(plugin_loader.importlib, "import_module", spy)
    _fake_module(monkeypatch, "fake_intake_a", _ok)
    monkeypatch.setenv(ENV, "fake_intake_a:run")

    with pytest.raises(PluginError) as raised:
        intake_plugins.run_intake_plugins(TICK_NOW)

    message = str(raised.value)
    assert ENV in message
    assert ALLOWLIST in message
    assert "fake_intake_a" not in imported
    assert CALLS == []


def test_should_refuse_a_module_outside_the_allowlist(monkeypatch):
    _fake_module(monkeypatch, "fake_intake_a", _ok)
    _configure(monkeypatch, "fake_intake_a:run", allowlist="some_other_package")

    with pytest.raises(PluginError, match="not in"):
        intake_plugins.run_intake_plugins(TICK_NOW)

    assert CALLS == []


def test_should_skip_every_plugin_when_plan_approval_is_off(monkeypatch, caplog):
    _fake_module(monkeypatch, "fake_intake_a", _ok)
    _configure(monkeypatch, "fake_intake_a:run")
    monkeypatch.setattr(intake_plugins, "approval_enabled", lambda: False)

    with caplog.at_level(logging.WARNING, logger="pipeline.intake_plugins"):
        result = intake_plugins.run_intake_plugins(TICK_NOW)

    assert result == {}
    assert CALLS == []
    assert "intake plugins skipped: plan approval gate is off" in caplog.text


def test_should_skip_every_plugin_when_the_approval_setting_is_unreadable(monkeypatch):
    _fake_module(monkeypatch, "fake_intake_a", _ok)
    _configure(monkeypatch, "fake_intake_a:run")

    def unreadable():
        raise ValueError("PIPELINE_PLAN_APPROVAL has an unrecognised value")

    monkeypatch.setattr(intake_plugins, "approval_enabled", unreadable)

    assert intake_plugins.run_intake_plugins(TICK_NOW) == {}
    assert CALLS == []


def test_should_keep_running_the_other_plugins_when_one_raises(monkeypatch):
    _fake_module(monkeypatch, "fake_intake_a", _boom)
    _fake_module(monkeypatch, "fake_intake_b", _ok)
    _configure(monkeypatch, "fake_intake_a:run,fake_intake_b:run")

    result = intake_plugins.run_intake_plugins(TICK_NOW)

    assert result == {"ran": 1, "failed": 1}
    assert ("fake_intake_b", TICK_NOW) in CALLS


def test_should_log_only_the_exception_type_when_a_plugin_raises(monkeypatch, caplog):
    _fake_module(monkeypatch, "fake_intake_a", _boom)
    _configure(monkeypatch, "fake_intake_a:run")

    with caplog.at_level(logging.DEBUG):
        intake_plugins.run_intake_plugins(TICK_NOW)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "a failing plugin must be logged at WARNING"
    assert all("secret-token-in-message" not in r.getMessage() for r in caplog.records)
    assert all(
        "secret-token-in-message" not in str(getattr(r, "error_type", ""))
        for r in caplog.records
    )
    assert any(getattr(r, "error_type", None) == "RuntimeError" for r in warnings)


def test_should_reread_the_plugin_list_on_every_call(monkeypatch):
    _fake_module(monkeypatch, "fake_intake_a", _ok)
    _fake_module(monkeypatch, "fake_intake_b", _ok)
    _configure(monkeypatch, "fake_intake_a:run")

    assert intake_plugins.run_intake_plugins(TICK_NOW) == {"ran": 1, "failed": 0}

    monkeypatch.setenv(ENV, "fake_intake_b:run")

    assert intake_plugins.run_intake_plugins(TICK_NOW) == {"ran": 1, "failed": 0}
    assert [name for name, _ in CALLS] == ["fake_intake_a", "fake_intake_b"]


def test_should_refuse_a_list_with_an_empty_entry(monkeypatch):
    _fake_module(monkeypatch, "fake_intake_a", _ok)
    _configure(monkeypatch, "fake_intake_a:run,")

    with pytest.raises(PluginError, match="empty entry"):
        intake_plugins.run_intake_plugins(TICK_NOW)

    assert CALLS == []


def test_should_run_intake_plugins_after_jira_intake_in_the_tick():
    source = SCHEDULER.read_text(encoding="utf-8")

    jira = source.index("jira_intake.run_jira_intake(")
    plugins = source.index("intake_plugins.run_intake_plugins(")
    srr2 = source.index("# SRR-2: the abandoned-worker ledger")

    assert jira < plugins < srr2


def test_should_contain_a_failing_intake_plugins_call_inside_the_tick_block():
    source = SCHEDULER.read_text(encoding="utf-8")

    plugins = source.index("intake_plugins.run_intake_plugins(")
    block = source[plugins:source.index("# SRR-2: the abandoned-worker ledger")]

    assert "except Exception as exc:" in block
    assert "intake plugins failed during tick" in block
    assert '"error_type": type(exc).__name__' in block
