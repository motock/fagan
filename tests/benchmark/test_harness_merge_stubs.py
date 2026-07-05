"""Tests for harness.py's install_merge_stubs, specifically the CI stub.

Before 2026-07-04 this unconditionally reported "pass" with no real check --
a rubber-stamp that gave the hermetic benchmark no equivalent to what a real
CI pipeline (running the actual test suite) would provide as a second,
independent check before merge. That gap is one plausible contributor to the
merged-but-wrong RLI-3 incident (see PRODUCT_ANALYST_VALIDATION_PLAN.md):
whatever let `tests_passed` get set incorrectly had no second layer to catch
it. The CI stub now actually runs the story's worktree test suite.
"""
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
if str(BENCH) not in sys.path:
    sys.path.insert(0, str(BENCH))

import harness  # noqa: E402
import pipeline_mcp_server as p  # noqa: E402


def _seed_worktree(root: Path, story_key: str, test_file_content: str) -> Path:
    """A minimal worktree: pyproject.toml (so detect_test_command picks
    pytest) + a real venv symlink (so it resolves to an interpreter with
    pytest installed, not whatever's on PATH) + one test file."""
    worktree = root / story_key
    worktree.mkdir(parents=True)
    (worktree / "pyproject.toml").write_text('[project]\nname = "t"\nversion = "0.0.0"\n')
    (worktree / ".venv").symlink_to(harness.PIPELINE_REPO / ".venv")
    (worktree / "test_x.py").write_text(test_file_content)
    return worktree


def test_ci_status_stub_fails_when_worktree_tests_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "WORKTREE_ROOT", tmp_path)
    _seed_worktree(tmp_path, "SOME-KEY", "def test_fail():\n    assert False\n")
    harness.install_merge_stubs(p, tmp_path / "repo_unused")

    result = p._ci_status("agent/some-key")

    assert result["state"] == "fail"
    assert result["error"]


def test_ci_status_stub_passes_when_worktree_tests_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "WORKTREE_ROOT", tmp_path)
    _seed_worktree(tmp_path, "SOME-KEY", "def test_ok():\n    assert True\n")
    harness.install_merge_stubs(p, tmp_path / "repo_unused")

    result = p._ci_status("agent/some-key")

    assert result["state"] == "pass"


def test_ci_status_stub_catches_missing_method_like_the_rli3_incident(tmp_path, monkeypatch):
    """The concrete shape of the 2026-07-04 incident: an acceptance-style
    test file calls a method the implementation never added. A real CI run
    of the worktree's test suite must report this as a failure."""
    monkeypatch.setattr(p, "WORKTREE_ROOT", tmp_path)
    worktree = _seed_worktree(
        tmp_path, "RLI-3",
        "from thing import Widget\n\n"
        "def test_calls_missing_method():\n"
        "    w = Widget()\n"
        "    assert w.missing_method() == 1\n",
    )
    (worktree / "thing.py").write_text("class Widget:\n    pass\n")
    harness.install_merge_stubs(p, tmp_path / "repo_unused")

    result = p._ci_status("agent/rli-3")

    assert result["state"] == "fail"


def test_ci_status_stub_passes_when_worktree_directory_is_missing(tmp_path, monkeypatch):
    """No worktree to check (e.g. it was already cleaned up) must not block
    the merge gate -- fall back to the same permissive behavior as before,
    not a spurious failure."""
    monkeypatch.setattr(p, "WORKTREE_ROOT", tmp_path)
    harness.install_merge_stubs(p, tmp_path / "repo_unused")

    result = p._ci_status("agent/does-not-exist")

    assert result["state"] == "pass"
