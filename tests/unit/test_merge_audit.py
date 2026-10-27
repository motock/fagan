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

The stream is shared. ``pipeline.authorization.authorize`` appends its own
``plan:pause`` record for every decision, allowed or denied, so a pause leaves
two lines behind: the authorization decision and the state transition this
task adds. Assertions therefore filter on the transition actions rather than
assuming the stream holds only one record.
"""

from __future__ import annotations

import inspect
import json

import pytest

from pipeline import merge, persistence, server
from pipeline.actor import ANONYMOUS, bind_principal

#: The transition actions this module records, distinct from the
#: ``plan:pause`` authorization decision that the same call also writes.
TRANSITIONS = ("plan.paused", "plan.resumed")

#: The AUTHORIZATION denial record's action and outcome, as written by
#: ``pipeline.authorization.authorize`` when the policy refuses.
DENIAL_RECORD = ("plan:pause", "denied")


def _write_manifest(tmp_path, paused=False):
    (tmp_path / "p.manifest.json").write_text(json.dumps({"plan": "p", "paused": paused}))


def _records(tmp_path):
    """Read the stream, treating an absent file as an empty one."""
    path = tmp_path / "p.audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _transitions(tmp_path):
    """The pause/resume transition records, in stream order."""
    return [r for r in _records(tmp_path) if r["action"] in TRANSITIONS]


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
    assert [(r["action"], r["outcome"]) for r in _transitions(plans)] == [
        ("plan.paused", "allowed")
    ]


def test_set_plan_paused_records_resumed(plans):
    merge._set_plan_paused("p", True)
    merge._set_plan_paused("p", False)
    assert [r["action"] for r in _transitions(plans)] == ["plan.paused", "plan.resumed"]


def test_denied_pause_records_no_transition(plans, monkeypatch):
    monkeypatch.setenv("PIPELINE_POLICY_ENFORCE", "1")
    with bind_principal(ANONYMOUS):
        result = merge._set_plan_paused("p", True)
    assert (
        isinstance(result, dict) and result.get("ok") is False,
        _transitions(plans),
        [(r["action"], r["outcome"]) for r in _records(plans)],
    ) == (True, [], [DENIAL_RECORD])
