"""Acceptance: a rework redispatch that carries an acceptance block must not
have its grade narrowed back to the oracle fixture.

dispatch.py arms a FULL-SUITE done-bar for exactly that round (rework_full_suite,
under ``ci_rework or review_feedback`` on a local backend), so re-applying the
FM-A acceptance scoping inside check_story_status grades the same round on a
narrower bar than the one the agent was held to. A round the agent could not
green then lands on tests_passed anyway - fail-open.

The fixture below models that gap directly: the oracle fixture alone is green,
the repo-wide suite is red, and whichever command the gate runs decides the
verdict. A first-pass round (nothing raised its done-bar) must stay scoped to
the oracle - that is the FM-A behavior this change must NOT widen.
"""
import json
import subprocess

from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import server as p

_PLAN = "reworknarrow"
_ORACLE_REL = "tests/acceptance_fixture.py"
_ACCEPTANCE = [{"path": _ORACLE_REL, "source": "def test_x(): pass"}]


def _grade(tmp_path, monkeypatch, extra_story, test_cmd):
    """Run check_story_status over one story; return (result, first subprocess cmd).

    The stub run makes the oracle fixture green and everything else red, so the
    gate's verdict is decided purely by which command it chose to run.
    """
    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", plans)
    monkeypatch.setattr(ppers, "PLAN_DIR", plans)
    monkeypatch.setattr(pcon, "PLAN_DIR", plans)

    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("[step 0] bash: pwd\n")
    (plans / f"{_PLAN}.manifest.json").write_text(json.dumps(
        {"epics": {}, "stories": {"S1": {
            "summary": "thing", "status": "in_progress",
            "pid": 4242, "worktree": str(worktree), **extra_story}}}, indent=2))

    monkeypatch.setattr(p.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, list(test_cmd)))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)
    monkeypatch.setattr(p, "_added_pytest_test_paths", lambda *a, **k: [])

    oracle = str(worktree / _ORACLE_REL)
    seen = []

    def _fake_run(cmd, **kwargs):
        seen.append(cmd)
        scoped_to_oracle = oracle in [str(c) for c in cmd]
        if scoped_to_oracle:
            return subprocess.CompletedProcess(cmd, 0, stdout="1 passed", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="1 failed", stderr="")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    return p.check_story_status(_PLAN, "S1"), (seen[0] if seen else [])


def test_ci_rework_round_is_graded_on_the_full_suite_not_the_oracle(tmp_path, monkeypatch):
    result, cmd = _grade(
        tmp_path, monkeypatch,
        {"backend": "ollama", "ci_rework": True, "acceptance": _ACCEPTANCE},
        ["pytest"],
    )
    assert not any(str(c).endswith(_ORACLE_REL) for c in cmd)
    assert result["status"] == "failed"


def test_review_feedback_round_is_graded_on_the_full_suite_not_the_oracle(tmp_path, monkeypatch):
    result, cmd = _grade(
        tmp_path, monkeypatch,
        {"backend": "ollama", "review_feedback": "the SQL is injectable",
         "acceptance": _ACCEPTANCE},
        ["pytest"],
    )
    assert not any(str(c).endswith(_ORACLE_REL) for c in cmd)
    assert result["status"] == "failed"


def test_first_pass_acceptance_story_stays_scoped_to_the_oracle(tmp_path, monkeypatch):
    """Nothing raised this round's done-bar, so the oracle remains the
    authoritative bar - the fix must not widen the cold-start grade."""
    result, cmd = _grade(
        tmp_path, monkeypatch,
        {"backend": "ollama", "acceptance": _ACCEPTANCE},
        ["pytest", "--ignore=tests"],
    )
    assert any(str(c).endswith(_ORACLE_REL) for c in cmd)
    assert result["status"] == "tests_passed"
