"""Ingest warns when a brief demands a security review the pipeline will not run.

The pipeline's security-engineer pass runs only for ``risk: "high"`` stories.
A brief that says "security-engineer review required" on a low or medium story
is therefore an unenforced requirement; ingest posts an advisory notice (never
blocks). Drives the real ``pipeline.server.ingest_plan``; only the notification
seam is stubbed.
"""

import json
from pathlib import Path

import pytest

import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import ingest as ingest_mod
from pipeline import persistence as ppers
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _clear_caches,
    _isolate_usage_state,
)

_REFERENCE = (Path(__file__).resolve().parents[2] / "REFERENCE.md").read_text()
_EVENT = "security_review_unenforced"


@pytest.fixture
def plans(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


@pytest.fixture
def notices(monkeypatch):
    calls = []
    monkeypatch.setattr(ingest_mod, "_notify_user", lambda *a, **k: calls.append((a, k)))
    return calls


def _ingest(plans, repo, *, risk, brief, summary="Rotate the signing key"):
    story = {"key": "SEC-1", "summary": summary, "agent_instructions": brief, "risk": risk}
    plan = {"repo_root": str(repo), "epics": [{"summary": "E1", "stories": [story]}]}
    (plans / "secplan.json").write_text(json.dumps(plan))
    return p.ingest_plan("secplan")


def _unenforced(calls):
    return [(a, k) for a, k in calls if k.get("event") == _EVENT]


def test_a_low_risk_story_that_requires_a_security_review_gets_a_notice(
    plans, notices, tmp_path
):
    result = _ingest(
        plans, tmp_path, risk="low", brief="Add login. A security-engineer review is required before merge."
    )

    assert result["ok"] is True
    found = _unenforced(notices)
    assert len(found) == 1
    assert found[0][1]["story_key"] == "SEC-1"


def test_the_notice_says_the_pass_runs_only_for_high_risk(plans, notices, tmp_path):
    _ingest(plans, tmp_path, risk="medium", brief="Requires a Security Review before merge.")

    text = _unenforced(notices)[0][0][1]
    assert text.startswith("SEC-1: ")
    assert "risk" in text
    assert "high" in text


def test_a_high_risk_story_gets_no_notice(plans, notices, tmp_path):
    _ingest(plans, tmp_path, risk="high", brief="A security review is required before merge.")

    assert _unenforced(notices) == []


def test_a_brief_that_does_not_mention_a_security_review_gets_no_notice(
    plans, notices, tmp_path
):
    _ingest(plans, tmp_path, risk="low", brief="Add a pagination cursor to the list endpoint.")

    assert _unenforced(notices) == []


def test_a_security_review_named_only_in_the_summary_still_gets_a_notice(
    plans, notices, tmp_path
):
    _ingest(
        plans, tmp_path, risk="low", brief="Do the thing.",
        summary="Session tokens: needs security-engineer review",
    )

    assert len(_unenforced(notices)) == 1


def test_the_notice_never_blocks_the_ingest(plans, notices, tmp_path):
    result = _ingest(plans, tmp_path, risk="low", brief="Security review required.")

    assert result["ok"] is True


def test_reference_documents_the_new_event():
    assert _EVENT in _REFERENCE
