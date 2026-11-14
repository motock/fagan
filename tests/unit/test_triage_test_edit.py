"""Tests for pipeline.triage_test_edit: the ``approve_test_edit`` executor (ov-5).

Written FIRST (TDD). The executor re-derives the branch diff from git, runs the
mechanical judge on it, reads the ``existing_test_edit`` tier through the real
authority-matrix reader, and only then authorises edits to the named test files
through the ``triage_diagnosis`` channel.

Git is real: each test builds a bare ``origin`` and a clone in ``tmp_path``, so
``origin/<base>`` and ``origin/<branch>`` are fetched exactly as in production.
The authority matrix is a real JSON file named through the environment. Only
notifications are captured, never sent.
"""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest

from pipeline import server, triage
from pipeline import triage_test_edit as te
from pipeline.authority_matrix import ORG_ENV_VAR, TEAM_ENV_VAR

_PLAN = "ov"
_STORY = "ov-5"
_BRANCH = "agent/ov-5"
_BASE = "master"
_APP = "pipeline/app.py"
_TEST = "tests/unit/test_app.py"
_OTHER_TEST = "tests/unit/test_other.py"
_PIN_EVENT = "story_test_edit_approved_by_overlord"

BRIEF = "Rename PLAN_DIR to PLAN_ROOT in pipeline/app.py; the pinning test follows it."
AUTHORISED = "Edits to tests/unit/test_app.py are authorised: they pin PLAN_DIR, which this story removes."

BASE_APP = 'PLAN_DIR = "plans"\n\n\ndef plan_dir():\n    return PLAN_DIR\n'
HEAD_APP = 'PLAN_ROOT = "plans"\n\n\ndef plan_dir():\n    return PLAN_ROOT\n'
BASE_TEST = (
    "from pipeline import app\n\n\n"
    "def test_reads_plan_dir_override(monkeypatch):\n"
    '    monkeypatch.setattr(app, "PLAN_DIR", "/tmp/plans")\n'
    '    assert app.plan_dir() == "/tmp/plans"\n\n\n'
    "def test_default_plan_dir():\n"
    '    assert app.plan_dir() == "plans"\n'
)
RENAMED_TEST = BASE_TEST.replace('app, "PLAN_DIR"', 'app, "PLAN_ROOT"')
PIN_OUTPUT = (
    "FAILED tests/unit/test_app.py::test_reads_plan_dir_override - "
    "AttributeError: <module 'pipeline.app'> has no attribute 'PLAN_DIR'\n"
    "AttributeError: <module 'pipeline.app'> has no attribute 'PLAN_DIR'\n"
)


def _run(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _write(repo: Path, files: dict) -> None:
    for path, content in files.items():
        target = repo / path
        if content is None:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    """A clone whose ``master`` is the base and whose ``agent/ov-5`` is the story branch.

    Returns a function that commits the branch's changes and pushes it. The
    branch starts with the production rename but no test edit, unless the test
    commits one.
    """
    origin = tmp_path / "origin.git"
    _run(tmp_path, "init", "-q", "--bare", str(origin))
    work = tmp_path / "work"
    _run(tmp_path, "init", "-q", "-b", _BASE, str(work))
    _run(work, "config", "user.email", "t@example.com")
    _run(work, "config", "user.name", "t")
    _run(work, "remote", "add", "origin", str(origin))
    _write(work, {_APP: BASE_APP, _TEST: BASE_TEST, _OTHER_TEST: "def test_other():\n    assert True\n"})
    _run(work, "add", "-A")
    _run(work, "commit", "-q", "-m", "base")
    _run(work, "push", "-q", "origin", _BASE)
    _run(work, "remote", "set-head", "origin", _BASE)
    _run(work, "checkout", "-q", "-b", _BRANCH)

    def commit_branch(changes: dict) -> None:
        _write(work, changes)
        _run(work, "add", "-A")
        _run(work, "commit", "-q", "--allow-empty", "-m", "story")
        _run(work, "push", "-q", "origin", _BRANCH)

    return commit_branch, work


@pytest.fixture
def notices(monkeypatch):
    sent: list[dict] = []

    def fake(plan_name, message, **kwargs):
        sent.append({"plan": plan_name, "message": message, **kwargs})

    monkeypatch.setattr(te, "_notify_user", fake)
    monkeypatch.setattr(triage, "_notify_user", fake)
    return sent


@pytest.fixture
def execute_mode(monkeypatch):
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "execute")


def _matrix_file(tmp_path: Path, name: str, tier) -> Path:
    path = tmp_path / name
    path.write_text(
        json.dumps({
            "version": 1,
            "layer": "org",
            "rules": [{
                "id": "existing_test_edit.tier",
                "class": "existing_test_edit",
                "type": "ordered_enum",
                "values": ["human", "auto+notify", "auto"],
                "value": tier,
                "delegation": "locked",
                "bounds": {"loosest": "auto+notify"},
            }],
        }),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def set_tier(tmp_path, monkeypatch):
    """Point the matrix at a real org layer whose ``existing_test_edit`` tier is ``value``."""
    monkeypatch.delenv(TEAM_ENV_VAR, raising=False)

    def apply(value: str) -> None:
        monkeypatch.setenv(ORG_ENV_VAR, str(_matrix_file(tmp_path, "overlord-authority.json", value)))

    apply("auto+notify")
    return apply


def _story(**overrides) -> dict:
    story = {
        "status": "parked",
        "parked_reason": "tests failed after the last rework",
        "branch": _BRANCH,
        "agent_instructions": BRIEF,
        "last_test_check": {"stdout_tail": PIN_OUTPUT},
    }
    story.update(overrides)
    return story


def _execute(story: dict, repo) -> str:
    """Run the executor against ``repo``, the fixture's ``(commit, work)`` pair."""
    manifest = {"plan": _PLAN, "repo_root": str(repo[1]), "stories": {_STORY: story}}
    return te.execute_approve_test_edit(_PLAN, _STORY, story, {"action": "approve_test_edit"}, manifest)


# --- gating: shadow mode and park classes ----------------------------------


def test_shadow_mode_returns_shadow_and_leaves_the_story_untouched(repo, notices, set_tier, monkeypatch):
    monkeypatch.delenv("PIPELINE_TRIAGE_MODE", raising=False)
    story = _story()
    before = copy.deepcopy(story)

    assert _execute(story, repo) == "shadow"
    assert story == before
    assert notices == []


def test_a_park_class_other_than_pin_is_refused(repo, execute_mode, notices, set_tier):
    commit, _ = repo
    commit({_APP: HEAD_APP})
    story = _story(parked_reason="rebase conflict: CONFLICT (content): Merge conflict in pipeline/app.py",
                   last_test_check=None)

    assert _execute(story, repo) == "park_for_human"
    assert story["status"] == "parked"
    assert "refused for park class rebase_conflict" in story["parked_reason"]


def test_a_failure_with_no_recorded_test_output_is_refused(repo, execute_mode, notices, set_tier):
    commit, _ = repo
    commit({_APP: HEAD_APP, _TEST: RENAMED_TEST})
    story = _story(last_test_check=None)

    assert _execute(story, repo) == "park_for_human"
    assert "refused for park class unknown" in story["parked_reason"]


# --- approval: the judge passes and the tier allows it ---------------------


def test_a_judged_rename_with_auto_notify_authorises_the_named_test_file(repo, execute_mode, notices, set_tier):
    commit, _ = repo
    commit({_APP: HEAD_APP, _TEST: RENAMED_TEST})
    story = _story()

    assert _execute(story, repo) == "approve_test_edit"
    assert story["status"] == "todo"
    assert "parked_reason" not in story
    entry = story["triage_diagnosis"][-1]
    assert entry["text"] == AUTHORISED
    assert entry["class"] == "pin_removed_test_failure"


def test_the_auto_tier_also_authorises(repo, execute_mode, notices, set_tier):
    commit, _ = repo
    commit({_APP: HEAD_APP, _TEST: RENAMED_TEST})
    set_tier("auto")
    story = _story()

    assert _execute(story, repo) == "approve_test_edit"
    assert story["status"] == "todo"


def test_an_authorisation_notifies_the_overlord_decision(repo, execute_mode, notices, set_tier):
    commit, _ = repo
    commit({_APP: HEAD_APP, _TEST: RENAMED_TEST})

    _execute(_story(), repo)

    assert [notice["event"] for notice in notices] == [_PIN_EVENT]


def test_the_executor_never_edits_a_test_file(repo, execute_mode, notices, set_tier):
    commit, work = repo
    commit({_APP: HEAD_APP, _TEST: RENAMED_TEST})
    before = (work / _TEST).read_text(encoding="utf-8")

    _execute(_story(), repo)

    assert (work / _TEST).read_text(encoding="utf-8") == before
    assert _run(work, "status", "--porcelain") == ""


# --- the matrix tier -------------------------------------------------------


def test_a_human_tier_parks_even_when_the_judge_passes(repo, execute_mode, notices, set_tier):
    commit, _ = repo
    commit({_APP: HEAD_APP, _TEST: RENAMED_TEST})
    set_tier("human")
    story = _story()

    assert _execute(story, repo) == "park_for_human"
    assert story["status"] == "parked"
    assert "existing_test_edit tier is human" in story["parked_reason"]
    assert "triage_diagnosis" not in story


def test_an_unreadable_matrix_file_is_treated_as_human(repo, execute_mode, notices, set_tier, tmp_path, monkeypatch):
    commit, _ = repo
    commit({_APP: HEAD_APP, _TEST: RENAMED_TEST})
    monkeypatch.setenv(ORG_ENV_VAR, str(tmp_path / "missing.json"))
    story = _story()

    assert _execute(story, repo) == "park_for_human"
    assert "existing_test_edit tier is human" in story["parked_reason"]


def test_a_malformed_matrix_file_is_treated_as_human(repo, execute_mode, notices, set_tier, tmp_path, monkeypatch):
    commit, _ = repo
    commit({_APP: HEAD_APP, _TEST: RENAMED_TEST})
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv(ORG_ENV_VAR, str(broken))
    story = _story()

    assert _execute(story, repo) == "park_for_human"


def test_a_matrix_with_no_rule_for_the_class_is_treated_as_human(repo, execute_mode, notices, set_tier, tmp_path, monkeypatch):
    commit, _ = repo
    commit({_APP: HEAD_APP, _TEST: RENAMED_TEST})
    empty = tmp_path / "empty-authority.json"
    empty.write_text(json.dumps({"version": 1, "layer": "org", "rules": []}), encoding="utf-8")
    monkeypatch.setenv(ORG_ENV_VAR, str(empty))

    assert _execute(_story(), repo) == "park_for_human"


# --- the judge declines: every decline parks with the reasons ---------------


def test_a_removed_assertion_parks_with_the_judge_reason(repo, execute_mode, notices, set_tier):
    commit, _ = repo
    commit({_APP: HEAD_APP, _TEST: BASE_TEST.replace('app, "PLAN_DIR"', 'app, "PLAN_ROOT"').replace(
        '    assert app.plan_dir() == "plans"\n', "    pass\n")})
    story = _story()

    assert _execute(story, repo) == "park_for_human"
    assert "judge verdict fail" in story["parked_reason"]
    assert "test_default_plan_dir: assertions 1 -> 0" in story["parked_reason"]
    assert any("assertions 1 -> 0" in notice["message"] for notice in notices)


def test_an_unrelated_test_edit_parks_though_it_shares_a_token_with_the_removed_lines(repo, execute_mode, notices, set_tier):
    # The removed production lines carry "plans" (from PLAN_DIR = "plans"); an edit
    # to the pinning test that only shares that token is not a response to PLAN_DIR.
    commit, _ = repo
    head_test = RENAMED_TEST.replace(
        '    assert app.plan_dir() == "plans"\n',
        '    assert app.plan_dir() == "plans" or app.plan_dir() is None\n',
    )
    commit({_APP: HEAD_APP, _TEST: head_test})
    story = _story()

    assert _execute(story, repo) == "park_for_human"
    assert "does not reference a removed name" in story["parked_reason"]


def test_a_deleted_test_parks_as_deletion_of_pin(repo, execute_mode, notices, set_tier):
    commit, _ = repo
    head = BASE_TEST.replace('app, "PLAN_DIR"', 'app, "PLAN_ROOT"').replace(
        "\n\n\ndef test_default_plan_dir():\n    assert app.plan_dir() == \"plans\"\n", "\n")
    commit({_APP: HEAD_APP, _TEST: head})
    story = _story()

    assert _execute(story, repo) == "park_for_human"
    assert "judge verdict deletion_of_pin" in story["parked_reason"]
    assert "test_default_plan_dir" in story["parked_reason"]


def test_an_edit_to_a_test_file_the_failure_does_not_name_parks(repo, execute_mode, notices, set_tier):
    commit, _ = repo
    commit({
        _APP: HEAD_APP,
        _TEST: RENAMED_TEST,
        _OTHER_TEST: "def test_other():\n    assert True\n\n\ndef test_extra():\n    assert True\n",
    })
    story = _story()

    assert _execute(story, repo) == "park_for_human"
    assert "outside the named test files" in story["parked_reason"]
    assert _OTHER_TEST in story["parked_reason"]


def test_a_branch_without_a_test_edit_parks_because_there_is_nothing_to_judge(repo, execute_mode, notices, set_tier):
    commit, _ = repo
    commit({_APP: HEAD_APP})
    story = _story()

    assert _execute(story, repo) == "park_for_human"
    assert "no existing test file changed" in story["parked_reason"]


# --- live evidence and named facts -----------------------------------------


def test_a_story_without_a_branch_parks(repo, execute_mode, notices, set_tier):
    story = _story(branch=None)

    assert _execute(story, repo) == "park_for_human"
    assert "no branch is recorded" in story["parked_reason"]


def test_a_branch_missing_on_origin_parks_on_unreadable_evidence(repo, execute_mode, notices, set_tier):
    story = _story(branch="agent/does-not-exist")

    assert _execute(story, repo) == "park_for_human"
    assert "live git evidence could not be read" in story["parked_reason"]


def test_a_pin_with_no_named_test_file_parks(repo, execute_mode, notices, set_tier):
    commit, _ = repo
    commit({_APP: HEAD_APP, _TEST: RENAMED_TEST})
    story = _story(last_test_check={
        "stdout_tail": "AttributeError: <module 'pipeline.app'> has no attribute 'PLAN_DIR'\n",
    })

    assert _execute(story, repo) == "park_for_human"
    assert "not named in the live failure" in story["parked_reason"]


# --- the plan's repository, never the server's -----------------------------


def test_the_judge_reads_the_plan_repo_when_the_server_points_elsewhere(
    repo, execute_mode, notices, set_tier, monkeypatch, tmp_path
):
    commit, _ = repo
    commit({_APP: HEAD_APP, _TEST: RENAMED_TEST})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(server, "REPO_ROOT", elsewhere)
    story = _story()

    assert _execute(story, repo) == "approve_test_edit"
    assert story["status"] == "todo"


def test_the_judge_reads_the_plan_repo_when_the_server_repo_is_an_unrelated_repository(
    repo, execute_mode, notices, set_tier, monkeypatch, tmp_path
):
    commit, _ = repo
    commit({_APP: HEAD_APP, _TEST: RENAMED_TEST})
    other = tmp_path / "other"
    other.mkdir()
    _run(other, "init", "-q", "-b", "main")
    monkeypatch.setattr(server, "REPO_ROOT", other)
    story = _story()

    assert _execute(story, repo) == "approve_test_edit"
    assert story["status"] == "todo"


def _unusable_root(case: str, tmp_path: Path):
    if case == "none":
        return None
    if case == "empty":
        return ""
    if case == "non_string":
        return 7
    if case == "not_a_directory":
        path = tmp_path / "plan.txt"
        path.write_text("x", encoding="utf-8")
        return str(path)
    assert case == "not_a_git_directory"
    path = tmp_path / "plain"
    path.mkdir()
    return str(path)


@pytest.mark.parametrize("case", ["missing", "none", "empty", "non_string", "not_a_directory", "not_a_git_directory"])
def test_a_plan_without_a_usable_repo_root_parks_and_never_falls_back(
    case, repo, execute_mode, notices, set_tier, monkeypatch, tmp_path
):
    commit, work = repo
    commit({_APP: HEAD_APP, _TEST: RENAMED_TEST})
    monkeypatch.setattr(server, "REPO_ROOT", work)
    story = _story()
    manifest = {"plan": _PLAN, "stories": {_STORY: story}}
    if case != "missing":
        manifest["repo_root"] = _unusable_root(case, tmp_path)

    result = te.execute_approve_test_edit(_PLAN, _STORY, story, {"action": "approve_test_edit"}, manifest)

    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert story["parked_reason"].startswith("approve_test_edit declined: ")
    assert "repo_root" in story["parked_reason"]
    assert "triage_diagnosis" not in story
