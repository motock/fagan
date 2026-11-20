"""The local-model reviewer's tools are confined to the story worktree.

The reviewer reads an agent-written diff, so its ``bash`` tool must not hand
it the pipeline's credentials, and its ``view_file`` tool must not read files
outside the worktree.
"""
import pytest

from app import ollama_prompt_utils as _opu

_SECRET_VALUE = "s3cret-value-do-not-leak"


@pytest.mark.parametrize(
    "secret_name",
    [
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "SLACK_WEBHOOK_URL",
        "PIPELINE_ANTHROPIC_API_KEY",
    ],
)
def test_bash_tool_child_does_not_see_secret(tmp_path, monkeypatch, secret_name):
    monkeypatch.setenv(secret_name, _SECRET_VALUE)
    result = _opu._run_readonly_tool(
        "bash", {"command": f"printenv {secret_name} || echo ABSENT"}, tmp_path
    )
    assert "ABSENT" in result
    assert _SECRET_VALUE not in result


def test_bash_tool_child_keeps_non_secret_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/custom-marker-path")
    result = _opu._run_readonly_tool("bash", {"command": "printenv PATH"}, tmp_path)
    assert "/custom-marker-path" in result


def test_view_file_refuses_parent_traversal(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (tmp_path / "outside.txt").write_text("outside-content\n")
    result = _opu._run_readonly_tool("view_file", {"path": "../outside.txt"}, worktree)
    assert "not a path inside the worktree" in result
    assert "outside-content" not in result


def test_view_file_refuses_absolute_path_outside_worktree(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside-content\n")
    result = _opu._run_readonly_tool("view_file", {"path": str(outside)}, worktree)
    assert "not a path inside the worktree" in result
    assert "outside-content" not in result


def test_view_file_refuses_symlink_escaping_worktree(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (tmp_path / "outside.txt").write_text("outside-content\n")
    (worktree / "link.txt").symlink_to(tmp_path / "outside.txt")
    result = _opu._run_readonly_tool("view_file", {"path": "link.txt"}, worktree)
    assert "not a path inside the worktree" in result
    assert "outside-content" not in result


def test_view_file_refuses_empty_path(tmp_path):
    result = _opu._run_readonly_tool("view_file", {"path": ""}, tmp_path)
    assert "not a path inside the worktree" in result


def test_view_file_refuses_missing_path_key(tmp_path):
    result = _opu._run_readonly_tool("view_file", {}, tmp_path)
    assert "not a path inside the worktree" in result


def test_view_file_returns_numbered_lines_for_file_inside_worktree(tmp_path):
    (tmp_path / "notes.txt").write_text("hello\nworld\n")
    result = _opu._run_readonly_tool("view_file", {"path": "notes.txt"}, tmp_path)
    assert result == "   1| hello\n   2| world\n"


def test_view_file_reports_missing_file_inside_worktree(tmp_path):
    result = _opu._run_readonly_tool("view_file", {"path": "missing.txt"}, tmp_path)
    assert result == "ERROR: missing.txt does not exist."
