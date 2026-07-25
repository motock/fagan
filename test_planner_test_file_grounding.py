"""Tests for the git_ops helpers that ground the planner in the test-author's
ACTUAL committed test files (strengthening ``_TEST_AUTHOR_ALREADY_RAN_CLAUSE``).

Root-caused live 2026-07-25 on MODE40-CI-REWORK-FEEDBACK-V2's THIRD reset: the
prohibition-only clause told the planner not to emit a write-the-test-file step,
but gave it no concrete grounding in WHICH file/tests actually exist on the
branch. Even sonnet, planning from the story's own ``agent_instructions``
(which still describe the pre-split TDD flow verbatim), re-derived a
"Write test_ci_rework_feedback.py" step with INVENTED test-case names that did
not match the ones the test-author had committed - handing the executor a
contradictory brief. The fix passes the test-author's actual committed file
names + test-case names into the planner's system prompt; these tests cover the
two pure git_ops leaf helpers that detect them against REAL git repos (no
mocked subprocess), mirroring ``test_pipeline_mcp_server.py``'s
``_make_worktree_repo`` real-git fixture style.

Run with the project venv:
    cd ~/.claude/mcp-servers/pipeline && .venv/bin/python -m pytest -q test_planner_test_file_grounding.py
"""

import subprocess
from pathlib import Path

from pipeline.git_ops import _test_files_added_on_branch, _test_names_in_file


def _repo_with_worktree_branch(tmp_path: Path):
    """Real git repo on 'main' + a worktree branch 'agent/s1' off it with no
    commits beyond the shared base - lets _test_files_added_on_branch run
    against real git without mocking subprocess."""
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)],
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=repo,
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo,
                   capture_output=True, text=True, check=True)
    (repo / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=repo,
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo,
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "worktree", "add", "-b", "agent/s1", str(wt)],
                   cwd=repo, capture_output=True, text=True, check=True)
    return repo, wt


# ---------- _test_files_added_on_branch ----------

def test_test_files_added_on_branch_returns_added_test_files(tmp_path):
    _repo, wt = _repo_with_worktree_branch(tmp_path)
    (wt / "test_foo.py").write_text("def test_a(): assert True\n")
    subprocess.run(["git", "add", "-A"], cwd=wt,
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-qm", "tests"], cwd=wt,
                   capture_output=True, text=True, check=True)
    assert _test_files_added_on_branch(wt, "main") == ["test_foo.py"]


def test_test_files_added_on_branch_excludes_non_test_files(tmp_path):
    _repo, wt = _repo_with_worktree_branch(tmp_path)
    (wt / "test_foo.py").write_text("def test_a(): assert True\n")
    (wt / "impl.py").write_text("x = 1\n")
    (wt / "README.md").write_text("more\n")
    subprocess.run(["git", "add", "-A"], cwd=wt,
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-qm", "mixed"], cwd=wt,
                   capture_output=True, text=True, check=True)
    assert _test_files_added_on_branch(wt, "main") == ["test_foo.py"]


def test_test_files_added_on_branch_empty_when_no_new_commits(tmp_path):
    _repo, wt = _repo_with_worktree_branch(tmp_path)
    assert _test_files_added_on_branch(wt, "main") == []


def test_test_files_added_on_branch_multiple_test_files(tmp_path):
    _repo, wt = _repo_with_worktree_branch(tmp_path)
    (wt / "test_a.py").write_text("def test_x(): assert True\n")
    (wt / "test_b.py").write_text("def test_y(): assert True\n")
    subprocess.run(["git", "add", "-A"], cwd=wt,
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-qm", "two"], cwd=wt,
                   capture_output=True, text=True, check=True)
    assert sorted(_test_files_added_on_branch(wt, "main")) == [
        "test_a.py", "test_b.py",
    ]


def test_test_files_added_on_branch_returns_empty_on_git_error(tmp_path):
    # A nonexistent base branch makes `git diff base..HEAD` fail; the helper
    # must fail open to [] rather than raising, so a git hiccup never blocks
    # the planner call (which degrades to the prohibition-only clause).
    _repo, wt = _repo_with_worktree_branch(tmp_path)
    assert _test_files_added_on_branch(wt, "nonexistent-branch") == []


def test_test_files_added_on_branch_not_a_git_repo_returns_empty(tmp_path):
    wt = tmp_path / "nogit"
    wt.mkdir()
    # No git repo here at all - fail open.
    assert _test_files_added_on_branch(wt, "main") == []


def test_test_files_added_on_branch_excludes_test_file_modified_not_added(tmp_path):
    # A test file present on the base branch that the branch merely MODIFIED
    # (not added) is not the test-author's new work; diff-filter=A excludes it.
    repo, wt = _repo_with_worktree_branch(tmp_path)
    # Seed a test file on main itself.
    (repo / "test_seed.py").write_text("def test_seed(): assert True\n")
    subprocess.run(["git", "add", "-A"], cwd=repo,
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-qm", "seed-test"], cwd=repo,
                   capture_output=True, text=True, check=True)
    # On the branch, modify it (no new test file added).
    (wt / "test_seed.py").write_text("def test_seed(): assert False\n")
    subprocess.run(["git", "add", "-A"], cwd=wt,
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-qm", "tweak"], cwd=wt,
                   capture_output=True, text=True, check=True)
    assert _test_files_added_on_branch(wt, "main") == []


# ---------- _test_names_in_file ----------

def test_test_names_in_file_extracts_top_level_test_functions(tmp_path):
    _repo, wt = _repo_with_worktree_branch(tmp_path)
    (wt / "test_foo.py").write_text(
        "def test_a():\n    assert True\n\n"
        "def helper():\n    pass\n\n"
        "def test_b_c():\n    assert 1\n"
    )
    assert _test_names_in_file(wt, "test_foo.py") == ["test_a", "test_b_c"]


def test_test_names_in_file_ignores_indented_def(tmp_path):
    # A nested `def test_nested` inside a test function is not its own test
    # case - the helper must only pick up column-0 defs.
    _repo, wt = _repo_with_worktree_branch(tmp_path)
    (wt / "test_foo.py").write_text(
        "def test_outer():\n"
        "    def test_nested():\n"
        "        assert True\n"
    )
    assert _test_names_in_file(wt, "test_foo.py") == ["test_outer"]


def test_test_names_in_file_empty_when_no_tests(tmp_path):
    _repo, wt = _repo_with_worktree_branch(tmp_path)
    (wt / "test_foo.py").write_text("def helper():\n    pass\n")
    assert _test_names_in_file(wt, "test_foo.py") == []


def test_test_names_in_file_missing_file_returns_empty(tmp_path):
    _repo, wt = _repo_with_worktree_branch(tmp_path)
    assert _test_names_in_file(wt, "test_missing.py") == []


def test_test_names_in_file_handles_subdir_path(tmp_path):
    # A test file committed under tests/ is addressed by its repo-relative
    # path; the helper must join it onto the worktree correctly.
    _repo, wt = _repo_with_worktree_branch(tmp_path)
    sub = wt / "tests"
    sub.mkdir()
    (sub / "test_x.py").write_text("def test_one(): assert True\n")
    assert _test_names_in_file(wt, "tests/test_x.py") == ["test_one"]