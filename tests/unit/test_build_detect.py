"""Tests for pipeline.build_detect._provision_worktree_venv.

Root-caused live 2026-07-24 on RUFF-016-ADOPTION: a fresh worktree has no
.venv (gitignored), so its test gate fell back to the shared main-repo
.venv. The story's own instructions told it to "install into .venv" - with
none present, it created one containing only what it explicitly installed,
which then shadowed the fully-configured shared venv (a worktree-local
.venv is checked before the shared one) and broke every subsequent test run
in that worktree with missing-dependency errors unrelated to anything the
story touched. The same shared-venv fallback also means any concurrently-
dispatched story that installs/bumps a dependency mutates the interpreter
every OTHER running story's test gate depends on.

_provision_worktree_venv gives each fresh Python worktree its own complete,
isolated venv up front, closing both problems. Subprocess calls (venv
creation, pip install) are mocked - no live network calls in the test
suite.
"""
from unittest.mock import Mock

import pipeline.build_detect as bd


def test_noop_for_non_python_project(tmp_path, monkeypatch):
    # No pyproject.toml/setup.py at all - e.g. an npm or cargo repo. Must not
    # attempt any venv setup.
    run = Mock()
    monkeypatch.setattr(bd.subprocess, "run", run)
    bd._provision_worktree_venv(tmp_path)
    run.assert_not_called()


def test_noop_for_python_project_without_a_requirements_file(tmp_path, monkeypatch):
    # A Python project that declares deps only in pyproject.toml (no
    # requirements*.txt) is outside this fix's narrow, testable scope - it
    # must no-op rather than guess, leaving the existing shared-venv
    # fallback as before.
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    run = Mock()
    monkeypatch.setattr(bd.subprocess, "run", run)
    bd._provision_worktree_venv(tmp_path)
    run.assert_not_called()


def test_provisions_venv_from_requirements_dev_when_present(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (tmp_path / "requirements.txt").write_text("httpx\n")
    (tmp_path / "requirements-dev.txt").write_text("-r requirements.txt\npytest\n")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return Mock(returncode=0)

    monkeypatch.setattr(bd.subprocess, "run", fake_run)
    bd._provision_worktree_venv(tmp_path)

    assert len(calls) == 2
    assert calls[0] == ["python3", "-m", "venv", str(tmp_path / ".venv")]
    venv_python = str(tmp_path / ".venv" / "bin" / "python3")
    assert calls[1] == [venv_python, "-m", "pip", "install", "--quiet",
                         "-r", "requirements-dev.txt"]


def test_falls_back_to_requirements_txt_when_no_dev_file(tmp_path, monkeypatch):
    (tmp_path / "setup.py").write_text("")
    (tmp_path / "requirements.txt").write_text("httpx\n")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return Mock(returncode=0)

    monkeypatch.setattr(bd.subprocess, "run", fake_run)
    bd._provision_worktree_venv(tmp_path)

    assert len(calls) == 2
    assert calls[1][-1] == "requirements.txt"


def test_prefers_requirements_dev_over_plain_requirements(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (tmp_path / "requirements.txt").write_text("httpx\n")
    (tmp_path / "requirements-dev.txt").write_text("-r requirements.txt\npytest\n")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return Mock(returncode=0)

    monkeypatch.setattr(bd.subprocess, "run", fake_run)
    bd._provision_worktree_venv(tmp_path)

    assert calls[1][-1] == "requirements-dev.txt"


def test_venv_creation_uses_check_true_and_propagates_failure(tmp_path, monkeypatch):
    # A real setup failure (no python3, network down, broken requirements
    # file) must fail loudly - dispatch_story's caller retries the whole
    # dispatch on the next tick rather than silently proceeding with a
    # worktree that has no working test environment at all.
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (tmp_path / "requirements-dev.txt").write_text("pytest\n")

    import subprocess as real_subprocess

    def failing_run(cmd, **kwargs):
        assert kwargs.get("check") is True
        raise real_subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(bd.subprocess, "run", failing_run)
    try:
        bd._provision_worktree_venv(tmp_path)
        assert False, "expected CalledProcessError to propagate"
    except real_subprocess.CalledProcessError:
        pass


# ---------------------------------------------------------------------------
# failed_node_ids: the single pytest short-summary parser both call sites use
# ---------------------------------------------------------------------------
def test_should_parse_an_error_line_as_a_failing_node_id():
    """pytest reports collection/setup/teardown errors under an ``ERROR`` tag
    in the same short-summary shape; those are failures too."""
    assert (
        bd.failed_node_ids("ERROR tests/a.py::test_x - fixture 'db' not found\n")
        == ["tests/a.py::test_x"]
    )


def test_should_keep_a_node_id_containing_spaces_whole():
    """The node id runs from the tag to the `` - `` reason separator; a
    parametrized id can itself contain spaces."""
    assert (
        bd.failed_node_ids("FAILED tests/a.py::test_x[foo bar] - assert False\n")
        == ["tests/a.py::test_x[foo bar]"]
    )


def test_should_ignore_a_bare_tag_with_no_node_id():
    """A line with nothing after the tag contributes no id rather than
    raising."""
    assert bd.failed_node_ids("FAILED \n") == []
    assert bd.failed_node_ids("ERROR\n") == []


def test_should_return_no_ids_for_empty_input():
    assert bd.failed_node_ids("") == []
    assert bd.failed_node_ids(None) == []
