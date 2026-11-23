"""A parked story whose pull request was merged on the forge is completed.

Background
----------
A team whose access grant lets the pipeline open pull requests but not merge
them parks the story with ``access refused: <token>`` while its PR stays open
and approved. A person then merges that PR on the forge, and nothing noticed:
the story stayed parked for ever.

``pipeline.external_merge.finish_if_merged_outside`` asks the forge about each
parked story that carries a ``pr_url``, at most once a minute, and finishes the
story exactly as a successful pipeline merge does when the forge says MERGED.

What is mocked
--------------
Only the forge (``get_forge``) and the clock (``_clock``) are faked. The
notification sink is spied on, as the other park tests do, so the recorded
event can be asserted. The worktree/branch cleanup runs through
``subprocess.run``; that is faked so no git command or push reaches a real
repository.
"""

# F811 is disabled because the imported `plan_dir` fixture is used only as a
# test-function / helper parameter name.
# ruff: noqa: F811
import copy
import logging
import subprocess

import pytest

import pipeline.external_merge as em
from pipeline import pr
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _write_manifest,
    plan_dir,
)

PLAN = "external-merge-plan"
KEY = "story-1"
DEPENDANT = "story-2"
PR_URL = "https://example.test/pr/42"
REASON = "access refused: merge"
EVENT = "story_merged_outside_pipeline"
SECRET = "ghp_SECRETVALUE123"


class FakeForge:
    """Answers ``pr_state`` with a fixed state, or raises a fixed error."""

    def __init__(self, state="OPEN", error=None):
        self.state = state
        self.error = error
        self.handles = []

    def pr_state(self, handle):
        self.handles.append(handle)
        if self.error is not None:
            raise self.error
        return self.state


class SpyNotify:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))

    @property
    def events(self):
        return [kw.get("event") for _, kw in self.calls]


@pytest.fixture(autouse=True)
def _isolate_throttle():
    em._last_checked.clear()
    yield
    em._last_checked.clear()


@pytest.fixture
def clock(monkeypatch):
    """A controllable monotonic clock; starts at 1000.0 seconds."""
    now = {"t": 1000.0}
    monkeypatch.setattr(em, "_clock", lambda: now["t"])
    return now


@pytest.fixture
def forge_calls(monkeypatch):
    """Install a forge fake; returns a holder whose ``forge`` the test sets."""
    holder = {"forge": FakeForge(), "resolved": 0}

    def fake_get_forge(repo_root):
        holder["resolved"] += 1
        return holder["forge"]

    monkeypatch.setattr(em, "get_forge", fake_get_forge)
    return holder


@pytest.fixture
def notify(monkeypatch):
    spy = SpyNotify()
    monkeypatch.setattr(em, "_notify_user", spy)
    return spy


@pytest.fixture
def git_calls(monkeypatch):
    """Record the cleanup subprocess calls instead of running git or pushing."""
    calls = []

    def fake_run(argv, *args, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr(pr.subprocess, "run", fake_run)
    return calls


def _parked_story(**over):
    story = {
        "summary": "parked story",
        "status": "parked",
        "parked_reason": REASON,
        "pr_url": PR_URL,
        "worktree": "/nonexistent-external-merge-worktree",
        "dependencies": [],
        "merge_parked_at": "2026-11-20T00:00:00+00:00",
        "merge_park_evidence": {"pr_checks": None},
        "merge_attempts": 2,
    }
    story.update(over)
    return story


def _manifest(plan_dir, stories):
    _write_manifest(plan_dir, PLAN, stories)
    return {"stories": stories, "repo_root": "/nonexistent-external-merge-repo"}


def _summary():
    return {
        "parked": [],
        "notify": [],
        "failed": [],
        "merged": [],
        "ci_pending": [],
    }


def _finish(manifest, key=KEY, summary=None):
    return em.finish_if_merged_outside(
        PLAN, key, manifest["stories"][key], manifest, summary or _summary()
    )


# ---------------------------------------------------------------------------
# Positive: a merged PR finishes the story
# ---------------------------------------------------------------------------


def test_merged_pr_marks_story_done(plan_dir, forge_calls, notify, git_calls):
    forge_calls["forge"] = FakeForge(state="MERGED")
    manifest = _manifest(plan_dir, {KEY: _parked_story()})

    finished = _finish(manifest)

    assert finished is True
    story = manifest["stories"][KEY]
    assert story["status"] == "done"
    assert "parked_reason" not in story
    assert "merge_parked_at" not in story
    assert "merge_attempts" not in story
    assert forge_calls["forge"].handles == [PR_URL]


def test_merged_pr_unblocks_dependant(plan_dir, forge_calls, notify, git_calls):
    forge_calls["forge"] = FakeForge(state="MERGED")
    stories = {
        KEY: _parked_story(),
        DEPENDANT: {
            "summary": "dependant",
            "status": "todo",
            "dependencies": [KEY],
            "worktree": "",
        },
    }
    manifest = _manifest(plan_dir, stories)

    _finish(manifest)

    done = {k for k, s in manifest["stories"].items() if s["status"] == "done"}
    assert KEY in done
    assert all(dep in done for dep in manifest["stories"][DEPENDANT]["dependencies"])


def test_merged_pr_removes_worktree_and_deletes_branch(
    plan_dir, forge_calls, notify, git_calls
):
    forge_calls["forge"] = FakeForge(state="MERGED")
    manifest = _manifest(plan_dir, {KEY: _parked_story()})

    _finish(manifest)

    removed = [c for c in git_calls if c[:3] == ["git", "worktree", "remove"]]
    deleted = [c for c in git_calls if c[:3] == ["git", "branch", "-D"]]
    assert removed and "/nonexistent-external-merge-worktree" in removed[0]
    assert deleted and deleted[0][-1] == f"agent/{KEY}"


def test_merged_pr_records_one_outside_merge_event(
    plan_dir, forge_calls, notify, git_calls
):
    forge_calls["forge"] = FakeForge(state="MERGED")
    manifest = _manifest(plan_dir, {KEY: _parked_story()})

    _finish(manifest)

    assert notify.events == [EVENT]
    args, kwargs = notify.calls[0]
    assert "outside the pipeline" in args[1]
    assert kwargs["story_key"] == KEY
    assert SECRET not in repr(notify.calls)


def test_merged_pr_appends_story_to_summary_merged(
    plan_dir, forge_calls, notify, git_calls
):
    forge_calls["forge"] = FakeForge(state="MERGED")
    manifest = _manifest(plan_dir, {KEY: _parked_story()})
    summary = _summary()

    _finish(manifest, summary=summary)

    assert summary["merged"] == [KEY]


# ---------------------------------------------------------------------------
# Negative: any other PR state leaves the story parked, reason intact
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["OPEN", "CLOSED", "UNKNOWN"])
def test_unmerged_pr_leaves_story_parked(
    plan_dir, forge_calls, notify, git_calls, state
):
    forge_calls["forge"] = FakeForge(state=state)
    stories = {KEY: _parked_story()}
    manifest = _manifest(plan_dir, stories)
    before = copy.deepcopy(manifest["stories"])

    finished = _finish(manifest)

    assert finished is False
    assert manifest["stories"] == before
    assert manifest["stories"][KEY]["parked_reason"] == REASON
    assert notify.calls == []
    assert git_calls == []


def test_forge_error_leaves_story_parked_and_does_not_raise(
    plan_dir, forge_calls, notify, git_calls
):
    forge_calls["forge"] = FakeForge(error=RuntimeError("boom"))
    manifest = _manifest(plan_dir, {KEY: _parked_story()})
    before = copy.deepcopy(manifest["stories"])

    finished = _finish(manifest)

    assert finished is False
    assert manifest["stories"] == before
    assert notify.calls == []


def test_forge_timeout_leaves_story_parked_and_does_not_raise(
    plan_dir, forge_calls, notify, git_calls
):
    forge_calls["forge"] = FakeForge(
        error=subprocess.TimeoutExpired(cmd="gh", timeout=30)
    )
    manifest = _manifest(plan_dir, {KEY: _parked_story()})

    assert _finish(manifest) is False
    assert manifest["stories"][KEY]["status"] == "parked"


def test_forge_error_logs_one_warning_without_credentials(
    plan_dir, forge_calls, notify, git_calls, caplog
):
    forge_calls["forge"] = FakeForge(
        error=RuntimeError(f"https://x-access-token:{SECRET}@github.com failed")
    )
    manifest = _manifest(plan_dir, {KEY: _parked_story()})

    with caplog.at_level(logging.WARNING):
        _finish(manifest)

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert SECRET not in caplog.text


def test_unknown_state_logs_at_most_one_warning(
    plan_dir, forge_calls, notify, git_calls, caplog
):
    forge_calls["forge"] = FakeForge(state="UNKNOWN")
    manifest = _manifest(plan_dir, {KEY: _parked_story()})

    with caplog.at_level(logging.WARNING):
        _finish(manifest)

    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) <= 1


# ---------------------------------------------------------------------------
# Negative: stories that are not parked, or have no PR, are never asked
# ---------------------------------------------------------------------------


def test_parked_story_without_pr_url_makes_no_forge_call(
    plan_dir, forge_calls, notify, git_calls
):
    forge_calls["forge"] = FakeForge(state="MERGED")
    story = _parked_story()
    del story["pr_url"]
    manifest = _manifest(plan_dir, {KEY: story})

    assert _finish(manifest) is False
    assert forge_calls["resolved"] == 0
    assert manifest["stories"][KEY]["status"] == "parked"


def test_parked_story_with_null_pr_url_makes_no_forge_call(
    plan_dir, forge_calls, notify, git_calls
):
    forge_calls["forge"] = FakeForge(state="MERGED")
    manifest = _manifest(plan_dir, {KEY: _parked_story(pr_url=None)})

    assert _finish(manifest) is False
    assert forge_calls["resolved"] == 0


@pytest.mark.parametrize("status", ["pr_open", "done", "failed", "tests_passed", "todo"])
def test_non_parked_story_makes_no_forge_call(
    plan_dir, forge_calls, notify, git_calls, status
):
    forge_calls["forge"] = FakeForge(state="MERGED")
    manifest = _manifest(plan_dir, {KEY: _parked_story(status=status)})
    before = copy.deepcopy(manifest["stories"])

    assert _finish(manifest) is False
    assert forge_calls["resolved"] == 0
    assert manifest["stories"] == before


# ---------------------------------------------------------------------------
# Throttle: at most one forge question per story per 60 seconds
# ---------------------------------------------------------------------------


def test_second_pass_inside_sixty_seconds_makes_no_forge_call(
    plan_dir, forge_calls, notify, git_calls, clock
):
    forge = FakeForge(state="OPEN")
    forge_calls["forge"] = forge
    manifest = _manifest(plan_dir, {KEY: _parked_story()})

    assert _finish(manifest) is False
    clock["t"] += 59.999
    assert _finish(manifest) is False

    assert len(forge.handles) == 1


def test_pass_after_sixty_seconds_asks_the_forge_again(
    plan_dir, forge_calls, notify, git_calls, clock
):
    forge = FakeForge(state="OPEN")
    forge_calls["forge"] = forge
    manifest = _manifest(plan_dir, {KEY: _parked_story()})

    _finish(manifest)
    clock["t"] += 60.0
    _finish(manifest)

    assert len(forge.handles) == 2


def test_throttle_applies_per_story(plan_dir, forge_calls, notify, git_calls, clock):
    forge = FakeForge(state="OPEN")
    forge_calls["forge"] = forge
    manifest = _manifest(
        plan_dir,
        {KEY: _parked_story(), DEPENDANT: _parked_story(pr_url=PR_URL + "/2")},
    )

    _finish(manifest, key=KEY)
    _finish(manifest, key=DEPENDANT)

    assert len(forge.handles) == 2


def test_throttle_is_not_started_by_a_skipped_story(
    plan_dir, forge_calls, notify, git_calls, clock
):
    forge = FakeForge(state="MERGED")
    forge_calls["forge"] = forge
    manifest = _manifest(plan_dir, {KEY: _parked_story(status="pr_open")})

    _finish(manifest)
    manifest["stories"][KEY]["status"] = "parked"
    clock["t"] += 1.0

    assert _finish(manifest) is True
    assert len(forge.handles) == 1
