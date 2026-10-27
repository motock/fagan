"""AUD-05: merges and plan pause/resume land in the audit stream.

``pipeline.merge._approve_merge_impl`` and ``pipeline.merge._set_plan_paused``
are the last two state transitions the service layer enforces that were still
invisible to the append-only stream. These tests pin the two records, the fact
that a *denied* pause writes no transition record (the authorization layer owns
that record), and - by source assertion, because a real merge needs gh, git and
CI - that the merge record sits after the manifest write that makes the story
durably ``done``.

``pipeline.audit`` reads ``pipeline.persistence.PLAN_DIR`` at call time and
``pipeline.store`` resolves ``PLAN_DIR`` live through ``pipeline.server``, so
patching both redirects the manifest, the lock and the stream together.
"""

from __future__ import annotations

import inspect
import json

import pytest

from pipeline import merge, persistence, server
from pipeline.actor import ANONYMOUS, bind_principal

#: The AUD-02 sink that records a policy refusal in this tree: the plan's
#: notification sidecar, one ``authorization_denied`` warning per refusal.
DENIAL_EVENT = ("authorization_denied", "warning")


def _write_manifest(tmp_path, paused=False):
    (tmp_path / "p.manifest.json").write_text(json.dumps({"plan": "p", "paused": paused}))


def _records(tmp_path):
    """Read the stream, treating an absent file as an empty one."""
    path = tmp_path / "p.audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture()
def plans(tmp_path, monkeypatch):
    """A throwaway plan directory holding the manifest, the lock and the stream."""
    monkeypatch.delenv("PIPELINE_POLICY_ENFORCE", raising=False)
    monkeypatch.setattr(server, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    _write_manifest(tmp_path)
    return tmp_path


def test_approve_merge_records_audit_after_status_done():
    src = inspect.getsource(merge._approve_merge_impl)
    assert src.index("record_audit(") > src.index('story["status"] = "done"')


def test_set_plan_paused_records_paused(plans):
    merge._set_plan_paused("p", True)
    assert [(r["action"], r["outcome"]) for r in _records(plans)] == [
        ("plan.paused", "allowed")
    ]


def test_set_plan_paused_records_resumed(plans):
    merge._set_plan_paused("p", True)
    merge._set_plan_paused("p", False)
    assert _records(plans)[1]["action"] == "plan.resumed"


def test_denied_pause_records_no_transition(plans, monkeypatch):
    monkeypatch.setenv("PIPELINE_POLICY_ENFORCE", "1")
    with bind_principal(ANONYMOUS):
        result = merge._set_plan_paused("p", True)
    notifications = [
        json.loads(line)
        for line in (plans / "p.notifications.jsonl").read_text().splitlines()
    ]
    assert (
        isinstance(result, dict) and result.get("ok") is False,
        [r["action"] for r in _records(plans) if r["action"].startswith("plan.")],
        [(n["event"], n["severity"]) for n in notifications],
    ) == (True, [], [DENIAL_EVENT])
