"""LD90-W0-08: attribution of review parks and the final-rework escalation notice.

`review_story` emits a "final rework attempt ... escalating to ..." notice when a
plan opts into `final_rework_escalation`, and three `event="story_parked"`
notices (two review-inconclusive parks, one rework-budget park). All four must
carry `story_key` so story_metrics attributes them to the story instead of the
"<uncorrelated>" bucket, and the escalation notice must carry
`event="escalated"` so it is counted as an escalation.

Tests 1-3 are source-level (AST) checks on `pipeline.review_orchestrator`; test 4
is behavioral and drives the inconclusive-park path for a story that has no
correlation_id.
"""

import ast
import inspect
import sys

from pipeline import server as p
from tests.unit._pipeline_mcp_server_test_helpers import (
    _read_manifest,
    _write_manifest,
)

# pipeline.server imports pipeline.review_orchestrator at module load, so the
# module object is already in sys.modules. Importing it directly here instead
# would re-enter that circular import from the other side and fail.
ro = sys.modules["pipeline.review_orchestrator"]


def _notify_user_calls():
    """Every `_notify_user(...)` call in review_orchestrator's source."""
    tree = ast.parse(inspect.getsource(ro))
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_notify = (
            isinstance(func, ast.Name) and func.id == "_notify_user"
        ) or (isinstance(func, ast.Attribute) and func.attr == "_notify_user")
        if is_notify:
            calls.append(node)
    return calls


def _joined_str_texts(call):
    """Concatenated Constant text of every JoinedStr argument of `call`."""
    texts = []
    values = [arg for arg in call.args]
    values += [kw.value for kw in call.keywords if kw.arg is not None]
    for value in values:
        if isinstance(value, ast.JoinedStr):
            texts.append(
                "".join(
                    part.value
                    for part in value.values
                    if isinstance(part, ast.Constant) and isinstance(part.value, str)
                )
            )
    return texts


def _keyword(call, name):
    """The value node of keyword `name` on `call`, or None when absent."""
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def test_final_rework_escalation_notice_is_stamped_escalated():
    matches = [
        call
        for call in _notify_user_calls()
        if any("final rework attempt" in text for text in _joined_str_texts(call))
    ]
    assert len(matches) == 1, "expected exactly one final-rework escalation notice"

    call = matches[0]
    event = _keyword(call, "event")
    assert isinstance(event, ast.Constant), "escalation notice must pass event="
    assert event.value == "escalated"
    assert _keyword(call, "story_key") is not None, (
        "escalation notice must pass story_key= so metrics attribute it"
    )


def test_every_story_parked_notice_carries_story_key():
    parked = [
        call
        for call in _notify_user_calls()
        if isinstance(_keyword(call, "event"), ast.Constant)
        and _keyword(call, "event").value == "story_parked"
    ]
    assert len(parked) >= 3, "expected at least the three known park notices"
    for call in parked:
        assert _keyword(call, "story_key") is not None, (
            "every story_parked notice must pass story_key= for attribution"
        )


def test_pr_open_failure_notice_is_not_an_event():
    matches = [
        call
        for call in _notify_user_calls()
        if any(
            "could not open PR / post review comment" in text
            for text in _joined_str_texts(call)
        )
    ]
    assert len(matches) == 1, "expected exactly one PR-open failure notice"
    assert _keyword(matches[0], "event") is None, (
        "the PR-open failure notice is not an event-bearing notice"
    )


def test_inconclusive_park_without_correlation_id_is_attributed_to_story(
    plan_dir, monkeypatch,
):
    """A story with no correlation_id still parks with story_key in the notice
    kwargs, so story_metrics buckets the park under the story, not
    "<uncorrelated>"."""
    _write_manifest(plan_dir, "rc_empty_park_attr", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: "VERDICT: REQUEST_CHANGES")
    monkeypatch.setattr(p, "_open_pr", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: calls.append((a, k)))

    p.review_story("rc_empty_park_attr", "S1")
    result2 = p.review_story("rc_empty_park_attr", "S1")

    assert result2["status"] == "parked"
    story = _read_manifest(plan_dir, "rc_empty_park_attr")["stories"]["S1"]
    assert "correlation_id" not in story, "fixture must exercise the uncorrelated path"

    parks = [kwargs for _args, kwargs in calls if kwargs.get("event") == "story_parked"]
    assert parks, "expected a story_parked notification"
    for kwargs in parks:
        assert kwargs.get("story_key") == "S1"
