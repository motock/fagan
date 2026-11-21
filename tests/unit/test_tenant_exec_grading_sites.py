"""Grading and merge re-check commands run through the command-wrap seam.

Each site that runs a repository test or build command asks
``tenant_exec.wrap`` for the argv first and runs what it returns. The
recorder below stands in for an operator plugin: it prefixes the argv with
``WRAPPED`` so a test can tell the wrapped argv from the original. A refusal
(``CommandWrapError``) must stop the command from running at all.
"""

import contextlib
import json
import subprocess
from types import SimpleNamespace

import pytest

from pipeline import ci as pci
from pipeline import detached_grade as dg
from pipeline import server as p
from pipeline import tenant_exec

ORIGINAL_TEST_CMD = ["pytest", "-q"]
ORIGINAL_BUILD_CMD = ["make", "build"]


class _Settings:
    """Stands in for the merge-gate settings; only ``value`` is read."""

    def __init__(self, **values):
        self._values = values

    def value(self, key):
        return self._values.get(key)


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


def _fake_run_recorder(monkeypatch, module):
    """Record every subprocess.run argv that is not a git probe."""
    ran = []

    def _fake_run(cmd, **kwargs):
        if cmd[0] != "git":
            ran.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", _fake_run)
    return ran


def _heavy_patches(monkeypatch, module, heavy):
    monkeypatch.setattr(module, "_is_heavy", lambda cmd: heavy)
    monkeypatch.setattr(module, "_heavy_lock", lambda: contextlib.nullcontext())


# --- _reverify_acceptance (pipeline/ci.py, kind='test') -------------------


@pytest.mark.parametrize("heavy", [False, True], ids=["light", "heavy"])
def test_reverify_acceptance_runs_wrapped_test_command(
    monkeypatch, tmp_path, wrap_calls, heavy
):
    _heavy_patches(monkeypatch, pci, heavy)
    monkeypatch.setattr(pci, "detect_test_command", lambda wt: (wt, list(ORIGINAL_TEST_CMD)))
    ran = _fake_run_recorder(monkeypatch, pci)

    pci._reverify_acceptance(
        {"summary": "x"}, str(tmp_path), settings=_Settings(reverify_full_suite=True)
    )

    assert wrap_calls == [(ORIGINAL_TEST_CMD, str(tmp_path), "test")]
    assert ran == [["WRAPPED", *ORIGINAL_TEST_CMD]]


@pytest.mark.parametrize("heavy", [False, True], ids=["light", "heavy"])
def test_reverify_acceptance_does_not_run_command_when_wrap_refuses(
    monkeypatch, tmp_path, refusing_wrap, heavy
):
    _heavy_patches(monkeypatch, pci, heavy)
    monkeypatch.setattr(pci, "detect_test_command", lambda wt: (wt, list(ORIGINAL_TEST_CMD)))
    ran = _fake_run_recorder(monkeypatch, pci)

    with pytest.raises(tenant_exec.CommandWrapError):
        pci._reverify_acceptance(
            {"summary": "x"}, str(tmp_path), settings=_Settings(reverify_full_suite=True)
        )

    assert ORIGINAL_TEST_CMD not in ran


# --- _reverify_build (pipeline/ci.py, kind='build') -----------------------


@pytest.mark.parametrize("heavy", [False, True], ids=["light", "heavy"])
def test_reverify_build_runs_wrapped_build_command(monkeypatch, tmp_path, wrap_calls, heavy):
    build_dir = tmp_path / "svc"
    build_dir.mkdir()
    _heavy_patches(monkeypatch, pci, heavy)
    monkeypatch.setattr(
        pci, "detect_build_command", lambda wt: (build_dir, list(ORIGINAL_BUILD_CMD))
    )
    ran = _fake_run_recorder(monkeypatch, pci)

    pci._reverify_build(str(tmp_path), settings=_Settings(merge_build_gate=True))

    assert wrap_calls == [(ORIGINAL_BUILD_CMD, str(build_dir), "build")]
    assert ran == [["WRAPPED", *ORIGINAL_BUILD_CMD]]


@pytest.mark.parametrize("heavy", [False, True], ids=["light", "heavy"])
def test_reverify_build_does_not_run_command_when_wrap_refuses(
    monkeypatch, tmp_path, refusing_wrap, heavy
):
    build_dir = tmp_path / "svc"
    build_dir.mkdir()
    _heavy_patches(monkeypatch, pci, heavy)
    monkeypatch.setattr(
        pci, "detect_build_command", lambda wt: (build_dir, list(ORIGINAL_BUILD_CMD))
    )
    ran = _fake_run_recorder(monkeypatch, pci)

    with pytest.raises(tenant_exec.CommandWrapError):
        pci._reverify_build(str(tmp_path), settings=_Settings(merge_build_gate=True))

    assert ORIGINAL_BUILD_CMD not in ran


# --- check_story_status test grade (pipeline/story_status.py, kind='test') -


def _grade_story(plan_dir, monkeypatch, *, heavy):
    """Stub the grade's external boundaries the way the scoped-test-scope tests do."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("[step 0] bash: pwd\n")
    story = {"summary": "thing", "status": "in_progress",
             "pid": 4242, "worktree": str(worktree)}
    (plan_dir / "tsc.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": {"S1": story}}, indent=2)
    )
    monkeypatch.setattr(
        p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()),
    )
    monkeypatch.setattr(
        p, "detect_test_command", lambda wt: (wt, list(ORIGINAL_TEST_CMD)),
    )
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p, "detect_lint_command", lambda wt: (wt, ["ruff", "check", "."]))
    _heavy_patches(monkeypatch, p, heavy)
    return worktree


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    return d


@pytest.mark.parametrize("heavy", [False, True], ids=["light", "heavy"])
def test_check_story_status_runs_wrapped_test_command(
    plan_dir, monkeypatch, wrap_calls, heavy
):
    worktree = _grade_story(plan_dir, monkeypatch, heavy=heavy)
    ran = _fake_run_recorder(monkeypatch, p)

    p.check_story_status("tsc", "S1")

    assert (ORIGINAL_TEST_CMD, str(worktree), "test") in wrap_calls
    assert ["WRAPPED", *ORIGINAL_TEST_CMD] in ran


@pytest.mark.parametrize("heavy", [False, True], ids=["light", "heavy"])
def test_check_story_status_does_not_run_command_when_wrap_refuses(
    plan_dir, monkeypatch, refusing_wrap, heavy
):
    _grade_story(plan_dir, monkeypatch, heavy=heavy)
    ran = _fake_run_recorder(monkeypatch, p)

    with pytest.raises(tenant_exec.CommandWrapError):
        p.check_story_status("tsc", "S1")

    assert ORIGINAL_TEST_CMD not in ran


# --- start_detached_grade (pipeline/detached_grade.py, kind='test') -------


def test_start_detached_grade_passes_wrapped_argv_to_grader(monkeypatch, tmp_path, wrap_calls):
    spawned = []

    def _fake_popen(argv, **kwargs):
        spawned.append(argv)
        return SimpleNamespace(pid=1234)

    monkeypatch.setattr(dg.subprocess, "Popen", _fake_popen)

    pid = dg.start_detached_grade(
        list(ORIGINAL_TEST_CMD), str(tmp_path), {}, str(tmp_path / "r.json"), str(tmp_path / "l.log")
    )

    assert pid == 1234
    assert wrap_calls == [(ORIGINAL_TEST_CMD, str(tmp_path), "test")]
    assert json.loads(spawned[0][3]) == ["WRAPPED", *ORIGINAL_TEST_CMD]


def test_start_detached_grade_does_not_spawn_when_wrap_refuses(monkeypatch, tmp_path, refusing_wrap):
    spawned = []
    def _fake_popen(argv, **kwargs):
        spawned.append(argv)
        return SimpleNamespace(pid=0)

    monkeypatch.setattr(dg.subprocess, "Popen", _fake_popen)

    with pytest.raises(tenant_exec.CommandWrapError):
        dg.start_detached_grade(
            list(ORIGINAL_TEST_CMD), str(tmp_path), {}, str(tmp_path / "r.json"), str(tmp_path / "l.log")
        )

    assert spawned == []
