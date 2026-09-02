"""Merge-gate notification event names (WIRING story).

The two "rebase auto-resolved" emit points in ``pipeline/merge.py`` must
stamp the structured ``event="rebase_auto_resolved"`` name on their
``_notify_user`` calls - same mechanism and rationale as the advance.py
stamping story: the record schema already supports ``event`` and the
notification sidecar needs machine-readable phase names for
cost-per-merged-story metrics.

Contract under test (additive, emit-point stamping only):

* Both call sites - the shared helper ``_rebase_and_push_for_merge`` and
  the inline emit in ``_approve_merge_impl`` - pass
  ``event="rebase_auto_resolved"`` to ``_notify_user`` (exactly one
  ``event=`` keyword argument per stamped call).
* The correlation_id conditional dicts and the free-text messages stay
  byte-for-byte unchanged: a story WITH a persisted correlation_id keeps
  ``correlation_id`` on the record; an older manifest WITHOUT the field
  keeps the legacy shape (key absent, not null); a story missing from the
  manifest degrades to the same legacy shape.
* No OTHER notification in ``pipeline/merge.py`` gains an ``event=``:
  every ``event=`` keyword argument in the module must use the stamped
  name, and the unrelated ``_mcp_restart_notice`` call site must remain
  byte-identical.

The source-level assertions use ``inspect.getsource`` membership (not
exact file totals) per the story brief. The behavioral tests drive the
rebase auto-resolved path with stubbed ``_rebase_onto_master``/``_store``
boundaries and read the REAL persistence JSONL sidecar so the full chain
(emit point -> _notify_user kwargs -> persisted record) is proven, exactly
like ``test_w4l_lifecycle_correlation.py``.

These tests deliberately FAIL until the implementation stamps the emit
points; the expected failure is a missing/mismatched ``event`` on the
records and absent ``event=`` in the sources, not a setup error.
"""

import inspect
import json
import re
from pathlib import Path

import pytest

import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import merge
from pipeline import persistence as ppers

PLAN = "mergeeventplan"
KEY = "S1"
CID = "cid-merge-01"

EVENT_NAME = "rebase_auto_resolved"
_STAMP = f'event="{EVENT_NAME}"'
# The correlation_id conditional dict both emit points must keep,
# byte-for-byte (story brief: "Keep the correlation_id conditional dicts
# ... byte-for-byte unchanged").
_CONDITIONAL_CID = '**({"correlation_id": _cid} if _cid else {})'
# The exact free-text message both emit points must keep (byte-for-byte),
# with _default_branch stubbed to "main".
_EXPECTED_MESSAGE = (
    f"{KEY} rebase auto-resolved an additive-import conflict against "
    "origin/main."
)
# The unrelated third _notify_user call in _approve_merge_impl must not be
# touched (story brief: "Do not touch any other call site ... in this file").
_MCP_CALL = "_notify_user(plan_name, _mcp_restart_notice(mcp_touched))"


# ---------------------------------------------------------------------------
# Fixtures (mirror test_w4l_lifecycle_correlation.py so this file is
# fully standalone).
# ---------------------------------------------------------------------------
@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    # persistence / concurrency read PLAN_DIR as a free var, so the patches
    # must land on their own bindings too.
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class _FakeStore:
    """Minimal ``_store`` stand-in: the merge-gate emit points only consult
    ``get_manifest(plan_name)["stories"]``."""

    def __init__(self, stories):
        self._stories = stories

    def get_manifest(self, plan_name):
        return {"epics": {}, "stories": self._stories}


def _story(**extra):
    story = {
        "summary": "Add thing",
        "status": "pr_open",
        "worktree": "/nonexistent-worktree",
        "risk": "low",
    }
    story.update(extra)
    return story


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


def _auto_resolved_records(plan_dir):
    """Records whose free-text message is the rebase auto-resolved notice
    (the emit points in scope interpolate the story key into the message
    rather than passing a story_key kwarg)."""
    return [
        r
        for r in _read_records(plan_dir)
        if "rebase auto-resolved" in (r.get("message") or "")
    ]


def _patch_helper_boundaries(
    monkeypatch, plan_dir, *, rebase_result=None, stories=None
):
    """Stub the shared helper's external boundaries EXCEPT ``_notify_user``,
    so the real ``persistence._notify_user`` writes the JSONL sidecar.

    ``_rebase_onto_master`` and ``_store`` are stubbed per the story brief;
    they must land on the live ``pipeline.server`` bindings because
    merge.py does ``from .server import ...`` at call time.
    """
    monkeypatch.setattr(
        p,
        "_rebase_onto_master",
        lambda *a, **k: rebase_result
        if rebase_result is not None
        else {"ok": True, "auto_resolved": True},
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p, "REPO_ROOT", plan_dir)
    monkeypatch.setattr(
        p,
        "_store",
        _FakeStore({KEY: _story()} if stories is None else stories),
    )


def _patch_merge_boundaries(monkeypatch):
    """Stub every external boundary approve_merge touches EXCEPT
    _notify_user (mirrors test_w4l_lifecycle_correlation.py)."""
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


def _write_manifest(plan_dir, story, *, story_key=KEY):
    manifest = {"epics": {}, "stories": {story_key: story}}
    (plan_dir / f"{PLAN}.manifest.json").write_text(json.dumps(manifest))


# ---------------------------------------------------------------------------
# (a) Source-level: both call sites stamp event="rebase_auto_resolved".
# ---------------------------------------------------------------------------
def test_helper_source_stamps_rebase_auto_resolved_event():
    """``_rebase_and_push_for_merge``'s auto-resolved ``_notify_user`` call
    must carry event="rebase_auto_resolved" (membership, not file totals),
    as exactly one keyword argument on its single call site, with the
    correlation_id conditional dict byte-for-byte unchanged."""
    src = inspect.getsource(merge._rebase_and_push_for_merge)
    assert _STAMP in src, (
        f"the shared merge helper must stamp {_STAMP} on its rebase "
        f"auto-resolved notification; source:\n{src}"
    )
    assert src.count("_notify_user(") == 1, (
        f"expected exactly one _notify_user call in the shared helper; "
        f"source:\n{src}"
    )
    assert src.count(_STAMP) == 1, (
        f"expected exactly one event= keyword argument on the helper's "
        f"single call; source:\n{src}"
    )
    assert _CONDITIONAL_CID in src, (
        f"the correlation_id conditional dict must stay byte-for-byte "
        f"unchanged; source:\n{src}"
    )


def test_approve_merge_impl_source_stamps_rebase_auto_resolved_event():
    """``_approve_merge_impl``'s inline auto-resolved ``_notify_user`` call
    must carry event="rebase_auto_resolved" (membership, not file totals),
    and the unrelated ``_mcp_restart_notice`` call site in the same
    function must remain byte-identical (no event= there)."""
    src = inspect.getsource(merge._approve_merge_impl)
    assert _STAMP in src, (
        f"the inline merge-path emit must stamp {_STAMP}; source:\n{src}"
    )
    assert src.count(_STAMP) == 1, (
        f"expected exactly one event= stamp in _approve_merge_impl (the "
        f"auto-resolved call only); source:\n{src}"
    )
    assert _MCP_CALL in src, (
        f"the unrelated _mcp_restart_notice call site must remain "
        f"byte-for-byte unchanged (no event= stamp); source:\n{src}"
    )
    assert _CONDITIONAL_CID in src, (
        f"the correlation_id conditional dict must stay byte-for-byte "
        f"unchanged; source:\n{src}"
    )


def test_no_other_merge_notification_gained_event_kwarg():
    """No OTHER notification in pipeline/merge.py may gain an ``event=``:
    every ``event=`` keyword argument in the module must use the stamped
    name (so any future "rebase auto-resolved" site stamped identically is
    fine, but no differently-named event may appear), and at least the two
    known call sites must be stamped."""
    src = Path(merge.__file__).read_text()
    matches = re.findall(r"\bevent\s*=\s*([\'\"])(.*?)\1", src)
    names = {name for _, name in matches}
    assert names == {EVENT_NAME}, (
        f"every event= kwarg in pipeline/merge.py must be "
        f"{EVENT_NAME!r}; found {sorted(names)!r}"
    )
    assert len(matches) >= 2, (
        f"expected both rebase auto-resolved call sites to be stamped; "
        f"found {len(matches)} event= occurrence(s) in pipeline/merge.py"
    )


# ---------------------------------------------------------------------------
# (b) Behavioral: the emitted records carry event= + unchanged
#     correlation_id, via the REAL persistence JSONL sidecar.
# ---------------------------------------------------------------------------
def test_helper_auto_resolved_record_carries_event_and_correlation_id(
    plan_dir, monkeypatch
):
    """Shared-helper path: the auto-resolved notice must carry
    event="rebase_auto_resolved" AND the story's correlation_id, with the
    free-text message byte-for-byte unchanged."""
    _patch_helper_boundaries(
        monkeypatch, plan_dir, stories={KEY: _story(correlation_id=CID)}
    )

    err, _sha = merge._rebase_and_push_for_merge(
        PLAN, KEY, f"agent/{KEY.lower()}", "/nonexistent-worktree"
    )

    assert err == "", err
    records = _auto_resolved_records(plan_dir)
    assert len(records) == 1, (
        f"expected exactly one rebase auto-resolved record, "
        f"got {_read_records(plan_dir)!r}"
    )
    record = records[0]
    assert record.get("event") == EVENT_NAME, (
        f"the auto-resolved record must carry event={EVENT_NAME!r}; "
        f"got {record!r}"
    )
    assert record.get("correlation_id") == CID, (
        f"the stamped record must keep the unchanged correlation_id "
        f"{CID!r}; got {record!r}"
    )
    assert record.get("message") == _EXPECTED_MESSAGE, (
        f"the free-text message must stay byte-for-byte unchanged; "
        f"got {record.get('message')!r}"
    )


def test_merge_path_auto_resolved_record_carries_event_and_correlation_id(
    plan_dir, monkeypatch
):
    """Inline ``_approve_merge_impl`` path (via the public approve_merge
    entry): the auto-resolved notice must carry
    event="rebase_auto_resolved" AND the story's correlation_id."""
    _write_manifest(
        plan_dir,
        _story(status="parked", review_verdict="APPROVE", correlation_id=CID),
    )
    _patch_merge_boundaries(monkeypatch)

    result = p.approve_merge(PLAN, KEY)

    assert result.get("ok") is True, result
    records = _auto_resolved_records(plan_dir)
    assert records, (
        f"expected the merge-path rebase auto-resolved record, "
        f"got {_read_records(plan_dir)!r}"
    )
    for record in records:
        assert record.get("event") == EVENT_NAME, (
            f"the merge-path auto-resolved record must carry "
            f"event={EVENT_NAME!r}; got {record!r}"
        )
        assert record.get("correlation_id") == CID, (
            f"the stamped record must keep the unchanged correlation_id "
            f"{CID!r}; got {record!r}"
        )
        assert record.get("message") == _EXPECTED_MESSAGE, (
            f"the free-text message must stay byte-for-byte unchanged; "
            f"got {record.get('message')!r}"
        )


# ---------------------------------------------------------------------------
# Boundary cases: legacy manifests and the non-auto-resolved path.
# ---------------------------------------------------------------------------
def test_helper_auto_resolved_legacy_manifest_record_has_no_correlation_id(
    plan_dir, monkeypatch
):
    """Older manifest WITHOUT correlation_id: the conditional dict must stay
    unchanged - the record carries event= but NO correlation_id key at all
    (absent, not None)."""
    _patch_helper_boundaries(monkeypatch, plan_dir, stories={KEY: _story()})

    err, _sha = merge._rebase_and_push_for_merge(
        PLAN, KEY, f"agent/{KEY.lower()}", "/nonexistent-worktree"
    )

    assert err == "", err
    records = _auto_resolved_records(plan_dir)
    assert len(records) == 1, (
        f"expected exactly one rebase auto-resolved record, "
        f"got {_read_records(plan_dir)!r}"
    )
    record = records[0]
    assert record.get("event") == EVENT_NAME, (
        f"the legacy-shape record must still carry event={EVENT_NAME!r}; "
        f"got {record!r}"
    )
    assert "correlation_id" not in record, (
        f"legacy records must keep the absent-key shape (not null): "
        f"{record!r}"
    )


def test_helper_auto_resolved_missing_story_record_has_no_correlation_id(
    plan_dir, monkeypatch
):
    """Boundary: the story key missing from the manifest degrades to the
    legacy shape (``or {}`` lookup) - the notice is still emitted with
    event= and no correlation_id key."""
    _patch_helper_boundaries(monkeypatch, plan_dir, stories={})

    err, _sha = merge._rebase_and_push_for_merge(
        PLAN, KEY, f"agent/{KEY.lower()}", "/nonexistent-worktree"
    )

    assert err == "", err
    records = _auto_resolved_records(plan_dir)
    assert len(records) == 1, (
        f"expected exactly one rebase auto-resolved record even when the "
        f"story is missing from the manifest, got {_read_records(plan_dir)!r}"
    )
    record = records[0]
    assert record.get("event") == EVENT_NAME, record
    assert "correlation_id" not in record, (
        f"a missing story must degrade to the absent-key legacy shape, "
        f"not null: {record!r}"
    )


def test_helper_without_auto_resolution_emits_no_notification(
    plan_dir, monkeypatch
):
    """Boundary: when the rebase result carries no auto_resolved flag, no
    notification is emitted at all - the event= stamp must not leak onto
    other paths."""
    _patch_helper_boundaries(
        monkeypatch, plan_dir, rebase_result={"ok": True}
    )

    err, _sha = merge._rebase_and_push_for_merge(
        PLAN, KEY, f"agent/{KEY.lower()}", "/nonexistent-worktree"
    )

    assert err == "", err
    assert _auto_resolved_records(plan_dir) == [], (
        f"no notification may be emitted when the rebase did not "
        f"auto-resolve; got {_read_records(plan_dir)!r}"
    )