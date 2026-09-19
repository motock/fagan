"""Story-park notifications must be e-mail-reachable.

The outbox sink (pipeline/notification_outbox.py:outbox_sink) selects records
using ONLY the structured field ``event["payload"]["event"]`` -- message text is
never matched. So a notification emitted without an ``event=`` kwarg can never
be e-mailed, no matter what it says.

The single most important alert -- "your story parked and needs a human" -- is
emitted by ``check_story_status`` in pipeline/story_status.py on the
no-new-commit rework-budget-exhausted path. These tests drive the REAL function
(not a source-text grep) and assert the ``event="story_parked"`` kwarg lands on
that call and only that call.

Everything here monkeypatches ``pipeline.server``'s namespace: story_status.py
rebinds ``check_story_status`` into the server module's globals at import time,
so the free variables it reads (``_notify_user``, ``_auto_escalation_enabled``,
``_acceptance_tampered``, ...) are looked up on ``pipeline.server``.

The implementation does not exist yet, so this file is expected to be RED
(failing assertions) until it lands.
"""
from pathlib import Path

import pytest

from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import server as p
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _clear_caches,
    _plane_configured,
    _read_manifest,
    _write_manifest,
)

PARK_EVENT = "story_parked"


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Local copy of the shared plan_dir fixture.

    Defined here (rather than imported) so the fixture name is not shadowed by
    the same-named test-function parameter, which ruff's F811 flags as a
    redefinition.
    """
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    # pipeline_persistence and pipeline_concurrency import PLAN_DIR from
    # pipeline_paths at module load and read it as a free var, so the patches
    # must land on their own bindings too.
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


class _NotifySpy:
    """Records every ``_notify_user`` call as (args, kwargs)."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))

    @property
    def events(self):
        return [kw.get("event") for _, kw in self.calls]


def _park_setup(plan_path, monkeypatch, *, rework_attempts=2,
                last_reviewed_sha="abc123", head_sha="abc123"):
    """Reach the park branch of check_story_status.

    Mirrors tests/unit/test_pipeline_mcp_server_nested_lock_and_cloud_gate.py's
    ``_css_setup``: a worktree + manifest, a dead pid (so the test run happens),
    test detection + the new-commits guard mocked, and subprocess.run routed so
    ``git rev-parse HEAD`` returns ``head_sha`` while the test command succeeds.

    With ``last_reviewed_sha == head_sha`` and ``rework_attempts`` at/over
    REWORK_MAX_ATTEMPTS_NO_COMMIT (2), the Mode 27 guard parks the story.
    """
    worktree = plan_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("ok\n")
    story = {
        "summary": "thing",
        "status": "in_progress",
        "pid": 4242,
        "worktree": str(worktree),
        "rework_attempts": rework_attempts,
        "last_reviewed_sha": last_reviewed_sha,
    }
    _write_manifest(plan_path, "plan", {"S1": story})

    monkeypatch.setattr(
        p.os, "kill",
        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()),
    )
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)
    # Keep the park path deterministic: no Claude escalation detour.
    monkeypatch.setattr(p, "_auto_escalation_enabled", lambda: False)

    class Result:
        def __init__(self, stdout="", returncode=0):
            self.stdout = stdout
            self.returncode = returncode

    def run_mock(*args, **kwargs):
        if "rev-parse" in args[0]:
            return Result(stdout=(head_sha or "") + "\n")
        return Result(returncode=0)

    monkeypatch.setattr(p.subprocess, "run", run_mock)
    return worktree


# --------------------------------------------------------------------------
# Positive: the park path stamps event="story_parked"
# --------------------------------------------------------------------------

def test_park_path_notifies_with_story_parked_event(plan_dir, monkeypatch):
    """The park notification carries event="story_parked" so the outbox sink
    can select it and the scheduler can e-mail it."""
    _park_setup(plan_dir, monkeypatch, rework_attempts=2)
    spy = _NotifySpy()
    monkeypatch.setattr(p, "_notify_user", spy)

    result = p.check_story_status("plan", "S1")

    assert result["status"] == "parked"
    assert spy.calls, "the park path must notify the user"
    assert spy.events == [PARK_EVENT], (
        f"expected exactly one notification stamped {PARK_EVENT!r}, "
        f"got {spy.events!r}"
    )


def test_park_notify_kwargs_are_exactly_the_event(plan_dir, monkeypatch):
    """The stamp is a bare keyword literal: the call is still (plan_name,
    message) positionally plus event=... and the story_key attribution (the
    fixture story has no correlation_id, so none is passed)."""
    _park_setup(plan_dir, monkeypatch, rework_attempts=2)
    spy = _NotifySpy()
    monkeypatch.setattr(p, "_notify_user", spy)

    p.check_story_status("plan", "S1")

    args, kwargs = spy.calls[0]
    assert args == (
        "plan",
        (
            "S1 parked: no new commit after 3 rework redispatches - "
            "needs human review."
        ),
    )
    assert kwargs == {"event": PARK_EVENT, "story_key": "S1"}


def test_park_notify_message_text_is_unchanged(plan_dir, monkeypatch):
    """The human-readable message must not be reworded by the stamp."""
    _park_setup(plan_dir, monkeypatch, rework_attempts=2)
    spy = _NotifySpy()
    monkeypatch.setattr(p, "_notify_user", spy)

    p.check_story_status("plan", "S1")

    message = spy.calls[0][0][1]
    assert message == (
        "S1 parked: no new commit after 3 rework redispatches - "
        "needs human review."
    )


# --------------------------------------------------------------------------
# The stamp must not disturb the park itself
# --------------------------------------------------------------------------

def test_park_status_and_reason_are_unchanged(plan_dir, monkeypatch):
    """Stamping the notification must not change the status assignment, the
    parked_reason text, the rework counter, or the returned dict."""
    _park_setup(plan_dir, monkeypatch, rework_attempts=2)
    monkeypatch.setattr(p, "_notify_user", _NotifySpy())

    result = p.check_story_status("plan", "S1")

    assert result == {
        "status": "parked",
        "reason": "no_new_commit_rework_budget_exhausted",
    }
    story = _read_manifest(plan_dir, "plan")["stories"]["S1"]
    assert story["status"] == "parked"
    assert story["parked_reason"] == (
        "no new commit after 3 rework redispatches - "
        "agent keeps parking/crashing without writing code."
    )
    assert story["rework_attempts"] == 3


# --------------------------------------------------------------------------
# Negative: other notifications from the same function are NOT stamped
# --------------------------------------------------------------------------

def test_non_park_notification_does_not_carry_story_parked(plan_dir, monkeypatch):
    """The acceptance-tampered branch notifies from the same function. It is
    not a park, so it must not carry event="story_parked" -- this proves the
    stamp landed on the right call."""
    _park_setup(plan_dir, monkeypatch, rework_attempts=0,
                last_reviewed_sha=None, head_sha="any")
    monkeypatch.setattr(p, "_acceptance_tampered", lambda story, wt: ["t.py"])
    spy = _NotifySpy()
    monkeypatch.setattr(p, "_notify_user", spy)

    result = p.check_story_status("plan", "S1")

    assert result["status"] != "parked"
    assert result["reason"] == "acceptance_tampered"
    assert spy.calls, "expected the acceptance-tampered branch to notify"
    assert PARK_EVENT not in spy.events


def test_changes_requested_path_does_not_carry_story_parked(plan_dir, monkeypatch):
    """The no-new-commit-but-under-the-cap path routes to changes_requested
    (a redispatch, not a park) and must never emit story_parked."""
    _park_setup(plan_dir, monkeypatch, rework_attempts=0,
                last_reviewed_sha="abc123", head_sha="abc123")
    spy = _NotifySpy()
    monkeypatch.setattr(p, "_notify_user", spy)

    result = p.check_story_status("plan", "S1")

    assert result["status"] == "changes_requested"
    assert result["reason"] == "no_new_commit_since_last_review"
    assert PARK_EVENT not in spy.events


def test_escalation_path_does_not_carry_story_parked(plan_dir, monkeypatch):
    """When auto-escalation is enabled the same no-new-commit condition
    escalates to Claude instead of parking. That is not a park, so no
    story_parked notification may be emitted."""
    _park_setup(plan_dir, monkeypatch, rework_attempts=2)
    monkeypatch.setattr(p, "_auto_escalation_enabled", lambda: True)
    escalated = []
    monkeypatch.setattr(
        p, "_escalate_review_to_claude",
        lambda story, key, plan, reason: escalated.append((key, reason)),
    )
    spy = _NotifySpy()
    monkeypatch.setattr(p, "_notify_user", spy)

    result = p.check_story_status("plan", "S1")

    assert result["status"] == "changes_requested"
    assert result["reason"] == "no_new_commit_escalated_to_claude"
    assert escalated == [("S1", "no new commit after 3 rework redispatches")]
    assert PARK_EVENT not in spy.events


# --------------------------------------------------------------------------
# Boundary: a failing notification must not lose the park
# --------------------------------------------------------------------------

def test_notify_failure_does_not_lose_the_park(plan_dir, monkeypatch):
    """The park is durably recorded before the notification is attempted, so a
    notification that blows up cannot leave the story un-parked."""
    _park_setup(plan_dir, monkeypatch, rework_attempts=2)

    def boom(*args, **kwargs):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(p, "_notify_user", boom)

    try:
        result = p.check_story_status("plan", "S1")
    except RuntimeError:
        result = None

    story = _read_manifest(plan_dir, "plan")["stories"]["S1"]
    assert story["status"] == "parked"
    assert story["parked_reason"] == (
        "no new commit after 3 rework redispatches - "
        "agent keeps parking/crashing without writing code."
    )
    if result is not None:
        assert result["status"] == "parked"


# --------------------------------------------------------------------------
# The `# noqa: F821` marker on the edited call must survive
# --------------------------------------------------------------------------

def test_park_notify_call_keeps_noqa_f821_marker():
    """The edited call must keep its noqa F821 comment: `_notify_user` is a
    free variable injected from pipeline.server's globals, so dropping the
    marker makes `ruff check pipeline/story_status.py` fail."""
    source = (Path(p.__file__).parent / "story_status.py").read_text()
    anchor = "parked: no new commit after"
    idx = source.index(anchor)
    window = source[max(0, idx - 400):idx]
    marker = "_notify_user(  # " + "noqa: F821"
    assert marker in window


def test_park_notify_uses_a_bare_string_literal_event():
    """The stamp must be the bare literal ``event="story_parked"`` (matching
    pipeline/advance.py's existing stamped sites), not a shared constant or an
    enum: five stories in this plan stamp five different files in parallel, and
    a shared constant would make them collide on one artifact."""
    source = (Path(p.__file__).parent / "story_status.py").read_text()
    anchor = "parked: no new commit after"
    idx = source.index(anchor)
    window = source[max(0, idx - 400):idx + 300]
    assert 'event="story_parked"' in window
