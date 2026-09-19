"""LD90-W0-02: event/story_key/correlation_id stamps on story_status notices.

`check_story_status` switches a struggling local story to a fallback model or
escalates it after repeated infrastructure failures / step caps, and notifies
the user. Those notices must carry `event=` and `story_key=` (plus the story's
`correlation_id` when it has one) so `story_metrics` counts them and groups
them with the story's `story_merged` record.
"""
import ast
import inspect
import json

# pipeline.story_status imports pipeline.server at module level and
# pipeline.server imports check_story_status back from story_status, so
# pipeline.server must be imported FIRST or collection dies on the cycle.
from pipeline import server as p
from pipeline import story_status as ss
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _make_fake_git_run,
    _write_manifest,
    plan_dir,
)


def _notify_user_calls():
    """Every `_notify_user(plan_name, <f-string>, ...)` call in story_status.

    Returns a list of `(skeleton, kwargs)` pairs: `skeleton` is the static text
    of the f-string message (its Constant parts joined), `kwargs` maps keyword
    name -> value node (the `**{...}` correlation spread is skipped, since its
    `kw.arg` is None).
    """
    tree = ast.parse(inspect.getsource(ss))
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "_notify_user"):
            continue
        if len(node.args) < 2 or not isinstance(node.args[1], ast.JoinedStr):
            continue
        skeleton = "".join(
            part.value for part in node.args[1].values
            if isinstance(part, ast.Constant)
        )
        kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
        calls.append((skeleton, kwargs))
    return calls


def _event(kwargs):
    return getattr(kwargs.get("event"), "value", None)


def test_escalation_notices_are_stamped():
    """Every escalation notice carries event="escalated" and a story_key.

    Membership, not a total: both the infra-failure and the step-cap
    escalation sites must be stamped, so a test that only looked at the first
    match would pass while the other stayed unstamped.
    """
    matches = [
        (skeleton, kwargs) for skeleton, kwargs in _notify_user_calls()
        if "escalating to" in skeleton
        and "no local_model_fallback configured" in skeleton
    ]
    assert len(matches) >= 2
    for skeleton, kwargs in matches:
        assert _event(kwargs) == "escalated", skeleton
        assert "story_key" in kwargs, skeleton


def test_fallback_notices_are_stamped():
    """Every fallback notice carries event="model_fallback" and a story_key."""
    matches = [
        (skeleton, kwargs) for skeleton, kwargs in _notify_user_calls()
        if "switching to fallback model" in skeleton
    ]
    assert len(matches) >= 2
    for skeleton, kwargs in matches:
        assert _event(kwargs) == "model_fallback", skeleton
        assert "story_key" in kwargs, skeleton


def test_park_notice_is_attributed():
    """The park notice keeps its event and gains a story_key attribution."""
    matches = [
        (skeleton, kwargs) for skeleton, kwargs in _notify_user_calls()
        if _event(kwargs) == "story_parked"
    ]
    assert matches
    for skeleton, kwargs in matches:
        assert "story_key" in kwargs, skeleton


def test_first_infra_notice_is_not_an_event():
    """The first-occurrence infra notice is not an escalation: no event kwarg."""
    matches = [
        (skeleton, kwargs) for skeleton, kwargs in _notify_user_calls()
        if "resuming from the last checkpoint" in skeleton
    ]
    assert matches
    for skeleton, kwargs in matches:
        assert "event" not in kwargs, skeleton


def test_infra_failure_escalation_notice_carries_correlation_id(
    request, tmp_path, monkeypatch,
):
    """The escalation notice lands in the story's correlation_id group.

    Mirrors test_check_story_status_infra_failure_streak_escalates_to_claude_at_threshold
    in tests/unit/test_pipeline_mcp_server_rebase_and_heavy_lock.py, with a
    correlation_id on the story so the JSONL record can be attributed.
    """
    plans_dir = request.getfixturevalue("plan_dir")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(
        "[step 5] LLM call failed after trim-retry: Server error '500'\n"
    )
    _write_manifest(plans_dir, "infra5", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "model": "gpt-oss:20b",
               "backend": "local", "dispatched_model": "gpt-oss:20b",
               "correlation_id": "cid-9",
               "infra_failure_streak": p.INFRA_FAILURE_FALLBACK_THRESHOLD - 1,
               "infra_failure_streak_model": "gpt-oss:20b"},
    })
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run tests")))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    result = p.check_story_status("infra5", "S1")

    assert result == {"status": "todo", "reason": "infra_failure_escalated_to_claude", "pid": 4242}
    records = [
        json.loads(line)
        for line in (plans_dir / "infra5.notifications.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert any(
        rec.get("event") == "escalated"
        and rec.get("story_key") == "S1"
        and rec.get("correlation_id") == "cid-9"
        for rec in records
    ), records
