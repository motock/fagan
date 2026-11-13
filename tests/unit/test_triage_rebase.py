"""Tests for pipeline.triage_rebase: the ``resolve_rebase`` executor (ov-3).

Written FIRST (TDD). The executor re-derives the conflict from git, refuses
anything outside its preconditions, and otherwise hands a deterministic task
to the worker through the ``triage_diagnosis`` channel. Git is stubbed at the
module seam ``_observe`` for the executor tests; the merge-tree parser is
exercised against a real repository built in ``tmp_path``.

Every executor test runs in ``PIPELINE_TRIAGE_MODE=execute`` unless it says
otherwise. Notifications are captured and never sent.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from pipeline import triage
from pipeline import triage_rebase as rb
from pipeline import triage_rebrief as tr

_PLAN = "ov"
_STORY = "ov-3"
_BRANCH = "agent/ov-3"
_BASE = "master"
_BASE_SHA = "1234567890abcdef1234567890abcdef12345678"
_OTHER_SHA = "abcdef1234567890abcdef1234567890abcdef12"
_NEW_SHA = "0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f"
_REBASE_REASON = "rebase conflict: CONFLICT (content): Merge conflict in pipeline/x.py"
_TRANSIENT_REASON = "branch head unavailable (got None)"
_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "triage_park_cases"


def _fixture_paths(name: str) -> list[str]:
    """The conflicted paths a real park recorded, read from the fixture corpus."""
    case = json.loads((_FIXTURES / name).read_text(encoding="utf-8"))
    return list(case["expected"]["facts"]["paths"])


@pytest.fixture
def notices(monkeypatch):
    """Capture the executor's notifications; silence the park notifier."""
    sent: list[dict] = []

    def fake(plan_name, message, **kwargs):
        sent.append({"plan": plan_name, "message": message, **kwargs})

    monkeypatch.setattr(rb, "_notify_user", fake)
    monkeypatch.setattr(triage, "_notify_user", lambda *args, **kwargs: None)
    return sent


@pytest.fixture
def execute_mode(monkeypatch):
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "execute")


@pytest.fixture
def live(monkeypatch):
    """Stub the live git evidence: a clean base, one commit ahead, no conflict."""
    state = {"base_sha": _BASE_SHA, "paths": [], "ahead": 1, "error": None, "calls": 0}

    def observe(repo_root, base, branch):
        state["calls"] += 1
        if state["error"] is not None:
            raise state["error"]
        assert base == _BASE and branch == _BRANCH
        return {"base_sha": state["base_sha"], "paths": list(state["paths"]), "ahead": state["ahead"]}

    monkeypatch.setattr(rb, "_observe", observe)
    monkeypatch.setattr(rb, "_repo_root", lambda: "/repo")
    monkeypatch.setattr(rb, "_default_branch", lambda: _BASE)
    return state


@pytest.fixture
def unparks(monkeypatch):
    """Record the unpark write instead of touching the plan store."""
    calls: list[tuple] = []

    def fake_clear(plan_name, story_key, fields, status=None):
        calls.append((plan_name, story_key, list(fields), status))
        return {}

    monkeypatch.setattr(tr, "clear_story_fields", fake_clear)
    return calls


def _story(**extra) -> dict:
    story = {
        "status": "parked",
        "summary": "Resolve a rebase conflict",
        "branch": _BRANCH,
        "files": ["pipeline/x.py"],
        "parked_reason": _REBASE_REASON,
    }
    story.update(extra)
    return story


def _resolve(story: dict) -> str:
    manifest = {"plan": _PLAN, "stories": {_STORY: story}}
    return rb.execute_resolve_rebase(_PLAN, _STORY, story, {"action": "resolve_rebase"}, manifest)


def _task(story: dict) -> str:
    return story["triage_diagnosis"][-1]["text"]


# ---------------------------------------------------------------------------
# Live merge-tree evidence, against a real repository
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _repo_with_branches(tmp_path: Path, base_edit, branch_edit) -> Path:
    """Build base and branch refs that diverge from one shared commit.

    ``base_edit`` and ``branch_edit`` are callables that change the work tree
    on each side. The refs are stored as ``origin/*`` so the parser sees the
    same names the executor uses.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "reg.py").write_text("A = 1\n", encoding="utf-8")
    (repo / "f.txt").write_text("keep\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    shared = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "-q", "-b", "base-side", shared)
    base_edit(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base side")
    _git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")

    _git(repo, "checkout", "-q", "-b", "branch-side", shared)
    branch_edit(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "branch side")
    _git(repo, "update-ref", "refs/remotes/origin/agent/ov-3", "HEAD")
    return repo


def test_merge_tree_paths_reports_a_content_conflict(tmp_path):
    repo = _repo_with_branches(
        tmp_path,
        base_edit=lambda r: (r / "reg.py").write_text("A = 3\n", encoding="utf-8"),
        branch_edit=lambda r: (r / "reg.py").write_text("A = 2\n", encoding="utf-8"),
    )

    assert rb._merge_tree_paths(repo, "origin/master", "origin/agent/ov-3") == ["reg.py"]


def test_merge_tree_paths_reports_a_modify_delete_conflict(tmp_path):
    repo = _repo_with_branches(
        tmp_path,
        base_edit=lambda r: (r / "f.txt").unlink(),
        branch_edit=lambda r: (r / "f.txt").write_text("changed\n", encoding="utf-8"),
    )

    assert rb._merge_tree_paths(repo, "origin/master", "origin/agent/ov-3") == ["f.txt"]


def test_merge_tree_paths_is_empty_when_the_merge_is_clean(tmp_path):
    repo = _repo_with_branches(
        tmp_path,
        base_edit=lambda r: (r / "other.txt").write_text("base\n", encoding="utf-8"),
        branch_edit=lambda r: (r / "new.txt").write_text("new\n", encoding="utf-8"),
    )

    assert rb._merge_tree_paths(repo, "origin/master", "origin/agent/ov-3") == []


def test_merge_tree_paths_raises_on_an_unknown_ref(tmp_path):
    repo = _repo_with_branches(
        tmp_path,
        base_edit=lambda r: (r / "other.txt").write_text("base\n", encoding="utf-8"),
        branch_edit=lambda r: (r / "new.txt").write_text("new\n", encoding="utf-8"),
    )

    with pytest.raises(RuntimeError):
        rb._merge_tree_paths(repo, "origin/missing", "origin/agent/ov-3")


# ---------------------------------------------------------------------------
# Shadow mode and the park-class gate
# ---------------------------------------------------------------------------


def test_shadow_mode_never_dispatches_or_reads_git(monkeypatch, live, notices):
    monkeypatch.delenv("PIPELINE_TRIAGE_MODE", raising=False)
    live["paths"] = ["pipeline/x.py"]
    story = _story()
    before = dict(story)

    assert _resolve(story) == "shadow"
    assert story == before
    assert live["calls"] == 0


def test_a_park_class_other_than_rebase_conflict_is_refused(execute_mode, live, notices):
    live["paths"] = ["pipeline/x.py"]
    story = _story(parked_reason=_TRANSIENT_REASON)

    assert _resolve(story) == "park_for_human"
    assert story["status"] == "parked"
    assert "triage_diagnosis" not in story
    assert live["calls"] == 0


# ---------------------------------------------------------------------------
# Dispatch: a modify/delete conflict and a content conflict name their paths
# ---------------------------------------------------------------------------


def test_modify_delete_conflict_is_handed_to_a_worker_naming_the_path(execute_mode, live, notices):
    paths = _fixture_paths("02_rebase_modify_delete.json")
    live["paths"] = paths
    story = _story(
        parked_reason="rebase conflict: CONFLICT (modify/delete): tests/unit/plan_dir_allowlist.json "
        "deleted in HEAD and modified in aac43ec3a6eb",
    )

    assert _resolve(story) == "resolve_rebase"

    assert story["status"] == "todo"
    assert "parked_reason" not in story
    assert "tests/unit/plan_dir_allowlist.json" in _task(story)
    assert story["triage_diagnosis"][-1]["class"] == "rebase_conflict"


def test_content_conflict_is_handed_to_a_worker_naming_the_path(execute_mode, live, notices):
    paths = _fixture_paths("03_rebase_content.json")
    live["paths"] = paths
    story = _story(
        parked_reason="rebase conflict: Auto-merging tests/unit/test_plan_dir_allowlist.py\n"
        "CONFLICT (content): Merge conflict in tests/unit/test_plan_dir_allowlist.py",
    )

    assert _resolve(story) == "resolve_rebase"

    assert story["status"] == "todo"
    assert "tests/unit/test_plan_dir_allowlist.py" in _task(story)


def test_task_carries_the_fixed_instructions_and_the_branch(execute_mode, live, notices):
    live["paths"] = ["pipeline/x.py"]
    story = _story()

    _resolve(story)

    task = _task(story)
    assert task.startswith(f"Your branch conflicts with {_BASE} in: pipeline/x.py. ")
    assert f"Rebase onto origin/{_BASE}." in task
    assert "accept the deletion" in task
    assert "git rm it" in task
    assert "take " + _BASE + "'s version and re-apply only this story's own additions and removals" in task
    assert f"--force-with-lease to HEAD:refs/heads/{_BRANCH}." in task


def test_task_text_is_byte_identical_for_identical_evidence(execute_mode, live, notices):
    live["paths"] = ["pipeline/x.py", "tests/unit/test_x.py"]
    first, second = _story(), _story()

    _resolve(first)
    _resolve(second)

    assert _task(first) == _task(second)


def test_task_text_does_not_depend_on_the_order_paths_were_observed(execute_mode, live, notices):
    live["paths"] = ["pipeline/b.py", "pipeline/a.py"]
    first = _story(files=["pipeline/a.py", "pipeline/b.py"])
    _resolve(first)
    live["paths"] = ["pipeline/a.py", "pipeline/b.py"]
    second = _story(files=["pipeline/a.py", "pipeline/b.py"])
    _resolve(second)

    assert _task(first) == _task(second)
    assert "in: pipeline/a.py, pipeline/b.py." in _task(first)


def test_dispatch_records_the_attempt_against_the_base_sha(execute_mode, live, notices):
    live["paths"] = ["pipeline/x.py"]
    story = _story()

    _resolve(story)

    assert len(story["rebase_attempts"]) == 1
    attempt = story["rebase_attempts"][0]
    assert attempt["base_sha"] == _BASE_SHA
    assert isinstance(attempt["ts"], str) and attempt["ts"]


def test_dispatch_is_announced_through_a_notice(execute_mode, live, notices):
    live["paths"] = ["pipeline/x.py"]

    _resolve(_story())

    assert [n["event"] for n in notices] == ["story_rebase_dispatched_by_overlord"]


# ---------------------------------------------------------------------------
# No conflict now: unpark, do not dispatch
# ---------------------------------------------------------------------------


def test_no_conflict_unparks_without_dispatching(execute_mode, live, unparks, notices):
    live["paths"] = []
    story = _story()

    assert _resolve(story) == "resolve_rebase"

    assert unparks == [(_PLAN, _STORY, ["parked_reason"], "tests_passed")]
    assert story["status"] == "tests_passed"
    assert "parked_reason" not in story
    assert "triage_diagnosis" not in story
    assert story["rebase_attempts"][0]["base_sha"] == _BASE_SHA


def test_no_conflict_records_one_unpark_notice(execute_mode, live, unparks, notices):
    live["paths"] = []

    _resolve(_story())

    assert [n["event"] for n in notices] == ["story_unparked_by_overlord"]


def test_a_failed_unpark_parks_the_story_for_a_human(execute_mode, live, monkeypatch, notices):
    live["paths"] = []

    def failing_clear(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(tr, "clear_story_fields", failing_clear)
    story = _story()

    assert _resolve(story) == "park_for_human"
    assert story["status"] == "parked"
    assert "failed to unpark" in story["parked_reason"]


# ---------------------------------------------------------------------------
# Preconditions: each refusal parks the story for a human
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/x.yml",
        "package-lock.json",
        "services/api/uv.lock",
        "db/migrations/0002_add_column.sql",
        "certs/server.pem",
        "deploy/id_ed25519",
        "overlord-authority.json",
    ],
)
def test_a_deny_listed_path_is_parked_even_when_the_story_declares_it(path, execute_mode, live, notices):
    live["paths"] = [path]
    story = _story(files=[path])

    assert _resolve(story) == "park_for_human"

    assert story["status"] == "parked"
    assert "triage_diagnosis" not in story
    assert path in story["parked_reason"]


def test_the_workflow_conflict_is_parked_with_a_reason_for_a_human(execute_mode, live, notices):
    live["paths"] = [".github/workflows/x.yml"]
    story = _story(files=[".github/workflows/x.yml"])

    _resolve(story)

    assert story["parked_reason"].startswith("resolve_rebase declined")
    assert "protected path" in story["parked_reason"]


def test_a_path_outside_files_that_is_not_a_test_is_parked(execute_mode, live, notices):
    live["paths"] = ["pipeline/other.py"]
    story = _story(files=["pipeline/x.py"])

    assert _resolve(story) == "park_for_human"
    assert story["status"] == "parked"
    assert "pipeline/other.py" in story["parked_reason"]


def test_a_path_outside_files_that_is_a_test_is_allowed(execute_mode, live, notices):
    live["paths"] = ["tests/unit/test_other.py"]
    story = _story(files=["pipeline/x.py"])

    assert _resolve(story) == "resolve_rebase"
    assert story["status"] == "todo"


def test_a_story_with_no_declared_files_parks_on_any_production_path(execute_mode, live, notices):
    live["paths"] = ["pipeline/x.py"]
    story = _story()
    del story["files"]

    assert _resolve(story) == "park_for_human"


def test_a_second_attempt_on_the_same_base_sha_is_refused(execute_mode, live, notices):
    live["paths"] = ["pipeline/x.py"]
    story = _story(rebase_attempts=[{"base_sha": _BASE_SHA, "ts": "2026-11-13T00:00:00+00:00"}])

    assert _resolve(story) == "park_for_human"
    assert story["status"] == "parked"
    assert len(story["rebase_attempts"]) == 1


def test_an_attempt_on_a_new_base_sha_is_allowed(execute_mode, live, notices):
    live["paths"] = ["pipeline/x.py"]
    story = _story(rebase_attempts=[{"base_sha": _OTHER_SHA, "ts": "2026-11-13T00:00:00+00:00"}])

    assert _resolve(story) == "resolve_rebase"
    assert [a["base_sha"] for a in story["rebase_attempts"]] == [_OTHER_SHA, _BASE_SHA]


def test_the_third_attempt_is_allowed_and_the_fourth_is_refused(execute_mode, live, notices):
    live["paths"] = ["pipeline/x.py"]
    two = [
        {"base_sha": _OTHER_SHA, "ts": "t1"},
        {"base_sha": _NEW_SHA, "ts": "t2"},
    ]
    third = _story(rebase_attempts=list(two))
    assert _resolve(third) == "resolve_rebase"
    assert len(third["rebase_attempts"]) == 3

    fourth = _story(rebase_attempts=[*two, {"base_sha": "2" * 40, "ts": "t3"}])
    assert _resolve(fourth) == "park_for_human"
    assert len(fourth["rebase_attempts"]) == 3


def test_a_malformed_attempt_record_fails_closed(execute_mode, live, notices):
    live["paths"] = ["pipeline/x.py"]
    story = _story(rebase_attempts="not-a-list")

    assert _resolve(story) == "park_for_human"
    assert live["calls"] == 0


def test_five_commits_ahead_is_allowed(execute_mode, live, notices):
    live["paths"] = ["pipeline/x.py"]
    live["ahead"] = 5

    assert _resolve(_story()) == "resolve_rebase"


def test_six_commits_ahead_is_parked(execute_mode, live, notices):
    live["paths"] = ["pipeline/x.py"]
    live["ahead"] = 6
    story = _story()

    assert _resolve(story) == "park_for_human"
    assert "commits ahead" in story["parked_reason"]
    assert "triage_diagnosis" not in story


def test_unreadable_live_git_evidence_parks_the_story(execute_mode, live, notices):
    live["error"] = RuntimeError("fetch failed")
    story = _story()

    assert _resolve(story) == "park_for_human"
    assert story["status"] == "parked"
    assert "could not be read" in story["parked_reason"]


def test_a_story_without_a_branch_is_parked(execute_mode, live, notices):
    live["paths"] = ["pipeline/x.py"]
    story = _story()
    del story["branch"]

    assert _resolve(story) == "park_for_human"
    assert live["calls"] == 0


# ---------------------------------------------------------------------------
# The deny-list constant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        "poetry.lock",
        "web/yarn.lock",
        "db/migrations/0001_init.sql",
        "migrate/versions/abc.py",
        "deploy/tls.key",
        "keys/service.p12",
        "keys/id_rsa",
        "overlord-authority.json",
    ],
)
def test_deny_list_matches_each_protected_category(path):
    assert rb.is_denied_path(path)


@pytest.mark.parametrize(
    "path",
    ["pipeline/triage.py", "README.md", "docs/migrations.md", "pipeline/keyboard.py", "tests/unit/test_x.py"],
)
def test_deny_list_leaves_ordinary_paths_alone(path):
    assert not rb.is_denied_path(path)
