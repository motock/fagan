"""Acceptance oracle: ``mark_done`` requires a MERGED PR (LAG-1).

Live incident: anagram story DSG-1 had new commits, a green suite and an OPEN
PR under REQUEST_CHANGES; triage marked it done and master never got the
change. ``pr_url`` is set as soon as a PR is OPENED, so it corroborates nothing
on its own. ``subprocess.run`` is the external boundary and is always mocked
here - no test ever shells out to a real ``gh``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline import server, triage, triage_story_actions

# The exact fail-closed park reason the brief pins.
_PARK_REASON = "mark_done ruled but live evidence does not corroborate"

# Real-shaped string from pipeline.triage._current_suite_state.
_SUITE_PASSES = "CURRENT STATE: full test suite PASSES at the worktree's current HEAD."

_PR_URL = "https://github.com/o/r/pull/1"
_POLICY = Path(__file__).resolve().parents[2] / "overlord-policy.md"


def _gh(state, returncode=0):
    """A fake ``gh pr view ... --jq .state`` result."""
    return SimpleNamespace(returncode=returncode, stdout=state, stderr="")


@pytest.fixture
def gh(monkeypatch):
    """Mock the ``gh`` boundary; record every argv/kwargs it is called with."""
    calls = []
    box = {"result": _gh("OPEN"), "exc": None}

    def fake_run(argv, **kwargs):
        calls.append({"argv": argv, "kwargs": kwargs})
        if box["exc"] is not None:
            raise box["exc"]
        return box["result"]

    monkeypatch.setattr(subprocess, "run", fake_run)
    return SimpleNamespace(calls=calls, box=box)


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """Isolate the executor: fake live probes, capture notify."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    calls = {"notify": []}
    state = {"new_commits": False, "suite": ""}
    monkeypatch.setattr(
        triage, "_worktree_has_new_commits", lambda *a, **k: state["new_commits"]
    )
    monkeypatch.setattr(triage, "_current_suite_state", lambda *a, **k: state["suite"])
    monkeypatch.setattr(
        triage, "_notify_user", lambda *a, **k: calls["notify"].append((a, k))
    )
    monkeypatch.setattr(server, "_default_branch", lambda: "master")
    if hasattr(triage, "_default_branch"):
        monkeypatch.setattr(triage, "_default_branch", lambda: "master")
    return SimpleNamespace(worktree=str(worktree), calls=calls, state=state)


def _story(harness, **over):
    story = {
        "key": "DSG-1",
        "story_key": "DSG-1",
        "summary": "anagram",
        "status": "parked",
        "parked_reason": "no new commits vs master",
        "worktree": harness.worktree,
    }
    story.update(over)
    return story


def _run(harness, story):
    return triage._execute_mark_done(
        "plan-a",
        story["key"],
        story,
        {"action": "mark_done", "rationale": "stale bookkeeping"},
        {"stories": {story["key"]: story}},
        Path("/tmp/plan-a.manifest.json"),
    )


def _assert_parked(harness, story, result):
    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert story["parked_reason"] == _PARK_REASON
    assert harness.calls["notify"], "an uncorroborated mark_done must notify a human"


# ---------------------------------------------------------------------------
# the executor: only a MERGED PR corroborates
# ---------------------------------------------------------------------------


def test_should_park_when_pr_open_even_with_commits_and_green_suite(harness, gh):
    # DSG-1 shape: new commits, a green suite and an OPEN PR under review.
    harness.state["new_commits"] = True
    harness.state["suite"] = _SUITE_PASSES
    gh.box["result"] = _gh("OPEN")
    story = _story(harness, pr_url=_PR_URL, triage_deferred_action="mark_done")

    _assert_parked(harness, story, _run(harness, story))
    assert story["triage_deferred_action"] == "mark_done"


def test_should_mark_done_when_gh_reports_merged(harness, gh):
    gh.box["result"] = _gh("MERGED")
    story = _story(harness, pr_url=_PR_URL, triage_deferred_action="mark_done")

    result = _run(harness, story)

    assert result == "mark_done"
    assert story["status"] == "done"
    assert "triage_deferred_action" not in story


def test_should_park_when_gh_reports_closed(harness, gh):
    gh.box["result"] = _gh("CLOSED")
    story = _story(harness, pr_url=_PR_URL)

    _assert_parked(harness, story, _run(harness, story))


def test_should_park_when_gh_exits_nonzero(harness, gh):
    gh.box["result"] = _gh("MERGED", returncode=1)
    story = _story(harness, pr_url=_PR_URL)

    _assert_parked(harness, story, _run(harness, story))


def test_should_park_when_gh_times_out(harness, gh):
    gh.box["exc"] = subprocess.TimeoutExpired("gh", 30)
    story = _story(harness, pr_url=_PR_URL)

    _assert_parked(harness, story, _run(harness, story))


def test_should_park_when_gh_missing(harness, gh):
    gh.box["exc"] = FileNotFoundError("gh")
    story = _story(harness, pr_url=_PR_URL)

    _assert_parked(harness, story, _run(harness, story))


@pytest.mark.parametrize("pr_url", [None, "", 123])
def test_should_park_when_pr_url_missing_or_empty(harness, gh, pr_url):
    story = _story(harness, pr_url=pr_url)

    _assert_parked(harness, story, _run(harness, story))
    assert gh.calls == [], "there is nothing to ask gh about without a pr_url"


# ---------------------------------------------------------------------------
# the helper: _pr_is_merged
# ---------------------------------------------------------------------------


def test_pr_is_merged_true_only_for_merged(gh):
    gh.box["result"] = _gh("MERGED")
    assert triage_story_actions._pr_is_merged(_PR_URL) is True
    gh.box["result"] = _gh("MERGED\n")  # stdout is stripped
    assert triage_story_actions._pr_is_merged(_PR_URL) is True


@pytest.mark.parametrize("state", ["OPEN", "CLOSED", "", "DRAFT"])
def test_pr_is_merged_false_for_other_states(gh, state):
    gh.box["result"] = _gh(state)
    assert triage_story_actions._pr_is_merged(_PR_URL) is False


def test_pr_is_merged_false_on_nonzero_exit(gh):
    gh.box["result"] = _gh("MERGED", returncode=1)
    assert triage_story_actions._pr_is_merged(_PR_URL) is False


@pytest.mark.parametrize(
    "exc", [subprocess.TimeoutExpired("gh", 30), FileNotFoundError("gh")]
)
def test_pr_is_merged_false_on_exception(gh, exc):
    gh.box["exc"] = exc
    assert triage_story_actions._pr_is_merged(_PR_URL) is False


def test_pr_is_merged_false_on_empty_url_without_calling_gh(gh):
    assert triage_story_actions._pr_is_merged("") is False
    assert gh.calls == []


def test_pr_is_merged_passes_pr_url_as_an_argv_element(gh):
    gh.box["result"] = _gh("MERGED")

    triage_story_actions._pr_is_merged(_PR_URL)

    assert len(gh.calls) == 1
    call = gh.calls[0]
    assert call["argv"] == [
        "gh",
        "pr",
        "view",
        _PR_URL,
        "--json",
        "state",
        "--jq",
        ".state",
    ]
    assert call["kwargs"].get("capture_output") is True
    assert call["kwargs"].get("text") is True
    assert call["kwargs"].get("timeout") == 30
    assert call["kwargs"].get("check") is False
    assert call["kwargs"].get("shell", False) is False


# ---------------------------------------------------------------------------
# overlord-policy.md: the published rule matches the new predicate
# ---------------------------------------------------------------------------


def _policy_lines() -> list[str]:
    return _POLICY.read_text().splitlines() if _POLICY.is_file() else []


def test_policy_stale_bookkeeping_row_no_longer_lists_the_old_corroborators():
    rows = [ln for ln in _policy_lines() if ln.strip().startswith("| stale bookkeeping")]
    assert len(rows) == 1, "the parked-story matrix must keep its stale-bookkeeping row"
    row = rows[0]
    assert "merged" in row.lower()
    assert "new commits vs base" not in row
    assert "suite green at HEAD" not in row


def test_policy_mark_done_bullet_requires_a_merged_pr():
    bullets = [ln for ln in _policy_lines() if ln.startswith("- `mark_done`")]
    assert len(bullets) == 1, "the Failure triage section must keep its mark_done bullet"
    bullet = bullets[0]
    assert "pr_url" in bullet
    assert "merged" in bullet.lower()
    assert "or the suite passing at HEAD" not in bullet
    assert "fails closed" in bullet
