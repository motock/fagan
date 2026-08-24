import json
from pathlib import Path

import pytest

from pipeline.server import FileStore, PipelineService


# Helper to set up plan dir
@pytest.fixture
def plan_dir(tmp_path):
    # monkeypatch PLAN_DIR to tmp_path
    from pipeline import server
    server.PLAN_DIR = tmp_path
    return tmp_path

# Helper to write file
def write_file(path: Path, content: str, encoding: str = "utf-8"):
    path.write_text(content, encoding=encoding)

# Test happy path for notifications log
def test_get_notifications_happy(plan_dir):
    plan = "testplan"
    log_path = plan_dir / f"{plan}.notifications.log"
    lines = [f"line {i}" for i in range(150)]
    write_file(log_path, "\n".join(lines))
    store = FileStore()
    assert store.get_notifications(plan) == lines[-100:]

# Test missing notifications log
def test_get_notifications_missing(plan_dir):
    store = FileStore()
    assert store.get_notifications("missing") == []

# Test non-utf8 notifications log
def test_get_notifications_non_utf8(plan_dir):
    plan = "utf8"
    log_path = plan_dir / f"{plan}.notifications.log"
    write_file(log_path, "\x80\x81", encoding="latin1")
    store = FileStore()
    # read_text with errors=replace will produce replacement chars
    assert store.get_notifications(plan) == ["��"]

# Test notification records JSONL happy
def test_get_notification_records_happy(plan_dir):
    plan = "rec"
    jsonl_path = plan_dir / f"{plan}.notifications.jsonl"
    records = [
        {"ts": "1", "message": "msg", "severity": "info", "story_key": "k", "event": "e", "dedup_key": "d"},
        {"ts": "2", "message": "msg2", "severity": "warning", "story_key": "k2", "event": "e2", "dedup_key": "d2"},
    ]
    write_file(jsonl_path, "\n".join(json.dumps(r) for r in records))
    store = FileStore()
    assert store.get_notification_records(plan) == records

# Test notification records malformed JSON
def test_get_notification_records_malformed(plan_dir):
    plan = "bad"
    jsonl_path = plan_dir / f"{plan}.notifications.jsonl"
    write_file(jsonl_path, "{bad json}\n")
    store = FileStore()
    assert store.get_notification_records(plan) == []

# Test decisions happy
def test_get_decisions_happy(plan_dir):
    plan = "dec"
    dec_path = plan_dir / f"{plan}.decisions.json"
    decisions = [{"question": "q", "answer": "a"}]
    write_file(dec_path, json.dumps(decisions))
    store = FileStore()
    assert store.get_decisions(plan) == decisions

# Test decisions missing
def test_get_decisions_missing(plan_dir):
    store = FileStore()
    assert store.get_decisions("missing") == []

# Test get_manifest_or_none happy
def test_get_manifest_or_none_happy(plan_dir):
    plan = "man"
    manifest_path = plan_dir / f"{plan}.manifest.json"
    data = {"foo": "bar"}
    write_file(manifest_path, json.dumps(data))
    store = FileStore()
    assert store.get_manifest_or_none(plan) == data

# Test get_manifest_or_none missing
def test_get_manifest_or_none_missing(plan_dir):
    store = FileStore()
    assert store.get_manifest_or_none("missing") is None

# Test get_manifest_or_none malformed
def test_get_manifest_or_none_malformed(plan_dir):
    plan = "badman"
    manifest_path = plan_dir / f"{plan}.manifest.json"
    write_file(manifest_path, "{bad json}")
    store = FileStore()
    assert store.get_manifest_or_none(plan) is None

# Test PipelineService delegators
def test_pipeline_service_delegators(plan_dir):
    svc = PipelineService()
    plan = "svc"
    # create files
    write_file(plan_dir / f"{plan}.notifications.log", "a\nb\nc")
    write_file(plan_dir / f"{plan}.notifications.jsonl", json.dumps({"ts": "1", "message": "m", "severity": "info", "story_key": "k", "event": "e", "dedup_key": "d"}))
    write_file(plan_dir / f"{plan}.decisions.json", json.dumps([{"q": "x"}]))
    write_file(plan_dir / f"{plan}.manifest.json", json.dumps({"x": 1}))
    assert svc.get_notifications(plan) == ["a", "b", "c"]
    assert svc.get_notification_records(plan) == [{"ts": "1", "message": "m", "severity": "info", "story_key": "k", "event": "e", "dedup_key": "d"}]
    assert svc.get_decisions(plan) == [{"q": "x"}]
    assert svc.get_manifest_or_none(plan) == {"x": 1}
