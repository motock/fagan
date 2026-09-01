"""Lifecycle correlation propagation (W4L): every event emitted by the
review, rework, escalation and merge paths must carry the SAME story
``correlation_id`` that dispatch minted (``pipeline/dispatch.py`` sets
``story["correlation_id"] = uuid.uuid4().hex[:12]`` at dispatch time).

Contract under test (additive, emit-point stamping only):

* Every ``_notify_user(...)`` emit point in ``pipeline/review_orchestrator.py``,
  ``pipeline/advance.py``, ``pipeline/escalation.py`` and ``pipeline/merge.py``
  that relates to a story (plan + story key in scope) passes the story's
  persisted ``correlation_id`` through to ``_notify_user``'s existing
  ``correlation_id=`` keyword (``pipeline.persistence._notify_user`` already
  forwards it into the bus payload and the JSONL record, and omits the key
  entirely when it is ``None``).
* ``attempt=story.get("dispatch_attempts", 0)`` accompanies the
  ``correlation_id`` ONLY where an attempt counter is already in scope (the
  rework-path emit points).
* When the story has NO ``correlation_id`` (older manifests dispatched before
  the field existed) the records are UNCHANGED: no ``correlation_id`` key at
  all - absent, not ``None`` - so records stay uniform with the legacy shape.

These tests deliberately do NOT patch ``_notify_user``: the real
``pipeline.persistence._notify_user`` must run so the structured JSONL sidecar
(``<plan>.notifications.jsonl``) is written into the isolated ``plan_dir``,
exactly like ``test_ci_pending_stalled_notification.py``. Assertions read the
JSONL records, which proves the FULL chain (emit point -> _notify_user kwargs
-> bus payload -> persisted record) rather than a mock's view of it.

Emit points exercised:

* APPROVE->PR (review): on the APPROVE branch ``review_story`` opens the PR
  and only notifies when ``_open_pr`` FAILS ("review APPROVEd but could not
  open PR ... will retry next tick") - a successful PR open emits no
  notification today. That failure notification IS the APPROVE->PR event, so
  the APPROVE-path test drives ``_open_pr`` to raise (mirroring
  ``test_review_pr_comment.py``'s approve-path error test).
* Rework (review): the rework-cap park notification and the mid-budget
  "could not open PR / post review comment" rework event.
* Merge: both story-scoped emit points are "rebase auto-resolved" notices -
  one in the shared helper ``_rebase_and_push_for_merge`` (which receives only
  plan+story key, NO story dict - so the correlation_id must be looked up from
  the manifest the same way neighboring code reads story fields) and one
  inline in ``_approve_merge_impl``.
* Advance: the merge-gate park notification in the advance tick.
* Escalation: ``_escalate_review_to_claude``'s "escalating to ..." event.

These tests are written to FAIL until the implementation stamps the emit
points; the expected failure is a missing ``correlation_id`` key on the
persisted records, not a setup error.
"""

import json
import subprocess

import pytest

import pipeline.advance as adv
import pipeline.escalation as esc
import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline.server import _advance_pipeline_locked

PLAN = "correlplan"
KEY = "S1"
CID = "abc"

_APPROVE_OUTPUT = "Looks good.\nVERDICT: APPROVE"
_REQUEST_CHANGES_OUTPUT = (
    "The error path is untested and the SQL is injectable; add coverage and "
    "parameterize the query.\nVERDICT: REQUEST_CHANGES"
)


# ---------------------------------------------------------------------------
# Fixtures (mirror test_final_rework_escalation.py / test_review_pr_comment.py
# so this file is fully standalone).
# ---------------------------------------------------------------------------
@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    # pipeline_persistence / pipeline_concurrency read PLAN_DIR as a free var,
    # so the patches must land on their own bindings too.
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
def _story(plan_dir, *, status, **extra):
    story = {
        "summary": "Add thing",
        "status": status,
        "worktree": str(plan_dir / "wt"),
        "risk": "low",
    }
    story.update(extra)
    return story


def _write_manifest(plan_dir, story, *, story_key=KEY):
    manifest = {"epics": {}, "stories": {story_key: story}}
    (plan_dir / f"{PLAN}.manifest.json").write_text(json.dumps(manifest))


def _read_story(plan_dir, story_key=KEY):
    return json.loads((plan_dir / f"{PLAN}.manifest.json").read_text())[
        "stories"
    ][story_key]


def _read_records(plan_dir):
    """Every JSONL notification record written for PLAN (may be empty)."""
    path = plan_dir / f"{PLAN}.notifications.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def _story_records(plan_dir, story_key=KEY):
    """Records whose free-text message names the story (the emit points in
    scope interpolate the story key into the message rather than passing a
    story_key kwarg)."""
    return [
        r
        for r in _read_records(plan_dir)
        if story_key in (r.get("message") or "")
    ]


def _patch_review_boundaries(monkeypatch, reviewer_output, *, open_pr_raises):
    """Stub every external boundary review_story touches EXCEPT _notify_user,
    so the real persistence._notify_user writes the JSONL records."""

    def _fake_reviewer(wt, br, **k):
        return reviewer_output

    monkeypatch.setattr(p, "_run_reviewer", _fake_reviewer)

    if open_pr_raises:

        def _boom_pr(*a, **k):
            raise subprocess.CalledProcessError(1, "gh")

        monkeypatch.setattr(p, "_open_pr", _boom_pr)
    else:
        monkeypatch.setattr(
            p, "_open_pr", lambda *a, **k: "https://example.test/pr/1"
        )
    monkeypatch.setattr(p, "_post_pr_comment", lambda *a, **k: None)
    monkeypatch.setattr(
        p, "_reverify_acceptance", lambda *a, **k: {"state": "pass"}
    )


def _disable_auto_escalation(monkeypatch):
    monkeypatch.setattr(p, "_auto_escalation_enabled", lambda: False)


def _patch_merge_boundaries(monkeypatch):
    """Stub every external boundary approve_merge touches EXCEPT _notify_user."""
    monkeypatch.setattr(
        p,
        "_rebase_onto_master",
        lambda *a, **k: {"ok": True, "auto_resolved": True},
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(
        p, "_ci_status", lambda *a, **k: {"state": "success", "error": ""}
    )
    monkeypatch.setattr(
        p, "_reverify_acceptance", lambda *a, **k: {"state": "pass"}
    )
    monkeypatch.setattr(p, "_reverify_build", lambda *a, **k: {"state": "pass"})
    monkeypatch.setattr(p, "_merge_pr", lambda *a, **k: "merged")
    monkeypatch.setattr(p, "_ci_rerun", lambda *a, **k: None)
    monkeypatch.setattr(p, "_mark_plane_done", lambda *a, **k: None)
    monkeypatch.setattr(p, "_mcp_self_source_touched", lambda *a, **k: [])


def _patch_advance_boundaries(monkeypatch):
    """Stub the advance tick's merge adjudication so the pr_open story takes
    the park branch (decision != merge), which emits a story-scoped
    notification - without consulting CI/rebase boundaries."""
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated", raising=False)
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low", raising=False)
    monkeypatch.setattr(
        p,
        "_ci_status_once",
        lambda *a, **k: {"state": "success", "error": ""},
        raising=False,
    )
    monkeypatch.setattr(
        p,
        "_rebase_and_push_for_merge",
        lambda *a, **k: ("", "deadbeef"),
        raising=False,
    )
    monkeypatch.setattr(
        p, "_reverify_acceptance", lambda *a, **k: {"state": "pass"},
        raising=False,
    )
    monkeypatch.setattr(
        p, "_reverify_build", lambda *a, **k: {"state": "pass"}, raising=False
    )
    monkeypatch.setattr(p, "_merge_pr", lambda *a, **k: "merged", raising=False)
    monkeypatch.setattr(p, "_ci_rerun", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(p, "_mark_plane_done", lambda *a, **k: None, raising=False)

    def _park(story, *a, **k):
        return {"action": "park", "reason": "ci failing"}

    monkeypatch.setattr(p, "_merge_decision", _park, raising=False)
    monkeypatch.setattr(adv, "_merge_decision", _park, raising=False)


# ---------------------------------------------------------------------------
# (1) Review path: the APPROVE->PR event carries the story correlation_id.
# ---------------------------------------------------------------------------
def test_review_approve_pr_event_carries_correlation_id(
    plan_dir, agents_dir, monkeypatch
):
    """review_story's APPROVE branch opens the PR; its emit point (the
    "review APPROVEd but could not open PR" notification) must carry the
    story's persisted correlation_id."""
    story = _story(plan_dir, status="tests_passed", correlation_id=CID)
    _write_manifest(plan_dir, story)
    _patch_review_boundaries(monkeypatch, _APPROVE_OUTPUT, open_pr_raises=True)

    result = p.review_story(PLAN, KEY)

    assert result["ok"] is True
    assert result["verdict"] == "APPROVE"
    approve_pr = [
        r
        for r in _story_records(plan_dir)
        if "could not open PR" in (r.get("message") or "")
    ]
    assert approve_pr, (
        f"expected the APPROVE->PR notification to be emitted, "
        f"got {_read_records(plan_dir)!r}"
    )
    for record in approve_pr:
        assert record.get("correlation_id") == CID, (
            f"the APPROVE->PR event must carry the story correlation_id "
            f"{CID!r}; got {record!r}"
        )
    # Goal-level invariant: EVERY story-scoped event on the review path
    # carries the same correlation_id - none may be left unstamped.
    for record in _story_records(plan_dir):
        assert record.get("correlation_id") == CID, (
            f"story-scoped review event missing correlation_id: {record!r}"
        )


# ---------------------------------------------------------------------------
# (2) Rework path: correlation_id + attempt == story["dispatch_attempts"].
# ---------------------------------------------------------------------------
def test_review_rework_park_event_carries_correlation_id_and_attempt(
    plan_dir, agents_dir, monkeypatch
):
    """Rework budget exhausted -> the park notification is a rework-path
    event. It must carry correlation_id "abc" and attempt must match
    story["dispatch_attempts"] (5) - NOT the rework cycle counter (3)."""
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    _disable_auto_escalation(monkeypatch)
    story = _story(
        plan_dir,
        status="tests_passed",
        correlation_id=CID,
        rework_attempts=2,  # this cycle's rework attempts -> 3 == rework cap
        dispatch_attempts=5,
    )
    _write_manifest(plan_dir, story)
    _patch_review_boundaries(
        monkeypatch, _REQUEST_CHANGES_OUTPUT, open_pr_raises=False
    )

    result = p.review_story(PLAN, KEY)

    assert result["ok"] is True
    assert result["verdict"] == "REQUEST_CHANGES"
    assert _read_story(plan_dir)["status"] == "parked"
    parked = [
        r
        for r in _story_records(plan_dir)
        if "parked: reviewer still requesting changes"
        in (r.get("message") or "")
    ]
    assert parked, (
        f"expected the rework-cap park notification, "
        f"got {_read_records(plan_dir)!r}"
    )
    for record in parked:
        assert record.get("correlation_id") == CID, record
        assert record.get("attempt") == 5, (
            f"attempt must come from story['dispatch_attempts'] (5), "
            f"not the rework cycle counter; got {record!r}"
        )


def test_review_rework_pr_failure_event_carries_correlation_id_and_attempt(
    plan_dir, agents_dir, tmp_path, monkeypatch
):
    """Mid-budget rework round with a real worktree: when re-opening the PR
    fails, the "could not open PR / post review comment" rework event must
    carry correlation_id and attempt == story['dispatch_attempts']."""
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    _disable_auto_escalation(monkeypatch)
    worktree = tmp_path / "wt-real"
    worktree.mkdir()
    story = _story(
        plan_dir,
        status="tests_passed",
        worktree=str(worktree),
        correlation_id=CID,
        rework_attempts=0,  # this cycle's rework attempts -> 1 (< cap)
        dispatch_attempts=5,
    )
    _write_manifest(plan_dir, story)
    _patch_review_boundaries(
        monkeypatch, _REQUEST_CHANGES_OUTPUT, open_pr_raises=True
    )

    result = p.review_story(PLAN, KEY)

    assert result["ok"] is True
    assert result["verdict"] == "REQUEST_CHANGES"
    assert result["status"] == "changes_requested"
    rework = [
        r
        for r in _story_records(plan_dir)
        if "could not open PR" in (r.get("message") or "")
    ]
    assert rework, (
        f"expected the rework PR-failure notification, "
        f"got {_read_records(plan_dir)!r}"
    )
    for record in rework:
        assert record.get("correlation_id") == CID, record
        assert record.get("attempt") == 5, record


# ---------------------------------------------------------------------------
# (3) Merge path: the merge event carries the correlation_id.
# ---------------------------------------------------------------------------
def test_merge_path_event_carries_correlation_id(plan_dir, monkeypatch):
    """approve_merge on a reviewer-approved parked story: the merge-path
    "rebase auto-resolved" notification (emitted inline in
    _approve_merge_impl) must carry the story correlation_id."""
    story = _story(
        plan_dir,
        status="parked",
        review_verdict="APPROVE",
        correlation_id=CID,
        worktree="/nonexistent-worktree",
    )
    _write_manifest(plan_dir, story)
    _patch_merge_boundaries(monkeypatch)

    result = p.approve_merge(PLAN, KEY)

    assert result["ok"] is True, result
    assert result["status"] == "done"
    merge_events = [
        r
        for r in _story_records(plan_dir)
        if "rebase auto-resolved" in (r.get("message") or "")
    ]
    assert merge_events, (
        f"expected the merge-path rebase notification, "
        f"got {_read_records(plan_dir)!r}"
    )
    for record in merge_events:
        assert record.get("correlation_id") == CID, (
            f"the merge event must carry the story correlation_id "
            f"{CID!r}; got {record!r}"
        )


def test_merge_shared_helper_event_carries_correlation_id(
    plan_dir, monkeypatch
):
    """The shared merge helper ``_rebase_and_push_for_merge`` receives only
    plan+story key (NO story dict), so it must look the correlation_id up
    from the manifest the same way neighboring code reads story fields - no
    new global. Its "rebase auto-resolved" notice must carry the id."""
    story = _story(
        plan_dir,
        status="parked",
        review_verdict="APPROVE",
        correlation_id=CID,
        worktree="/nonexistent-worktree",
    )
    _write_manifest(plan_dir, story)

    # merge.py's shared helper does ``from .server import ...`` at call time,
    # so the stubs must land on the live pipeline.server bindings.
    monkeypatch.setattr(
        p,
        "_rebase_onto_master",
        lambda *a, **k: {"ok": True, "auto_resolved": True},
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p, "REPO_ROOT", plan_dir)
    # Route the helper's _notify_user to the REAL persistence writer so the
    # JSONL record is produced (merge.py resolves _notify_user through
    # pipeline.server, which the other fixtures leave patched to a recorder).
    monkeypatch.setattr(p, "_notify_user", ppers._notify_user)

    error, _sha = p._rebase_and_push_for_merge(
        PLAN, KEY, "agent/s1", "/nonexistent-worktree"
    )

    assert error == ""
    merge_events = [
        r
        for r in _story_records(plan_dir)
        if "rebase auto-resolved" in (r.get("message") or "")
    ]
    assert merge_events, (
        f"expected the shared-helper rebase notification, "
        f"got {_read_records(plan_dir)!r}"
    )
    for record in merge_events:
        assert record.get("correlation_id") == CID, (
            f"the shared merge helper's event must carry the story "
            f"correlation_id {CID!r}; got {record!r}"
        )


# ---------------------------------------------------------------------------
# (4) Negative: a story WITHOUT correlation_id (older manifest) produces
#     records with NO correlation_id key at all (absent, not None).
# ---------------------------------------------------------------------------
def test_review_path_without_correlation_id_emits_no_correlation_key(
    plan_dir, agents_dir, monkeypatch
):
    """Older manifest: no correlation_id field. Every record on BOTH the
    APPROVE->PR path and the rework path must have NO correlation_id key -
    absent, not None - and no attempt key either."""
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    _disable_auto_escalation(monkeypatch)

    # APPROVE->PR path.
    story = _story(
        plan_dir, status="tests_passed", dispatch_attempts=5
    )  # no correlation_id
    _write_manifest(plan_dir, story)
    _patch_review_boundaries(monkeypatch, _APPROVE_OUTPUT, open_pr_raises=True)
    result = p.review_story(PLAN, KEY)
    assert result["ok"] is True
    assert result["verdict"] == "APPROVE"

    # Rework path (same story dict re-read from disk, still no correlation_id).
    story = _read_story(plan_dir)
    story.update({"status": "tests_passed", "rework_attempts": 2})
    _write_manifest(plan_dir, story)
    _patch_review_boundaries(
        monkeypatch, _REQUEST_CHANGES_OUTPUT, open_pr_raises=False
    )
    result = p.review_story(PLAN, KEY)
    assert result["ok"] is True
    assert result["verdict"] == "REQUEST_CHANGES"
    assert _read_story(plan_dir)["status"] == "parked"

    records = _story_records(plan_dir)
    assert records, f"expected story-scoped notifications, got {records!r}"
    for record in records:
        assert "correlation_id" not in record, (
            f"legacy story without correlation_id must produce records with "
            f"NO correlation_id key (absent, not None); got {record!r}"
        )
        assert "attempt" not in record, (
            f"legacy story must produce records with NO attempt key; "
            f"got {record!r}"
        )


# ---------------------------------------------------------------------------
# (5) Negative: escalation events for a story without correlation_id are
#     unchanged (backward-compat for old manifests).
# ---------------------------------------------------------------------------
def test_escalation_without_correlation_id_unchanged(monkeypatch):
    """_escalate_review_to_claude on a legacy story (no correlation_id) must
    emit its notification exactly as today: no correlation_id key, no attempt
    key."""
    calls = []
    monkeypatch.setattr(
        esc,
        "_notify_user",
        lambda plan_name, message, **kwargs: calls.append((message, kwargs)),
    )
    monkeypatch.setattr(esc, "_escalation_target", lambda: ("claude", None))

    story = {"status": "tests_passed", "dispatch_attempts": 5}
    esc._escalate_review_to_claude(
        story, KEY, PLAN, "rework budget exhausted after 3 review cycles"
    )

    assert story["backend"] == "claude"
    assert len(calls) == 1, f"expected exactly one escalation notice, got {calls!r}"
    message, kwargs = calls[0]
    assert KEY in message and "escalating" in message.lower()
    assert "correlation_id" not in kwargs, (
        f"legacy story escalation must not gain a correlation_id kwarg; "
        f"got {kwargs!r}"
    )
    assert "attempt" not in kwargs, (
        f"legacy story escalation must not gain an attempt kwarg; "
        f"got {kwargs!r}"
    )


def test_escalation_with_correlation_id_carries_it(monkeypatch):
    """Goal invariant: the escalation path's event carries the story's
    correlation_id when the story has one."""
    calls = []
    monkeypatch.setattr(
        esc,
        "_notify_user",
        lambda plan_name, message, **kwargs: calls.append((message, kwargs)),
    )
    monkeypatch.setattr(esc, "_escalation_target", lambda: ("claude", None))

    story = {"status": "tests_passed", "correlation_id": CID}
    esc._escalate_review_to_claude(
        story, KEY, PLAN, "rework budget exhausted after 3 review cycles"
    )

    assert len(calls) == 1, f"expected exactly one escalation notice, got {calls!r}"
    _, kwargs = calls[0]
    assert kwargs.get("correlation_id") == CID, (
        f"the escalation event must carry the story correlation_id "
        f"{CID!r}; got {kwargs!r}"
    )


# ---------------------------------------------------------------------------
# (6) Advance path: the merge-gate park event carries the correlation_id.
# ---------------------------------------------------------------------------
def test_advance_merge_gate_park_event_carries_correlation_id(
    plan_dir, monkeypatch
):
    """The advance tick's merge adjudication park branch emits a story-scoped
    notification; it must carry the story correlation_id."""
    story = _story(
        plan_dir,
        status="pr_open",
        review_verdict="APPROVE",
        correlation_id=CID,
        worktree="/nonexistent-worktree",
        dispatch_attempts=5,
    )
    _write_manifest(plan_dir, story)
    _patch_advance_boundaries(monkeypatch)

    _advance_pipeline_locked(PLAN)

    park_events = [
        r
        for r in _story_records(plan_dir)
        if "parked" in (r.get("message") or "").lower()
    ]
    assert park_events, (
        f"expected the advance merge-gate park notification, "
        f"got {_read_records(plan_dir)!r}"
    )
    for record in park_events:
        assert record.get("correlation_id") == CID, (
            f"the advance merge-gate event must carry the story "
            f"correlation_id {CID!r}; got {record!r}"
        )


def test_advance_merge_gate_park_event_without_correlation_id_unchanged(
    plan_dir, monkeypatch
):
    """Backward-compat: an older pr_open story without correlation_id keeps
    the exact legacy record shape on the advance park branch."""
    story = _story(
        plan_dir,
        status="pr_open",
        review_verdict="APPROVE",
        worktree="/nonexistent-worktree",
        dispatch_attempts=5,
    )
    _write_manifest(plan_dir, story)
    _patch_advance_boundaries(monkeypatch)

    _advance_pipeline_locked(PLAN)

    park_events = [
        r
        for r in _story_records(plan_dir)
        if "parked" in (r.get("message") or "").lower()
    ]
    assert park_events, (
        f"expected the advance merge-gate park notification, "
        f"got {_read_records(plan_dir)!r}"
    )
    for record in park_events:
        assert "correlation_id" not in record, record
        assert "attempt" not in record, record