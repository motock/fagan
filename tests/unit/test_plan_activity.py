"""Unit tests for ``pipeline.plan_activity.is_inert`` (SQ-01).

``is_inert`` decides whether the scheduler's advance passes can skip a plan
without losing any work. Its contract is deliberately one-sided: anything it
cannot prove inert must be advanced, so every malformed manifest answers False.
"""

from __future__ import annotations

import pytest

# pipeline.server must be imported before any other pipeline.* module: the
# package has a pre-existing import cycle, so a cold import of a leaf module
# first fails. Same idiom as tests/unit/test_triage_cap_silent_skip.py.
import pipeline.server  # noqa: F401
from pipeline import plan_activity, plan_approval


@pytest.fixture(autouse=True)
def _approved(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat every manifest as approved unless a test says otherwise.

    The approval gate is env-driven in the real module; pinning it keeps these
    tests about story status and pause state only.
    """
    monkeypatch.setattr(plan_approval, "is_activatable", lambda manifest: True)


class _ExplodingManifest(dict):
    """A dict whose reads raise: ``is_inert`` must swallow it and answer False."""

    def get(self, *args, **kwargs):
        raise RuntimeError("manifest access failed")

    def __getitem__(self, key):
        raise RuntimeError("manifest access failed")


def test_empty_stories_is_not_inert() -> None:
    assert plan_activity.is_inert({"stories": {}}) is False


def test_all_stories_done_is_inert() -> None:
    manifest = {"stories": {"a": {"status": "done"}, "b": {"status": "done"}}}
    assert plan_activity.is_inert(manifest) is True


def test_one_parked_story_is_not_inert() -> None:
    manifest = {"stories": {"a": {"status": "done"}, "b": {"status": "parked"}}}
    assert plan_activity.is_inert(manifest) is False


def test_one_pr_open_story_is_not_inert() -> None:
    manifest = {"stories": {"a": {"status": "done"}, "b": {"status": "pr_open"}}}
    assert plan_activity.is_inert(manifest) is False


def test_paused_without_live_agent_is_inert() -> None:
    manifest = {"paused": True, "stories": {"a": {"status": "todo"}}}
    assert plan_activity.is_inert(manifest) is True


def test_paused_with_live_agent_is_not_inert() -> None:
    manifest = {
        "paused": True,
        "stories": {
            "a": {"status": "in_progress"},
            "b": {"status": "in_progress", "pid": 4242},
        },
    }
    assert plan_activity.is_inert(manifest) is False


def test_paused_in_progress_without_pid_key_is_inert() -> None:
    manifest = {"paused": True, "stories": {"a": {"status": "in_progress"}}}
    assert plan_activity.is_inert(manifest) is True


def test_unapproved_plan_is_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plan_approval, "is_activatable", lambda manifest: False)
    manifest = {"stories": {"a": {"status": "todo"}}}
    assert plan_activity.is_inert(manifest) is True


@pytest.mark.parametrize("manifest", [None, "x", []])
def test_non_dict_manifest_is_not_inert(manifest: object) -> None:
    assert plan_activity.is_inert(manifest) is False  # type: ignore[arg-type]


def test_missing_stories_is_not_inert() -> None:
    assert plan_activity.is_inert({"name": "plan"}) is False


def test_stories_as_list_is_not_inert() -> None:
    assert plan_activity.is_inert({"stories": [{"status": "done"}]}) is False


def test_raising_manifest_is_not_inert() -> None:
    assert plan_activity.is_inert(_ExplodingManifest()) is False
