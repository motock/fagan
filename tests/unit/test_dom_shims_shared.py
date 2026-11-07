"""Dashboard Node-eval tests share one copy of each identical DOM shim.

Dashboard test files used to carry their own copies of the same Node-eval DOM
shims: byte-identical ones, and near-identical pairs that differ by at most two
lines of code. They now import them from ``tests.unit._dom_shims``; this guards
against a file growing its own copy again, which would drift silently from the
shared one.
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


NEAR_IDENTICAL = {
    "toast_behavior": "TOAST_SHIM",
    "toast_stripe_and_key": "TOAST_SHIM",
    "board_diff": "BOARD_DIFF_SHIM",
    "notifications_diff": "NOTIFICATIONS_DIFF_SHIM",
    "planlist_diff": "PLANLIST_DIFF_SHIM",
    "overview_diff": "OVERVIEW_DIFF_SHIM",
    "comms_send": "COMMS_SEND_SHIM",
    "always_poll": "ALWAYS_POLL_SHIM",
}


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


@pytest.mark.parametrize(("stem", "constant"), sorted(NEAR_IDENTICAL.items()))
def test_should_use_the_shared_near_identical_shim(stem: str, constant: str) -> None:
    assert _load(stem)._SHIM is getattr(_dom_shims, constant)


@pytest.mark.parametrize(
    ("with_extra", "without_extra", "extra_code"),
    [
        ("BOARD_DIFF_SHIM", "NOTIFICATIONS_DIFF_SHIM", 'textContent: "",'),
        ("OVERVIEW_DIFF_SHIM", "PLANLIST_DIFF_SHIM", "__planDetail"),
        ("ALWAYS_POLL_SHIM", "COMMS_SEND_SHIM", "hidden: false,"),
    ],
)
def test_should_carry_the_pair_delta_in_exactly_one_shim_of_each_pair(
    with_extra: str, without_extra: str, extra_code: str
) -> None:
    assert extra_code in getattr(_dom_shims, with_extra)
    assert extra_code not in getattr(_dom_shims, without_extra)


@pytest.mark.parametrize("constant", sorted(set(NEAR_IDENTICAL.values())))
def test_should_leave_no_unresolved_marker_in_a_composed_shim(constant: str) -> None:
    assert "@@" not in getattr(_dom_shims, constant)
