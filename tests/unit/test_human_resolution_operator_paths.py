"""Every operator path that answers a ruled story records one human resolution.

``patch_story`` rewriting a parked story's brief, ``dispatch_story`` relaunching
it and ``interrupt_story`` stopping it were answers to the overlord's ruling
that wrote nothing. Each now writes one ``human resolution`` beside the latest
ruling, once per ruling: a second answer to the same ruling adds no record.
A path with no ruling on record, or a story that is not parked, records nothing.
"""

from __future__ import annotations

import json

import pytest

from pipeline import persistence as ppers
from pipeline import server as p
from pipeline import unpark as unpark_mod
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _write_manifest,
    plan_dir,
)

PLAN = "hr-paths"
KEY = "S1"
DECIDED = "2026-11-20T10:00:00+00:00"


def _decisions() -> list[dict]:
    path = ppers._decisions_path(PLAN)
    return json.loads(path.read_text()) if path.exists() else []


def _resolutions() -> list[dict]:
    return [r for r in _decisions() if r.get("question") == "human resolution"]


def _seed_ruling(decided_at: str = DECIDED) -> None:
    ppers._append_decision(
        PLAN,
        {
            "story_key": KEY,
            "question": "failure triage",
            "action": "park_for_human",
            "park_class": "rework",
            "decided_by": "overlord-triage",
            "decided_at": decided_at,
        },
    )


def _parked(**extra) -> dict:
    story = {
        "summary": "s",
        "status": "parked",
        "agent_instructions": "Old.",
        "dispatched_at": "2026-11-19T00:00:00+00:00",
    }
    story.update(extra)
    return story


# --------------------------------------------------------------------------
# patch_story: a brief rewrite on a parked story is a rebrief
# --------------------------------------------------------------------------


def test_brief_rewrite_on_parked_story_records_one_rebrief(plan_dir) -> None:  # noqa: F811
    _write_manifest(plan_dir, PLAN, {KEY: _parked()})
    _seed_ruling()

    result = p.patch_story(PLAN, KEY, {"agent_instructions": "New."})

    assert result["ok"] is True
    resolutions = _resolutions()
    assert len(resolutions) == 1
    assert resolutions[0]["human_action"] == "rebrief"
    assert resolutions[0]["brief_changed"] is True
    assert resolutions[0]["ruling_ref"] == DECIDED
    assert resolutions[0]["new_status"] == "parked"


def test_brief_rewrite_without_a_ruling_records_nothing(plan_dir) -> None:  # noqa: F811
    _write_manifest(plan_dir, PLAN, {KEY: _parked()})

    result = p.patch_story(PLAN, KEY, {"agent_instructions": "New."})

    assert result["ok"] is True
    assert _resolutions() == []


def test_brief_rewrite_with_unchanged_text_records_nothing(plan_dir) -> None:  # noqa: F811
    _write_manifest(plan_dir, PLAN, {KEY: _parked()})
    _seed_ruling()

    result = p.patch_story(PLAN, KEY, {"agent_instructions": "Old."})

    assert result["ok"] is True
    assert _resolutions() == []


def test_brief_rewrite_on_a_story_that_is_not_parked_records_nothing(plan_dir) -> None:  # noqa: F811
    _write_manifest(plan_dir, PLAN, {KEY: _parked(status="in_progress")})
    _seed_ruling()

    result = p.patch_story(PLAN, KEY, {"agent_instructions": "New."})

    assert result["ok"] is True
    assert _resolutions() == []


def test_second_brief_rewrite_against_the_same_ruling_adds_no_resolution(plan_dir) -> None:  # noqa: F811
    _write_manifest(plan_dir, PLAN, {KEY: _parked()})
    _seed_ruling()

    p.patch_story(PLAN, KEY, {"agent_instructions": "New."})
    p.patch_story(PLAN, KEY, {"agent_instructions": "Newer."})

    assert len(_resolutions()) == 1


def test_a_rejected_patch_records_nothing(plan_dir) -> None:  # noqa: F811
    _write_manifest(plan_dir, PLAN, {KEY: _parked()})
    _seed_ruling()

    result = p.patch_story(PLAN, KEY, {"not_a_field": "x"})

    assert result["ok"] is False
    assert _resolutions() == []


def test_patch_of_a_field_outside_the_brief_records_nothing(plan_dir) -> None:  # noqa: F811
    _write_manifest(plan_dir, PLAN, {KEY: _parked()})
    _seed_ruling()

    result = p.patch_story(PLAN, KEY, {"risk": "high"})

    assert result["ok"] is True
    assert _resolutions() == []


# --------------------------------------------------------------------------
# dispatch_story: relaunching a parked story is a retry
# --------------------------------------------------------------------------


def _fake_dispatch(plan_dir, monkeypatch, result: dict, status: str = "in_progress"):  # noqa: F811
    def fake(plan_name: str, story_key: str) -> dict:
        manifest = json.loads((plan_dir / f"{plan_name}.manifest.json").read_text())
        manifest["stories"][story_key]["status"] = status
        (plan_dir / f"{plan_name}.manifest.json").write_text(json.dumps(manifest))
        return dict(result)

    monkeypatch.setattr(p, "_dispatch_story_impl", fake)


def test_dispatch_of_parked_story_records_one_retry(plan_dir, monkeypatch) -> None:  # noqa: F811
    _write_manifest(plan_dir, PLAN, {KEY: _parked()})
    _seed_ruling()
    _fake_dispatch(plan_dir, monkeypatch, {"ok": True, "story_key": KEY})

    result = p.dispatch_story(PLAN, KEY)

    assert result["ok"] is True
    resolutions = _resolutions()
    assert len(resolutions) == 1
    assert resolutions[0]["human_action"] == "retry"
    assert resolutions[0]["new_status"] == "in_progress"
    assert resolutions[0]["ruling_ref"] == DECIDED


def test_refused_dispatch_of_parked_story_records_nothing(plan_dir, monkeypatch) -> None:  # noqa: F811
    _write_manifest(plan_dir, PLAN, {KEY: _parked()})
    _seed_ruling()
    _fake_dispatch(plan_dir, monkeypatch, {"ok": False, "error": "refused"}, status="parked")

    result = p.dispatch_story(PLAN, KEY)

    assert result["ok"] is False
    assert _resolutions() == []


def test_locked_dispatch_of_parked_story_records_nothing(plan_dir, monkeypatch) -> None:  # noqa: F811
    _write_manifest(plan_dir, PLAN, {KEY: _parked()})
    _seed_ruling()
    _fake_dispatch(plan_dir, monkeypatch, {"ok": True, "skipped": "locked"}, status="parked")

    p.dispatch_story(PLAN, KEY)

    assert _resolutions() == []


def test_dispatch_of_a_story_that_is_not_parked_records_nothing(plan_dir, monkeypatch) -> None:  # noqa: F811
    _write_manifest(plan_dir, PLAN, {KEY: _parked(status="todo")})
    _seed_ruling()
    _fake_dispatch(plan_dir, monkeypatch, {"ok": True, "story_key": KEY})

    p.dispatch_story(PLAN, KEY)

    assert _resolutions() == []


# --------------------------------------------------------------------------
# interrupt_story: stopping a parked story's agent is recorded as other
# --------------------------------------------------------------------------


def _fake_terminate(monkeypatch) -> None:
    def fake(manifest, plan_name, story_key, story, **kwargs) -> str:
        story["status"] = "interrupted"
        p._store.save_manifest(plan_name, manifest)
        return "abc123"

    monkeypatch.setattr(p, "_terminate_and_checkpoint", fake)


def test_interrupt_of_parked_story_records_one_other(plan_dir, monkeypatch) -> None:  # noqa: F811
    _write_manifest(plan_dir, PLAN, {KEY: _parked(pid=4242)})
    _seed_ruling()
    _fake_terminate(monkeypatch)

    result = p.interrupt_story(PLAN, KEY)

    assert result["ok"] is True
    resolutions = _resolutions()
    assert len(resolutions) == 1
    assert resolutions[0]["human_action"] == "other"
    assert resolutions[0]["new_status"] == "interrupted"
    assert resolutions[0]["ruling_ref"] == DECIDED


def test_refused_interrupt_of_parked_story_records_nothing(plan_dir, monkeypatch) -> None:  # noqa: F811
    _write_manifest(plan_dir, PLAN, {KEY: _parked()})
    _seed_ruling()
    _fake_terminate(monkeypatch)

    result = p.interrupt_story(PLAN, KEY)

    assert result["ok"] is False
    assert _resolutions() == []


def test_interrupt_of_parked_story_without_a_ruling_records_nothing(plan_dir, monkeypatch) -> None:  # noqa: F811
    _write_manifest(plan_dir, PLAN, {KEY: _parked(pid=4242)})
    _fake_terminate(monkeypatch)

    p.interrupt_story(PLAN, KEY)

    assert _resolutions() == []


def test_dispatch_of_an_unknown_plan_keeps_its_refusal(plan_dir, monkeypatch) -> None:  # noqa: F811
    _fake_dispatch(plan_dir, monkeypatch, {"ok": False, "error": "refused"})

    result = p.dispatch_story("no-such-plan", KEY)

    assert result["ok"] is False
    assert _resolutions() == []


# --------------------------------------------------------------------------
# approve_merge: a human merge of a parked story is recorded as merge_held
# --------------------------------------------------------------------------


def _approve_mocks(monkeypatch) -> None:
    monkeypatch.setattr(p, "_rebase_onto_master", lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status", lambda br, **_: {"state": "pass", "error": ""})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)


def _approvable_parked(**extra) -> dict:
    return _parked(review_verdict="APPROVE", review_verdict_sha="stub-head", risk="low", worktree="/x", **extra)


def test_approve_merge_of_parked_story_records_one_merge_held(plan_dir, monkeypatch) -> None:  # noqa: F811
    _write_manifest(plan_dir, PLAN, {KEY: _approvable_parked()})
    _seed_ruling()
    _approve_mocks(monkeypatch)

    result = p.approve_merge(PLAN, KEY)

    assert result["ok"] is True
    resolutions = _resolutions()
    assert len(resolutions) == 1
    assert resolutions[0]["human_action"] == "merge_held"
    assert resolutions[0]["new_status"] == "done"
    assert resolutions[0]["ruling_ref"] == DECIDED


def test_refused_approve_merge_of_parked_story_records_nothing(plan_dir, monkeypatch) -> None:  # noqa: F811
    _write_manifest(plan_dir, PLAN, {KEY: _parked()})
    _seed_ruling()
    _approve_mocks(monkeypatch)

    result = p.approve_merge(PLAN, KEY)

    assert result["ok"] is False
    assert _resolutions() == []


def test_approve_merge_of_parked_story_without_a_ruling_records_nothing(plan_dir, monkeypatch) -> None:  # noqa: F811
    _write_manifest(plan_dir, PLAN, {KEY: _approvable_parked()})
    _approve_mocks(monkeypatch)

    result = p.approve_merge(PLAN, KEY)

    assert result["ok"] is True
    assert _resolutions() == []


# --------------------------------------------------------------------------
# unblock_story: one answer per ruling, however many unblocks follow it
# --------------------------------------------------------------------------


@pytest.fixture
def quiet_unblock(monkeypatch):
    monkeypatch.setattr(unpark_mod, "_notify_user", lambda *a, **k: None)


def test_second_unblock_against_the_same_ruling_adds_no_resolution(plan_dir, quiet_unblock) -> None:  # noqa: F811
    _write_manifest(plan_dir, PLAN, {KEY: _parked()})
    _seed_ruling()

    p.unblock_story(PLAN, KEY, ["rework_attempts"])
    p.unblock_story(PLAN, KEY, ["rework_attempts"])

    assert len(_resolutions()) == 1


# --------------------------------------------------------------------------
# dispatch_story: the audit record never decides whether a dispatch runs
# --------------------------------------------------------------------------


def _store_reads_fail_after(monkeypatch, reads_allowed: int) -> None:
    real = p._store.get_manifest_or_none
    seen = {"reads": 0}

    def flaky(plan_name):
        seen["reads"] += 1
        if seen["reads"] > reads_allowed:
            raise OSError("store unreadable")
        return real(plan_name)

    monkeypatch.setattr(p._store, "get_manifest_or_none", flaky)


def test_unreadable_store_before_dispatch_does_not_fail_the_dispatch(plan_dir, monkeypatch) -> None:  # noqa: F811
    _write_manifest(plan_dir, PLAN, {KEY: _parked()})
    _seed_ruling()
    _fake_dispatch(plan_dir, monkeypatch, {"ok": True, "story_key": KEY})
    _store_reads_fail_after(monkeypatch, reads_allowed=0)

    result = p.dispatch_story(PLAN, KEY)

    assert result["ok"] is True
    assert _resolutions() == []


def test_unreadable_store_after_dispatch_does_not_fail_the_dispatch(plan_dir, monkeypatch) -> None:  # noqa: F811
    _write_manifest(plan_dir, PLAN, {KEY: _parked()})
    _seed_ruling()
    _fake_dispatch(plan_dir, monkeypatch, {"ok": True, "story_key": KEY})
    _store_reads_fail_after(monkeypatch, reads_allowed=1)

    result = p.dispatch_story(PLAN, KEY)

    assert result == {"ok": True, "story_key": KEY}


def test_patch_of_description_on_parked_story_is_refused_and_records_nothing(plan_dir) -> None:  # noqa: F811
    # description is not a patchable story field, so the patch is refused before
    # any brief comparison: no rebrief, and the stored description is untouched.
    _write_manifest(plan_dir, PLAN, {KEY: _parked(description="Old.")})
    _seed_ruling()

    result = p.patch_story(PLAN, KEY, {"description": "New."})

    assert result["ok"] is False
    assert "cannot patch field" in result["error"]
    assert _resolutions() == []


def test_patch_of_files_on_parked_story_is_refused_and_records_nothing(plan_dir) -> None:  # noqa: F811
    # files is not a patchable story field either: a refused patch writes nothing.
    _write_manifest(plan_dir, PLAN, {KEY: _parked(files=["pipeline/a.py"])})
    _seed_ruling()

    result = p.patch_story(PLAN, KEY, {"files": ["pipeline/b.py"]})

    assert result["ok"] is False
    assert "cannot patch field" in result["error"]
    assert _resolutions() == []
