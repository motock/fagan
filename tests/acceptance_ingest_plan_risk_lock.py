"""Acceptance: re-ingesting a plan must not silently change the `risk`
field of a story that has already progressed past `todo` (dispatched,
reviewed, parked, or done).

`chat-security-hardening` closed the risk-downgrade bypass on the chat
`patch_story` tool (risk cannot be forwarded via chat). But `ingest_plan`
re-applies every field in `_INGEST_AUTHORED_STORY_FIELDS` - including
`risk` - onto an EXISTING story on every re-ingest, even with
overwrite=False, as long as the story's key matches. `_merge_decision`
(pipeline/server.py) reads `risk` live off the manifest at merge time, so
silently downgrading it via `save_plan` + `ingest_plan` reopens the exact
same bypass for a story already parked at `pr_open` with an APPROVE
verdict: the next scheduler tick reads the new, lower risk and auto-merges
work that was supposed to be held for human review. This fixture proves a
re-ingest can no longer move risk once a story is out of `todo`, while
confirming risk edits still work normally before that point (an author
correcting risk before the story has ever been dispatched is legitimate
and must keep working).
"""
import json

import pytest

from pipeline import server as p
from pipeline import ticketing as pt


def _explode_plane(*a, **kw):
    raise AssertionError("Plane should not be called - PLANE_* env is unset in tests")


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    return d


def _read_manifest(plan_dir, plan_name):
    return json.loads((plan_dir / f"{plan_name}.manifest.json").read_text())


def test_reingest_does_not_downgrade_risk_on_an_already_dispatched_story(
    plan_dir, monkeypatch, tmp_path
):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{
            "summary": "E1",
            "stories": [{
                "summary": "Do the thing",
                "key": "S1",
                "risk": "high",
            }],
        }],
    }
    (plan_dir / "lock.json").write_text(json.dumps(plan))
    p.ingest_plan("lock")

    # Simulate real pipeline progress: the story was dispatched, reviewed,
    # and approved, and now sits parked awaiting human merge approval.
    manifest = _read_manifest(plan_dir, "lock")
    manifest["stories"]["S1"]["status"] = "pr_open"
    manifest["stories"]["S1"]["review_verdict"] = "APPROVE"
    (plan_dir / "lock.manifest.json").write_text(json.dumps(manifest))

    # An attacker (or an injected chat completion) re-saves the plan with
    # the same story key but a downgraded risk, then re-ingests it.
    plan["epics"][0]["stories"][0]["risk"] = "low"
    (plan_dir / "lock.json").write_text(json.dumps(plan))
    result = p.ingest_plan("lock")

    assert result["ok"] is True
    merged = _read_manifest(plan_dir, "lock")
    assert merged["stories"]["S1"]["risk"] == "high"
    # The story's genuine runtime progress must still be untouched.
    assert merged["stories"]["S1"]["status"] == "pr_open"
    assert merged["stories"]["S1"]["review_verdict"] == "APPROVE"


def test_reingest_still_allows_risk_edits_before_the_story_is_dispatched(
    plan_dir, monkeypatch, tmp_path
):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{
            "summary": "E1",
            "stories": [{
                "summary": "Do the thing",
                "key": "S1",
                "risk": "low",
            }],
        }],
    }
    (plan_dir / "todo.json").write_text(json.dumps(plan))
    p.ingest_plan("todo")

    # Story is still "todo" (never dispatched) - a plan author legitimately
    # correcting its risk before work starts must still work.
    plan["epics"][0]["stories"][0]["risk"] = "high"
    (plan_dir / "todo.json").write_text(json.dumps(plan))
    result = p.ingest_plan("todo")

    assert result["ok"] is True
    merged = _read_manifest(plan_dir, "todo")
    assert merged["stories"]["S1"]["risk"] == "high"
