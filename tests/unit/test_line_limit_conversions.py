"""Guards that the size pins route through the policy-aware line-limit helper."""

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
IMPORT_LINE = "from tests.unit._line_limit_policy import assert_within_line_limit"

CONVERTED_CALLS = [
    (
        "tests/unit/test_record_security_audit_tool.py",
        'assert_within_line_limit(len(lines), 1000, "pipeline/server.py")',
        "assert len(lines) <= 1000",
    ),
    (
        "tests/unit/test_scheduler_timeouts_extraction.py",
        'assert_within_line_limit(len(DAEMON_PATH.read_text(encoding="utf-8").splitlines()), 999, DAEMON_PATH.name)',
        'assert len(DAEMON_PATH.read_text(encoding="utf-8").splitlines()) < 1000',
    ),
    (
        "tests/unit/test_story_status_records_split.py",
        "assert_within_line_limit(len((REPO / path).read_text().splitlines()), 999, path)",
        "assert len((REPO / path).read_text().splitlines()) < 1000",
    ),
    (
        "tests/unit/test_tool_docs_list_every_story_field.py",
        'assert_within_line_limit(len(lines), 1000, "pipeline/server.py")',
        "assert len(lines) <= 1000",
    ),
    (
        "tests/unit/test_worktree_patch_split.py",
        "assert_within_line_limit(line_count, 999, relative)",
        "assert line_count < 1000",
    ),
]


@pytest.mark.parametrize(("relative", "call", "old_assert"), CONVERTED_CALLS)
def test_should_route_size_pin_through_policy_helper(relative, call, old_assert):
    source = (REPO / relative).read_text(encoding="utf-8")

    assert f"    {call}" in source


@pytest.mark.parametrize(("relative", "call", "old_assert"), CONVERTED_CALLS)
def test_should_import_policy_helper_in_converted_file(relative, call, old_assert):
    source = (REPO / relative).read_text(encoding="utf-8")

    assert IMPORT_LINE in source


@pytest.mark.parametrize(("relative", "call", "old_assert"), CONVERTED_CALLS)
def test_should_not_keep_hard_size_assert_after_conversion(relative, call, old_assert):
    source = (REPO / relative).read_text(encoding="utf-8")

    assert f"    {old_assert}" not in source
