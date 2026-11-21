"""Baseline, autofix, triage, setup, lint, validate and review commands run
through the command-wrap seam.

Each site asks ``tenant_exec.wrap`` for the argv first and runs what it
returns. The recorder stands in for an operator plugin: it prefixes the argv
with ``WRAPPED`` so a test can tell the wrapped argv from the original. A
refusal (``CommandWrapError``) must stop the command from running at all.
"""

import subprocess
from types import SimpleNamespace

import pytest

from app import backend_claude
from pipeline import build_detect as bd
from pipeline import dispatch_baseline as db
from pipeline import server as p
from pipeline import tenant_exec, triage

DETECTED_CMD = ["pytest", "-q"]


@pytest.fixture
def wrap_calls(monkeypatch):
    calls = []

    def _recorder(argv, *, cwd, kind):
        calls.append((list(argv), cwd, kind))
        return ["WRAPPED", *argv]

    monkeypatch.setattr(tenant_exec, "wrap", _recorder)
    return calls


@pytest.fixture
def refusing_wrap(monkeypatch):
    def _refuse(argv, *, cwd, kind):
        raise tenant_exec.CommandWrapError("refused")

    monkeypatch.setattr(tenant_exec, "wrap", _refuse)


def _record_runs(monkeypatch, returncode=0, stdout="", git_stdout=None):
    """Stub every subprocess.run and record the argv each one was asked to run.

    ``git_stdout`` maps a git subcommand to the stdout it should return; any
    git call not in the map gets an empty stdout."""
    ran = []
    git_stdout = git_stdout or {}

    def _fake_run(cmd, **kwargs):
        ran.append(list(cmd))
        if cmd and cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, stdout=git_stdout.get(cmd[1], ""), stderr="")
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    return ran


# --- dispatch_baseline: unscoped snapshot (kind='test') -------------------


def test_baseline_snapshot_runs_wrapped_test_command_in_test_dir(
    tmp_path, monkeypatch, wrap_calls
):
    test_dir = tmp_path / "suite"
    test_dir.mkdir()
    monkeypatch.setattr(bd, "detect_test_command", lambda wt: (test_dir, list(DETECTED_CMD)))
    ran = _record_runs(monkeypatch)

    snapshot = db._run_baseline_test_snapshot_uncached(tmp_path)

    assert wrap_calls == [(DETECTED_CMD, str(test_dir), "test")]
    assert ran == [["WRAPPED", *DETECTED_CMD]]
    assert snapshot["cmd"] == DETECTED_CMD


def test_baseline_snapshot_refused_wrap_runs_nothing(tmp_path, monkeypatch, refusing_wrap):
    monkeypatch.setattr(bd, "detect_test_command", lambda wt: (tmp_path, list(DETECTED_CMD)))
    ran = _record_runs(monkeypatch)

    with pytest.raises(tenant_exec.CommandWrapError):
        db._run_baseline_test_snapshot_uncached(tmp_path)

    assert ran == []


# --- dispatch_baseline: scoped snapshot (kind='test') ---------------------


def test_scoped_baseline_snapshot_runs_wrapped_scoped_command(
    tmp_path, monkeypatch, wrap_calls
):
    acceptance = "tests/unit/test_fixture.py"
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / acceptance).write_text("def test_x():\n    pass\n")
    story = {
        "test_scope_resolution": {"test_scope": "scoped"},
        "acceptance": [{"path": acceptance, "source": "def test_x():\n    pass\n"}],
    }
    monkeypatch.setattr(bd, "detect_test_command", lambda wt: (tmp_path, list(DETECTED_CMD)))
    monkeypatch.setattr(db, "detect_test_command", lambda wt: (tmp_path, list(DETECTED_CMD)))
    ran = _record_runs(monkeypatch)

    snapshot = db.baseline_snapshot_for(story, db._run_baseline_test_snapshot)(tmp_path)

    assert len(wrap_calls) == 1
    scoped_argv, cwd, kind = wrap_calls[0]
    assert kind == "test"
    assert cwd == str(tmp_path)
    assert str(tmp_path / acceptance) in scoped_argv
    assert ran == [["WRAPPED", *scoped_argv]]
    assert snapshot["cmd"] == scoped_argv


def test_scoped_baseline_refused_wrap_runs_nothing(tmp_path, monkeypatch, refusing_wrap):
    acceptance = "tests/unit/test_fixture.py"
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / acceptance).write_text("def test_x():\n    pass\n")
    story = {
        "test_scope_resolution": {"test_scope": "scoped"},
        "acceptance": [{"path": acceptance, "source": "def test_x():\n    pass\n"}],
    }
    monkeypatch.setattr(bd, "detect_test_command", lambda wt: (tmp_path, list(DETECTED_CMD)))
    monkeypatch.setattr(db, "detect_test_command", lambda wt: (tmp_path, list(DETECTED_CMD)))
    ran = _record_runs(monkeypatch)

    with pytest.raises(tenant_exec.CommandWrapError):
        db.baseline_snapshot_for(story, db._run_baseline_test_snapshot)(tmp_path)

    assert ran == []


# --- review_autofix: reviewer self-fix test re-run (kind='test') ----------


def test_reviewer_autofix_reruns_wrapped_test_command(tmp_path, monkeypatch, wrap_calls):
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (tmp_path, list(DETECTED_CMD)))
    ran = _record_runs(
        monkeypatch,
        stdout="ok",
        git_stdout={"rev-parse": "after\n", "diff": "1\t1\ta.py\n"},
    )

    verdict, _feedback = p._verify_reviewer_auto_fix(
        str(tmp_path), {"risk": "low"}, "VERDICT: APPROVE_WITH_FIX", "before",
    )

    assert wrap_calls == [(DETECTED_CMD, str(tmp_path), "test")]
    assert ran[-1] == ["WRAPPED", *DETECTED_CMD]
    assert verdict == "APPROVE"


# --- triage: current-suite state re-check (kind='test') -------------------


def test_triage_suite_state_runs_wrapped_test_command(
    tmp_path, monkeypatch, wrap_calls
):
    test_dir = tmp_path / "suite"
    test_dir.mkdir()
    monkeypatch.setattr(triage, "detect_test_command", lambda wt: (test_dir, list(DETECTED_CMD)))
    monkeypatch.setattr(triage, "_is_heavy", lambda cmd: False)
    ran = []

    def _fake_run(cmd, **kwargs):
        ran.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    # triage captures subprocess.run at import time under this key, so the
    # stub has to replace that entry rather than subprocess.run itself.
    monkeypatch.setitem(triage.__dict__, "subprocess.run", _fake_run)

    state = triage._current_suite_state(str(tmp_path), {})

    assert wrap_calls == [(DETECTED_CMD, str(test_dir), "test")]
    assert ran == [["WRAPPED", *DETECTED_CMD]]
    assert "PASSES" in state


# --- build_detect: worktree venv provisioning (kind='setup') --------------


def _python_project_with_pins(path):
    (path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (path / "requirements.txt").write_text("pytest\n")
    (path / ".core-pin").mkdir()
    (path / ".core-pin" / "requirements.txt").write_text("httpx\n")


def test_worktree_venv_setup_wraps_all_three_commands_as_setup(
    tmp_path, monkeypatch, wrap_calls
):
    _python_project_with_pins(tmp_path)
    ran = _record_runs(monkeypatch)

    bd._provision_worktree_venv(tmp_path)

    assert [kind for _, _, kind in wrap_calls] == ["setup", "setup", "setup"]
    assert [cwd for _, cwd, _ in wrap_calls] == [str(tmp_path)] * 3
    assert wrap_calls[0][0][:3] == ["python3", "-m", "venv"]
    assert "requirements.txt" in " ".join(wrap_calls[1][0])
    assert "httpx" not in " ".join(wrap_calls[1][0])
    assert str(tmp_path / ".core-pin" / "requirements.txt") in wrap_calls[2][0]
    assert ran == [["WRAPPED", *argv] for argv, _, _ in wrap_calls]


def test_worktree_venv_refused_wrap_propagates_and_runs_no_pip(
    tmp_path, monkeypatch, refusing_wrap
):
    _python_project_with_pins(tmp_path)
    ran = _record_runs(monkeypatch)

    with pytest.raises(tenant_exec.CommandWrapError):
        bd._provision_worktree_venv(tmp_path)

    assert ran == []


# --- build_detect: lint runner (kind='lint') ------------------------------


def test_lint_gate_runs_wrapped_lint_command_in_lint_dir(tmp_path, monkeypatch, wrap_calls):
    lint_cmd = ["ruff", "check", "."]
    monkeypatch.setattr(bd, "detect_lint_command", lambda wt: (tmp_path, list(lint_cmd)))
    ran = _record_runs(monkeypatch)

    result = bd._run_lint_gate_unbound(tmp_path, {})

    assert wrap_calls == [(lint_cmd, str(tmp_path), "lint")]
    assert ran == [["WRAPPED", *lint_cmd]]
    assert result["cmd"] == lint_cmd


# --- build_detect: acceptance fixture collection (kind='validate') --------


def test_acceptance_collection_runs_wrapped_collect_only_in_repo_root(
    tmp_path, monkeypatch, wrap_calls
):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    story = {
        "acceptance": [
            {"path": "tests/unit/test_fixture.py", "source": "def test_x():\n    pass\n"},
        ],
    }
    monkeypatch.setattr(bd.shutil, "which", lambda name: f"/usr/bin/{name}")
    ran = _record_runs(monkeypatch)

    status, _message = bd._pytest_acceptance_fixtures(story, str(repo_root))

    assert status == "clean"
    assert len(wrap_calls) == 1
    argv, cwd, kind = wrap_calls[0]
    assert kind == "validate"
    assert cwd == str(repo_root)
    assert "--collect-only" in argv
    assert ran == [["WRAPPED", *argv]]


# --- app/backend_claude: ClaudeCliDriver.complete (kind='review') ---------


def test_claude_complete_wraps_command_as_review_in_cwd(tmp_path, monkeypatch, wrap_calls):
    ran = _record_runs(monkeypatch, stdout="the review")

    text = backend_claude.ClaudeCliDriver().complete(
        "review this", model="m", cwd=str(tmp_path),
    )

    assert len(wrap_calls) == 1
    argv, cwd, kind = wrap_calls[0]
    assert kind == "review"
    assert cwd == str(tmp_path)
    assert argv[:5] == ["claude", "-p", "review this", "--model", "m"]
    assert ran == [["WRAPPED", *argv]]
    assert text == "the review"


def test_claude_complete_without_cwd_never_wraps(monkeypatch, wrap_calls):
    ran = _record_runs(monkeypatch, stdout="the review")

    backend_claude.ClaudeCliDriver().complete("review this", model="m")

    assert wrap_calls == []
    assert ran[0][0] == "claude"
