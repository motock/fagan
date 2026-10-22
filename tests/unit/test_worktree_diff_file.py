import os
import subprocess

import pytest

from pipeline import worktree_diff as wd

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}
INVALID = {"ok": False, "status": 400, "error": "invalid path"}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def wt(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-b", "master")
    (main / "mod.txt").write_text("a\nb\nc\n")
    (main / "other.txt").write_text("o\n")
    (main / "gone.txt").write_text("x\ny\n")
    (main / "bin.dat").write_bytes(b"\0\1\2")
    git(main, "add", "-A")
    git(main, "commit", "-m", "base")
    path = tmp_path / "wt"
    git(main, "worktree", "add", "-b", "agent/x", str(path))
    return path


def diff(wt, path, allowed=None, **kw):
    sha, _ = wd.resolve_base_sha(wt)
    return wd.collect_file_diff(wt, sha, path, allowed if allowed is not None else {path}, **kw)


def test_should_return_only_requested_tracked_file_hunks(wt):
    (wt / "mod.txt").write_text("a\nB\nc\n")
    (wt / "other.txt").write_text("changed\n")
    result = diff(wt, "mod.txt")
    assert result["available"] is True
    assert "+B" in result["diff"] and "other.txt" not in result["diff"]
    assert result["truncated"] is False and result["binary"] is False


def test_should_synthesize_addition_diff_for_untracked_text_file(wt):
    (wt / "new.txt").write_text("hello\n")
    result = diff(wt, "new.txt")
    assert result["available"] is True
    assert "+hello" in result["diff"]


@pytest.mark.parametrize("bad", ["../../etc/passwd", "/etc/passwd", ".git/config", "mod.txt/../x"])
def test_should_reject_unsafe_path_without_reading_filesystem(wt, monkeypatch, bad):
    sha, _ = wd.resolve_base_sha(wt)
    def boom(*a, **k):
        raise AssertionError("filesystem touched")
    monkeypatch.setattr(wd.os, "lstat", boom)
    monkeypatch.setattr(wd, "_git", boom)
    assert wd.collect_file_diff(wt, sha, bad, {bad}) == INVALID


def test_should_reject_path_absent_from_allowed_list(wt, monkeypatch):
    sha, _ = wd.resolve_base_sha(wt)
    def boom(*a, **k):
        raise AssertionError("filesystem touched")
    monkeypatch.setattr(wd.os, "lstat", boom)
    monkeypatch.setattr(wd, "_git", boom)
    assert wd.collect_file_diff(wt, sha, "mod.txt", {"other.txt"}) == INVALID


def test_should_skip_untracked_symlink(wt, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET\n")
    (wt / "link.txt").symlink_to(secret)
    result = diff(wt, "link.txt")
    assert "TOPSECRET" not in result.get("diff", "")
    assert result["diff"] == "" and result.get("skipped") == "symlink"


def test_should_skip_untracked_file_over_one_mib(wt):
    (wt / "big.txt").write_bytes(b"x" * (1024 * 1024 + 1))
    result = diff(wt, "big.txt")
    assert result["skipped"] == "too_large" and result["diff"] == ""


def test_should_report_binary_tracked_file(wt):
    (wt / "bin.dat").write_bytes(b"\0\3\4\5")
    result = diff(wt, "bin.dat")
    assert result["binary"] is True and result["diff"] == ""


def test_should_report_binary_untracked_file(wt):
    (wt / "new.bin").write_bytes(b"\0\1")
    result = diff(wt, "new.bin")
    assert result["binary"] is True and result["diff"] == ""


def test_should_truncate_with_explicit_note_over_max_chars(wt):
    (wt / "mod.txt").write_text("a\n" + "line\n" * 200)
    result = diff(wt, "mod.txt", max_chars=100)
    assert result["truncated"] is True
    assert result["diff"].startswith("diff --git")
    assert "truncated" in result["note"]


def test_should_return_deletion_diff_for_deleted_file(wt):
    (wt / "gone.txt").unlink()
    result = diff(wt, "gone.txt")
    assert result["binary"] is False
    assert "deleted file mode" in result["diff"] and "-x" in result["diff"]


def test_should_report_unavailable_when_git_fails(tmp_path):
    result = wd.collect_file_diff(tmp_path, "deadbeef", "a.txt", {"a.txt"})
    assert result["available"] is False and result["reason"]
