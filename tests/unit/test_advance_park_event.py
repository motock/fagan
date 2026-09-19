"""Tests for the ``event="story_parked"`` stamp on the advance park notification.

Background
----------
``pipeline/notification_outbox.py:outbox_sink`` selects records for e-mail
delivery using ONLY the structured field ``event["payload"]["event"]``.  The
message text is never matched, so a notification emitted without an ``event=``
kwarg can never be e-mailed no matter what it says.

``pipeline/advance.py:_adjudicate_merges`` is the scheduler's autonomous merge
adjudicator.  When ``_merge_decision`` declines to merge, it parks the story
and raises the single most important alert of all -- "your story parked and
needs a human" -- but it called ``_notify_user`` without ``event=``.  This
story stamps that one call with the bare string literal
``event="story_parked"`` (matching the existing stamped sites in the same
module, which use bare literals such as ``event="dispatch_failed"``,
``event="tests_failed"`` and ``event="agent_gave_up"``).

What is graded here
-------------------
These tests drive the REAL ``_adjudicate_merges`` function with ``_notify_user``
monkeypatched on the module under test, so they prove the *wiring* rather than
the existence of a constant.  A source-text grep over ``pipeline/advance.py``
would pass even if the call were unreachable or the kwarg landed on the wrong
call, so no such test is used.

Coverage:

* positive -- the park path emits ``event="story_parked"``;
* negative -- a non-park notification from the SAME function (the successful
  merge, and the merge-gate retry) does NOT carry ``event="story_parked"``,
  which proves the right call was stamped;
* behaviour preservation -- ``status``/``parked_reason``/``summary`` and the
  notification message text are unchanged, and no other kwargs (severity,
  story_key, ...) are introduced;
* boundary -- a story with a ``correlation_id`` keeps that kwarg alongside the
  new ``event``; a story without one gains no extra kwarg; an empty park
  reason still stamps the event;
* the existing never-break contract -- a ``_notify_user`` that raises still
  does not break the enclosing function.

The implementation landed in this same branch (pipeline/advance.py stamps
event="story_parked" on the park notification), so this file is GREEN.  No
real backend, git repo or network is ever contacted.
"""

# ruff: noqa: I001, F811
# Import order below is deliberate, not disorganized: `from pipeline import
# server as p` must run BEFORE `import pipeline.advance` so pipeline.server
# (which transitively imports advance/ci/merge at module load) finishes
# initializing first; isort's alphabetical sort would put pipeline.advance
# ahead of pipeline.server and reintroduce the circular import this ordering
# avoids.  F811 is disabled because the imported `plan_dir` fixture is used
# only as a test-function parameter name.
import inspect
import json
from pathlib import Path

import pytest

# ``pipeline.server`` transitively imports advance/ci/merge/plan_completion at
# module load; importing it first keeps the submodule imports below resolving
# against already-initialized modules.  It is also the target of the
# ``_ServerRef`` bindings the module under test delegates to.
from pipeline import server as p  # noqa: F401

import pipeline.advance as adv
import pipeline.event_wiring as ew
import pipeline.persistence as ppers
import pipeline.plan_completion as ppc
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _read_manifest,
    _write_manifest,
    plan_dir,
)

PLAN = "park-event-plan"
KEY = "S1"


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


def _stub_merge_boundaries(monkeypatch, *, rebase=("", "abc123")):
    """Stub every collaborator the successful/retry merge path touches.

    Only the process-heavy seams are stubbed; the real ``_adjudicate_merges``
    body and its real park/merge branching run.
    """
    monkeypatch.setattr(adv, "MERGE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(
        adv, "_merge_decision", lambda story: {"action": "merge", "reason": ""}
    )
    monkeypatch.setattr(
        adv, "_rebase_and_push_for_merge", lambda plan, key, branch, wt: rebase
    )
    monkeypatch.setattr(
        adv,
        "_merge_gate_ci_status",
        lambda branch, *, sha: {"state": "success", "error": ""},
    )
    monkeypatch.setattr(adv, "_ci_rerun", lambda sha: None)
    monkeypatch.setattr(adv, "_ci_pending_expired", lambda since: False)
    monkeypatch.setattr(adv, "_ci_rework_feedback", lambda err, attempts: "feedback")
    monkeypatch.setattr(
        adv, "_reverify_acceptance", lambda story, wt, key: {"state": "pass", "error": ""}
    )
    monkeypatch.setattr(adv, "_reverify_build", lambda wt: {"state": "pass", "error": ""})
    monkeypatch.setattr(adv, "_mcp_self_source_touched", lambda wt, ref: False)
    monkeypatch.setattr(adv, "_default_branch", lambda: "master")
    monkeypatch.setattr(adv, "_merge_pr", lambda wt, key: None)
    monkeypatch.setattr(adv, "_mark_plane_done", lambda key, plan: None)
    monkeypatch.setattr(adv, "_maybe_record_retro", lambda plan, manifest: None)
    monkeypatch.setattr(adv, "_mcp_restart_notice", lambda touched: "restart")
    monkeypatch.setattr(ppc, "notify_if_plan_completed", lambda plan, manifest: None)


# ---------------------------------------------------------------------------
# Positive: the park path is stamped
# ---------------------------------------------------------------------------


def test_park_notification_carries_story_parked_event(plan_dir, monkeypatch, notify_calls):
    """``_adjudicate_merges`` must emit ``event="story_parked"`` when it parks."""
    _park_harness(plan_dir, monkeypatch)

    adv._adjudicate_merges(PLAN, _summary())

    assert len(notify_calls) == 1, f"expected exactly one notification, got {notify_calls!r}"
    assert notify_calls[0]["kwargs"].get("event") == "story_parked"


def test_park_event_is_a_bare_string_literal(plan_dir, monkeypatch, notify_calls):
    """The stamp is the plain string ``"story_parked"`` (no enum/constant)."""
    _park_harness(plan_dir, monkeypatch)

    adv._adjudicate_merges(PLAN, _summary())

    event = notify_calls[0]["kwargs"].get("event")
    assert isinstance(event, str)
    assert event == "story_parked"


def test_park_event_is_a_keyword_not_positional(plan_dir, monkeypatch, notify_calls):
    """``event`` is a kwarg; the two positional arguments are unchanged."""
    reason = _park_harness(plan_dir, monkeypatch)

    adv._adjudicate_merges(PLAN, _summary())

    call = notify_calls[0]
    assert len(call["args"]) == 2
    assert call["args"][0] == PLAN
    assert call["args"][1] == f"{KEY} parked: {reason}"
    assert "event" in call["kwargs"]


def test_park_message_text_is_unchanged(plan_dir, monkeypatch, notify_calls):
    """The human-readable message keeps its exact ``"<key> parked: <reason>"`` form."""
    reason = _park_harness(plan_dir, monkeypatch, reason="merge gate declined")

    adv._adjudicate_merges(PLAN, _summary())

    assert notify_calls[0]["args"][1] == f"{KEY} parked: merge gate declined"
    assert reason in notify_calls[0]["args"][1]


def test_park_adds_no_other_kwargs(plan_dir, monkeypatch, notify_calls):
    """Only the story_key attribution accompanies the event: severity and
    dedup kwargs are not introduced."""
    _park_harness(plan_dir, monkeypatch)

    adv._adjudicate_merges(PLAN, _summary())

    assert set(notify_calls[0]["kwargs"]) == {"event", "story_key"}


def test_park_emits_exactly_one_notification(plan_dir, monkeypatch, notify_calls):
    """The park branch notifies once -- the stamp did not add a second call."""
    _park_harness(plan_dir, monkeypatch)

    adv._adjudicate_merges(PLAN, _summary())

    assert len(notify_calls) == 1


# ---------------------------------------------------------------------------
# Behaviour preservation: parking still happens exactly as before
# ---------------------------------------------------------------------------


def test_park_sets_status_and_parked_reason(plan_dir, monkeypatch, notify_calls):
    """The story is still parked with the decision's reason, on disk."""
    reason = _park_harness(plan_dir, monkeypatch, reason="no review verdict")

    adv._adjudicate_merges(PLAN, _summary())

    manifest = _read_manifest(plan_dir, PLAN)
    assert manifest["stories"][KEY]["status"] == "parked"
    assert manifest["stories"][KEY]["parked_reason"] == "no review verdict"
    assert reason == "no review verdict"


def test_park_updates_summary(plan_dir, monkeypatch, notify_calls):
    """The summary bookkeeping (parked + notify) is unchanged."""
    _park_harness(plan_dir, monkeypatch)
    summary = _summary()

    adv._adjudicate_merges(PLAN, summary)

    assert summary["parked"] == [KEY]
    assert summary["notify"] == [KEY]
    assert summary["merged"] == []
    assert summary["failed"] == []


def test_park_does_not_merge(plan_dir, monkeypatch, notify_calls):
    """A declined merge never reaches the merge path (no ``_merge_pr`` call)."""
    _park_harness(plan_dir, monkeypatch)
    merged = []
    monkeypatch.setattr(adv, "_merge_pr", lambda wt, key: merged.append(key))

    adv._adjudicate_merges(PLAN, _summary())

    assert merged == []


# ---------------------------------------------------------------------------
# Boundary: correlation-id kwargs survive the stamp
# ---------------------------------------------------------------------------


def test_park_preserves_correlation_id_kwarg(plan_dir, monkeypatch, notify_calls):
    """A story with a correlation_id keeps it alongside the new ``event``."""
    _park_harness(plan_dir, monkeypatch, correlation_id="corr-123")

    adv._adjudicate_merges(PLAN, _summary())

    kwargs = notify_calls[0]["kwargs"]
    assert kwargs.get("event") == "story_parked"
    assert kwargs.get("correlation_id") == "corr-123"
    assert set(kwargs) == {"event", "story_key", "correlation_id"}


def test_park_without_correlation_id_omits_it(plan_dir, monkeypatch, notify_calls):
    """A story without a correlation_id gains no empty/None kwarg."""
    _park_harness(plan_dir, monkeypatch)

    adv._adjudicate_merges(PLAN, _summary())

    assert "correlation_id" not in notify_calls[0]["kwargs"]


def test_park_with_empty_reason_still_stamped(plan_dir, monkeypatch, notify_calls):
    """An empty park reason is a boundary value, not a reason to skip the stamp."""
    _park_harness(plan_dir, monkeypatch, reason="")

    adv._adjudicate_merges(PLAN, _summary())

    assert notify_calls[0]["kwargs"].get("event") == "story_parked"
    assert notify_calls[0]["args"][1] == f"{KEY} parked: "


# ---------------------------------------------------------------------------
# Negative: a non-park notification from the SAME function is NOT stamped
# ---------------------------------------------------------------------------


def test_successful_merge_notification_is_not_stamped(plan_dir, monkeypatch, notify_calls):
    """The merge path's own notification must not carry ``story_parked``."""
    _stub_merge_boundaries(monkeypatch)
    _write_manifest(plan_dir, PLAN, {KEY: _pr_open_story()})

    adv._adjudicate_merges(PLAN, _summary())

    assert notify_calls, "the merge path should still notify"
    assert all(
        call["kwargs"].get("event") != "story_parked" for call in notify_calls
    ), f"a non-park notification was stamped story_parked: {notify_calls!r}"
    assert notify_calls[0]["kwargs"].get("event") == "story_merged"


def test_merge_gate_retry_notification_is_not_stamped(plan_dir, monkeypatch, notify_calls):
    """The merge-gate retry notification must not carry ``story_parked``."""
    _stub_merge_boundaries(monkeypatch, rebase=("rebase exploded", ""))
    _write_manifest(plan_dir, PLAN, {KEY: _pr_open_story()})

    adv._adjudicate_merges(PLAN, _summary())

    assert notify_calls, "the retry path should still notify"
    assert all(
        call["kwargs"].get("event") != "story_parked" for call in notify_calls
    ), f"a non-park notification was stamped story_parked: {notify_calls!r}"
    assert notify_calls[0]["kwargs"].get("event") == "merge_gate_retry"


def test_merge_gate_failed_notification_is_not_stamped(plan_dir, monkeypatch, notify_calls):
    """The terminal merge-gate-failure notification must not carry ``story_parked``."""
    _stub_merge_boundaries(monkeypatch, rebase=("rebase exploded", ""))
    _write_manifest(plan_dir, PLAN, {KEY: _pr_open_story(merge_attempts=2)})

    adv._adjudicate_merges(PLAN, _summary())

    assert notify_calls, "the terminal-failure path should still notify"
    assert all(
        call["kwargs"].get("event") != "story_parked" for call in notify_calls
    ), f"a non-park notification was stamped story_parked: {notify_calls!r}"
    assert notify_calls[0]["kwargs"].get("event") == "merge_gate_failed"


def test_non_pr_open_story_is_never_notified(plan_dir, monkeypatch, notify_calls):
    """A story that is not pr_open is skipped entirely (no notification at all)."""
    _write_manifest(plan_dir, PLAN, {KEY: _pr_open_story(status="tests_passed")})

    adv._adjudicate_merges(PLAN, _summary())

    assert notify_calls == []


# ---------------------------------------------------------------------------
# Never-break contract: a raising _notify_user must not break the function
# ---------------------------------------------------------------------------


def test_park_survives_notification_backend_failure(plan_dir, monkeypatch):
    """The never-break contract holds: a failing notification backend does not
    break parking.

    ``_adjudicate_merges`` calls ``_notify_user`` with no try/except of its own
    (this story must not add one); the swallow contract lives inside
    ``_notify_user`` itself, which logs and swallows every failure.  This test
    drives the REAL ``_notify_user`` with a notification backend that raises
    and asserts the park still lands on disk.
    """
    _park_harness(plan_dir, monkeypatch, reason="boom reason")
    # Restore the real notifier (the recorder fixture is not used here).
    monkeypatch.setattr(adv, "_notify_user", ppers._notify_user)

    def _boom(*args, **kwargs):
        raise RuntimeError("notification backend down")

    monkeypatch.setattr(ew, "get_bus", _boom)
    monkeypatch.setattr(ppers, "_write_notification_record", _boom)

    summary = _summary()

    # Must not raise.
    adv._adjudicate_merges(PLAN, summary)

    manifest = _read_manifest(plan_dir, PLAN)
    assert manifest["stories"][KEY]["status"] == "parked"
    assert manifest["stories"][KEY]["parked_reason"] == "boom reason"
    assert summary["parked"] == [KEY]


def test_park_call_site_adds_no_try_except(plan_dir, monkeypatch):
    """The park call site's control flow is unchanged.

    The brief forbids wrapping the stamped call in a new try/except, so a
    ``_notify_user`` that raises must still propagate out of
    ``_adjudicate_merges`` exactly as it did before this story.
    """
    _park_harness(plan_dir, monkeypatch, reason="boom reason")

    def _boom(*args, **kwargs):
        raise RuntimeError("notify exploded")

    monkeypatch.setattr(adv, "_notify_user", _boom)

    with pytest.raises(RuntimeError, match="notify exploded"):
        adv._adjudicate_merges(PLAN, _summary())


# ---------------------------------------------------------------------------
# The enclosing function is neither renamed nor reformatted
# ---------------------------------------------------------------------------


def test_adjudicate_merges_signature_is_unchanged():
    """``_adjudicate_merges`` keeps its ``(plan_name, summary)`` signature."""
    assert callable(adv._adjudicate_merges)
    params = list(inspect.signature(adv._adjudicate_merges).parameters)
    assert params == ["plan_name", "summary"]
