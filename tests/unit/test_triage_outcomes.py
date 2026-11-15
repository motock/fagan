"""Tests for the human resolution record and ``unpark``'s ``--add-files`` path.

A human who unparks a story after the overlord ruled on it is making a
decision the ruling did not make. ``pipeline.triage_outcomes`` writes that
resolution beside the latest ``failure triage`` ruling so the decisions stream
shows both the machine's call and the human's answer to it. ``unpark`` gains
``--add-files`` (the sanctioned replacement for hand-editing a story's
``files``) and ``--human-action``, and records the resolution on every
successful operator unpark.

Every test points ``PLAN_DIR`` at ``tmp_path`` through the same patch the
sibling ``test_unpark.py`` uses, so nothing touches the real plan directory.
"""

from __future__ import annotations

import json
import logging

import pytest

import pipeline.server as p
from pipeline import persistence as ppers
from pipeline import triage_outcomes as to
from pipeline import unpark as unpark_mod
from pipeline.store import FileStore

_PLAN = "forge"
_STORY = "FORGE-S1B"
_OTHER = "FORGE-S2"


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    return d


def _write_manifest(story: dict, key: str = _STORY) -> None:
    FileStore().save_manifest(_PLAN, {"plan": _PLAN, "stories": {key: story}})


def _read_story(key: str = _STORY) -> dict:
    return FileStore().get_manifest(_PLAN)["stories"][key]


def _manifest_bytes() -> bytes:
    return (p.PLAN_DIR / f"{_PLAN}.manifest.json").read_bytes()


def _decisions() -> list[dict]:
    path = ppers._decisions_path(_PLAN)
    return json.loads(path.read_text()) if path.exists() else []


def _human_records() -> list[dict]:
    return [r for r in _decisions() if r.get("question") == "human resolution"]


def _seed_ruling(
    story_key: str, action: str, decided_at: str, park_class: str = "rework"
) -> None:
    ppers._append_decision(
        _PLAN,
        {
            "story_key": story_key,
            "question": "failure triage",
            "action": action,
            "park_class": park_class,
            "decided_by": "overlord-triage",
            "decided_at": decided_at,
        },
    )


def _capture_notices(monkeypatch) -> list[dict]:
    notices: list[dict] = []

    def fake(plan_name, message, **kwargs):
        notices.append({"plan": plan_name, "message": message, **kwargs})

    monkeypatch.setattr(unpark_mod, "_notify_user", fake)
    return notices


def _parked_story(**extra) -> dict:
    story = {
        "status": "parked",
        "files": ["a.py"],
        "rework_attempts": 2,
    }
    story.update(extra)
    return story


# --------------------------------------------------------------------------
# record_human_resolution
# --------------------------------------------------------------------------


def test_record_carries_the_fixed_shape_and_the_latest_ruling_for_this_story(plan_dir):
    _seed_ruling(_STORY, "rebrief", "2026-11-01T00:00:00+00:00", park_class="review")
    _seed_ruling(
        _STORY, "amend_scope", "2026-11-02T00:00:00+00:00", park_class="review"
    )
    _seed_ruling(_OTHER, "retry", "2026-11-03T00:00:00+00:00")
    ppers._append_decision(
        _PLAN,
        {
            "story_key": _STORY,
            "question": "failure triage execution",
            "action": "retry",
            "decided_at": "2026-11-04T00:00:00+00:00",
        },
    )

    record = to.record_human_resolution(
        _PLAN,
        _STORY,
        human_action="resume_review",
        files_added=("b.py",),
        new_status="tests_passed",
        principal="operator-1",
    )

    assert set(record) == {
        "question",
        "story_key",
        "ruling_ref",
        "ruling_action",
        "park_class",
        "human_action",
        "files_added",
        "new_status",
        "decided_by",
        "decided_at",
    }
    assert record["question"] == "human resolution"
    assert record["story_key"] == _STORY
    assert record["ruling_ref"] == "2026-11-02T00:00:00+00:00"
    assert record["ruling_action"] == "amend_scope"
    assert record["park_class"] == "review"
    assert record["human_action"] == "resume_review"
    assert record["files_added"] == ["b.py"]
    assert record["new_status"] == "tests_passed"
    assert record["decided_by"] == "operator-1"
    assert record["decided_at"]
    assert _human_records() == [record]


def test_record_without_a_prior_ruling_writes_null_ruling_fields(plan_dir):
    record = to.record_human_resolution(
        _PLAN,
        _STORY,
        human_action="other",
        new_status="interrupted",
        principal="operator-1",
    )

    assert record["ruling_ref"] is None
    assert record["ruling_action"] is None
    assert record["park_class"] is None
    assert record["files_added"] == []


def test_ruling_for_another_story_is_not_used(plan_dir):
    _seed_ruling(_OTHER, "retry", "2026-11-03T00:00:00+00:00")

    record = to.record_human_resolution(
        _PLAN, _STORY, human_action="other", new_status="interrupted", principal="op"
    )

    assert record["ruling_ref"] is None
    assert record["ruling_action"] is None


@pytest.mark.parametrize("bad", ["", "approve", "AMEND_SCOPE", "merge", None])
def test_unknown_human_action_is_refused_before_any_write(plan_dir, bad):
    with pytest.raises(ValueError):
        to.record_human_resolution(
            _PLAN, _STORY, human_action=bad, new_status="interrupted", principal="op"
        )

    assert not ppers._decisions_path(_PLAN).exists()


def test_human_action_set_is_the_fixed_documented_set():
    assert to.HUMAN_ACTIONS == frozenset(
        {
            "amend_scope",
            "resume_review",
            "rebrief",
            "approve_test_edit",
            "merge_held",
            "park",
            "retry",
            "other",
        }
    )


# --------------------------------------------------------------------------
# unpark --add-files
# --------------------------------------------------------------------------


def test_add_files_widens_files_and_records_amend_scope_with_latest_ruling_action(
    plan_dir, monkeypatch, capsys
):
    _write_manifest(_parked_story())
    _seed_ruling(_STORY, "rebrief", "2026-11-01T00:00:00+00:00")
    _capture_notices(monkeypatch)

    rc = unpark_mod.main(["--add-files", "b.py,c/d.py", _PLAN, _STORY])

    assert rc == 0
    assert _read_story()["files"] == ["a.py", "b.py", "c/d.py"]
    (record,) = _human_records()
    assert record["human_action"] == "amend_scope"
    assert record["ruling_action"] == "rebrief"
    assert record["ruling_ref"] == "2026-11-01T00:00:00+00:00"
    assert record["files_added"] == ["b.py", "c/d.py"]
    assert "re-approved" in capsys.readouterr().out


def test_add_files_alone_does_not_require_clear(plan_dir, monkeypatch):
    _write_manifest(_parked_story())
    _capture_notices(monkeypatch)

    assert unpark_mod.main(["--add-files", "b.py", _PLAN, _STORY]) == 0

    assert _read_story()["files"] == ["a.py", "b.py"]
    assert _read_story()["rework_attempts"] == 2


def test_add_files_does_not_duplicate_a_path_already_declared(plan_dir, monkeypatch):
    _write_manifest(_parked_story())
    _capture_notices(monkeypatch)

    assert unpark_mod.main(["--add-files", "a.py,b.py", _PLAN, _STORY]) == 0

    assert _read_story()["files"] == ["a.py", "b.py"]
    (record,) = _human_records()
    assert record["files_added"] == ["b.py"]


def test_add_files_creates_the_list_when_the_story_declares_none(plan_dir, monkeypatch):
    _write_manifest({"status": "parked"})
    _capture_notices(monkeypatch)

    assert unpark_mod.main(["--add-files", "b.py", _PLAN, _STORY]) == 0

    assert _read_story()["files"] == ["b.py"]


def test_dotted_file_name_is_accepted(plan_dir, monkeypatch):
    _write_manifest(_parked_story())
    _capture_notices(monkeypatch)

    assert unpark_mod.main(["--add-files", "docs/v1..2.md", _PLAN, _STORY]) == 0

    assert _read_story()["files"] == ["a.py", "docs/v1..2.md"]


@pytest.mark.parametrize(
    "bad_path",
    ["../escape.py", "src/../../escape.py", "/etc/passwd", "src\\win.py", "a.py\x00"],
)
def test_invalid_added_path_is_refused_before_any_write(
    plan_dir, monkeypatch, bad_path
):
    _write_manifest(_parked_story())
    notices = _capture_notices(monkeypatch)
    before = _manifest_bytes()

    with pytest.raises(ValueError):
        unpark_mod.unpark_story(_PLAN, _STORY, [], add_files=["ok.py", bad_path])

    assert _manifest_bytes() == before
    assert not ppers._decisions_path(_PLAN).exists()
    assert notices == []


def test_invalid_added_path_exits_one_and_names_no_path_value(
    plan_dir, monkeypatch, capsys
):
    _write_manifest(_parked_story())
    notices = _capture_notices(monkeypatch)
    before = _manifest_bytes()

    rc = unpark_mod.main(["--add-files", "../secret-looking.py", _PLAN, _STORY])

    assert rc == 1
    assert "secret-looking" not in capsys.readouterr().err
    assert _manifest_bytes() == before
    assert notices == []


def test_add_files_with_no_clear_and_no_add_files_is_a_usage_error(plan_dir):
    with pytest.raises(SystemExit):
        unpark_mod.main([_PLAN, _STORY])


# --------------------------------------------------------------------------
# human_action derivation and override
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("prior_status", "args", "expected"),
    [
        ("failed", ["--status", "tests_passed"], "resume_review"),
        ("parked", ["--status", "tests_passed"], "other"),
        ("parked", ["--status", "changes_requested"], "rebrief"),
        ("parked", [], "other"),
    ],
)
def test_human_action_is_derived_from_the_status_transition(
    plan_dir, monkeypatch, prior_status, args, expected
):
    _write_manifest(_parked_story(status=prior_status))
    _capture_notices(monkeypatch)

    assert unpark_mod.main([*args, "--clear", "rework_attempts", _PLAN, _STORY]) == 0

    (record,) = _human_records()
    assert record["human_action"] == expected


def test_explicit_human_action_overrides_the_derivation(plan_dir, monkeypatch):
    _write_manifest(_parked_story(status="failed"))
    _capture_notices(monkeypatch)

    rc = unpark_mod.main(
        [
            "--human-action",
            "merge_held",
            "--status",
            "tests_passed",
            "--clear",
            "rework_attempts",
            _PLAN,
            _STORY,
        ]
    )

    assert rc == 0
    (record,) = _human_records()
    assert record["human_action"] == "merge_held"


def test_invalid_human_action_exits_one_before_any_write(plan_dir, monkeypatch):
    _write_manifest(_parked_story())
    notices = _capture_notices(monkeypatch)
    before = _manifest_bytes()

    rc = unpark_mod.main(
        ["--human-action", "approve", "--clear", "rework_attempts", _PLAN, _STORY]
    )

    assert rc == 1
    assert _manifest_bytes() == before
    assert not ppers._decisions_path(_PLAN).exists()
    assert notices == []


# --------------------------------------------------------------------------
# record-write failure and the unchanged path
# --------------------------------------------------------------------------


def test_record_write_failure_warns_and_does_not_fail_the_unpark(
    plan_dir, monkeypatch, caplog
):
    _write_manifest(_parked_story(status="failed"))
    notices = _capture_notices(monkeypatch)

    def boom(plan_name, record):
        raise OSError("disk full")

    monkeypatch.setattr(ppers, "_append_decision", boom)

    with caplog.at_level(logging.WARNING, logger="pipeline"):
        rc = unpark_mod.main(["--clear", "rework_attempts", _PLAN, _STORY])

    assert rc == 0
    assert _read_story()["status"] == "interrupted"
    assert "rework_attempts" not in _read_story()
    assert len(notices) == 1
    assert any("human resolution" in r.getMessage() for r in caplog.records)


def test_unchanged_path_writes_the_same_manifest_and_notice_as_before(
    plan_dir, monkeypatch
):
    # With no add-files and no human-action, the manifest bytes and the
    # unblock notice must match what a plain clear has always written. The
    # resolution record is the only addition, and it lives in the decisions
    # stream, not the manifest.
    story = _parked_story(
        status="parked", pr_url="https://x/1", agent_instructions="brief"
    )
    _write_manifest(story)
    notices = _capture_notices(monkeypatch)

    assert unpark_mod.main(["--clear", "rework_attempts", _PLAN, _STORY]) == 0
    actual = _manifest_bytes()

    expected = {k: v for k, v in story.items() if k != "rework_attempts"}
    expected["status"] = "interrupted"
    FileStore().save_manifest(_PLAN, {"plan": _PLAN, "stories": {_STORY: expected}})
    assert actual == _manifest_bytes()

    assert notices == [
        {
            "plan": _PLAN,
            "message": "manual unblock: cleared rework_attempts; status -> interrupted",
            "story_key": _STORY,
            "severity": "warning",
            "event": "story_unparked_manually",
        }
    ]
    (record,) = _human_records()
    assert record["new_status"] == "interrupted"
