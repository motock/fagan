"""Guards the five line-limit pins converted to assert_within_line_limit.

Each converted test must route its size check through the shared helper so
that the effective file_size_policy decides whether an over-limit file fails.
A hard `assert count <= N` would bypass the policy again, so these checks read
the test source and require the helper call with the expected limit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
HELPER_IMPORT = "from tests.unit._line_limit_policy import assert_within_line_limit"

# (test file, expected helper call, the hard assert it replaced)
CONVERTED_PINS = [
    (
        "test_notify_user_enqueues_outbox.py",
        "assert_within_line_limit(count, PRODUCTION_LINE_CEILING, module)",
        "assert count <= PRODUCTION_LINE_CEILING",
    ),
    (
        "test_dispatch_baseline_test_scope.py",
        'assert_within_line_limit(line_count, 1000, "pipeline/dispatch.py")',
        "assert line_count <= 1000",
    ),
    (
        "test_check_story_status_test_scope.py",
        'assert_within_line_limit(line_count, 999, "pipeline/story_status.py")',
        "assert line_count <= 999",
    ),
    (
        "test_dispatch_local_scope_kwargs_wiring.py",
        'assert_within_line_limit(len(lines), 999, "pipeline/dispatch.py")',
        "assert len(lines) <= 999",
    ),
    (
        "test_ingest_plan_test_scope.py",
        'assert_within_line_limit(line_count, 1000, "pipeline/ingest.py")',
        "assert line_count <= 1000",
    ),
]


@pytest.mark.parametrize(
    ("filename", "helper_call", "replaced_assert"),
    CONVERTED_PINS,
    ids=[name for name, _, _ in CONVERTED_PINS],
)
def test_size_pin_routes_through_line_limit_helper(
    filename, helper_call, replaced_assert
):
    source = (TESTS_DIR / filename).read_text(encoding="utf-8")
    assert HELPER_IMPORT in source
    assert helper_call in source


@pytest.mark.parametrize(
    ("filename", "helper_call", "replaced_assert"),
    CONVERTED_PINS,
    ids=[name for name, _, _ in CONVERTED_PINS],
)
def test_size_pin_no_longer_uses_hard_assert(filename, helper_call, replaced_assert):
    source = (TESTS_DIR / filename).read_text(encoding="utf-8")
    assert replaced_assert not in source
