"""The ingest-time "story sizing risk" notice carries an event and its story key.

It was emitted as ``_notify_user(plan, f"{key}: {warning}")`` with no event.
The local-success classifier keyword-matches event-less records, and a
sizing warning for a story that edits ``pipeline/triage.py`` matched the
keyword ``triage`` - so an advisory about a file's size marked a story
"not first-pass clean". The notice now carries ``event="sizing_risk"`` (not a
first-pass disqualifier) and ``story_key``.

Drives the real ``pipeline.server.ingest_plan`` entry point; only the
notification seam is stubbed.
"""

import json

import pytest

import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import ingest as ingest_mod
from pipeline import persistence as ppers
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _clear_caches,
    _isolate_usage_state,
)

PREFLIGHT = "Preflight: test fixture - not a real plan\n"


@pytest.fixture
def sizing_plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", "gpt-oss-20b-high:latest")
    return d


@pytest.fixture
def notices(monkeypatch):
    calls = []
    monkeypatch.setattr(ingest_mod, "_notify_user", lambda *a, **k: calls.append((a, k)))
    return calls


def _ingest(plan_dir, repo, line_count):
    (repo / "pipeline").mkdir()
    (repo / "pipeline" / "triage.py").write_text("x = 1\n" * line_count)
    story = {
        "key": "SZ-1",
        "summary": "edit a big file",
        "agent_instructions": PREFLIGHT + "Do it.",
        "backend": "ollama",
        "model": "glm-5.3-flash:cloud",
        "files": ["pipeline/triage.py"],
    }
    plan = {"repo_root": str(repo), "epics": [{"summary": "E1", "stories": [story]}]}
    (plan_dir / "sizing1.json").write_text(json.dumps(plan))
    return p.ingest_plan("sizing1")


def _sizing(calls):
    return [(a, k) for a, k in calls if len(a) > 1 and "story sizing risk" in a[1]]


def test_the_sizing_notice_carries_the_sizing_risk_event(sizing_plan_dir, notices, tmp_path):
    result = _ingest(sizing_plan_dir, tmp_path, 1200)

    assert result["ok"] is True
    sizing = _sizing(notices)
    assert len(sizing) == 1
    assert sizing[0][1]["event"] == "sizing_risk"


def test_the_sizing_notice_carries_its_story_key(sizing_plan_dir, notices, tmp_path):
    _ingest(sizing_plan_dir, tmp_path, 1200)

    assert _sizing(notices)[0][1]["story_key"] == "SZ-1"


def test_the_sizing_notice_text_is_unchanged(sizing_plan_dir, notices, tmp_path):
    _ingest(sizing_plan_dir, tmp_path, 1200)

    text = _sizing(notices)[0][0][1]
    assert text.startswith("SZ-1: story sizing risk (see .claude/rules/agent-dispatch-story-sizing.md): ")
    assert "edits pipeline/triage.py (1200 lines)" in text


def test_a_small_story_gets_no_sizing_notice(sizing_plan_dir, notices, tmp_path):
    _ingest(sizing_plan_dir, tmp_path, 10)

    assert _sizing(notices) == []
