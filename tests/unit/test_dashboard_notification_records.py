"""Tests for the get_plan endpoint's new ``notification_records`` key.

These cover the JSONL-backed structured notification panel added to
``app/dashboard.py``: a new ``_tail_notification_records`` helper reads
``<plan>.notifications.jsonl`` line-by-line, normalizes each surviving
record to a fixed six-key shape, and returns the last ``limit`` records
in chronological order. The endpoint exposes them alongside (never
replacing) the existing raw ``notifications`` string list.

The implementation does not exist yet on this branch, so this file is
intentionally RED until a later dispatch adds it.
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import dashboard as d

# --- fixtures (mirrors tests/unit/test_dashboard.py) ----------------------


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
    """Write records (dicts or raw strings) as one JSON-per-line JSONL."""
    lines = []
    for rec in records:
        if isinstance(rec, str):
            lines.append(rec)
        else:
            lines.append(json.dumps(rec))
    (plan_dir / f"{name}.notifications.jsonl").write_text("\n".join(lines) + "\n")


# --- the new helper exists and is wired -----------------------------------


def test_notifications_jsonl_path_helper_exists():
    """The tiny path helper is duplicated next to _notifications_path."""
    assert hasattr(d, "_notifications_jsonl_path")
    path = d._notifications_jsonl_path("demo")
    assert isinstance(path, Path)
    assert path == d.PLAN_DIR / "demo.notifications.jsonl"


def test_tail_notification_records_helper_exists():
    assert hasattr(d, "_tail_notification_records")


def test_get_plan_returns_notification_records(client, plan_dir):
    """A plan with two JSONL records returns both under
    ``notification_records``, each carrying exactly the six normalized keys."""
    _write_manifest(plan_dir, "demo", {"S1": {"summary": "x", "status": "todo"}})
    _write_jsonl(plan_dir, "demo", [
        {"ts": "2026-06-25T00:00:00+00:00", "message": "first",
         "severity": "info", "story_key": "S1", "event": "dispatched",
         "dedup_key": "d1"},
        {"ts": "2026-06-25T00:01:00+00:00", "message": "second",
         "severity": "warning", "story_key": "S2", "event": "merged",
         "dedup_key": "d2"},
    ])

    res = client.get("/api/plans/demo")
    assert res.status_code == 200
    records = res.json()["notification_records"]
    assert len(records) == 2

    # P3-7's view-layer collapse adds count/last_ts to every record;
    # these two have distinct dedup_keys so none merge, but each still
    # carries count=1 and last_ts==ts.
    expected_keys = {"ts", "message", "severity", "story_key",
                     "event", "dedup_key", "count", "last_ts"}
    for rec in records:
        assert set(rec.keys()) == expected_keys
        assert rec["count"] == 1
        assert rec["last_ts"] == rec["ts"]

    assert records[0]["message"] == "first"
    assert records[0]["severity"] == "info"
    assert records[0]["story_key"] == "S1"
    assert records[0]["event"] == "dispatched"
    assert records[0]["dedup_key"] == "d1"

    assert records[1]["message"] == "second"
    assert records[1]["severity"] == "warning"
    assert records[1]["story_key"] == "S2"
    assert records[1]["event"] == "merged"
    assert records[1]["dedup_key"] == "d2"


def test_missing_jsonl_returns_empty_list(client, plan_dir):
    """A plan with no .notifications.jsonl returns ``notification_records``
    == [] (and must not 500)."""
    _write_manifest(plan_dir, "bare", {"S1": {"summary": "x", "status": "todo"}})
    res = client.get("/api/plans/bare")
    assert res.status_code == 200
    assert res.json()["notification_records"] == []


def test_malformed_line_is_skipped(client, plan_dir):
    """A malformed JSON line is skipped, never fatal: valid, broken, valid
    yields two records and no 500."""
    _write_manifest(plan_dir, "mixed", {"S1": {"summary": "x", "status": "todo"}})
    _write_jsonl(plan_dir, "mixed", [
        {"ts": "t0", "message": "ok0", "severity": "info"},
        "{not json",
        {"ts": "t2", "message": "ok2", "severity": "info"},
    ])

    res = client.get("/api/plans/mixed")
    assert res.status_code == 200
    records = res.json()["notification_records"]
    assert len(records) == 2
    assert records[0]["message"] == "ok0"
    assert records[1]["message"] == "ok2"


def test_non_dict_line_is_skipped(client, plan_dir):
    """A line that parses to a non-dict (e.g. a JSON array) is skipped."""
    _write_manifest(plan_dir, "nd", {"S1": {"summary": "x", "status": "todo"}})
    _write_jsonl(plan_dir, "nd", [
        "[1,2,3]",
        {"ts": "t1", "message": "keep", "severity": "info"},
    ])

    res = client.get("/api/plans/nd")
    assert res.status_code == 200
    records = res.json()["notification_records"]
    assert len(records) == 1
    assert records[0]["message"] == "keep"


def test_unknown_severity_is_normalized_to_info(client, plan_dir):
    """A severity outside info/warning/error is normalized to 'info' so the
    UI colour-coding never receives an unknown value."""
    _write_manifest(plan_dir, "sev", {"S1": {"summary": "x", "status": "todo"}})
    _write_jsonl(plan_dir, "sev", [
        {"ts": "t0", "message": "boom", "severity": "catastrophic"},
    ])

    res = client.get("/api/plans/sev")
    assert res.status_code == 200
    records = res.json()["notification_records"]
    assert len(records) == 1
    assert records[0]["severity"] == "info"


def test_missing_keys_get_defaults(client, plan_dir):
    """A record containing only ``{"message": "m"}`` comes back with
    severity 'info' and story_key/event/dedup_key all None, plus ts ''."""
    _write_manifest(plan_dir, "def", {"S1": {"summary": "x", "status": "todo"}})
    _write_jsonl(plan_dir, "def", [{"message": "m"}])

    res = client.get("/api/plans/def")
    assert res.status_code == 200
    records = res.json()["notification_records"]
    assert len(records) == 1
    rec = records[0]
    assert set(rec.keys()) == {"ts", "message", "severity", "story_key",
                               "event", "dedup_key", "count", "last_ts"}
    assert rec["ts"] == ""
    assert rec["count"] == 1
    assert rec["last_ts"] == ""
    assert rec["message"] == "m"
    assert rec["severity"] == "info"
    assert rec["story_key"] is None
    assert rec["event"] is None
    assert rec["dedup_key"] is None


def test_records_are_tailed_to_limit(client, plan_dir):
    """150 records return the last 100, oldest-first, newest last."""
    _write_manifest(plan_dir, "chatty", {"S1": {"summary": "x", "status": "todo"}})
    _write_jsonl(plan_dir, "chatty", [
        {"ts": f"t{i:03d}", "message": f"m{i}", "severity": "info"}
        for i in range(150)
    ])

    res = client.get("/api/plans/chatty")
    assert res.status_code == 200
    records = res.json()["notification_records"]
    assert len(records) == 100
    # Last 100 of 0..149 -> indices 50..149, oldest-first.
    assert records[0]["message"] == "m50"
    assert records[-1]["message"] == "m149"
    # Chronological order preserved.
    assert records[0]["ts"] == "t050"
    assert records[-1]["ts"] == "t149"


def test_blank_lines_are_skipped(client, plan_dir):
    """Blank lines in the file are skipped, not treated as records."""
    _write_manifest(plan_dir, "blank", {"S1": {"summary": "x", "status": "todo"}})
    (plan_dir / "blank.notifications.jsonl").write_text(
        "\n\n"
        + json.dumps({"ts": "t0", "message": "only", "severity": "info"})
        + "\n\n\n"
    )

    res = client.get("/api/plans/blank")
    assert res.status_code == 200
    records = res.json()["notification_records"]
    assert len(records) == 1
    assert records[0]["message"] == "only"


def test_severity_values_pass_through(client, plan_dir):
    """The three known severities pass through unchanged."""
    _write_manifest(plan_dir, "sevs", {"S1": {"summary": "x", "status": "todo"}})
    _write_jsonl(plan_dir, "sevs", [
        {"ts": "t0", "message": "a", "severity": "info"},
        {"ts": "t1", "message": "b", "severity": "warning"},
        {"ts": "t2", "message": "c", "severity": "error"},
    ])

    res = client.get("/api/plans/sevs")
    assert res.status_code == 200
    records = res.json()["notification_records"]
    assert [r["severity"] for r in records] == ["info", "warning", "error"]


def test_consecutive_duplicates_are_collapsed_by_dedup_key(client, plan_dir):
    """Consecutive records sharing a truthy dedup_key collapse into one
    entry carrying count and last_ts (P3-7 view-layer dedup)."""
    _write_manifest(plan_dir, "dups", {"S1": {"summary": "x", "status": "todo"}})
    _write_jsonl(plan_dir, "dups", [
        {"ts": "t0", "message": "same", "severity": "info", "dedup_key": "k"},
        {"ts": "t1", "message": "same", "severity": "info", "dedup_key": "k"},
        {"ts": "t2", "message": "same", "severity": "info", "dedup_key": "k"},
    ])

    res = client.get("/api/plans/dups")
    assert res.status_code == 200
    records = res.json()["notification_records"]
    assert len(records) == 1
    assert records[0]["count"] == 3
    assert records[0]["ts"] == "t0"
    assert records[0]["last_ts"] == "t2"
    assert records[0]["message"] == "same"
    assert records[0]["severity"] == "info"
    assert records[0]["dedup_key"] == "k"


def test_raw_notifications_key_is_unchanged(client, plan_dir):
    """The existing ``notifications`` key still returns the raw string list
    read from the .log file, unchanged by the new addition."""
    _write_manifest(plan_dir, "demo", {"S1": {"summary": "x", "status": "todo"}})
    (plan_dir / "demo.notifications.log").write_text(
        "2026-06-25T00:00:00+00:00 first note\n"
        "2026-06-25T00:01:00+00:00 second note\n"
    )
    _write_jsonl(plan_dir, "demo", [
        {"ts": "2026-06-25T00:00:00+00:00", "message": "first", "severity": "info"},
    ])

    res = client.get("/api/plans/demo")
    assert res.status_code == 200
    body = res.json()
    assert body["notifications"] == [
        "2026-06-25T00:00:00+00:00 first note",
        "2026-06-25T00:01:00+00:00 second note",
    ]
    # And the new key coexists.
    assert "notification_records" in body
    assert len(body["notification_records"]) == 1


def test_empty_jsonl_file_returns_empty_list(client, plan_dir):
    """A present-but-empty .notifications.jsonl returns [] and does not 500."""
    _write_manifest(plan_dir, "empty", {"S1": {"summary": "x", "status": "todo"}})
    (plan_dir / "empty.notifications.jsonl").write_text("")

    res = client.get("/api/plans/empty")
    assert res.status_code == 200
    assert res.json()["notification_records"] == []


def test_helper_directly_missing_file_returns_empty_list(plan_dir):
    """The helper itself returns [] for a missing file (unit-level)."""
    assert d._tail_notification_records("nope") == []


def test_helper_directly_tails_to_limit(plan_dir):
    """The helper returns the last `limit` records, oldest-first."""
    _write_jsonl(plan_dir, "h", [
        {"ts": f"t{i:03d}", "message": f"m{i}", "severity": "info"}
        for i in range(10)
    ])
    out = d._tail_notification_records("h", limit=3)
    assert [r["message"] for r in out] == ["m7", "m8", "m9"]


def test_helper_directly_normalizes_unknown_severity(plan_dir):
    _write_jsonl(plan_dir, "h", [{"message": "x", "severity": "boom"}])
    out = d._tail_notification_records("h")
    assert out[0]["severity"] == "info"


def test_helper_directly_skips_non_dict(plan_dir):
    _write_jsonl(plan_dir, "h", ["[1,2,3]", json.dumps({"message": "keep"})])
    out = d._tail_notification_records("h")
    assert len(out) == 1
    assert out[0]["message"] == "keep"