"""Every operator path that answers a ruled story records one human resolution.

``patch_story`` rewriting a parked story's brief, ``dispatch_story`` relaunching
it and ``interrupt_story`` stopping it were answers to the overlord's ruling
that wrote nothing. Each now writes one ``human resolution`` beside the latest
ruling, once per ruling: a second answer to the same ruling adds no record.
A path with no ruling on record, or a story that is not parked, records nothing.
"""

from __future__ import annotations

import json

from pipeline import persistence as ppers
from pipeline import server as p
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
