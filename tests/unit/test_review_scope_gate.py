"""Pre-review scope gate in ``review_story`` (PLD90-W3-4).

A story that declares ``files`` must never reach the LLM reviewer while its
branch changes production paths outside that scope: the gate sends the branch
back with the exact offending paths instead.  These tests drive the REAL
``pipeline.server.review_story`` entry point against a real git repo, so the
gate's git adapter (``pipeline.scope_gate.check_branch_scope``) runs for real
in the regression test.

Fixture shape is cribbed from ``tests/unit/test_review_orchestrator_park_event.py``
(PLAN_DIR patched on ``p``/``persistence``/``concurrency``,
``_auto_escalation_enabled`` disabled, ``_notify_user`` recorded).
"""

import json
import subprocess

import pytest

import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import review_orchestrator as ro

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    # pipeline_persistence and pipeline_concurrency import PLAN_DIR from
    # pipeline_paths at module load and read it as a free var, so patches
    # must land on their own bindings too.
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_manifest_with_story(plan_dir, plan_name, story_key, story):
    manifest = {"epics": {}, "stories": {story_key: story}}
    (plan_dir / f"{plan_name}.manifest.json").write_text(json.dumps(manifest))


def _record_notify(monkeypatch):
    """Monkeypatch pipeline.server._notify_user to append (args, kwargs) to a
    list instead of spooling an e-mail. Returns the list."""
    calls = []

    def _fake_notify(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(p, "_notify_user", _fake_notify)
    return calls


def _disable_auto_escalation(monkeypatch):
    monkeypatch.setattr(p, "_auto_escalation_enabled", lambda: False)


def _record_reviewer(monkeypatch, output="The error path is untested.\nVERDICT: REQUEST_CHANGES"):
    """Record every ``_run_reviewer`` call; return a REQUEST_CHANGES verdict so
    the downstream path never tries to open a PR (no ``gh`` in tests)."""
    calls = []

    def _fake_reviewer(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return output

    monkeypatch.setattr(p, "_run_reviewer", _fake_reviewer)
    return calls


def _git(repo, *args):
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )


def _init_repo(tmp_path):
    """A repo on branch ``main`` with a base commit, then branch ``feature``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "pipeline").mkdir()
    (repo / "pipeline" / "foo.py").write_text("x = 1\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_foo.py").write_text("def test_x():\n    assert True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    # Pin the default branch name: _first_review_base probes main/master.
    _git(repo, "branch", "-M", "main")
    _git(repo, "checkout", "-b", "feature")
    return repo


def _commit(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


def _story(plan_dir, *, worktree, **extra):
    story = {
        "summary": "Add thing",
        "status": "tests_passed",
        "worktree": worktree,
        "risk": "low",
    }
    story.update(extra)
    return story


# ---------------------------------------------------------------------------
# The incident reproduction: out-of-scope branch is sent back, not reviewed.
# ---------------------------------------------------------------------------

def test_scope_gate_blocks_out_of_scope_branch(plan_dir, monkeypatch):
    """A branch that edits ``scripts/pipeline-env.sh`` and vendors a new
    top-level ``httpx/`` package while ``files`` names only ``pipeline/foo.py``
    is sent back with the exact offending paths and never LLM-reviewed."""
    _disable_auto_escalation(monkeypatch)
    calls = _record_notify(monkeypatch)
    reviewer_calls = _record_reviewer(monkeypatch)

    repo = _init_repo(plan_dir.parent)
    (repo / "pipeline" / "foo.py").write_text("x = 2\n")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "pipeline-env.sh").write_text("export A=1\n")
    (repo / "httpx").mkdir()
    (repo / "httpx" / "__init__.py").write_text("")
    _commit(repo, "out of scope")

    story = _story(plan_dir, worktree=str(repo), files=["pipeline/foo.py"])
    _write_manifest_with_story(plan_dir, "pe", "S1", story)

    result = p.review_story("pe", "S1")

    assert result["status"] == "changes_requested"
    feedback = json.loads(
        (plan_dir / "pe.manifest.json").read_text()
    )["stories"]["S1"]["review_feedback"]
    assert "scripts/pipeline-env.sh" in feedback
    assert "httpx/" in feedback
    assert reviewer_calls == []
    scope_calls = [c for c in calls if c["kwargs"].get("event") == "scope_gate_failed"]
    assert len(scope_calls) == 1
    assert scope_calls[0]["kwargs"].get("story_key") == "S1"


# ---------------------------------------------------------------------------
# The gate passes in-scope work through to the reviewer.
# ---------------------------------------------------------------------------

def test_scope_gate_passes_in_scope_branch(plan_dir, monkeypatch):
    """Only ``pipeline/foo.py`` and a test file change -> the reviewer runs."""
    _disable_auto_escalation(monkeypatch)
    _record_notify(monkeypatch)
    reviewer_calls = _record_reviewer(monkeypatch)

    repo = _init_repo(plan_dir.parent)
    (repo / "pipeline" / "foo.py").write_text("x = 2\n")
    (repo / "tests" / "test_foo.py").write_text("def test_x():\n    assert 1\n")
    _commit(repo, "in scope")

    story = _story(plan_dir, worktree=str(repo), files=["pipeline/foo.py"])
    _write_manifest_with_story(plan_dir, "pe", "S1", story)

    result = p.review_story("pe", "S1")

    assert result["status"] == "changes_requested"
    assert len(reviewer_calls) == 1


def test_no_files_skips_gate(plan_dir, monkeypatch):
    """A story without ``files`` is never gated: the reviewer runs and
    ``check_branch_scope`` is never invoked."""
    _disable_auto_escalation(monkeypatch)
    _record_notify(monkeypatch)
    reviewer_calls = _record_reviewer(monkeypatch)

    gate_calls = []
    monkeypatch.setattr(
        ro, "check_branch_scope", lambda *a, **k: gate_calls.append(a) or []
    )

    # A real repo, so the ONLY reason the gate is skipped is the absent `files`.
    repo = _init_repo(plan_dir.parent)
    (repo / "pipeline" / "foo.py").write_text("x = 2\n")
    _commit(repo, "no declared scope")

    story = _story(plan_dir, worktree=str(repo))
    _write_manifest_with_story(plan_dir, "pe", "S1", story)

    result = p.review_story("pe", "S1")

    assert result["status"] == "changes_requested"
    assert len(reviewer_calls) == 1
    assert gate_calls == []


def test_missing_worktree_skips_gate(plan_dir, monkeypatch):
    """``files`` set but the worktree does not exist -> gate skipped, reviewer
    runs (the gate must not shell out against a missing directory)."""
    _disable_auto_escalation(monkeypatch)
    _record_notify(monkeypatch)
    reviewer_calls = _record_reviewer(monkeypatch)

    story = _story(
        plan_dir,
        worktree=str(plan_dir / "does-not-exist"),
        files=["pipeline/foo.py"],
    )
    _write_manifest_with_story(plan_dir, "pe", "S1", story)

    result = p.review_story("pe", "S1")

    assert result["status"] == "changes_requested"
    assert len(reviewer_calls) == 1


def test_empty_violations_passes(plan_dir, monkeypatch):
    """``check_branch_scope`` returning ``[]`` -> the reviewer runs."""
    _disable_auto_escalation(monkeypatch)
    _record_notify(monkeypatch)
    reviewer_calls = _record_reviewer(monkeypatch)

    repo = _init_repo(plan_dir.parent)
    (repo / "pipeline" / "foo.py").write_text("x = 2\n")
    _commit(repo, "in scope")

    monkeypatch.setattr(ro, "check_branch_scope", lambda *a, **k: [])

    story = _story(plan_dir, worktree=str(repo), files=["pipeline/foo.py"])
    _write_manifest_with_story(plan_dir, "pe", "S1", story)

    result = p.review_story("pe", "S1")

    assert result["status"] == "changes_requested"
    assert len(reviewer_calls) == 1
