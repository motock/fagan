"""Tests for the ``event="story_parked"`` stamp on ``review_story``'s park sites.

``pipeline/notification_outbox.py:outbox_sink`` selects e-mailable records using
ONLY the structured field ``event["payload"]["event"]``; message text is never
matched. So a notification emitted without an ``event=`` kwarg can never be
e-mailed. ``review_story`` has three branches that set
``story["status"] = "parked"`` and all three must stamp ``event="story_parked"``:

1. the UNKNOWN/inconclusive park (``**_cid_kwargs``),
2. the empty-REQUEST_CHANGES park (same block, byte-identical),
3. the rework-budget park (``**_rework_kwargs``).

Non-park notifications from the same function (retry, skip, ...) must NOT carry
the stamp -- those stories are still alive.

These tests drive the REAL ``review_story`` entry point (``p.review_story``) with
``_notify_user`` monkeypatched on ``pipeline.server`` (``review_orchestrator``
resolves it through a ``_ServerRef`` at call time, so the patch lands). Fixture
shape is cribbed from ``tests/unit/test_final_rework_escalation.py``.
"""

import json

import pytest

import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import persistence as ppers

# ---------------------------------------------------------------------------
# Fixtures (mirror test_final_rework_escalation.py -- standalone, no cross-file
# imports of test code).
# ---------------------------------------------------------------------------

@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    # pipeline_persistence and pipeline_concurrency import PLAN_DIR from
    # pipeline_paths at module load and read it as a free var, so patches
    # must land on their own bindings too.
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(p, "AGENTS_DIR", d)
    return d


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_story(plan_dir, *, status, worktree=None, **extra):
    story = {
        "summary": "Add thing",
        "status": status,
        "worktree": str(plan_dir / "wt") if worktree is None else worktree,
        "risk": "low",
    }
    story.update(extra)
    return story


def _write_manifest_with_story(plan_dir, plan_name, story_key, story):
    manifest = {"epics": {}, "stories": {story_key: story}}
    (plan_dir / f"{plan_name}.manifest.json").write_text(json.dumps(manifest))


def _read_story(plan_dir, plan_name, story_key):
    return json.loads(
        (plan_dir / f"{plan_name}.manifest.json").read_text()
    )["stories"][story_key]


def _record_notify(monkeypatch):
    """Monkeypatch pipeline.server._notify_user to append (args, kwargs) to a
    list instead of spooling an e-mail. Returns the list."""
    calls = []

    def _fake_notify(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(p, "_notify_user", _fake_notify)
    return calls


def _disable_auto_escalation(monkeypatch):
    monkeypatch.setattr(p, "_auto_escalation_enabled", lambda: False)


def _force_unknown_review(monkeypatch, output="I could not tell if this is correct."):
    """Mock _run_reviewer to return text with no VERDICT line -> UNKNOWN."""

    def _fake_reviewer(wt, br, backend_name=None, plan_role_config=None,
                       acceptance=None, since_sha=None, risk=None):
        return output

    monkeypatch.setattr(p, "_run_reviewer", _fake_reviewer)


def _force_request_changes(monkeypatch):
    """REQUEST_CHANGES with genuine findings text (a real rejection that
    consumes the rework budget, not the empty-findings inconclusive path)."""
    out = (
        "The error path is untested and the SQL is injectable; add coverage "
        "and parameterize the query.\nVERDICT: REQUEST_CHANGES"
    )
    _force_unknown_review(monkeypatch, output=out)


# ---------------------------------------------------------------------------
# Positive: the park path emits event="story_parked".
# ---------------------------------------------------------------------------

def test_park_emits_story_parked(plan_dir, agents_dir, monkeypatch):
    """UNKNOWN verdict at REVIEW_INCONCLUSIVE_MAX parks the story; that park
    notification must carry event="story_parked" so the outbox sink can e-mail
    it."""
    _disable_auto_escalation(monkeypatch)
    monkeypatch.setattr(p, "REVIEW_INCONCLUSIVE_MAX", 1)
    calls = _record_notify(monkeypatch)

    story = _make_story(plan_dir, status="tests_passed")
    _write_manifest_with_story(plan_dir, "pe", "S1", story)
    _force_unknown_review(monkeypatch)

    result = p.review_story("pe", "S1")

    assert result["ok"] is True
    assert result["status"] == "parked"
    assert len(calls) == 1
    assert calls[-1]["kwargs"].get("event") == "story_parked"


# ---------------------------------------------------------------------------
# Negative: a non-park notification from the same function does NOT carry the
# stamp (proves the kwarg landed on the park call, not a neighbour).
# ---------------------------------------------------------------------------

def test_non_park_does_not_emit_story_parked(plan_dir, agents_dir, monkeypatch):
    """An UNKNOWN below the inconclusive cap is a retry, not a park: the story
    stays alive and its notification must not claim event="story_parked"."""
    _disable_auto_escalation(monkeypatch)
    monkeypatch.setattr(p, "REVIEW_INCONCLUSIVE_MAX", 3)
    calls = _record_notify(monkeypatch)

    story = _make_story(plan_dir, status="tests_passed")
    _write_manifest_with_story(plan_dir, "pe", "S1", story)
    _force_unknown_review(monkeypatch)

    result = p.review_story("pe", "S1")

    assert result["ok"] is True
    assert result["status"] == "tests_passed"  # still alive, will retry
    assert len(calls) == 1
    assert "will retry" in calls[-1]["args"][1]
    assert calls[-1]["kwargs"].get("event") != "story_parked"


# ---------------------------------------------------------------------------
# The stamp must not disturb behaviour: status and parked_reason unchanged.
# ---------------------------------------------------------------------------

def test_park_status_and_reason_unchanged(plan_dir, agents_dir, monkeypatch):
    _disable_auto_escalation(monkeypatch)
    monkeypatch.setattr(p, "REVIEW_INCONCLUSIVE_MAX", 1)
    _record_notify(monkeypatch)

    story = _make_story(plan_dir, status="tests_passed")
    _write_manifest_with_story(plan_dir, "pe", "S1", story)
    _force_unknown_review(monkeypatch)

    p.review_story("pe", "S1")

    on_disk = _read_story(plan_dir, "pe", "S1")
    assert on_disk["status"] == "parked"
    assert on_disk["parked_reason"] == (
        "review inconclusive after 1 attempts - needs human review"
    )


def test_rework_budget_park_status_reason_and_event(plan_dir, agents_dir,
                                                    monkeypatch):
    """The rework-budget park (the **_rework_kwargs site) is stamped too, and
    its status/parked_reason are unchanged."""
    _disable_auto_escalation(monkeypatch)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 1)
    calls = _record_notify(monkeypatch)

    story = _make_story(
        plan_dir, status="tests_passed", rework_attempts=0,
    )
    _write_manifest_with_story(plan_dir, "pe", "S1", story)
    _force_request_changes(monkeypatch)

    result = p.review_story("pe", "S1")

    assert result["ok"] is True
    assert result["status"] == "parked"
    assert len(calls) == 1
    assert calls[-1]["kwargs"].get("event") == "story_parked"
    on_disk = _read_story(plan_dir, "pe", "S1")
    assert on_disk["status"] == "parked"
    assert on_disk["parked_reason"] == (
        "rework budget exhausted after 1 review cycles"
    )


# ---------------------------------------------------------------------------
# A raising _notify_user must not break the enclosing function (the existing
# swallow/never-break contract holds).
# ---------------------------------------------------------------------------

def test_notify_raising_does_not_break_review_story(
    plan_dir, agents_dir, monkeypatch,
):
    """The never-break contract: a failing notification sink must not break
    review_story. The swallow lives INSIDE the real ``_notify_user``
    (pipeline/persistence.py wraps bus publish + direct write in a broad
    try/except), so raise inside the transport it calls
    (``_write_notification_record``) and let the real swallow run."""
    _disable_auto_escalation(monkeypatch)
    monkeypatch.setattr(p, "REVIEW_INCONCLUSIVE_MAX", 1)

    from pipeline import persistence as ppers_notify

    def _boom(*args, **kwargs):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(ppers_notify, "_write_notification_record", _boom)

    story = _make_story(plan_dir, status="tests_passed")
    _write_manifest_with_story(plan_dir, "pe", "S1", story)
    _force_unknown_review(monkeypatch)

    result = p.review_story("pe", "S1")

    assert result["ok"] is True
    assert result["status"] == "parked"
    on_disk = _read_story(plan_dir, "pe", "S1")
    assert on_disk["status"] == "parked"