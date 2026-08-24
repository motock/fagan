"""Tests for the opt-in notification rollup on GET /api/plans.

This story adds a small, OPT-IN rollup so the list route (GET /api/plans)
can carry each plan's most recent notification without forcing the per-plan
detail route (GET /api/plans/{plan}) to pay for it twice.

Behavior under test (all in app/dashboard.py):

  * `_plan_summary` gains a keyword-only parameter
    `include_notification_summary: bool = False`.
  * When True, the returned dict gains a `"latest_notification"` key whose
    value is the LAST element of
    `_collapse_duplicate_notifications(_tail_notification_records(plan_name,
    limit=5))` (or None when that list is empty).
  * When False (the default), the key is absent entirely - the dict shape
    is byte-for-byte identical to today for every existing caller.
  * `list_plans` (the GET /api/plans handler) passes
    `include_notification_summary=True`; the per-plan detail route does NOT
    pass it (it already returns the full `notification_records` list).
  * The new field must never 500 the plan list: a missing or corrupt
    notifications.jsonl resolves to None rather than raising.

These tests follow the same plan-directory fixture pattern as
tests/unit/test_dashboard.py: a temp PLAN_DIR with a manifest.json and a
`<plan>.notifications.jsonl` file, exercised through the FastAPI
TestClient.
"""
import inspect
import json

import pytest
from fastapi.testclient import TestClient

from app import dashboard as d

# ---------------------------------------------------------------------------
# Fixtures (mirror tests/unit/test_dashboard.py)
# ---------------------------------------------------------------------------


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


def _write_notifications(plan_dir, name, records):
    """Write a `<name>.notifications.jsonl` file, one JSON object per line."""
    path = plan_dir / f"{name}.notifications.jsonl"
    lines = [json.dumps(rec) for rec in records]
    path.write_text("\n".join(lines) + ("\n" if lines else ""))


def _notification_record(ts, message="m", severity="info", story_key=None,
                         event=None, dedup_key=None):
    """Build a raw notification record matching the jsonl on-disk shape."""
    return {
        "ts": ts,
        "message": message,
        "severity": severity,
        "story_key": story_key,
        "event": event,
        "dedup_key": dedup_key,
    }


# ---------------------------------------------------------------------------
# Signature requirement: keyword-only include_notification_summary param
# ---------------------------------------------------------------------------


def test_plan_summary_has_keyword_only_include_notification_summary_param():
    """`_plan_summary` must accept a keyword-only `include_notification_summary`
    parameter defaulting to False."""
    sig = inspect.signature(d._plan_summary)
    assert "include_notification_summary" in sig.parameters
    param = sig.parameters["include_notification_summary"]
    assert param.default is False
    # Keyword-only: KIND is KEYWORD_ONLY (not POSITIONAL_OR_KEYWORD).
    assert param.kind == inspect.Parameter.KEYWORD_ONLY


# ---------------------------------------------------------------------------
# GET /api/plans - happy path: latest_notification is the most recent record
# ---------------------------------------------------------------------------


def test_list_plans_latest_notification_is_most_recent_record(plan_dir, client):
    plan = "demo"
    _write_manifest(plan_dir, plan, {"S1": {"summary": "x", "status": "todo"}})
    records = [
        _notification_record("2024-01-01T00:00:00Z", message="oldest"),
        _notification_record("2024-01-02T00:00:00Z", message="middle"),
        _notification_record("2024-01-03T00:00:00Z", message="newest"),
    ]
    _write_notifications(plan_dir, plan, records)

    res = client.get("/api/plans")
    assert res.status_code == 200
    plans = res.json()["plans"]
    assert len(plans) == 1
    entry = plans[0]
    assert "latest_notification" in entry

    # The expected value is the LAST element of the collapsed tail - compute
    # it from the same helpers the implementation must call, so the test
    # does not re-invent the record shape.
    collapsed = d._collapse_duplicate_notifications(
        d._tail_notification_records(plan, limit=5)
    )
    assert collapsed, "expected non-empty collapsed notifications"
    expected = collapsed[-1]
    assert entry["latest_notification"] == expected

    # The most recent record's message must surface (sanity check on ordering).
    assert entry["latest_notification"]["message"] == "newest"
    assert entry["latest_notification"]["ts"] == "2024-01-03T00:00:00Z"


# ---------------------------------------------------------------------------
# GET /api/plans - negative: no notifications.jsonl file at all
# ---------------------------------------------------------------------------


def test_list_plans_latest_notification_none_when_no_notifications_file(
    plan_dir, client
):
    plan = "silent"
    _write_manifest(plan_dir, plan, {"S1": {"summary": "x", "status": "todo"}})
    # Intentionally do NOT create <plan>.notifications.jsonl.

    res = client.get("/api/plans")
    assert res.status_code == 200, "must not 500 when notifications file absent"
    plans = res.json()["plans"]
    assert len(plans) == 1
    assert plans[0]["latest_notification"] is None


# ---------------------------------------------------------------------------
# GET /api/plans - negative: empty notifications.jsonl (zero lines)
# ---------------------------------------------------------------------------


def test_list_plans_latest_notification_none_when_notifications_file_empty(
    plan_dir, client
):
    plan = "blank"
    _write_manifest(plan_dir, plan, {"S1": {"summary": "x", "status": "todo"}})
    # Create the file but write nothing into it.
    (plan_dir / f"{plan}.notifications.jsonl").write_text("")

    res = client.get("/api/plans")
    assert res.status_code == 200, "must not 500 when notifications file empty"
    plans = res.json()["plans"]
    assert len(plans) == 1
    assert plans[0]["latest_notification"] is None


# ---------------------------------------------------------------------------
# GET /api/plans - negative: corrupt JSON line must not 500
# ---------------------------------------------------------------------------


def test_list_plans_latest_notification_none_when_notifications_corrupt(
    plan_dir, client
):
    """A notifications.jsonl full of unparseable lines must resolve the new
    field to None rather than 500ing the whole plan list. _tail_notification_records
    already skips bad JSON lines, so the collapsed list is empty -> None."""
    plan = "corrupt"
    _write_manifest(plan_dir, plan, {"S1": {"summary": "x", "status": "todo"}})
    (plan_dir / f"{plan}.notifications.jsonl").write_text(
        "not json at all\n{also broken\n"
    )

    res = client.get("/api/plans")
    assert res.status_code == 200, "must not 500 on corrupt notifications file"
    plans = res.json()["plans"]
    assert len(plans) == 1
    assert plans[0]["latest_notification"] is None


# ---------------------------------------------------------------------------
# Default behavior: _plan_summary without the new param omits the key
# ---------------------------------------------------------------------------


def test_plan_summary_default_omits_latest_notification_key(plan_dir):
    """When include_notification_summary is not passed (the default), the
    returned dict must NOT contain a `latest_notification` key at all - the
    shape must be byte-for-byte identical to today for every existing
    caller that doesn't pass the new parameter."""
    plan = "demo"
    _write_manifest(plan_dir, plan, {"S1": {"summary": "x", "status": "todo"}})
    _write_notifications(
        plan_dir, plan,
        [_notification_record("2024-01-01T00:00:00Z", message="hi")],
    )

    summary = d._plan_summary(plan, {"epics": {}, "stories": {}})
    assert "latest_notification" not in summary, (
        "default _plan_summary must not add latest_notification"
    )


def test_plan_summary_explicit_false_omits_latest_notification_key(plan_dir):
    """Passing include_notification_summary=False explicitly must also omit
    the key (identical to the default)."""
    plan = "demo"
    _write_manifest(plan_dir, plan, {"S1": {"summary": "x", "status": "todo"}})
    _write_notifications(
        plan_dir, plan,
        [_notification_record("2024-01-01T00:00:00Z", message="hi")],
    )

    summary = d._plan_summary(
        plan, {"epics": {}, "stories": {}}, include_notification_summary=False
    )
    assert "latest_notification" not in summary


def test_plan_summary_explicit_true_adds_latest_notification_key(plan_dir):
    """Passing include_notification_summary=True must add the key."""
    plan = "demo"
    _write_manifest(plan_dir, plan, {"S1": {"summary": "x", "status": "todo"}})
    _write_notifications(
        plan_dir, plan,
        [_notification_record("2024-01-01T00:00:00Z", message="hi")],
    )

    summary = d._plan_summary(
        plan, {"epics": {}, "stories": {}}, include_notification_summary=True
    )
    assert "latest_notification" in summary
    assert summary["latest_notification"] is not None


# ---------------------------------------------------------------------------
# Regression guard: per-plan detail route does NOT gain latest_notification
# ---------------------------------------------------------------------------


def test_get_plan_detail_route_has_no_latest_notification_key(plan_dir, client):
    """GET /api/plans/{plan_name} already returns the full `notification_records`
    list under its own existing key; it must NOT also gain a top-level
    `latest_notification` key on its plan-summary fields (no redundant I/O)."""
    plan = "demo"
    _write_manifest(plan_dir, plan, {"S1": {"summary": "x", "status": "todo"}})
    _write_notifications(
        plan_dir, plan,
        [
            _notification_record("2024-01-01T00:00:00Z", message="a"),
            _notification_record("2024-01-02T00:00:00Z", message="b"),
        ],
    )

    res = client.get(f"/api/plans/{plan}")
    assert res.status_code == 200
    body = res.json()
    # The detail route must still carry the full notification_records list...
    assert "notification_records" in body
    assert isinstance(body["notification_records"], list)
    assert body["notification_records"], "detail route still returns full list"
    # ...but must NOT add the new rollup key.
    assert "latest_notification" not in body, (
        "per-plan detail route must not duplicate the rollup field"
    )


# ---------------------------------------------------------------------------
# Boundary: a single notification record
# ---------------------------------------------------------------------------


def test_list_plans_latest_notification_single_record(plan_dir, client):
    """Boundary: exactly one notification record -> latest_notification is
    that record (the last element of a one-element collapsed list)."""
    plan = "solo"
    _write_manifest(plan_dir, plan, {"S1": {"summary": "x", "status": "todo"}})
    _write_notifications(
        plan_dir, plan,
        [_notification_record("2024-01-01T00:00:00Z", message="only")],
    )

    res = client.get("/api/plans")
    assert res.status_code == 200
    plans = res.json()["plans"]
    assert len(plans) == 1
    latest = plans[0]["latest_notification"]
    assert latest is not None
    assert latest["message"] == "only"
    assert latest["ts"] == "2024-01-01T00:00:00Z"