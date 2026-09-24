"""The scope gate's remedy must be runnable by the executor that receives it.

The gate sends an out-of-scope branch back with REQUEST_CHANGES feedback that
tells the executor how to revert. A local-agent executor cannot follow advice
that spells a command its bash tool refuses (``git checkout <base> -- <path>``
is matched by ``destructive_git_op``), and its ``restore_file`` fallback
restores from the LAST COMMIT, which already carries the out-of-scope change.
So the remedy must name only commands the harness allows.

These tests drive the REAL ``pipeline.server.review_story`` entry point against
a real git repo, so the feedback grade covers the wiring and not just the
constant. Fixture shape is cribbed from ``tests/unit/test_review_scope_gate.py``.
"""

import json
import subprocess

import pytest

# Import pipeline.server FIRST: pipeline.review_orchestrator -> server ->
# review_orchestrator is a circular import, so importing the orchestrator
# standalone raises ImportError. Importing the server module first breaks the
# cycle (same idiom as tests/unit/test_review_scope_gate.py).
import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline.local_agent_common import destructive_git_op
from pipeline.scope_gate import SCOPE_GATE_REMEDY


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _init_repo(tmp_path):
    """A repo on branch ``main`` with a base commit, then branch ``feature``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "pipeline").mkdir()
    (repo / "pipeline" / "foo.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    _git(repo, "branch", "-M", "main")
    _git(repo, "checkout", "-b", "feature")
    return repo


def _send_back_or_review(plan_dir, monkeypatch, repo):
    """Run review_story on ``repo`` (story scope: pipeline/foo.py only) with the
    LLM reviewer stubbed; return the review_feedback stored in the manifest."""
    monkeypatch.setattr(p, "_auto_escalation_enabled", lambda: False)
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(
        p, "_run_reviewer", lambda *a, **k: "Add a test.\nVERDICT: REQUEST_CHANGES"
    )
    story = {
        "summary": "Add thing",
        "status": "tests_passed",
        "worktree": str(repo),
        "risk": "low",
        "files": ["pipeline/foo.py"],
    }
    manifest = {"epics": {}, "stories": {"S1": story}}
    (plan_dir / "pe.manifest.json").write_text(json.dumps(manifest))
    p.review_story("pe", "S1")
    saved = json.loads((plan_dir / "pe.manifest.json").read_text())
    return saved["stories"]["S1"]["review_feedback"]


def test_remedy_is_not_refused_by_the_local_agent_guard():
    assert destructive_git_op(SCOPE_GATE_REMEDY) is None


def test_guard_still_refuses_the_old_remedy_command():
    # Guards the fix's direction: the remedy changed, the guard did not relax.
    old = "git checkout origin/master -- app/backend.py"
    assert destructive_git_op(old) == "git checkout -- <paths>"


@pytest.mark.parametrize(
    "needle",
    [
        "git show origin/<default-branch>:<path> > <path>",
        "git rm <path>",
        "commit the revert in the same step",
    ],
)
def test_remedy_prescribes_the_runnable_revert_and_the_commit(needle):
    assert needle in SCOPE_GATE_REMEDY


def test_remedy_warns_that_restore_file_reapplies_the_committed_change():
    assert "restore_file" in SCOPE_GATE_REMEDY
    assert "last commit" in SCOPE_GATE_REMEDY


@pytest.mark.parametrize(
    "blocked", ["git checkout", "git restore", "git reset --hard", "git clean"]
)
def test_remedy_never_spells_a_blocked_git_command(blocked):
    assert blocked not in SCOPE_GATE_REMEDY


def test_sent_back_feedback_carries_a_runnable_remedy(plan_dir, monkeypatch):
    repo = _init_repo(plan_dir.parent)
    (repo / "pipeline" / "foo.py").write_text("x = 2\n")
    (repo / "stray.py").write_text("y = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "out of scope")

    feedback = _send_back_or_review(plan_dir, monkeypatch, repo)

    assert "SCOPE GATE" in feedback
    assert "stray.py" in feedback
    assert SCOPE_GATE_REMEDY in feedback
    assert "git checkout <base>" not in feedback
    assert destructive_git_op(feedback) is None
    assert feedback.rstrip().endswith("VERDICT: REQUEST_CHANGES")


def test_in_scope_branch_feedback_does_not_carry_the_remedy(plan_dir, monkeypatch):
    repo = _init_repo(plan_dir.parent)
    (repo / "pipeline" / "foo.py").write_text("x = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "in scope")

    feedback = _send_back_or_review(plan_dir, monkeypatch, repo)

    assert SCOPE_GATE_REMEDY not in feedback
    assert "SCOPE GATE" not in feedback
