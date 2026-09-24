"""A ``repo_issue`` triage ruling files a follow-up story instead of stalling.

Until now ``repo_issue`` was a deferred action: triage recorded it and parked
the story "for a human" (PRH-2 and CYC-3, both 2026-09-24). Per
overlord-policy.md the overlord never edits the repo; a detected repo issue
becomes a normal pipeline story that goes through TDD, review and CI. The
executor now creates that story (``<key>-repo-issue``) and parks the original
with a reason naming it. The original does NOT depend on the new story, so a
repo fix that never lands cannot deadlock the plan (open question 1 in
docs/plans/OVERLORD_FAILURE_TRIAGE_PLAN.md). Creations count against the
plan's triage-created-story ceiling.

The sweep runs for real; only the overlord ruling, repo-health probe and
evidence collection are stubbed.
"""

import json

import pytest

import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import triage

PLAN_NAME = "rix1"
RULING = {
    "ruling": "the lint baseline on master is red",
    "tier": "hard",
    "risk": "low",
    "rationale": "ruff fails on master at a file the story never touched",
    "notify_user": True,
    "action": "repo_issue",
    "failed_open": False,
}


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


@pytest.fixture(autouse=True)
def _sweep_stubs(monkeypatch):
    monkeypatch.setenv("PIPELINE_AUTO_TRIAGE", "1")
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "full")
    monkeypatch.setattr(triage, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(triage, "classify_repo_health", lambda story, checkout, *a, **k: [])
    monkeypatch.setattr(triage, "collect_triage_evidence", lambda *a, **k: "evidence")
    monkeypatch.setattr(triage, "rule_on_story", lambda plan_name, key, story, evidence: dict(RULING))


def _write(plan_dir, stories, **extra):
    path = plan_dir / f"{PLAN_NAME}.manifest.json"
    path.write_text(json.dumps({"stories": stories, **extra}))
    return path


def _parent(**over):
    story = {
        "summary": "Parent story",
        "status": "parked",
        "parked_reason": "stuck",
        "worktree": "",
        "agent_instructions": "PARENT BRIEF",
        "persona": "software-engineer",
        "risk": "low",
        "backend": "ollama",
        "model": "gpt-oss-20b-high:latest",
        "files": ["pipeline/foo.py"],
        "acceptance": [{"path": "tests/unit/test_foo.py", "source": "def test_x():\n    assert False\n"}],
        "triage_attempts": 0,
        "triage_actions": [],
    }
    story.update(over)
    return story


def _manifest(path):
    return json.loads(path.read_text())


def test_a_repo_issue_ruling_creates_the_follow_up_story(plan_dir):
    path = _write(plan_dir, {"S1": _parent()})

    triage.run_triage_sweep(PLAN_NAME)

    assert "S1-repo-issue" in _manifest(path)["stories"]


def test_the_follow_up_is_a_todo_story_keyed_to_itself(plan_dir):
    path = _write(plan_dir, {"S1": _parent()})

    triage.run_triage_sweep(PLAN_NAME)

    child = _manifest(path)["stories"]["S1-repo-issue"]
    assert child["status"] == "todo"
    assert child["key"] == "S1-repo-issue"


def test_the_follow_up_summary_names_the_parent_and_the_ruling(plan_dir):
    path = _write(plan_dir, {"S1": _parent()})

    triage.run_triage_sweep(PLAN_NAME)

    summary = _manifest(path)["stories"]["S1-repo-issue"]["summary"]
    assert summary == "Repo issue from S1: the lint baseline on master is red"


def test_the_follow_up_brief_carries_the_ruling_and_rationale_not_the_parent_brief(plan_dir):
    path = _write(plan_dir, {"S1": _parent()})

    triage.run_triage_sweep(PLAN_NAME)

    brief = _manifest(path)["stories"]["S1-repo-issue"]["agent_instructions"]
    assert "=== REPO ISSUE FILED BY TRIAGE ===" in brief
    assert "the lint baseline on master is red" in brief
    assert "ruff fails on master at a file the story never touched" in brief
    assert "PARENT BRIEF" not in brief


def test_the_follow_up_copies_routing_but_not_scope_or_oracle(plan_dir):
    path = _write(plan_dir, {"S1": _parent()})

    triage.run_triage_sweep(PLAN_NAME)

    child = _manifest(path)["stories"]["S1-repo-issue"]
    assert (child["persona"], child["risk"], child["backend"]) == ("software-engineer", "low", "ollama")
    assert "acceptance" not in child
    assert "files" not in child
    assert "dependencies" not in child


def test_the_parent_is_parked_naming_the_follow_up(plan_dir):
    path = _write(plan_dir, {"S1": _parent()})

    triage.run_triage_sweep(PLAN_NAME)

    parent = _manifest(path)["stories"]["S1"]
    assert parent["status"] == "parked"
    assert "S1-repo-issue" in parent["parked_reason"]
    assert "not implemented" not in parent["parked_reason"]


def test_the_parent_does_not_depend_on_the_follow_up(plan_dir):
    path = _write(plan_dir, {"S1": _parent()})

    triage.run_triage_sweep(PLAN_NAME)

    assert "S1-repo-issue" not in (_manifest(path)["stories"]["S1"].get("dependencies") or [])


def test_a_filed_repo_issue_counts_against_the_plan_ceiling(plan_dir):
    path = _write(plan_dir, {"S1": _parent()}, triage_created_stories=1)

    triage.run_triage_sweep(PLAN_NAME)

    assert _manifest(path)["triage_created_stories"] == 2


def test_an_exhausted_plan_ceiling_files_nothing(plan_dir):
    path = _write(plan_dir, {"S1": _parent()}, triage_created_stories=triage.TRIAGE_MAX_CREATED_STORIES)

    triage.run_triage_sweep(PLAN_NAME)

    manifest = _manifest(path)
    assert "S1-repo-issue" not in manifest["stories"]
    assert manifest["stories"]["S1"]["parked_reason"] == "plan triage budget exhausted"


def test_an_existing_follow_up_is_never_duplicated_or_overwritten(plan_dir):
    existing = {"key": "S1-repo-issue", "summary": "already filed", "status": "done"}
    path = _write(plan_dir, {"S1": _parent(), "S1-repo-issue": existing})

    triage.run_triage_sweep(PLAN_NAME)

    manifest = _manifest(path)
    assert manifest["stories"]["S1-repo-issue"] == existing
    assert manifest.get("triage_created_stories", 0) == 0


def test_an_existing_follow_up_parks_the_parent_naming_it(plan_dir):
    existing = {"key": "S1-repo-issue", "summary": "already filed", "status": "done"}
    path = _write(plan_dir, {"S1": _parent(), "S1-repo-issue": existing})

    triage.run_triage_sweep(PLAN_NAME)

    assert "S1-repo-issue" in _manifest(path)["stories"]["S1"]["parked_reason"]


def test_an_empty_ruling_line_still_gets_a_readable_summary(plan_dir, monkeypatch):
    monkeypatch.setattr(
        triage, "rule_on_story", lambda plan_name, key, story, evidence: {**RULING, "ruling": ""}
    )
    path = _write(plan_dir, {"S1": _parent()})

    triage.run_triage_sweep(PLAN_NAME)

    summary = _manifest(path)["stories"]["S1-repo-issue"]["summary"]
    assert summary == "Repo issue from S1: environmental failure"


def test_a_long_ruling_line_is_cut_to_120_characters_in_the_summary(plan_dir, monkeypatch):
    long_ruling = "x" * 300
    monkeypatch.setattr(
        triage, "rule_on_story", lambda plan_name, key, story, evidence: {**RULING, "ruling": long_ruling}
    )
    path = _write(plan_dir, {"S1": _parent()})

    triage.run_triage_sweep(PLAN_NAME)

    summary = _manifest(path)["stories"]["S1-repo-issue"]["summary"]
    assert summary == "Repo issue from S1: " + "x" * 120


def test_repo_issue_is_no_longer_a_deferred_action():
    assert "repo_issue" not in triage.DEFERRED_ACTIONS
