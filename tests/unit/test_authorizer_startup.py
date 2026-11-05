"""Start-up activation of the authorizer seam.

Loader guidance: a seam's plugin is resolved once at process start-up, not on
the first request that happens to need it. These tests pin that behavior for
the authorizer: :func:`app.policy.activate_authorizer` builds nothing new (the
plugin loader's cache still owns the object), records which authorizer is live
with exactly two log keys, propagates :class:`~app.plugin_loader.PluginError` so
a broken or unallowlisted authorizer stops the process instead of falling back
to :class:`app.policy.LocalPolicy`, and never leaks the attribute name.
"""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path

import pytest

import app.policy
from app.plugin_loader import ALLOWLIST_ENV, PluginError
from app.policy import LocalPolicy, activate_authorizer

#: The operator-controlled variable that selects the authorizer seam.
PLUGIN_ENV = "PIPELINE_AUTHORIZER_PLUGIN"

#: The record every activation emits, whatever the seam resolved to.
ACTIVE_MESSAGE = "authorizer active"

#: The module part of ``module:attr`` is what may be logged; never the attr.
ATTRIBUTE = "make_authorizer"


def _plugin_source(attr: str) -> str:
    """Return plugin source whose factory ``attr`` counts its own calls."""
    return f'''
FACTORY_CALLS = 0


class _Authorizer:
    def allow(self, principal, action, resource=None):
        return True


def {attr}():
    global FACTORY_CALLS
    FACTORY_CALLS += 1
    return _Authorizer()
'''


def _install_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    *,
    attr: str = ATTRIBUTE,
    allowlist: str | None = None,
) -> None:
    """Write the plugin as module *name* in ``tmp_path`` and select it.

    The authorizer seam requires ``PIPELINE_PLUGIN_MODULES``; the module name is
    allowlisted unless the test asks for ``"<unset>"``.
    """
    (tmp_path / f"{name}.py").write_text(_plugin_source(attr), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv(PLUGIN_ENV, f"{name}:{attr}")
    if allowlist == "<unset>":
        monkeypatch.delenv(ALLOWLIST_ENV, raising=False)
    else:
        monkeypatch.setenv(ALLOWLIST_ENV, name if allowlist is None else allowlist)


def _active_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return only this seam's activation records, from our logger."""
    return [
        record
        for record in caplog.records
        if record.name == "app.policy" and record.getMessage() == ACTIVE_MESSAGE
    ]


@pytest.fixture(autouse=True)
def clear_authorizer_cache():
    """Isolate every test from the process-wide authorizer plugin cache."""
    app.policy._PLUGIN_AUTHORIZERS.clear()
    yield
    app.policy._PLUGIN_AUTHORIZERS.clear()


def test_unset_seam_activates_local_policy_and_logs_builtin(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing configured: the built-in policy is live and logged as such."""
    monkeypatch.delenv(PLUGIN_ENV, raising=False)
    monkeypatch.delenv(ALLOWLIST_ENV, raising=False)

    with caplog.at_level(logging.INFO, logger="app.policy"):
        authorizer = activate_authorizer()

    assert isinstance(authorizer, LocalPolicy)
    records = _active_records(caplog)
    assert len(records) == 1
    assert records[0].plugin_seam == "authorizer"
    assert records[0].plugin_module == "builtin"


def test_plugin_seam_activation_logs_the_module_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A plugin authorizer is returned and the record names its module."""
    name = "authorizer_startup_plugin"
    _install_plugin(tmp_path, monkeypatch, name)

    with caplog.at_level(logging.INFO, logger="app.policy"):
        authorizer = activate_authorizer()

    assert not isinstance(authorizer, LocalPolicy)
    assert callable(getattr(authorizer, "allow", None))
    records = _active_records(caplog)
    assert len(records) == 1
    assert records[0].plugin_seam == "authorizer"
    assert records[0].plugin_module == name
    # The attribute half of the spec and the raw spec stay out of the record.
    assert not hasattr(records[0], "plugin_attr")
    assert not hasattr(records[0], "plugin_spec")
    assert all(ATTRIBUTE not in str(value) for value in records[0].__dict__.values())


def test_second_activation_reuses_the_built_authorizer_and_logs_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Activation is not memoized: same object, one factory call, two records."""
    name = "authorizer_startup_cached"
    _install_plugin(tmp_path, monkeypatch, name)

    with caplog.at_level(logging.INFO, logger="app.policy"):
        first = activate_authorizer()
        second = activate_authorizer()

    assert second is first
    assert sys.modules[name].FACTORY_CALLS == 1
    records = _active_records(caplog)
    assert [record.plugin_module for record in records] == [name, name]


def test_plugin_without_allowlist_refuses_and_logs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No allowlist means no authorizer plugin: the refusal propagates."""
    _install_plugin(
        tmp_path, monkeypatch, "authorizer_startup_noallowlist", allowlist="<unset>"
    )

    with caplog.at_level(logging.INFO, logger="app.policy"), pytest.raises(PluginError):
        activate_authorizer()

    assert _active_records(caplog) == []
    assert app.policy._PLUGIN_AUTHORIZERS == {}


def test_invalid_spec_raises_instead_of_falling_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spec that cannot be resolved raises; it never degrades to LocalPolicy."""
    monkeypatch.setenv(PLUGIN_ENV, "no_such_authorizer_module:make_authorizer")
    monkeypatch.setenv(ALLOWLIST_ENV, "no_such_authorizer_module")

    with pytest.raises(PluginError):
        activate_authorizer()

    assert app.policy._PLUGIN_AUTHORIZERS == {}


def test_daemon_refuses_to_start_with_a_broken_authorizer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daemon activation precedes the scheduler loop, so no plan advances."""
    from pipeline import scheduler_daemon

    _install_plugin(
        tmp_path, monkeypatch, "authorizer_startup_daemon", allowlist="<unset>"
    )

    loop_calls: list[str] = []

    class _RecordingDaemon:
        def __init__(self, **kwargs: object) -> None:
            pass

        def run_forever(self, stop: object) -> None:
            loop_calls.append("run_forever")

    monkeypatch.setattr(scheduler_daemon, "SchedulerDaemon", _RecordingDaemon)

    with pytest.raises(PluginError):
        scheduler_daemon.run_daemon()

    assert loop_calls == []


def test_dashboard_import_refuses_a_broken_authorizer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Importing the dashboard resolves the seam, so a bad plugin fails fast."""
    import app.dashboard

    name = "authorizer_startup_dashboard"
    _install_plugin(tmp_path, monkeypatch, name, allowlist="<unset>")

    with pytest.raises(PluginError):
        importlib.reload(app.dashboard)

    # Restoring the allowlist must make the module importable again.
    monkeypatch.setenv(ALLOWLIST_ENV, name)
    importlib.reload(app.dashboard)


def test_attribute_name_never_reaches_a_log_record_or_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Neither the happy path nor the refusal may name the plugin attribute."""
    name = "authorizer_startup_negative"
    _install_plugin(tmp_path, monkeypatch, name)

    with caplog.at_level(logging.DEBUG, logger="app.policy"):
        activate_authorizer()

    records = _active_records(caplog)
    assert len(records) == 1
    for record in records:
        assert ATTRIBUTE not in record.getMessage()
        assert all(ATTRIBUTE not in str(value) for value in record.__dict__.values())

    app.policy._PLUGIN_AUTHORIZERS.clear()
    monkeypatch.delenv(ALLOWLIST_ENV, raising=False)
    with pytest.raises(PluginError) as excinfo:
        activate_authorizer()

    assert ATTRIBUTE not in str(excinfo.value)
