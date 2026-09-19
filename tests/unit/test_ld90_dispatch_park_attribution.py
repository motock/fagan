"""Attribution tests for the ``event="story_parked"`` park notification.

``pipeline/dispatch.py:_dispatch_story_impl`` has exactly one branch that parks
a story: the unresolvable rebase conflict on a resumed worktree.  Its
notification carries ``event="story_parked"`` so the outbox can e-mail it, but
the message text starts with ``"story <key> parked:"`` rather than ``"<key> "``,
so no metric can attribute the park to a story.  This story adds the
``story_key`` kwarg and the conditional ``correlation_id`` spread to that one
call, matching every other stamped notification site.

Coverage:

* source-level -- every ``_notify_user`` call stamped with the constant
  ``event="story_parked"`` also passes a ``story_key`` keyword;
* behavioural -- driving the real park path, the captured notification carries
  ``story_key`` equal to the dispatched key and a non-empty ``correlation_id``
  equal to the manifest story's correlation id.

Only membership and the two attributed values are asserted; the exact kwarg set
or total call count of the notification vocabulary is deliberately not pinned.
"""

import ast
import inspect

import pytest

import pipeline.dispatch as pdisp
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import persona as pper
from pipeline import server as p
from tests.unit.test_dispatch_park_event import (
    _CONFLICT_ERROR,
    _dispatch_resumed,
    _make_origin_and_repo,
    _make_resumed_worktree,
    _push_extra_commit_directly_to_origin,
    _read_story,
)


# ---------- Fixtures (copied from test_dispatch_park_event.py) ----------
@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    d = tmp_path / "agents"
    d.mkdir()
    (d / "overlord.md").write_text(
        '---\nname: "overlord"\nmodel: opus\nmemory: user\n---\n\n'
        "You are the Overlord body text.\n"
    )
    (d / "software-engineer.md").write_text(
        '---\nname: "software-engineer"\nmodel: sonnet\n---\n\nEngineer body.\n'
    )
    (d / "code-reviewer.md").write_text(
        '---\nname: "code-reviewer"\nmodel: sonnet\n---\n\nReviewer body.\n'
    )
    (d / "product-analyst.md").write_text(
        '---\nname: "product-analyst"\nmodel: opus\n---\n\nAnalyst body.\n'
    )
    monkeypatch.setattr(p, "AGENTS_DIR", d)
    monkeypatch.setattr(pper, "AGENTS_DIR", d)
    return d


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


@pytest.fixture
def worktree_root(tmp_path, monkeypatch):
    d = tmp_path / "worktrees"
    d.mkdir()
    monkeypatch.setattr(p, "WORKTREE_ROOT", d)
    return d


def _story_parked_notify_kwarg_names():
    """The keyword names of every ``_notify_user`` call in ``pipeline.dispatch``
    whose ``event`` keyword is the constant ``"story_parked"``."""
    tree = ast.parse(inspect.getsource(pdisp))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "_notify_user":
            continue
        event_value = None
        for kw in node.keywords:
            if kw.arg == "event":
                event_value = kw.value
        if isinstance(event_value, ast.Constant) and event_value.value == "story_parked":
            found.append({kw.arg for kw in node.keywords})
    return found


def test_source_story_parked_notify_carries_story_key():
    calls = _story_parked_notify_kwarg_names()
    assert calls, (
        "expected at least one _notify_user(..., event='story_parked') call in "
        "pipeline.dispatch"
    )
    for kwargs in calls:
        assert "story_key" in kwargs, (
            f"story_parked notification is missing story_key: {kwargs}"
        )


def test_park_notification_attributes_story_key_and_correlation_id(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    origin, repo, branch = _make_origin_and_repo(tmp_path)
    _make_resumed_worktree(tmp_path, worktree_root, repo, branch, "S1")
    _push_extra_commit_directly_to_origin(tmp_path, origin, branch, name="extra0")

    notify_calls = []
    result = _dispatch_resumed(
        plan_dir, worktree_root, agents_dir, monkeypatch,
        "park-attr", "S1", repo, branch,
        rebase_result={"ok": False, "conflict": True, "error": _CONFLICT_ERROR},
        notify_calls=notify_calls)

    assert result["status"] == "parked"
    park = [c for c in notify_calls if c["kwargs"].get("event") == "story_parked"]
    assert len(park) == 1, f"expected one stamped park call, got: {notify_calls}"

    kwargs = park[0]["kwargs"]
    assert kwargs["story_key"] == "S1"
    correlation_id = kwargs["correlation_id"]
    assert correlation_id, "park notification correlation_id must be non-empty"
    assert correlation_id == _read_story(plan_dir, "park-attr", "S1")["correlation_id"]
