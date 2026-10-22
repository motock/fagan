"""Tests for the PIPELINE_AUTO_MERGE config flag (AUTO-MERGE-1).

The flag is a boolean opt-out read once at import time. It is deliberately an
exact match on the string ``"0"`` rather than a general bool parser: any other
value (including ``"false"``, ``"off"``, ``""`` and ``"0 "``) leaves auto-merge
enabled. These tests pin that contract so a later "helpful" refactor to
``strtobool`` cannot silently change the semantics.
"""

from __future__ import annotations

import importlib
import os

import pytest

import pipeline.config
import pipeline.config_provenance


def _reload_config_with(monkeypatch: pytest.MonkeyPatch, value: str | None) -> bool:
    """Reload ``pipeline.config`` with PIPELINE_AUTO_MERGE set to *value*.

    ``None`` means the variable is absent from the environment. The module is
    reloaded again on the way out so the rest of the suite sees the ambient
    environment rather than this test's patched one.
    """
    if value is None:
        monkeypatch.delenv("PIPELINE_AUTO_MERGE", raising=False)
    else:
        monkeypatch.setenv("PIPELINE_AUTO_MERGE", value)
    try:
        module = importlib.reload(pipeline.config)
        return module.PIPELINE_AUTO_MERGE
    finally:
        monkeypatch.undo()
        importlib.reload(pipeline.config)


def test_default_is_enabled_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _reload_config_with(monkeypatch, None) is True


def test_explicit_one_is_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _reload_config_with(monkeypatch, "1") is True


def test_explicit_zero_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _reload_config_with(monkeypatch, "0") is False


@pytest.mark.parametrize("value", ["true", "yes", "off", "", "0 "])
def test_non_zero_values_leave_auto_merge_enabled(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Only the exact string "0" opts out; this is not a general bool parser."""
    assert _reload_config_with(monkeypatch, value) is True


def test_flag_is_exported() -> None:
    assert "PIPELINE_AUTO_MERGE" in pipeline.config.__all__


def test_env_var_catalog_has_exactly_one_auto_merge_row() -> None:
    rows = [
        spec
        for spec in pipeline.config_provenance.ENV_VAR_CATALOG
        if spec.name == "PIPELINE_AUTO_MERGE"
    ]
    assert len(rows) == 1
    assert rows[0].default == "1"


def test_importing_config_does_not_mutate_environ() -> None:
    before = dict(os.environ)
    importlib.reload(pipeline.config)
    assert dict(os.environ) == before
