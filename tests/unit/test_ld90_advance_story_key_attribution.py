"""``story_key`` attribution on advance's park / escalation / fallback notices.

Background
----------
``story_metrics`` groups notification records by ``story_key``.  A notice that
carries only the ``correlation_id`` spread (which contributes nothing for a
story without a ``correlation_id``) lands in the ``"<uncorrelated>"`` bucket,
so the park, the a-posteriori escalation and the local-fallback notices in
``pipeline/advance.py`` could not be attributed to the story that produced
them.

What is graded here
-------------------
* source-level -- every ``_notify_user`` call in ``pipeline/advance.py`` whose
  ``event`` keyword is one of ``"story_parked"``, ``"escalated"`` or
  ``"model_fallback"`` also passes ``story_key``;
* negative -- the ``story_merged`` call keeps its pre-existing ``story_key``;
* behavioural -- driving the real ``_adjudicate_merges`` park path with
  ``_notify_user`` monkeypatched proves the kwarg is wired to the loop's story
  key at runtime, not merely present in the source text.

No real backend, git repo or network is ever contacted.
"""

# ruff: noqa: I001, F811
# Import order below is deliberate, not disorganized: `from pipeline import
# server as p` must run BEFORE `import pipeline.advance` so pipeline.server
# (which transitively imports advance/ci/merge at module load) finishes
# initializing first; isort's alphabetical sort would put pipeline.advance
# ahead of pipeline.server and reintroduce the circular import this ordering
# avoids.  F811 is disabled because the imported `plan_dir` fixture is used
# only as a test-function parameter name.
import ast
import inspect
import json
from pathlib import Path

import pytest

from pipeline import server as p  # noqa: F401

import pipeline.advance as adv
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _read_manifest,
    _write_manifest,
    plan_dir,
)

PLAN = "park-event-plan"
KEY = "S1"

ATTRIBUTED_EVENTS = {"story_parked", "escalated", "model_fallback"}


# ---------------------------------------------------------------------------
# Fixtures / helpers (mirrors tests/unit/test_advance_park_event.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def notify_calls(monkeypatch):
    """Capture every ``_notify_user`` call made by the module under test."""
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
        "worktree": "/nonexistent-plannotify-advance-worktree",
        "dependencies": [],
    }
    story.update(over)
    return story


def _park_harness(plan_dir, monkeypatch, *, reason="review verdict REJECT", **story_over):
    """Write a pr_open story and force ``_merge_decision`` to decline a merge."""
    monkeypatch.setattr(
        adv, "_merge_decision", lambda story: {"action": "park", "reason": reason}
    )
    _write_manifest(plan_dir, PLAN, {KEY: _pr_open_story(**story_over)})
    return reason


def _notify_calls_by_event(notify_calls):
    """Map ``event`` -> list of captured calls, skipping unstamped calls."""
    by_event = {}
    for call in notify_calls:
        event = call["kwargs"].get("event")
        if isinstance(event, str):
            by_event.setdefault(event, []).append(call)
    return by_event


def _notify_user_calls():
    """Every ``_notify_user(...)`` call node in ``pipeline.advance``'s source."""
    tree = ast.parse(inspect.getsource(adv))
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name == "_notify_user":
            calls.append(node)
    return calls


def _keyword_names(node):
    """Keyword names of a call, ignoring the ``**spread`` entry (``arg is None``)."""
    return {k.arg for k in node.keywords if k.arg is not None}


def _event_literal(node):
    """The string literal bound to ``event=``, or ``None`` when absent/dynamic."""
    for k in node.keywords:
        if k.arg == "event" and isinstance(k.value, ast.Constant):
            return k.value.value
    return None


# ---------------------------------------------------------------------------
# Source-level: the three attributed notices pass story_key
# ---------------------------------------------------------------------------


def test_attributed_notices_pass_story_key():
    """Every park/escalation/fallback notice carries ``story_key``."""
    seen = set()
    missing = []
    for node in _notify_user_calls():
        event = _event_literal(node)
        if event not in ATTRIBUTED_EVENTS:
            continue
        seen.add(event)
        if "story_key" not in _keyword_names(node):
            missing.append((event, node.lineno))

    assert seen == ATTRIBUTED_EVENTS, f"events not found in source: {ATTRIBUTED_EVENTS - seen}"
    assert not missing, f"notices missing story_key: {missing}"


# ---------------------------------------------------------------------------
# Negative: the merge notice keeps its pre-existing story_key
# ---------------------------------------------------------------------------


def test_story_merged_notice_keeps_story_key():
    """The ``story_merged`` notice still passes ``story_key`` exactly once."""
    merged = [n for n in _notify_user_calls() if _event_literal(n) == "story_merged"]

    assert merged, "no story_merged _notify_user call found in pipeline.advance"
    for node in merged:
        assert "story_key" in _keyword_names(node)


# ---------------------------------------------------------------------------
# Behavioural: the park notice is attributed to the parked story
# ---------------------------------------------------------------------------


def test_park_notice_is_attributed_to_the_parked_story(plan_dir, monkeypatch, notify_calls):
    """A story without a correlation_id still gets its park attributed."""
    _park_harness(plan_dir, monkeypatch)

    adv._adjudicate_merges(PLAN, _summary())

    manifest = _read_manifest(plan_dir, PLAN)
    parked_keys = [
        key for key, story in manifest["stories"].items() if story["status"] == "parked"
    ]
    assert len(parked_keys) == 1, f"expected one parked story, got {parked_keys!r}"
    parked_key = parked_keys[0]

    parks = _notify_calls_by_event(notify_calls)["story_parked"]
    assert len(parks) == 1, f"expected one park notice, got {notify_calls!r}"
    assert parks[0]["kwargs"]["story_key"] == parked_key
