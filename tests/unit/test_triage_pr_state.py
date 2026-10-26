"""``_pr_is_merged`` asks the resolved forge, not ``gh`` directly (FORGE-S3).

The PR handle is opaque: the helper passes it to ``Forge.pr_state`` untouched
and treats exactly one return value - the string ``"MERGED"`` - as merged.
``"OPEN"``, ``"CLOSED"``, ``"UNKNOWN"`` (the failed-call path), ``""`` and
``None`` all mean "not merged", so an unreadable PR can never corroborate a
``mark_done`` ruling.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pipeline import triage_story_actions

# Deliberately opaque: nothing outside the adapter may parse it.
_HANDLE = "opaque-handle-123"


@pytest.fixture
def forge(monkeypatch):
    """Replace the forge factory; record every handle ``pr_state`` receives."""
    seen: list = []
    box = {"state": "MERGED"}

    def fake_pr_state(handle):
        seen.append(handle)
        return box["state"]

    fake = SimpleNamespace(pr_state=fake_pr_state)
    monkeypatch.setattr(triage_story_actions, "get_forge", lambda repo_root: fake)
    return SimpleNamespace(seen=seen, box=box)


def test_pr_is_merged_true_only_for_merged(forge):
    forge.box["state"] = "MERGED"

    assert triage_story_actions._pr_is_merged(_HANDLE) is True
    assert forge.seen == [_HANDLE], "the handle must reach pr_state untouched"


@pytest.mark.parametrize(
    "state", ["OPEN", "CLOSED", "UNKNOWN", "", None, "merged", "NOT_MERGED"]
)
def test_pr_is_merged_false_for_open_closed_unknown_and_empty(forge, state):
    forge.box["state"] = state

    assert triage_story_actions._pr_is_merged(_HANDLE) is False
    assert forge.seen == [_HANDLE], "the handle must reach pr_state untouched"
