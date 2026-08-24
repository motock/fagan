"""Tests for the view-layer duplicate-notification collapsing added to
``app/dashboard.py``.

A new pure helper ``_collapse_duplicate_notifications`` sits next to
``_tail_notification_records`` and is wired at the single call site so the
endpoint returns collapsed records (tail FIRST, then collapse). The JSONL
file on disk keeps every record; this is a view-layer transform only.

The implementation does not exist yet on this branch, so this file is
intentionally RED until a later dispatch adds it.
"""
import copy
import json

import pytest
from fastapi.testclient import TestClient

from app import dashboard as d

# --- fixtures (mirrors tests/unit/test_dashboard_notification_records.py) -


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "PLAN_DIR", tmp_path)
    from pipeline import server as _srv
    monkeypatch.setattr(_srv, "PLAN_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def client():
    return TestClient(d.app)


def _write_manifest(plan_dir, name, stories, epics=None, paused=False):
    manifest = {"epics": epics or {}, "stories": stories}
    if paused:
        manifest["paused"] = True
    (plan_dir / f"{name}.manifest.json").write_text(json.dumps(manifest))


def _write_jsonl(plan_dir, name, records):
    lines = []
    for rec in records:
        if isinstance(rec, str):
            lines.append(rec)
        else:
            lines.append(json.dumps(rec))
    (plan_dir / f"{name}.notifications.jsonl").write_text("\n".join(lines) + "\n")


def _rec(ts, message, severity="info", dedup_key=None, story_key="S1",
         event="dispatched"):
    return {
        "ts": ts,
        "message": message,
        "severity": severity,
        "story_key": story_key,
        "event": event,
        "dedup_key": dedup_key,
    }


# --- the helper exists ----------------------------------------------------


def test_collapse_duplicate_notifications_helper_exists():
    assert hasattr(d, "_collapse_duplicate_notifications")
    assert callable(d._collapse_duplicate_notifications)


# --- core collapsing semantics -------------------------------------------


def test_consecutive_duplicates_collapse_with_count():
    records = [
        _rec("t1", "first", dedup_key="k"),
        _rec("t2", "second", dedup_key="k"),
        _rec("t3", "third", dedup_key="k"),
    ]
    out = d._collapse_duplicate_notifications(records)
    assert len(out) == 1
    assert out[0]["count"] == 3


def test_collapsed_entry_keeps_first_ts_and_last_ts():
    records = [
        _rec("t1", "first", dedup_key="k"),
        _rec("t2", "second", dedup_key="k"),
        _rec("t3", "third", dedup_key="k"),
    ]
    out = d._collapse_duplicate_notifications(records)
    assert out[0]["ts"] == "t1"
    assert out[0]["last_ts"] == "t3"


def test_collapsed_entry_takes_latest_message_and_severity():
    records = [
        _rec("t1", "first", severity="info", dedup_key="k"),
        _rec("t2", "second", severity="warning", dedup_key="k"),
        _rec("t3", "third", severity="error", dedup_key="k"),
    ]
    out = d._collapse_duplicate_notifications(records)
    assert out[0]["message"] == "third"
    assert out[0]["severity"] == "error"


def test_non_consecutive_duplicates_do_not_merge():
    """A,A,B,A yields three entries with counts 2,1,1 in that order."""
    records = [
        _rec("t1", "a1", dedup_key="A"),
        _rec("t2", "a2", dedup_key="A"),
        _rec("t3", "b1", dedup_key="B"),
        _rec("t4", "a3", dedup_key="A"),
    ]
    out = d._collapse_duplicate_notifications(records)
    assert [r["count"] for r in out] == [2, 1, 1]
    assert [r["message"] for r in out] == ["a2", "b1", "a3"]
    assert [r["dedup_key"] for r in out] == ["A", "B", "A"]


def test_null_dedup_key_never_collapses():
    records = [
        _rec("t1", "first", dedup_key=None),
        _rec("t2", "second", dedup_key=None),
        _rec("t3", "third", dedup_key=None),
    ]
    out = d._collapse_duplicate_notifications(records)
    assert len(out) == 3
    assert all(r["count"] == 1 for r in out)


def test_empty_dedup_key_never_collapses():
    records = [
        _rec("t1", "first", dedup_key=""),
        _rec("t2", "second", dedup_key=""),
        _rec("t3", "third", dedup_key=""),
    ]
    out = d._collapse_duplicate_notifications(records)
    assert len(out) == 3
    assert all(r["count"] == 1 for r in out)


def test_falsy_dedup_key_does_not_collapse_into_truthy_neighbor():
    """A record with a falsy dedup_key never collapses, even adjacent to a
    truthy one (and vice versa)."""
    records = [
        _rec("t1", "first", dedup_key="k"),
        _rec("t2", "second", dedup_key=None),
        _rec("t3", "third", dedup_key="k"),
    ]
    out = d._collapse_duplicate_notifications(records)
    assert len(out) == 3
    assert [r["count"] for r in out] == [1, 1, 1]


# --- boundary / shape -----------------------------------------------------


def test_uncollapsed_records_still_get_count_and_last_ts():
    records = [_rec("t1", "solo", dedup_key="k")]
    out = d._collapse_duplicate_notifications(records)
    assert len(out) == 1
    assert out[0]["count"] == 1
    assert out[0]["last_ts"] == "t1"
    assert out[0]["ts"] == "t1"


def test_empty_input_returns_empty_list():
    assert d._collapse_duplicate_notifications([]) == []


def test_every_returned_record_carries_count_and_last_ts():
    """Mixed run: every entry, collapsed or not, has int count and str last_ts."""
    records = [
        _rec("t1", "a1", dedup_key="k"),
        _rec("t2", "a2", dedup_key="k"),
        _rec("t3", "b1", dedup_key=None),
        _rec("t4", "c1", dedup_key="j"),
        _rec("t5", "c2", dedup_key="j"),
    ]
    out = d._collapse_duplicate_notifications(records)
    assert len(out) == 3
    for r in out:
        assert "count" in r
        assert "last_ts" in r
        assert isinstance(r["count"], int)
        assert isinstance(r["last_ts"], str)
    assert [r["count"] for r in out] == [2, 1, 2]


def test_collapsed_run_preserves_other_keys_from_first_record():
    """The kept record keeps the first record's non-overridden keys
    (story_key, event) while message/severity come from the last."""
    records = [
        _rec("t1", "first", severity="info", dedup_key="k",
             story_key="S1", event="dispatched"),
        _rec("t2", "second", severity="error", dedup_key="k",
             story_key="S2", event="merged"),
    ]
    out = d._collapse_duplicate_notifications(records)
    assert out[0]["story_key"] == "S1"
    assert out[0]["event"] == "dispatched"
    assert out[0]["message"] == "second"
    assert out[0]["severity"] == "error"


# --- non-mutation ---------------------------------------------------------


def test_input_records_are_not_mutated():
    records = [
        _rec("t1", "first", dedup_key="k"),
        _rec("t2", "second", dedup_key="k"),
        _rec("t3", "third", dedup_key="k"),
    ]
    snapshot = copy.deepcopy(records)
    d._collapse_duplicate_notifications(records)
    assert records == snapshot
    # And specifically: the input dicts must not gain count/last_ts keys.
    for original, snap in zip(records, snapshot):
        assert set(original.keys()) == set(snap.keys())


def test_function_does_not_mutate_input_for_uncollapsed():
    records = [_rec("t1", "solo", dedup_key=None)]
    snapshot = copy.deepcopy(records)
    d._collapse_duplicate_notifications(records)
    assert records == snapshot


# --- wiring at the call site ----------------------------------------------


def test_endpoint_returns_collapsed_records(client, plan_dir):
    """A plan whose JSONL has nine identical warning records returns one
    ``notification_records`` entry with count == 9."""
    _write_manifest(plan_dir, "demo", {"S1": {"summary": "x", "status": "todo"}})
    _write_jsonl(plan_dir, "demo", [
        _rec(f"t{i:02d}", "same", severity="warning", dedup_key="dup")
        for i in range(9)
    ])

    res = client.get("/api/plans/demo")
    assert res.status_code == 200
    records = res.json()["notification_records"]
    assert len(records) == 1
    assert records[0]["count"] == 9
    assert records[0]["ts"] == "t00"
    assert records[0]["last_ts"] == "t08"
    assert records[0]["message"] == "same"
    assert records[0]["severity"] == "warning"


def test_endpoint_collapses_only_consecutive(client, plan_dir):
    """Through the endpoint, A,A,B,A yields three entries with counts 2,1,1."""
    _write_manifest(plan_dir, "demo", {"S1": {"summary": "x", "status": "todo"}})
    _write_jsonl(plan_dir, "demo", [
        _rec("t1", "a1", dedup_key="A"),
        _rec("t2", "a2", dedup_key="A"),
        _rec("t3", "b1", dedup_key="B"),
        _rec("t4", "a3", dedup_key="A"),
    ])

    res = client.get("/api/plans/demo")
    assert res.status_code == 200
    records = res.json()["notification_records"]
    assert [r["count"] for r in records] == [2, 1, 1]


def test_endpoint_raw_notifications_key_is_unchanged(client, plan_dir):
    """The raw ``notifications`` string list is untouched by the collapse."""
    _write_manifest(plan_dir, "demo", {"S1": {"summary": "x", "status": "todo"}})
    (plan_dir / "demo.notifications.log").write_text(
        "2026-06-25T00:00:00+00:00 first note\n"
        "2026-06-25T00:01:00+00:00 second note\n"
    )
    _write_jsonl(plan_dir, "demo", [
        _rec("t1", "same", dedup_key="k"),
        _rec("t2", "same", dedup_key="k"),
    ])

    res = client.get("/api/plans/demo")
    assert res.status_code == 200
    body = res.json()
    assert body["notifications"] == [
        "2026-06-25T00:00:00+00:00 first note",
        "2026-06-25T00:01:00+00:00 second note",
    ]
    assert len(body["notification_records"]) == 1


def test_endpoint_empty_jsonl_returns_empty_list(client, plan_dir):
    _write_manifest(plan_dir, "empty", {"S1": {"summary": "x", "status": "todo"}})
    (plan_dir / "empty.notifications.jsonl").write_text("")

    res = client.get("/api/plans/empty")
    assert res.status_code == 200
    assert res.json()["notification_records"] == []


# --- source-level guardrails (anchored on the task's exact instructions) --


def test_call_site_wires_collapse_after_tail():
    """The single call site must wrap the notification-records read with
    _collapse_duplicate_notifications (tail first, then collapse)."""
    import inspect
    text = inspect.getsource(d)
    assert (
        '"notification_records": _collapse_duplicate_notifications(\n'
        '            _store.get_notification_records(plan_name)\n'
        '        ),'
    ) in text


def test_tail_notification_records_is_unchanged():
    """The collapse helper must be a NEW function; _tail_notification_records
    itself must not be edited to do collapsing."""
    import inspect
    src = inspect.getsource(d._tail_notification_records)
    assert "count" not in src
    assert "last_ts" not in src
    assert "_collapse_duplicate_notifications" not in src