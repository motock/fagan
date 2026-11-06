"""Dashboard Node-eval tests share one copy of each identical DOM shim.

Six dashboard test files carried byte-identical copies of two shims (178 and
27 lines). They now import them from ``tests.unit._dom_shims``; this guards
against a file growing its own copy again, which would drift silently from
the shared one.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from tests.unit import _dom_shims

UNIT_DIR = Path(__file__).resolve().parent

PLANLIST_FILES = ("planlist_diff_dataset", "planlist_nav_order")
DROPDOWN_FILES = (
    "config_dropdowns",
    "notification_filter",
    "role_model_dropdown",
    "toast_dedup_key",
)


def _load(stem: str):
    path = UNIT_DIR / f"test_dashboard_{stem}.py"
    spec = importlib.util.spec_from_file_location(f"_shim_probe_{stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("stem", PLANLIST_FILES)
def test_should_use_the_shared_planlist_shim(stem: str) -> None:
    assert _load(stem)._SHIM is _dom_shims.PLANLIST_SHIM


@pytest.mark.parametrize("stem", DROPDOWN_FILES)
def test_should_use_the_shared_dropdown_shim(stem: str) -> None:
    assert _load(stem)._SHIM is _dom_shims.DROPDOWN_SHIM


def test_should_keep_the_two_shared_shims_distinct() -> None:
    assert _dom_shims.PLANLIST_SHIM != _dom_shims.DROPDOWN_SHIM


def test_should_keep_the_unique_notification_filter_shim_local() -> None:
    module = _load("notification_filter")

    assert module._DOM_SHIM not in (
        _dom_shims.PLANLIST_SHIM,
        _dom_shims.DROPDOWN_SHIM,
    )
