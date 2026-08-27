"""Tests for pipeline.workspace.create_workspace.

create_workspace(raw) scaffolds a brand-new workspace: it normalizes the
raw path (same contract as validate_workspace - a ValueError from
normalize_workspace_path becomes an ok=False result, never a raised
exception), creates the target directory (including any missing parents),
runs `git init`, and creates an empty initial commit ("Initial commit") so
the repo has a reachable HEAD and a default branch. It must never clobber
an existing non-empty directory, and must set a local git identity if the
environment has no usable global one, so the commit always succeeds.

Return shape mirrors validate_workspace exactly: {"ok": bool, "path": str,
"error": str | None}.
"""

import os
import subprocess
from pathlib import Path

import pytest

from pipeline.workspace import create_workspace, validate_workspace


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )


def _is_root() -> bool:
    return hasattr(os, "getuid") and os.getuid() == 0


# --- happy path ---


class TestCreateWorkspaceHappyPath:
    def test_new_path_returns_ok_true(self, tmp_path):
        target = tmp_path / "newrepo"
        result = create_workspace(str(target))
        assert result["ok"] is True
        assert result["error"] is None

    def test_new_path_creates_directory(self, tmp_path):
        target = tmp_path / "newrepo"
        create_workspace(str(target))
        assert target.is_dir()

    def test_new_path_result_path_matches_resolved_target(self, tmp_path):
        target = tmp_path / "newrepo"
        result = create_workspace(str(target))
        assert result["path"] == str(target.resolve())

    def test_new_path_result_shape_has_exactly_ok_path_error_keys(self, tmp_path):
        target = tmp_path / "newrepo"
        result = create_workspace(str(target))
        assert set(result.keys()) == {"ok", "path", "error"}

    def test_new_path_is_a_git_repository(self, tmp_path):
        target = tmp_path / "newrepo"
        create_workspace(str(target))
        git_dir_result = _git("rev-parse", "--git-dir", cwd=target)
        assert git_dir_result.returncode == 0

    def test_new_path_head_is_reachable(self, tmp_path):
        target = tmp_path / "newrepo"
        create_workspace(str(target))
        head_result = _git("rev-parse", "HEAD", cwd=target)
        assert head_result.returncode == 0
        assert head_result.stdout.strip() != ""

    def test_initial_commit_message_is_exact(self, tmp_path):
        target = tmp_path / "newrepo"
        create_workspace(str(target))
        subject = _git("log", "-1", "--pretty=%s", cwd=target)
        assert subject.stdout.strip() == "Initial commit"

    def test_initial_commit_is_empty(self, tmp_path):
        target = tmp_path / "newrepo"
        create_workspace(str(target))
        changed_files = _git(
            "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD", cwd=target
        )
        assert changed_files.stdout.strip() == ""

    def test_new_path_validates_via_validate_workspace(self, tmp_path):
        target = tmp_path / "newrepo"
        create_workspace(str(target))
        validated = validate_workspace(str(target))
        assert validated["ok"] is True


# --- missing parent directories ---


class TestCreateWorkspaceCreatesParents:
    def test_creates_missing_parent_directories(self, tmp_path):
        target = tmp_path / "a" / "b" / "c" / "repo"
        result = create_workspace(str(target))
        assert result["ok"] is True
        assert target.is_dir()
        assert (tmp_path / "a" / "b" / "c").is_dir()

    def test_parents_become_a_valid_repo(self, tmp_path):
        target = tmp_path / "x" / "y" / "repo"
        create_workspace(str(target))
        head_result = _git("rev-parse", "HEAD", cwd=target)
        assert head_result.returncode == 0


# --- existing target directory: non-empty ---


class TestCreateWorkspaceExistingNonEmptyDirectory:
    def test_non_empty_directory_returns_ok_false(self, tmp_path):
        target = tmp_path / "existing"
        target.mkdir()
        (target / "keep.txt").write_text("do not touch\n")
        result = create_workspace(str(target))
        assert result["ok"] is False
        assert result["error"]

    def test_non_empty_directory_contents_untouched(self, tmp_path):
        target = tmp_path / "existing"
        target.mkdir()
        marker = target / "keep.txt"
        marker.write_text("do not touch\n")
        create_workspace(str(target))
        assert marker.exists()
        assert marker.read_text() == "do not touch\n"

    def test_non_empty_directory_not_turned_into_git_repo(self, tmp_path):
        target = tmp_path / "existing"
        target.mkdir()
        (target / "keep.txt").write_text("do not touch\n")
        create_workspace(str(target))
        assert not (target / ".git").exists()


# --- existing target directory: empty ---


class TestCreateWorkspaceExistingEmptyDirectory:
    def test_empty_existing_directory_returns_ok_true(self, tmp_path):
        target = tmp_path / "empty_existing"
        target.mkdir()
        result = create_workspace(str(target))
        assert result["ok"] is True
        assert result["error"] is None

    def test_empty_existing_directory_becomes_git_repo(self, tmp_path):
        target = tmp_path / "empty_existing"
        target.mkdir()
        create_workspace(str(target))
        head_result = _git("rev-parse", "HEAD", cwd=target)
        assert head_result.returncode == 0


# --- existing target is a file ---


class TestCreateWorkspaceExistingFile:
    def test_target_is_a_file_returns_ok_false(self, tmp_path):
        target = tmp_path / "afile.txt"
        target.write_text("i am a file\n")
        result = create_workspace(str(target))
        assert result["ok"] is False
        assert result["error"]

    def test_target_is_a_file_left_untouched(self, tmp_path):
        target = tmp_path / "afile.txt"
        target.write_text("i am a file\n")
        create_workspace(str(target))
        assert target.is_file()
        assert target.read_text() == "i am a file\n"


# --- invalid / malformed raw input (mirrors WS-01's validate_workspace negatives) ---


class TestCreateWorkspaceInvalidInput:
    def test_none_returns_ok_false_not_raise(self):
        try:
            result = create_workspace(None)
        except ValueError:
            pytest.fail("create_workspace must not raise ValueError on None")
        assert result["ok"] is False
        assert result["error"]

    def test_empty_string_returns_ok_false(self):
        result = create_workspace("")
        assert result["ok"] is False
        assert result["error"]

    def test_whitespace_only_returns_ok_false(self):
        result = create_workspace("   ")
        assert result["ok"] is False
        assert result["error"]

    def test_relative_path_returns_ok_false(self):
        result = create_workspace("foo/bar")
        assert result["ok"] is False
        assert result["error"]

    def test_traversal_segment_returns_ok_false(self):
        result = create_workspace("/tmp/../etc")
        assert result["ok"] is False
        assert result["error"]

    def test_invalid_input_result_path_is_empty_string(self):
        # Mirrors validate_workspace's contract: on a ValueError from
        # normalize_workspace_path, "path" is "" (never a partially
        # resolved or garbage path).
        result = create_workspace(None)
        assert result["path"] == ""

    def test_integer_input_returns_ok_false_not_raise(self):
        try:
            result = create_workspace(123)
        except (TypeError, AttributeError) as exc:
            pytest.fail(f"create_workspace must not raise on non-string input (raised {exc!r})")
        assert result["ok"] is False

    def test_list_input_returns_ok_false_not_raise(self):
        try:
            result = create_workspace([])
        except (TypeError, AttributeError) as exc:
            pytest.fail(f"create_workspace must not raise on non-string input (raised {exc!r})")
        assert result["ok"] is False


# --- parent directory not writable ---


class TestCreateWorkspaceUnwritableParent:
    def test_unwritable_parent_returns_ok_false(self, tmp_path):
        if _is_root():
            pytest.skip("root bypasses directory write permission checks")
        readonly_parent = tmp_path / "readonly_parent"
        readonly_parent.mkdir()
        target = readonly_parent / "newrepo"
        os.chmod(readonly_parent, 0o555)
        try:
            result = create_workspace(str(target))
            assert result["ok"] is False
            assert result["error"]
        finally:
            os.chmod(readonly_parent, 0o755)

    def test_unwritable_parent_leaves_no_partial_directory(self, tmp_path):
        if _is_root():
            pytest.skip("root bypasses directory write permission checks")
        readonly_parent = tmp_path / "readonly_parent"
        readonly_parent.mkdir()
        target = readonly_parent / "newrepo"
        os.chmod(readonly_parent, 0o555)
        try:
            create_workspace(str(target))
            assert not target.exists()
        finally:
            os.chmod(readonly_parent, 0o755)


# --- git identity fallback (commit must succeed even with no usable global identity) ---


class TestCreateWorkspaceGitIdentityFallback:
    def test_succeeds_without_global_git_identity(self, tmp_path, monkeypatch):
        # Point HOME/XDG config at an empty directory with no .gitconfig,
        # and strip any identity from the environment, so the only way the
        # empty initial commit can succeed is if create_workspace sets a
        # local user.name/user.email on the repo it just created.
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home / ".config"))
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        for var in (
            "GIT_AUTHOR_NAME",
            "GIT_AUTHOR_EMAIL",
            "GIT_COMMITTER_NAME",
            "GIT_COMMITTER_EMAIL",
            "GIT_CONFIG_GLOBAL",
            "EMAIL",
        ):
            monkeypatch.delenv(var, raising=False)

        target = tmp_path / "identityless_repo"
        result = create_workspace(str(target))

        assert result["ok"] is True
        assert result["error"] is None
        head_result = _git("rev-parse", "HEAD", cwd=target)
        assert head_result.returncode == 0
