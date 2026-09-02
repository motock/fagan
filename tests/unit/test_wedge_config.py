"""Tests for the wedge-detector threshold settings in pipeline/config.py.

Three new knobs (added by the wedge-config story):

* ``WEDGE_SCAN_ENABLED``            <- PIPELINE_WEDGE_SCAN_ENABLED            (default "1")
* ``WEDGE_STALE_ACTIVITY_SECONDS``  <- PIPELINE_WEDGE_STALE_ACTIVITY_SECONDS  (default "1800")
* ``WEDGE_NOTIFY_COOLDOWN_SECONDS`` <- PIPELINE_WEDGE_NOTIFY_COOLDOWN_SECONDS (default "3600")

Per .claude/rules/testing-config-gates.md, every assertion resolves the
setting from a STUBBED environ (monkeypatch.setenv / monkeypatch.delenv) -
never against today's live environment. pipeline/config.py reads env vars
once at import time, so each test reloads the module after stubbing.

``config.__all__`` and ``config_provenance.ENV_VAR_CATALOG`` are cumulative
shared artifacts that later stories extend, so these tests assert MEMBERSHIP
of the wedge entries only - never exact contents, count, or order.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

import pipeline.config as pipeline_config
from pipeline import config_provenance

WEDGE_ENV_VARS: tuple[str, ...] = (
    "PIPELINE_WEDGE_SCAN_ENABLED",
    "PIPELINE_WEDGE_STALE_ACTIVITY_SECONDS",
    "PIPELINE_WEDGE_NOTIFY_COOLDOWN_SECONDS",
)

# name -> expected hardcoded fallback when the env var is absent.
WEDGE_DEFAULTS: dict[str, int] = {
    "WEDGE_SCAN_ENABLED": 1,
    "WEDGE_STALE_ACTIVITY_SECONDS": 1800,
    "WEDGE_NOTIFY_COOLDOWN_SECONDS": 3600,
}

# env var name -> the config constant it feeds.
ENV_VAR_TO_CONSTANT: dict[str, str] = {
    "PIPELINE_WEDGE_SCAN_ENABLED": "WEDGE_SCAN_ENABLED",
    "PIPELINE_WEDGE_STALE_ACTIVITY_SECONDS": "WEDGE_STALE_ACTIVITY_SECONDS",
    "PIPELINE_WEDGE_NOTIFY_COOLDOWN_SECONDS": "WEDGE_NOTIFY_COOLDOWN_SECONDS",
}

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _restore_wedge_config_module():
    """Reload pipeline/config.py from a pristine environ after each test.

    pipeline/config.py resolves its constants at import time, so a test that
    stubs an env var must reload the module to observe the stub. The snapshot
    is taken at fixture setup (before the test stubs anything), and teardown
    restores it wholesale before reloading, so a malformed value a test set
    (e.g. PIPELINE_USAGE_STALE_AFTER_SECONDS=abc) can never leak into the
    teardown reload or into a later test. Restoring the snapshot is safe even
    if the test's own monkeypatch undo runs after this teardown: undo puts
    back the same pre-test values this snapshot already holds.
    """
    pristine_env = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(pristine_env)
    importlib.reload(pipeline_config)


def _reload_with_env(monkeypatch, *, setenv=None, absent=WEDGE_ENV_VARS):
    """Stub environ then reload pipeline/config.py; return the fresh module."""
    for name in absent:
        monkeypatch.delenv(name, raising=False)
    for name, value in (setenv or {}).items():
        monkeypatch.setenv(name, value)
    return importlib.reload(pipeline_config)


# ---------------------------------------------------------------------------
# Defaults: hardcoded-fallback invariant (stub env empty, safe fallback fires)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("constant,expected", sorted(WEDGE_DEFAULTS.items()))
def test_default_when_env_absent(monkeypatch, constant, expected):
    """With all three PIPELINE_WEDGE_* vars absent, fallbacks are 1/1800/3600."""
    module = _reload_with_env(monkeypatch)
    resolved = getattr(module, constant)
    assert resolved == expected
    # Values are ints, not strings.
    assert isinstance(resolved, int)
    assert not isinstance(resolved, bool)


@pytest.mark.parametrize("env_var,constant", sorted(ENV_VAR_TO_CONSTANT.items()))
def test_default_when_only_that_var_absent(monkeypatch, env_var, constant):
    """Each fallback fires even when the OTHER wedge vars are set."""
    others = {name: "999999" for name in WEDGE_ENV_VARS if name != env_var}
    module = _reload_with_env(monkeypatch, setenv=others)
    assert getattr(module, constant) == WEDGE_DEFAULTS[constant]


# ---------------------------------------------------------------------------
# Overrides: env var wins over the hardcoded fallback
# ---------------------------------------------------------------------------


def test_override_stale_activity_seconds(monkeypatch):
    """PIPELINE_WEDGE_STALE_ACTIVITY_SECONDS=60 resolves to 60."""
    module = _reload_with_env(
        monkeypatch, setenv={"PIPELINE_WEDGE_STALE_ACTIVITY_SECONDS": "60"}
    )
    assert module.WEDGE_STALE_ACTIVITY_SECONDS == 60
    assert isinstance(module.WEDGE_STALE_ACTIVITY_SECONDS, int)


def test_override_notify_cooldown_seconds(monkeypatch):
    """PIPELINE_WEDGE_NOTIFY_COOLDOWN_SECONDS=600 resolves to 600."""
    module = _reload_with_env(
        monkeypatch, setenv={"PIPELINE_WEDGE_NOTIFY_COOLDOWN_SECONDS": "600"}
    )
    assert module.WEDGE_NOTIFY_COOLDOWN_SECONDS == 600


def test_override_scan_enabled(monkeypatch):
    """PIPELINE_WEDGE_SCAN_ENABLED=0 resolves to 0 (scan off)."""
    module = _reload_with_env(monkeypatch, setenv={"PIPELINE_WEDGE_SCAN_ENABLED": "0"})
    assert module.WEDGE_SCAN_ENABLED == 0
    assert isinstance(module.WEDGE_SCAN_ENABLED, int)


def test_scan_enabled_zero_is_int_zero_not_falsy_string(monkeypatch):
    """The disabled value parses as the int 0, not the string "0"."""
    module = _reload_with_env(monkeypatch, setenv={"PIPELINE_WEDGE_SCAN_ENABLED": "0"})
    assert module.WEDGE_SCAN_ENABLED == 0
    assert module.WEDGE_SCAN_ENABLED is not False
    assert not isinstance(module.WEDGE_SCAN_ENABLED, str)


@pytest.mark.parametrize(
    "env_var,constant", sorted(ENV_VAR_TO_CONSTANT.items())
)
def test_zero_parses_for_every_wedge_var(monkeypatch, env_var, constant):
    """Boundary: "0" is a valid int for all three knobs."""
    module = _reload_with_env(monkeypatch, setenv={env_var: "0"})
    assert getattr(module, constant) == 0


@pytest.mark.parametrize(
    "env_var,constant", sorted(ENV_VAR_TO_CONSTANT.items())
)
def test_large_value_parses_for_every_wedge_var(monkeypatch, env_var, constant):
    """Boundary: a large-but-legal value round-trips through int()."""
    module = _reload_with_env(monkeypatch, setenv={env_var: "86400"})
    assert getattr(module, constant) == 86400


# ---------------------------------------------------------------------------
# Malformed values: ValueError at resolution, matching USAGE_STALE_AFTER_SECONDS
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env_var,constant", sorted(ENV_VAR_TO_CONSTANT.items())
)
@pytest.mark.parametrize("bad_value", ["abc", "", "12.5", "1 800"])
def test_non_numeric_value_raises_value_error(monkeypatch, env_var, bad_value, constant):
    """A non-numeric value raises ValueError at import/resolution time.

    No new error handling: this matches the existing convention of
    USAGE_STALE_AFTER_SECONDS = int(os.environ.get(...)), where int() itself
    raises. Assert both the exception type and that the offending value is
    named in the message.
    """
    with pytest.raises(ValueError) as excinfo:
        _reload_with_env(monkeypatch, setenv={env_var: bad_value})
    assert bad_value in str(excinfo.value)


def test_non_numeric_matches_existing_usage_stale_behavior(monkeypatch):
    """The wedge knobs fail exactly like USAGE_STALE_AFTER_SECONDS does."""
    with pytest.raises(ValueError):
        _reload_with_env(
            monkeypatch, setenv={"PIPELINE_USAGE_STALE_AFTER_SECONDS": "abc"}
        )
    with pytest.raises(ValueError):
        _reload_with_env(
            monkeypatch, setenv={"PIPELINE_WEDGE_STALE_ACTIVITY_SECONDS": "abc"}
        )


# ---------------------------------------------------------------------------
# config.__all__ membership (shared artifact: membership only, never exact)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("constant", sorted(WEDGE_DEFAULTS))
def test_constant_exported_in_all(constant):
    """Each wedge constant name appears in pipeline.config.__all__."""
    assert constant in pipeline_config.__all__


@pytest.mark.parametrize("constant", sorted(WEDGE_DEFAULTS))
def test_all_entry_matches_real_attribute(constant):
    """The __all__ entry names a module attribute that actually exists."""
    assert hasattr(pipeline_config, constant)


# ---------------------------------------------------------------------------
# ENV_VAR_CATALOG membership (shared artifact: membership only, never exact)
# ---------------------------------------------------------------------------


def _catalog_names():
    return [spec.name for spec in config_provenance.ENV_VAR_CATALOG]


@pytest.mark.parametrize("env_var", sorted(WEDGE_ENV_VARS))
def test_env_var_in_catalog(env_var):
    """Each PIPELINE_WEDGE_* var is catalogued in config_provenance.

    Without the entry, get_effective_config would report the var under
    ignored_env_vars_present even though it now has an effect.
    """
    assert env_var in _catalog_names()


@pytest.mark.parametrize(
    "env_var,expected_default",
    [
        ("PIPELINE_WEDGE_SCAN_ENABLED", "1"),
        ("PIPELINE_WEDGE_STALE_ACTIVITY_SECONDS", "1800"),
        ("PIPELINE_WEDGE_NOTIFY_COOLDOWN_SECONDS", "3600"),
    ],
)
def test_catalog_entry_records_default(env_var, expected_default):
    """The catalog spec for each wedge var records the same default as config."""
    specs = {spec.name: spec for spec in config_provenance.ENV_VAR_CATALOG}
    assert env_var in specs
    assert specs[env_var].default == expected_default


@pytest.mark.parametrize("env_var", sorted(WEDGE_ENV_VARS))
def test_wedge_vars_are_not_transport_only_ignored(env_var):
    """Wedge vars must NOT be listed in IGNORED_ENV_VARS (they have an effect)."""
    ignored_names = [name for name, _replacement in config_provenance.IGNORED_ENV_VARS]
    assert env_var not in ignored_names


# ---------------------------------------------------------------------------
# README env-var documentation (only if README catalogues env vars at all)
# ---------------------------------------------------------------------------


def test_readme_documents_wedge_vars_if_it_catalogues_env_vars():
    """If README.md has a PIPELINE_ env-var table, the three wedge rows exist.

    Membership-only: later stories may add rows, so never assert the table's
    full contents. Skips (via the guard below) when README does not catalogue
    env vars at all.
    """
    readme = REPO_ROOT / "README.md"
    if not readme.exists():
        pytest.skip("README.md does not exist")
    text = readme.read_text(encoding="utf-8")
    if "PIPELINE_PAUSE_THRESHOLD" not in text:
        pytest.skip("README.md does not catalogue PIPELINE_ env vars")
    for env_var in WEDGE_ENV_VARS:
        assert env_var in text, f"README.md env-var table is missing {env_var}"