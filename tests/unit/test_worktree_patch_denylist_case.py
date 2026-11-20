"""Case-insensitive matching for the deny list in ``pipeline.worktree_patch``.

On a case-insensitive filesystem (the macOS default) ``.Claude/settings.json``,
``claude.md`` and ``.GIT/hooks/pre-commit`` address the protected files, so the
deny-by-default predicate must match every component case-folded. The existing
exact-spelling tests in ``test_worktree_patch_denylist.py`` keep passing; this
file grades the case-folded behaviour only.
"""

from __future__ import annotations

import pytest

from pipeline import worktree_patch as wp


@pytest.mark.parametrize(
    "relative_path",
    [
        ".Claude/settings.json",
        ".CLAUDE/x",
        "claude.md",
        "Claude.MD",
        "sub/dir/CLAUDE.MD",
        ".MCP.json",
        ".Mcp.Json",
        ".GIT/hooks/pre-commit",
        "a/.Git/config",
        "AGENT.LOG",
        ".Agent_Log_1/x",
        ".AGENT_PLAN.md",
        ".agent_scratchpad.MD",
        ".AGENT_TRANSCRIPT.JSON",
    ],
)
def test_denies_protected_path_in_any_case(relative_path: str) -> None:
    assert wp.is_denied_relative_path(relative_path) is True


@pytest.mark.parametrize(
    "relative_path",
    [".git/config", ".claude/settings.json", "CLAUDE.md", ".mcp.json"],
)
def test_still_denies_protected_path_in_original_spelling(relative_path: str) -> None:
    assert wp.is_denied_relative_path(relative_path) is True


@pytest.mark.parametrize(
    "relative_path",
    [
        "src/claude.py",
        "docs/CLAUDE.md.bak",
        "git/config",
        "a/claudemd",
        "README.md",
        "src/agent_log.py",
    ],
)
def test_allows_path_that_only_resembles_a_protected_name(relative_path: str) -> None:
    assert wp.is_denied_relative_path(relative_path) is False


@pytest.mark.parametrize("relative_path", ["", "/", None, 42])
def test_denies_malformed_input_fail_closed(relative_path: object) -> None:
    assert wp.is_denied_relative_path(relative_path) is True  # type: ignore[arg-type]
