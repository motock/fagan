"""Read-only worktree status for a story (branch, commits ahead, dirty paths, scratchpad).

The git behaviour is exercised against real repositories built in ``tmp_path``:
the porcelain parser is the interesting part, and a fake would not prove that a
path containing a space, a newline or a leading dash survives it intact.
"""

# ruff: noqa: I001
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pipeline import server
from pipeline import server_tools_read, story_worktree_status
from pipeline.remote_tool_policy import (
    PLAN_SCOPED_READ,
    REMOTE_READ,
    remote_tool_allowed,
)

TOOL = "get_story_worktree"
SCRATCHPAD = ".agent_scratchpad.md"


def _git(cwd: Path, *args: str) -> str:
    """Run one git command in *cwd*, isolated from the host's git config."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )
    if result.returncode != 0:
        raise AssertionError(f"git {args[0]} failed: {result.stderr}")
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository with one commit on ``main``."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("a\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "base")
    return root


@pytest.fixture
def worktrees_root(tmp_path: Path) -> Path:
    root = tmp_path / "worktrees"
    root.mkdir()
    return root


@pytest.fixture
def worktree(repo: Path, worktrees_root: Path) -> Path:
    """A story worktree on branch ``story/S-1``, level with ``main``."""
    path = worktrees_root / "S-1"
    _git(repo, "worktree", "add", "-q", "-b", "story/S-1", str(path), "main")
    return path


# --------------------------------------------------------------------------- #
# The status body
# --------------------------------------------------------------------------- #
def test_should_report_one_commit_ahead_one_modified_and_one_untracked(
    worktree: Path, worktrees_root: Path
) -> None:
    (worktree / "b.txt").write_text("b\n")
    _git(worktree, "add", "b.txt")
    _git(worktree, "commit", "-q", "-m", "add b")
    head = _git(worktree, "rev-parse", "HEAD").strip()
    (worktree / "a.txt").write_text("changed\n")
    (worktree / "c.txt").write_text("c\n")

    result = story_worktree_status.collect({"worktree": str(worktree)}, worktrees_root)

    assert result["available"] is True
    assert result["branch"] == "story/S-1"
    assert result["head"] == head
    assert result["commits_ahead"] == [{"sha": head, "subject": "add b"}]
    assert {(entry["path"], entry["state"]) for entry in result["uncommitted"]} == {
        ("a.txt", "M"),
        ("c.txt", "??"),
    }
    assert result["scratchpad"] == ""


def test_should_report_a_clean_worktree_as_empty_lists(
    worktree: Path, worktrees_root: Path
) -> None:
    result = story_worktree_status.collect({"worktree": str(worktree)}, worktrees_root)

    assert result["available"] is True
    assert result["branch"] == "story/S-1"
    assert result["commits_ahead"] == []
    assert result["uncommitted"] == []
    assert result["scratchpad"] == ""


@pytest.mark.parametrize(
    "story",
    [
        {"summary": "no worktree key"},
        {"worktree": None},
        {"worktree": ""},
        {"worktree": 42},
        {"worktree": "worktrees/S-1"},
        {"worktree": "../outside"},
    ],
)
def test_should_report_no_worktree_without_raising(
    story: dict, worktrees_root: Path
) -> None:
    result = story_worktree_status.collect(story, worktrees_root)

    assert result["available"] is False
    assert result["reason"] == "no_worktree"
    assert result["branch"] is None
    assert result["head"] is None
    assert result["commits_ahead"] == []
    assert result["uncommitted"] == []
    assert result["scratchpad"] == ""


def test_should_refuse_a_manifest_worktree_outside_the_root(
    tmp_path: Path, worktrees_root: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    _git(outside, "init", "-q", "-b", "main")

    result = story_worktree_status.collect({"worktree": str(outside)}, worktrees_root)

    assert result["available"] is False
    assert result["reason"] == "no_worktree"


def test_should_refuse_a_worktree_root_that_merely_shares_a_prefix(
    tmp_path: Path, worktrees_root: Path
) -> None:
    sibling = worktrees_root.parent / (worktrees_root.name + "-evil")
    sibling.mkdir()

    result = story_worktree_status.collect({"worktree": str(sibling)}, worktrees_root)

    assert result["available"] is False
    assert result["reason"] == "no_worktree"


def test_should_report_awkward_paths_intact_and_never_execute_them(
    worktree: Path, worktrees_root: Path
) -> None:
    names = [
        "a b.txt",
        "-rf.txt",
        "line\nbreak.txt",
        "$(touch PWNED).txt",
        "semi;touch PWNED2;.txt",
    ]
    for name in names:
        (worktree / name).write_text("x")

    result = story_worktree_status.collect({"worktree": str(worktree)}, worktrees_root)

    reported = {entry["path"] for entry in result["uncommitted"]}
    assert set(names) <= reported
    assert not (worktree / "PWNED").exists()
    assert not (worktree / "PWNED2").exists()


def test_should_cap_commits_ahead_at_ten_newest_first(
    worktree: Path, worktrees_root: Path
) -> None:
    for index in range(story_worktree_status.MAX_COMMITS_AHEAD + 2):
        (worktree / f"f{index}.txt").write_text("x")
        _git(worktree, "add", f"f{index}.txt")
        _git(worktree, "commit", "-q", "-m", f"c{index}")

    result = story_worktree_status.collect({"worktree": str(worktree)}, worktrees_root)

    assert story_worktree_status.MAX_COMMITS_AHEAD == 10
    assert [entry["subject"] for entry in result["commits_ahead"]] == [
        f"c{index}" for index in range(11, 1, -1)
    ]


def test_should_cap_uncommitted_paths_at_two_hundred(
    worktree: Path, worktrees_root: Path
) -> None:
    for index in range(story_worktree_status.MAX_UNCOMMITTED + 5):
        (worktree / f"u{index:03d}.txt").write_text("x")

    result = story_worktree_status.collect({"worktree": str(worktree)}, worktrees_root)

    assert story_worktree_status.MAX_UNCOMMITTED == 200
    assert len(result["uncommitted"]) == 200


# --------------------------------------------------------------------------- #
# Scratchpad
# --------------------------------------------------------------------------- #
def test_should_mask_secrets_in_the_scratchpad(
    worktree: Path, worktrees_root: Path
) -> None:
    token = "ghp_" + "a" * 36
    (worktree / SCRATCHPAD).write_text(
        f"step 1\nGITHUB_TOKEN={token}\nAuthorization: Bearer abc.def-123\n"
    )

    result = story_worktree_status.collect({"worktree": str(worktree)}, worktrees_root)

    assert result["scratchpad"].startswith("step 1")
    assert token not in result["scratchpad"]
    assert "abc.def-123" not in result["scratchpad"]
    assert "[redacted]" in result["scratchpad"]


def test_should_cap_the_scratchpad_length(worktree: Path, worktrees_root: Path) -> None:
    (worktree / SCRATCHPAD).write_text(
        "x" * (story_worktree_status.MAX_SCRATCHPAD_CHARS + 500)
    )

    result = story_worktree_status.collect({"worktree": str(worktree)}, worktrees_root)

    assert story_worktree_status.MAX_SCRATCHPAD_CHARS == 20000
    assert len(result["scratchpad"]) == story_worktree_status.MAX_SCRATCHPAD_CHARS


def test_should_not_follow_a_scratchpad_symlink_out_of_the_worktree(
    tmp_path: Path, worktree: Path, worktrees_root: Path
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("outside secret")
    (worktree / SCRATCHPAD).symlink_to(outside)

    result = story_worktree_status.collect({"worktree": str(worktree)}, worktrees_root)

    assert result["scratchpad"] == ""


def test_should_report_an_absent_scratchpad_as_an_empty_string(
    worktree: Path, worktrees_root: Path
) -> None:
    assert not (worktree / SCRATCHPAD).exists()

    result = story_worktree_status.collect({"worktree": str(worktree)}, worktrees_root)

    assert result["scratchpad"] == ""


# --------------------------------------------------------------------------- #
# git invocation and failure handling
# --------------------------------------------------------------------------- #
def test_should_run_git_with_a_fixed_argv_no_shell_and_fsmonitor_disabled(
    worktree: Path, worktrees_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[list[str], dict]] = []
    real_run = subprocess.run

    def _record(cmd, **kwargs):
        seen.append((cmd, kwargs))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(story_worktree_status.subprocess, "run", _record)

    story_worktree_status.collect({"worktree": str(worktree)}, worktrees_root)

    assert seen
    for cmd, kwargs in seen:
        assert isinstance(cmd, list)
        assert all(isinstance(part, str) for part in cmd)
        assert cmd[0] == "git"
        assert "core.fsmonitor=false" in cmd
        assert kwargs["shell"] is False
        assert kwargs["timeout"] == story_worktree_status.GIT_TIMEOUT_SECONDS > 0
        assert Path(kwargs["cwd"]) == worktree


def test_should_report_a_stable_error_on_timeout_without_echoing_stderr(
    worktree: Path, worktrees_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "ghp_" + "a" * 36

    def _timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(
            cmd=["git"], timeout=1, output="MIIsecret", stderr=f"TOKEN={token}"
        )

    monkeypatch.setattr(story_worktree_status.subprocess, "run", _timeout)

    result = story_worktree_status.collect({"worktree": str(worktree)}, worktrees_root)

    assert result["available"] is False
    assert result["reason"] == "git_failed"
    assert result["commits_ahead"] == [] and result["uncommitted"] == []
    assert result["scratchpad"] == ""
    assert token not in json.dumps(result)
    assert "MIIsecret" not in json.dumps(result)


def test_should_report_a_stable_error_on_a_non_zero_exit_without_echoing_stderr(
    worktree: Path, worktrees_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "ghp_" + "b" * 36

    class _Proc:
        returncode = 128
        stdout = ""
        stderr = f"fatal: bad object {token}"

    monkeypatch.setattr(story_worktree_status.subprocess, "run", lambda *a, **k: _Proc())

    result = story_worktree_status.collect({"worktree": str(worktree)}, worktrees_root)

    assert result["available"] is False
    assert result["reason"] == "git_failed"
    dumped = json.dumps(result)
    assert token not in dumped
    assert "fatal" not in dumped


def test_should_report_a_stable_error_when_git_is_missing(
    worktree: Path, worktrees_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _missing(*_args, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(story_worktree_status.subprocess, "run", _missing)

    result = story_worktree_status.collect({"worktree": str(worktree)}, worktrees_root)

    assert result["available"] is False
    assert result["reason"] == "git_failed"


# --------------------------------------------------------------------------- #
# The MCP tool
# --------------------------------------------------------------------------- #
@pytest.fixture
def fake_service(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    service = MagicMock()
    service.get_manifest_or_none.return_value = {"stories": {"S-1": {"summary": "s"}}}
    monkeypatch.setattr(server, "_service", service)
    return service


def test_should_register_the_tool_on_the_mcp_tool_manager() -> None:
    registered = server.mcp._tool_manager.get_tool(TOOL)

    assert registered.fn is server_tools_read.get_story_worktree


def test_should_take_only_plan_and_story_arguments() -> None:
    import inspect

    assert list(inspect.signature(server_tools_read.get_story_worktree).parameters) == [
        "plan_name",
        "story_key",
    ]


def test_should_answer_an_unknown_plan_with_404(fake_service: MagicMock) -> None:
    fake_service.get_manifest_or_none.return_value = None

    assert server_tools_read.get_story_worktree("nope", "S-1") == {
        "ok": False,
        "status": 404,
        "error": "unknown plan/story",
    }


def test_should_answer_an_unknown_story_with_404(fake_service: MagicMock) -> None:
    fake_service.get_manifest_or_none.return_value = {"stories": {"S-2": {}}}

    assert server_tools_read.get_story_worktree("plan", "S-1")["status"] == 404


def test_should_report_no_worktree_for_a_story_without_one(fake_service: MagicMock) -> None:
    result = server_tools_read.get_story_worktree("plan", "S-1")

    assert result["available"] is False
    assert result["reason"] == "no_worktree"


def test_should_resolve_the_worktree_from_the_manifest_story(
    monkeypatch: pytest.MonkeyPatch, worktree: Path, worktrees_root: Path
) -> None:
    monkeypatch.setattr(server, "WORKTREE_ROOT", worktrees_root)
    monkeypatch.setattr(
        server,
        "_service",
        MagicMock(
            get_manifest_or_none=lambda _plan: {
                "stories": {"S-1": {"worktree": str(worktree)}}
            }
        ),
    )

    result = server_tools_read.get_story_worktree("plan", "S-1")

    assert result["available"] is True
    assert result["branch"] == "story/S-1"


# --------------------------------------------------------------------------- #
# Remote reachability: classified, plan-scoped, and nothing else reclassified
# --------------------------------------------------------------------------- #
def test_should_be_classified_as_a_remote_read_tool() -> None:
    assert TOOL in REMOTE_READ
    assert remote_tool_allowed(TOOL) is True


def test_should_be_gated_on_its_plan_name_like_the_other_plan_scoped_reads() -> None:
    assert TOOL in PLAN_SCOPED_READ


def test_should_leave_every_other_read_tool_classified_as_before() -> None:
    assert REMOTE_READ == {
        "check_story_status",
        "check_usage",
        "export_plan",
        "get_effective_config",
        "get_story_diff",
        "get_story_events",
        "get_story_journal",
        "get_story_log",
        "get_story_worktree",
        "list_decisions",
        "list_plans",
        "list_ready_stories",
        "verify_audit",
    }
