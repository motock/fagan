"""The human resolution records that an unblock changed the story's brief.

``unblock_story`` with a ``note`` appends the note to the story's
``agent_instructions``. The resolution it writes carries ``brief_changed``
so the readiness report can see that the human supplied input. The note's
text is never written to the record: the record holds identifiers, actions
and booleans only.
"""

from __future__ import annotations

import json

import pytest

import pipeline.server as p
from app.identity import Principal
from pipeline import decision_log, plan_approval
from pipeline import persistence as ppers
from pipeline import server_tools_lifecycle as lifecycle
from pipeline import triage_outcomes as to
from pipeline import unpark as unpark_mod
from pipeline.actor import bind_principal
from pipeline.authorization import ENFORCE_ENV
from pipeline.store import FileStore

PLAN = "forge"
STORY = "FORGE-S1"
ALICE = Principal(subject="https://idp#alice", auth_method="oidc")
BASE_BRIEF = "Do the thing."
MARKER = "MARKER-7f3a-do-not-record"


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    monkeypatch.delenv(plan_approval.APPROVAL_ENV, raising=False)
    return d


@pytest.fixture
def notices(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(
        unpark_mod,
        "_notify_user",
        lambda plan, message, **kw: seen.append(
            {"plan": plan, "message": message, **kw}
        ),
    )
    return seen


def _write() -> None:
    story = {
        "summary": "s",
        "status": "parked",
        "agent_instructions": BASE_BRIEF,
        "files": ["a.py"],
        "rework_attempts": 3,
        "parked_reason": "x",
    }
    FileStore().save_manifest(
        PLAN, {"plan": PLAN, "owner": "https://idp#alice", "stories": {STORY: story}}
    )


def _story() -> dict:
    return FileStore().get_manifest(PLAN)["stories"][STORY]


def _resolutions() -> list[dict]:
    return [
        r
        for r in decision_log.read_decisions(PLAN)
        if r.get("question") == "human resolution"
    ]


def _unblock(**kwargs) -> dict:
    args = {"plan_name": PLAN, "story_key": STORY, "clear_fields": ["rework_attempts"]}
    args.update(kwargs)
    with bind_principal(ALICE):
        return lifecycle.unblock_story(**args)


# --------------------------------------------------------------------------
# record_human_resolution(brief_changed=...)
# --------------------------------------------------------------------------


def test_brief_changed_true_is_written_to_the_record(plan_dir):
    _write()
    record = to.record_human_resolution(
        PLAN,
        STORY,
        human_action="other",
        new_status="interrupted",
        principal="alice",
        brief_changed=True,
    )
    assert record["brief_changed"] is True
    assert _resolutions()[-1]["brief_changed"] is True


def test_brief_changed_defaults_to_no_key_in_the_record(plan_dir):
    _write()
    record = to.record_human_resolution(
        PLAN,
        STORY,
        human_action="other",
        new_status="interrupted",
        principal="alice",
    )
    assert "brief_changed" not in record
    assert "brief_changed" not in _resolutions()[-1]


# --------------------------------------------------------------------------
# unblock_story with a note
# --------------------------------------------------------------------------


def test_unblock_with_a_note_records_brief_changed_without_the_note_text(
    plan_dir, notices
):
    _write()
    result = _unblock(note=f"the premise was wrong: {MARKER}")
    assert result["ok"] is True
    assert MARKER in _story()["agent_instructions"]
    records = _resolutions()
    assert len(records) == 1
    assert records[0]["brief_changed"] is True
    assert MARKER not in json.dumps(records[0])


def test_note_text_is_absent_from_the_whole_decisions_stream(plan_dir, notices):
    _write()
    _unblock(note=f"rewritten brief {MARKER}")
    stream = decision_log.read_decisions(PLAN)
    assert MARKER not in json.dumps(stream)


def test_unblock_without_a_note_records_no_brief_changed(plan_dir, notices):
    _write()
    _unblock(status="interrupted")
    records = _resolutions()
    assert len(records) == 1
    assert "brief_changed" not in records[0]


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_unblock_with_a_blank_note_records_no_brief_changed(plan_dir, notices, blank):
    _write()
    _unblock(note=blank)
    records = _resolutions()
    assert len(records) == 1
    assert "brief_changed" not in records[0]
    assert _story()["agent_instructions"] == BASE_BRIEF
