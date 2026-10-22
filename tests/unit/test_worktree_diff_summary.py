import os
import subprocess
from pathlib import Path

import pytest

from pipeline import worktree_diff as wd

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    """(worktree_root, main_repo, worktree) with a base commit on master."""
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-b", "master")
    (main / "mod.txt").write_text("a\nb\nc\n")
    (main / "gone.txt").write_text("x\ny\n")
    (main / "old name.txt").write_text("one\ntwo\nthree\nfour\n")
    (main / "keep.txt").write_text("k\n")
    git(main, "add", "-A")
    git(main, "commit", "-m", "base")
    root = tmp_path / "worktrees"
    root.mkdir()
    wt = root / "WTD-1"
    git(main, "worktree", "add", "-b", "agent/wtd-1", str(wt))
    return root, main, wt


def summary(wt):
    sha, ref = wd.resolve_base_sha(wt)
    return wd.collect_diff_summary(wt, sha, base_ref=ref)


def by_path(result):
    return {f["path"]: f for f in result["files"]}


def test_should_report_each_change_kind_once_with_counts(repo):
    _, _, wt = repo
    (wt / "mod.txt").write_text("a\nB\nc\nd\n")
    (wt / "gone.txt").unlink()
    (wt / "added.txt").write_text("1\n2\n")
    git(wt, "add", "added.txt")
    (wt / "new.txt").write_text("p\nq\nr\n")

    result = summary(wt)

    assert result["available"] is True
    assert [f["path"] for f in result["files"]] == ["added.txt", "gone.txt", "mod.txt", "new.txt"]
    files = by_path(result)
    assert (files["mod.txt"]["status"], files["mod.txt"]["added"], files["mod.txt"]["removed"]) == ("M", 2, 1)
    assert (files["added.txt"]["status"], files["added.txt"]["added"]) == ("A", 2)
    assert (files["gone.txt"]["status"], files["gone.txt"]["removed"]) == ("D", 2)
    assert files["new.txt"]["status"] == "?"
    assert files["new.txt"]["untracked"] is True
    assert files["new.txt"]["added"] == 3
    assert files["mod.txt"]["untracked"] is False


def test_should_fill_totals_and_head_metadata(repo):
    _, _, wt = repo
    (wt / "mod.txt").write_text("a\nb\nc\nd\n")
    git(wt, "commit", "-am", "c1")
    (wt / "keep.txt").write_text("k\nk2\n")
    (wt / "new.txt").write_text("n\n")

    result = summary(wt)

    assert result["branch"] == "agent/wtd-1"
    assert result["head_sha"] == git(wt, "rev-parse", "HEAD").strip()
    assert result["base_ref"] == "master"
    assert result["totals"] == {"files": 3, "added": 3, "removed": 0, "untracked": 1,
                                "committed": 1, "working": 2}


@pytest.mark.parametrize("name", [
    "agent.log", "agent.log.ts", ".agent_scratchpad.md", ".agent_plan.md", ".dispatch_baseline_test_checked",
])
def test_should_exclude_pipeline_artifacts_even_without_info_exclude(repo, name):
    _, _, wt = repo
    (wt / name).write_text("noise\n")
    (wt / "real.txt").write_text("real\n")

    paths = [f["path"] for f in summary(wt)["files"]]

    assert paths == ["real.txt"]


def test_should_exclude_tracked_artifact_changes_from_diff(repo):
    _, _, wt = repo
    (wt / "agent.log").write_text("v1\n")
    git(wt, "add", "-f", "agent.log")
    git(wt, "commit", "-m", "oops")

    assert summary(wt)["files"] == []


def test_should_parse_rename_with_space_in_path(repo):
    _, _, wt = repo
    git(wt, "mv", "old name.txt", "new name.txt")

    files = by_path(summary(wt))

    assert list(files) == ["new name.txt"]
    assert files["new name.txt"]["status"] == "R"
    assert files["new name.txt"]["old_path"] == "old name.txt"


def test_should_handle_untracked_path_with_space(repo):
    _, _, wt = repo
    (wt / "sp ace.txt").write_text("z\n")

    assert by_path(summary(wt))["sp ace.txt"]["untracked"] is True


def test_should_flag_binary_file(repo):
    _, _, wt = repo
    (wt / "blob.bin").write_bytes(b"\0\1\2\3")

    entry = by_path(summary(wt))["blob.bin"]

    assert entry["binary"] is True
    assert entry["added"] == 0


def test_should_truncate_when_max_files_reached(repo):
    _, _, wt = repo
    for i in range(3):
        (wt / f"f{i}.txt").write_text("x\n")
    sha, _ = wd.resolve_base_sha(wt)

    result = wd.collect_diff_summary(wt, sha, max_files=2)

    assert result["truncated"] is True
    assert len(result["files"]) == 2


def test_should_not_truncate_at_exact_limit(repo):
    _, _, wt = repo
    for i in range(2):
        (wt / f"f{i}.txt").write_text("x\n")
    sha, _ = wd.resolve_base_sha(wt)

    assert wd.collect_diff_summary(wt, sha, max_files=2)["truncated"] is False


def test_should_leave_status_and_index_unchanged(repo):
    _, _, wt = repo
    (wt / "mod.txt").write_text("changed\n")
    (wt / "new.txt").write_text("n\n")
    git_dir = Path(git(wt, "rev-parse", "--absolute-git-dir").strip())
    before_status = git(wt, "status", "--porcelain")
    before_index = (git_dir / "index").read_bytes()

    summary(wt)
    summary(wt)

    assert git(wt, "status", "--porcelain") == before_status
    assert (git_dir / "index").read_bytes() == before_index


def test_should_resolve_worktree_inside_root(repo):
    root, _, wt = repo

    assert wd.resolve_worktree({"worktree": str(wt)}, root) == wt.resolve()


def test_should_reject_worktree_outside_root(repo):
    root, main, _ = repo

    assert wd.resolve_worktree({"worktree": str(main)}, root) is None


def test_should_reject_traversal_out_of_root(repo):
    root, _, _ = repo

    assert wd.resolve_worktree({"worktree": f"{root}/../main"}, root) is None


@pytest.mark.parametrize("story", [{}, {"worktree": ""}, {"worktree": "rel/path"}, {"worktree": 5}, None])
def test_should_reject_malformed_story_worktree(repo, story):
    root, _, _ = repo

    assert wd.resolve_worktree(story, root) is None


def test_should_reject_missing_directory_in_resolve(repo):
    root, _, _ = repo

    assert wd.resolve_worktree({"worktree": str(root / "nope")}, root) is None


def test_should_report_worktree_gone(tmp_path):
    result = wd.collect_diff_summary(tmp_path / "missing", "abc")

    assert (result["available"], result["reason"]) == (False, "worktree_gone")


def test_should_report_not_a_worktree(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    result = wd.collect_diff_summary(plain, "abc")

    assert (result["available"], result["reason"]) == (False, "not_a_worktree")


def test_should_report_unavailable_for_unresolved_worktree():
    assert wd.collect_diff_summary(None, "abc")["available"] is False


def test_should_report_no_base_ref(repo):
    _, _, wt = repo

    result = wd.collect_diff_summary(wt, None)

    assert (result["available"], result["reason"]) == (False, "no_base_ref")


def test_should_return_no_base_when_no_candidate_resolves(tmp_path):
    lone = tmp_path / "lone"
    lone.mkdir()
    git(lone, "init", "-b", "feature")
    (lone / "f").write_text("1\n")
    git(lone, "add", "-A")
    git(lone, "commit", "-m", "x")

    assert wd.resolve_base_sha(lone) == (None, None)


def test_should_resolve_base_sha_and_ref(repo):
    _, main, wt = repo

    assert wd.resolve_base_sha(wt) == (git(main, "rev-parse", "master").strip(), "master")

