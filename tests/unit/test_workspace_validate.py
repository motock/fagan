"""Tests for pipeline.workspace: normalize_workspace_path + validate_workspace.

normalize_workspace_path is pure path-safety logic (no filesystem existence
check). validate_workspace layers existence/git-repo/has-commits checks on
top and must never raise - it converts normalize_workspace_path's ValueError
into an ok=False result.
"""

import subprocess
from pathlib import Path

import pytest
from pipeline.workspace import normalize_workspace_path, validate_workspace


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo_with_commit(path: Path) -> Path:
    path.mkdir()
    _git("init", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)
    (path / "README.md").write_text("hello\n")
    _git("add", "README.md", cwd=path)
    _git("commit", "-m", "initial commit", cwd=path)
    return path


def _init_repo_no_commit(path: Path) -> Path:
    path.mkdir()
    _git("init", cwd=path)
    return path


# --- normalize_workspace_path ---


class TestNormalizeWorkspacePath:
    def test_returns_resolved_absolute_path(self, tmp_path):
        target = tmp_path / "myrepo"
        target.mkdir()
        result = normalize_workspace_path(str(target))
        assert isinstance(result, Path)
        assert result == target.resolve()
        assert result.is_absolute()

    def test_none_raises_value_error(self):
        with pytest.raises(ValueError):
            normalize_workspace_path(None)

    def test_empty_string_raises_value_error(self):
        with pytest.raises(ValueError):
            normalize_workspace_path("")

    def test_whitespace_only_raises_value_error(self):
        with pytest.raises(ValueError):
            normalize_workspace_path("   ")

    def test_relative_path_raises_value_error(self):
        with pytest.raises(ValueError):
            normalize_workspace_path("foo/bar")

    def test_relative_path_error_message_mentions_absolute(self):
        with pytest.raises(ValueError, match="absolute"):
            normalize_workspace_path("relative/path")

    def test_traversal_segment_raises_value_error(self):
        with pytest.raises(ValueError):
            normalize_workspace_path("/tmp/../etc")

    def test_traversal_segment_error_message(self):
        with pytest.raises(ValueError, match=r"\.\."):
            normalize_workspace_path("/tmp/../etc")

    def test_traversal_rejected_even_though_resolve_would_normalize_it_away(self):
        # /tmp/foo/../foo resolves to a perfectly normal existing-looking
        # path, but the RAW input contains a ".." segment and must be
        # rejected before resolution ever happens.
        with pytest.raises(ValueError):
            normalize_workspace_path("/tmp/foo/../foo")

    def test_tilde_expansion(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        workspace = tmp_path / "project"
        workspace.mkdir()
        result = normalize_workspace_path("~/project")
        assert result == workspace.resolve()

    def test_does_not_require_path_to_exist(self, tmp_path):
        # normalize_workspace_path is pure path-safety logic; existence is
        # validate_workspace's concern, not this function's.
        missing = tmp_path / "does-not-exist"
        result = normalize_workspace_path(str(missing))
        assert result == missing.resolve()


# --- validate_workspace ---


class TestValidateWorkspace:
    def test_valid_git_repo_with_commit_returns_ok_true(self, tmp_path):
        repo = _init_repo_with_commit(tmp_path / "repo")
        result = validate_workspace(str(repo))
        assert result["ok"] is True
        assert result["path"] == str(repo.resolve())
        assert result["error"] is None

    def test_nonexistent_path_returns_ok_false(self, tmp_path):
        missing = tmp_path / "nope"
        result = validate_workspace(str(missing))
        assert result["ok"] is False
        assert result["error"]
        assert isinstance(result["error"], str)

    def test_path_is_a_file_not_a_directory_returns_ok_false(self, tmp_path):
        file_path = tmp_path / "afile.txt"
        file_path.write_text("not a directory")
        result = validate_workspace(str(file_path))
        assert result["ok"] is False
        assert result["error"]

    def test_directory_not_a_git_repo_returns_ok_false(self, tmp_path):
        plain_dir = tmp_path / "plain"
        plain_dir.mkdir()
        result = validate_workspace(str(plain_dir))
        assert result["ok"] is False
        assert result["error"]

    def test_git_repo_with_zero_commits_returns_ok_false(self, tmp_path):
        repo = _init_repo_no_commit(tmp_path / "empty_repo")
        result = validate_workspace(str(repo))
        assert result["ok"] is False
        assert result["error"]

    def test_none_returns_ok_false_not_raise(self):
        result = validate_workspace(None)
        assert result["ok"] is False
        assert result["error"]

    def test_empty_string_returns_ok_false_not_raise(self):
        result = validate_workspace("")
        assert result["ok"] is False
        assert result["error"]

    def test_whitespace_only_returns_ok_false_not_raise(self):
        result = validate_workspace("   ")
        assert result["ok"] is False
        assert result["error"]

    def test_relative_path_returns_ok_false(self):
        result = validate_workspace("foo/bar")
        assert result["ok"] is False
        assert result["error"]

    def test_traversal_path_returns_ok_false(self):
        result = validate_workspace("/tmp/../etc")
        assert result["ok"] is False
        assert result["error"]

    def test_tilde_expansion_resolves_and_validates(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        repo = _init_repo_with_commit(tmp_path / "project")
        result = validate_workspace("~/project")
        assert result["ok"] is True
        assert result["path"] == str(repo.resolve())

    def test_result_shape_has_exactly_ok_path_error_keys(self, tmp_path):
        repo = _init_repo_with_commit(tmp_path / "repo2")
        result = validate_workspace(str(repo))
        assert set(result.keys()) == {"ok", "path", "error"}

    def test_never_raises_on_invalid_input(self):
        # validate_workspace must catch normalize_workspace_path's
        # ValueError internally and convert it to an ok=False result -
        # it must never propagate the exception to the caller.
        try:
            result = validate_workspace(None)
        except ValueError:
            pytest.fail(
                "validate_workspace must not raise ValueError; it should "
                "catch normalize_workspace_path's error and return ok=False"
            )
        assert result["ok"] is False

    def test_integer_input_returns_ok_false_not_raise(self):
        try:
            result = validate_workspace(123)
        except (TypeError, AttributeError) as exc:
            pytest.fail(f"validate_workspace must not raise on non-string input (raised {exc!r})")
        assert result["ok"] is False

    def test_list_input_returns_ok_false_not_raise(self):
        try:
            result = validate_workspace([])
        except (TypeError, AttributeError) as exc:
            pytest.fail(f"validate_workspace must not raise on non-string input (raised {exc!r})")
        assert result["ok"] is False

    def test_bytes_input_returns_ok_false_not_raise(self):
        try:
            result = validate_workspace(b"not-a-path")
        except (TypeError, AttributeError) as exc:
            pytest.fail(f"validate_workspace must not raise on non-string input (raised {exc!r})")
        assert result["ok"] is False
