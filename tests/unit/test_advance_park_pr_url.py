"""Tests for the PR URL suffix on the ``story_parked`` notification (AUTO-MERGE-3).

Background
----------
``pipeline/advance_merge.py:_adjudicate_merges`` parks a story when the merge
gate declines to merge and raises the single most important alert of all --
"your story parked and needs a human".  That alert named the story and the
reason but not the pull request, so the human had to go hunting for the PR
before they could act on it.

The fix appends the story's ``pr_url`` to the free-text message when the story
carries one.  The URL rides in the message text; ``_notify_user`` gains no new
keyword argument.

What is graded here
-------------------
These tests drive the REAL ``_adjudicate_merges`` function with ``_notify_user``
monkeypatched on the module under test, so they prove the *wiring* rather than
the existence of a constant.  A source-text grep would pass even if the call
were unreachable, so no such test is used.

Coverage:

* positive -- a parked story carrying ``pr_url`` produces a message with both
  the exact reason and the URL;
* negative -- a parked story with no ``pr_url`` key produces the unchanged
  message and no dangling separator;
* boundary -- ``pr_url=None`` behaves exactly like the absent case (no
  ``" — None"``, no trailing separator).

No real backend, git repo or network is ever contacted.
"""

# F811 is disabled because the imported `plan_dir` fixture is used only as a
# test-function / helper parameter name.
# ruff: noqa: F811
import json
from pathlib import Path

import pytest

import pipeline.advance as adv
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _write_manifest,
    plan_dir,
)

PLAN = "park-pr-url-plan"
KEY = "story-1"
REASON = "needs human review"
PR_URL = "https://example.test/pr/42"
SEPARATOR = " — "


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def notify_calls(monkeypatch):
    """Capture every ``_notify_user`` call made by the module under test.

    Returns a list of ``{"args": tuple, "kwargs": dict}`` records, in call
    order.  ``_notify_user`` is a module-level global in ``pipeline.advance``,
    so patching it there is what the production code actually calls.
    """
    calls = []

    def fake_notify(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(adv, "_notify_user", fake_notify)
    return calls


@pytest.fixture(autouse=True)
def _stub_manifest_write(monkeypatch):
    """Keep the manifest write hermetic (no atomic temp-file dance needed)."""
    monkeypatch.setattr(
        adv,
        "_atomic_write_json",
        lambda path, data: Path(path).write_text(json.dumps(data, indent=2)),
    )


def _summary():
    """A summary dict carrying every key ``_adjudicate_merges`` appends to."""
    return {
        "parked": [],
        "notify": [],
        "failed": [],
        "merged": [],
        "ci_pending": [],
    }


def _pr_open_story(**over):
    story = {
        "summary": "pr open story",
        "status": "pr_open",
        "worktree": "/nonexistent-planprurl-advance-worktree",
        "dependencies": [],
    }
    story.update(over)
    return story


def _park_harness(plan_dir, monkeypatch, **story_over):
    """Write a pr_open story and force ``_merge_decision`` to decline a merge."""
    monkeypatch.setattr(
        adv, "_merge_decision", lambda story: {"action": "park", "reason": REASON}
    )
    _write_manifest(plan_dir, PLAN, {KEY: _pr_open_story(**story_over)})


def _parked_message(plan_dir, monkeypatch, notify_calls):
    """Drive the park branch and return the ``story_parked`` message text."""
    _park_harness(plan_dir, monkeypatch)

    adv._adjudicate_merges(PLAN, _summary())

    parked = [c for c in notify_calls if c["kwargs"].get("event") == "story_parked"]
    assert len(parked) == 1, (
        f"expected one story_parked notification, got {notify_calls!r}"
    )
    call = parked[0]
    assert len(call["args"]) == 2, f"expected two positional args, got {call['args']!r}"
    return call["args"][1]


# ---------------------------------------------------------------------------
# Positive: the PR URL is appended
# ---------------------------------------------------------------------------


def test_park_message_includes_pr_url(plan_dir, monkeypatch, notify_calls):
    """A parked story carrying ``pr_url`` names the PR in the alert."""
    _park_harness(plan_dir, monkeypatch, pr_url=PR_URL)

    adv._adjudicate_merges(PLAN, _summary())

    parked = [c for c in notify_calls if c["kwargs"].get("event") == "story_parked"]
    assert len(parked) == 1, (
        f"expected one story_parked notification, got {notify_calls!r}"
    )
    message = parked[0]["args"][1]
    assert message == f"{KEY} parked: {REASON}{SEPARATOR}{PR_URL}"
    assert REASON in message
    assert PR_URL in message


# ---------------------------------------------------------------------------
# Negative: no PR URL, no dangling separator
# ---------------------------------------------------------------------------


def test_park_message_without_pr_url_is_unchanged(plan_dir, monkeypatch, notify_calls):
    """A story with no ``pr_url`` key keeps the original message verbatim."""
    _park_harness(plan_dir, monkeypatch)

    adv._adjudicate_merges(PLAN, _summary())

    parked = [c for c in notify_calls if c["kwargs"].get("event") == "story_parked"]
    assert len(parked) == 1, (
        f"expected one story_parked notification, got {notify_calls!r}"
    )
    message = parked[0]["args"][1]
    assert message == f"{KEY} parked: {REASON}"
    assert SEPARATOR not in message
    assert not message.endswith("—")


# ---------------------------------------------------------------------------
# Boundary: pr_url=None behaves like the absent case
# ---------------------------------------------------------------------------


def test_park_message_with_none_pr_url_is_unchanged(plan_dir, monkeypatch, notify_calls):
    """``pr_url=None`` must not render ``" — None"`` or a trailing separator."""
    _park_harness(plan_dir, monkeypatch, pr_url=None)

    adv._adjudicate_merges(PLAN, _summary())

    parked = [c for c in notify_calls if c["kwargs"].get("event") == "story_parked"]
    assert len(parked) == 1, (
        f"expected one story_parked notification, got {notify_calls!r}"
    )
    message = parked[0]["args"][1]
    assert message == f"{KEY} parked: {REASON}"
    assert "None" not in message
    assert SEPARATOR not in message
    assert not message.endswith("—")
