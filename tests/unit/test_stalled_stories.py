"""Tests for the read-only stalled-story listing (pipeline/stalled_stories.py).

The listing must mirror ``stall_scan._run_stall_scan``'s eligibility rule while
staying pure: no notification, no announce-once state, no writes.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pipeline.stalled_stories import stalled_stories

from pipeline import stall_scan

PLAN = "demo-plan"
ELIGIBLE_AGE = 40


def _iso(epoch: float) -> str:
    """ISO-8601 UTC timestamp that ``stall_scan._parse_timestamp`` reads back."""
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def _story(
    status: str = "todo",
    interrupted_at: float | None = 1000,
    dependencies: list[str] | None = None,
) -> dict:
    """A manifest story, with ``interrupted_at`` as the only age signal."""
    story: dict = {
        "status": status,
        "summary": f"summary of a {status} story",
        "dependencies": list(dependencies or []),
    }
    if interrupted_at is not None:
        story["interrupted_at"] = _iso(interrupted_at)
    return story


def _entries(keys: list[str], status: str = "todo", age_s: int = ELIGIBLE_AGE) -> list:
    return [{"story": key, "status": status, "age_s": age_s} for key in keys]


@pytest.mark.parametrize("status", list(stall_scan._STALL_STATUSES))
def test_eligible_story_over_threshold_is_listed(status):
    manifest = {"stories": {"todo-a": _story(status, interrupted_at=1000)}}

    assert stalled_stories(PLAN, manifest, now=1040, threshold=30) == _entries(
        ["todo-a"], status=status
    )


@pytest.mark.parametrize(
    ("now", "threshold", "expected"),
    [
        # age == threshold is NOT stalled (strictly greater is required)
        (1100, 100, []),
        # one second over the threshold is stalled
        (1101, 100, [{"story": "todo-a", "status": "todo", "age_s": 101}]),
        # age equals the lowered threshold exactly -> still not stalled
        (1100, 99, [{"story": "todo-a", "status": "todo", "age_s": 100}]),
    ],
)
def test_threshold_is_strictly_greater_than(now, threshold, expected):
    manifest = {"stories": {"todo-a": _story(interrupted_at=1000)}}

    assert stalled_stories(PLAN, manifest, now=now, threshold=threshold) == expected


@pytest.mark.parametrize(
    ("parent_status", "expected"),
    [
        ("in_progress", []),
        ("done", [{"story": "child", "status": "todo", "age_s": ELIGIBLE_AGE}]),
    ],
)
def test_dependency_must_be_completed(parent_status, expected):
    manifest = {
        "stories": {
            "parent": _story(parent_status, interrupted_at=1099),
            "child": _story(interrupted_at=1000, dependencies=["parent"]),
        }
    }

    assert stalled_stories(PLAN, manifest, now=1040, threshold=30) == expected


@pytest.mark.parametrize("status", ["done", "in_progress"])
def test_non_stall_status_is_never_listed(status):
    manifest = {"stories": {"todo-a": _story(status, interrupted_at=1000)}}

    assert stalled_stories(PLAN, manifest, now=1040, threshold=30) == []


def test_story_without_age_signal_is_not_listed():
    # No interrupted_at and no journal / agent.log -> _stall_age_seconds is None.
    manifest = {"stories": {"todo-a": _story(interrupted_at=None)}}

    assert stalled_stories(PLAN, manifest, now=1040, threshold=0) == []


@pytest.mark.parametrize(("keys", "expected_keys"), [(("z", "a"), ["a", "z"]), (("a", "z"), ["a", "z"])])
def test_result_is_sorted_by_story_key(keys, expected_keys):
    manifest = {"stories": {key: _story(interrupted_at=1000) for key in keys}}

    assert stalled_stories(PLAN, manifest, now=1040, threshold=30) == _entries(
        expected_keys
    )


@pytest.mark.parametrize(
    "manifest",
    [
        None,
        {},
        {"stories": None},
        {"stories": []},
        {"stories": {"bad": None}},
        {"stories": {"bad": None, "also-bad": None}},
        # a dict story missing the fields _completed_dep_ids reads must not raise
        {"stories": {"bad": {"status": "todo"}}},
        {"stories": {"bad": {"summary": "no status"}}},
    ],
)
def test_malformed_manifest_yields_empty_list_without_raising(manifest):
    assert stalled_stories(PLAN, manifest, now=1040, threshold=30) == []


def test_mixed_manifest_skips_non_dict_story():
    manifest = {
        "stories": {
            "bad": None,
            "todo-a": _story(interrupted_at=1000),
        }
    }

    assert stalled_stories(PLAN, manifest, now=1040, threshold=30) == _entries(
        ["todo-a"]
    )


@pytest.mark.parametrize(
    ("now", "expected_age"),
    [
        (200, 40),
        (260, 100),
    ],
)
def test_announce_once_state_is_never_touched(monkeypatch, now, expected_age):
    monkeypatch.setattr(stall_scan, "_STALL_LAST_EMIT", {"todo-a": 123.0})
    manifest = {"stories": {"todo-a": _story(interrupted_at=160)}}

    assert stalled_stories(PLAN, manifest, now=now, threshold=30) == [
        {"story": "todo-a", "status": "todo", "age_s": expected_age}
    ]
    assert stall_scan._STALL_LAST_EMIT == {"todo-a": 123.0}


def test_called_twice_returns_same_list_and_keeps_state(monkeypatch):
    monkeypatch.setattr(stall_scan, "_STALL_LAST_EMIT", {"demo-plan:todo-a:stalled": 1.0})
    manifest = {"stories": {"todo-a": _story(interrupted_at=1000)}}

    first = stalled_stories(PLAN, manifest, now=1040, threshold=30)
    second = stalled_stories(PLAN, manifest, now=1040, threshold=30)

    assert first == second == _entries(["todo-a"])
    assert stall_scan._STALL_LAST_EMIT == {"demo-plan:todo-a:stalled": 1.0}


def test_no_notifier_and_no_notifying_scan_is_called(monkeypatch):
    def _fail(*args, **kwargs):
        pytest.fail("stalled_stories must not notify")

    monkeypatch.setattr(stall_scan, "_notify_user", _fail)
    monkeypatch.setattr(stall_scan, "run_stall_scan", _fail)
    manifest = {"stories": {"todo-a": _story(interrupted_at=1000)}}

    assert stalled_stories(PLAN, manifest, now=1040, threshold=30) == _entries(
        ["todo-a"]
    )


def test_entries_carry_only_identifiers_and_numbers():
    manifest = {"stories": {"todo-a": _story(interrupted_at=1000)}}

    (entry,) = stalled_stories(PLAN, manifest, now=1040, threshold=30)

    assert set(entry) == {"story", "status", "age_s"}
    assert entry["story"] == "todo-a"
    assert entry["status"] == "todo"
    assert entry["age_s"] == ELIGIBLE_AGE
    assert isinstance(entry["age_s"], int)
