"""Tests for ``python -m pipeline.unpark`` (HIF-02).

A human unblocking a parked story by clearing ``rework_attempts`` /
``last_review_findings`` is allowed, but a hand edit bypasses the plan lock and
leaves no record, so ``pipeline.local_success.classify_story`` reports nothing
and the retro instrument lies. ``pipeline.unpark`` gives that intervention a
sanctioned path: it clears the allowlisted retry counters under the plan lock
and appends a ``story_unparked_manually`` record to the notification sidecar.

Every test points ``PLAN_DIR`` at ``tmp_path`` through the three-way patch (see
``sizing_plan_dir`` in ``test_ingest_files_sizing_and_route.py``): patching only
``pipeline.server.PLAN_DIR`` would leave ``FileStore`` writing into the real
repo ``plans/`` directory.
"""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import unpark as unpark_mod
from pipeline.store import FileStore

_PLAN = "forge"
_STORY = "FORGE-S1B"


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Point every ``PLAN_DIR`` binding at ``tmp_path``."""
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    # pipeline_persistence and pipeline_concurrency import PLAN_DIR from
    # pipeline_paths at module load and read it as a free var, so patches
    # must land on their own bindings too.
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


def _write_manifest(story: dict) -> None:
    FileStore().save_manifest(_PLAN, {"plan": _PLAN, "stories": {_STORY: story}})


def _read_story() -> dict:
    return FileStore().get_manifest(_PLAN)["stories"][_STORY]


def _manifest_bytes() -> bytes:
    return FileStore().manifest_path(_PLAN).read_bytes()


def _capture_notices(monkeypatch) -> list[dict]:
    """Replace the notification seam and record every call's kwargs."""
    notices: list[dict] = []

    def fake(plan_name, message, **kwargs):
        notices.append({"plan": plan_name, "message": message, **kwargs})

    monkeypatch.setattr(unpark_mod, "_notify_user", fake)
    return notices


def _populated_story() -> dict:
    return {
        "status": "parked",
        "pr_url": "https://x/1",
        "files": ["a.py"],
        "agent_instructions": "do the thing",
        "acceptance": ["it works"],
        "pid": 1234,
        "worktree": "/tmp/wt",
        "rework_attempts": 2,
        "last_review_findings": ["F1"],
        "review_verdict": "changes_requested",
        "triage_attempts": 1,
    }


def test_clears_named_fields_and_keeps_every_other_key(plan_dir, monkeypatch):
    _write_manifest(_populated_story())
    _capture_notices(monkeypatch)

    result = unpark_mod.clear_story_fields(
        _PLAN, _STORY, ["rework_attempts", "last_review_findings"]
    )

    assert result == {
        "plan": _PLAN,
        "story": _STORY,
        "cleared": ["last_review_findings", "rework_attempts"],
        "status": None,
    }
    assert set(_read_story()) == {
        "status",
        "pr_url",
        "files",
        "agent_instructions",
        "acceptance",
        "pid",
        "worktree",
        "review_verdict",
        "triage_attempts",
    }


def test_absent_field_is_not_an_error_and_is_not_reported(plan_dir, monkeypatch):
    _write_manifest({"status": "interrupted", "pr_url": "https://x/1"})
    notices = _capture_notices(monkeypatch)

    result = unpark_mod.clear_story_fields(_PLAN, _STORY, ["rework_attempts"])

    assert result["cleared"] == []
    assert "rework_attempts" not in _read_story()
    assert notices[0]["message"] == "manual unblock: cleared nothing"


def test_absent_field_is_not_recreated_on_a_follow_up_call(plan_dir, monkeypatch):
    _write_manifest(_populated_story())
    _capture_notices(monkeypatch)
    unpark_mod.clear_story_fields(
        _PLAN, _STORY, ["rework_attempts", "last_review_findings"], status="interrupted"
    )

    result = unpark_mod.clear_story_fields(_PLAN, _STORY, ["rework_attempts"])

    assert result["cleared"] == []
    assert result["status"] is None
    assert _read_story()["status"] == "interrupted"
    assert "rework_attempts" not in _read_story()


def test_rejected_field_raises_and_leaves_manifest_byte_identical(plan_dir, monkeypatch):
    _write_manifest(_populated_story())
    notices = _capture_notices(monkeypatch)
    before = _manifest_bytes()

    with pytest.raises(ValueError, match="status"):
        unpark_mod.clear_story_fields(_PLAN, _STORY, ["rework_attempts", "status"])

    assert _manifest_bytes() == before
    assert notices == []


def test_unknown_story_raises_key_error_and_writes_nothing(plan_dir, monkeypatch):
    _write_manifest(_populated_story())
    notices = _capture_notices(monkeypatch)
    before = _manifest_bytes()

    with pytest.raises(KeyError):
        unpark_mod.clear_story_fields(_PLAN, "NOPE-1", ["rework_attempts"])

    assert _manifest_bytes() == before
    assert notices == []


def test_status_is_written_and_echoed(plan_dir, monkeypatch):
    _write_manifest(_populated_story())
    _capture_notices(monkeypatch)

    result = unpark_mod.clear_story_fields(_PLAN, _STORY, ["rework_attempts"], status="interrupted")

    assert result["status"] == "interrupted"
    assert _read_story()["status"] == "interrupted"


def test_invalid_status_raises_value_error(plan_dir, monkeypatch):
    _write_manifest(_populated_story())
    notices = _capture_notices(monkeypatch)
    before = _manifest_bytes()

    with pytest.raises(ValueError, match="bogus"):
        unpark_mod.clear_story_fields(_PLAN, _STORY, ["rework_attempts"], status="bogus")

    assert _manifest_bytes() == before
    assert notices == []


def test_notice_is_emitted_once_after_the_save(plan_dir, monkeypatch):
    _write_manifest(_populated_story())
    notices = _capture_notices(monkeypatch)

    unpark_mod.clear_story_fields(
        _PLAN, _STORY, ["rework_attempts", "last_review_findings"], status="interrupted"
    )

    assert len(notices) == 1
    notice = notices[0]
    assert notice["plan"] == _PLAN
    assert notice["story_key"] == _STORY
    assert notice["event"] == "story_unparked_manually"
    # "warning", not "warn": NOTIFY_SEVERITIES is {"info", "warning", "error"},
    # so "warn" would be rewritten to "info" and log a warning on every unpark.
    assert notice["severity"] == "warning"
    assert "rework_attempts" in notice["message"]
    assert "last_review_findings" in notice["message"]
    assert "status -> interrupted" in notice["message"]


def test_failed_save_does_not_emit_a_notice(plan_dir, monkeypatch):
    _write_manifest(_populated_story())
    notices = _capture_notices(monkeypatch)

    def boom(self, plan_name, manifest):
        raise OSError("disk full")

    monkeypatch.setattr(FileStore, "save_manifest", boom)

    with pytest.raises(OSError):
        unpark_mod.clear_story_fields(_PLAN, _STORY, ["rework_attempts"])

    assert notices == []


def test_lock_refused_raises_runtime_error_and_writes_nothing(plan_dir, monkeypatch):
    _write_manifest(_populated_story())
    notices = _capture_notices(monkeypatch)
    before = _manifest_bytes()

    @contextmanager
    def _refused(self, plan_name):
        yield False

    monkeypatch.setattr(FileStore, "transaction", _refused)

    with pytest.raises(RuntimeError):
        unpark_mod.clear_story_fields(_PLAN, _STORY, ["rework_attempts"])

    assert _manifest_bytes() == before
    assert notices == []


def test_main_rejects_a_bogus_field_and_leaves_manifest_untouched(plan_dir, monkeypatch, capsys):
    _write_manifest(_populated_story())
    notices = _capture_notices(monkeypatch)
    before = _manifest_bytes()

    rc = unpark_mod.main(["--clear", "bogus_field", _PLAN, _STORY])

    assert rc == 1
    assert "bogus_field" in capsys.readouterr().err
    assert _manifest_bytes() == before
    assert notices == []


def test_main_success_prints_a_summary_and_returns_zero(plan_dir, monkeypatch, capsys):
    _write_manifest(_populated_story())
    notices = _capture_notices(monkeypatch)

    rc = unpark_mod.main(["--clear", "rework_attempts", _PLAN, _STORY])

    assert rc == 0
    out = capsys.readouterr().out
    assert _STORY in out
    assert "rework_attempts" in out
    assert "rework_attempts" not in _read_story()
    assert len(notices) == 1


def test_main_defaults_status_to_interrupted(plan_dir, monkeypatch):
    _write_manifest(_populated_story())
    _capture_notices(monkeypatch)

    assert unpark_mod.main(["--clear", "rework_attempts", _PLAN, _STORY]) == 0

    assert _read_story()["status"] == "interrupted"


def test_allowlist_is_exactly_the_retry_counters_and_review_records():
    assert unpark_mod.UNPARKABLE_FIELDS == frozenset(
        {
            "acceptance_failed_review",
            "ci_rework",
            "escalated",
            "last_review_findings",
            "last_reviewed_logic_fingerprint",
            "last_reviewed_sha",
            "merge_attempts",
            "merge_park_evidence",
            "merge_parked_at",
            "parked_reason",
            "pre_escalation_backend",
            "pre_escalation_model",
            "review_deferred_count",
            "review_feedback",
            "review_inconclusive_count",
            "review_verdict",
            "rework_attempts",
            "rework_test_author_done_for_sha",
            "triage_actions",
            "triage_attempts",
        }
    )
    for forbidden in ("status", "pr_url", "files", "agent_instructions", "acceptance", "pid", "worktree"):
        assert forbidden not in unpark_mod.UNPARKABLE_FIELDS


def test_duplicate_names_are_cleared_once_without_raising(plan_dir, monkeypatch):
    _write_manifest(_populated_story())
    notices = _capture_notices(monkeypatch)

    result = unpark_mod.clear_story_fields(
        _PLAN, _STORY, ["rework_attempts", "rework_attempts"]
    )

    assert result["cleared"] == ["rework_attempts"]
    assert "rework_attempts" not in _read_story()
    assert len(notices) == 1


def test_manifest_is_valid_json_after_a_clear(plan_dir, monkeypatch):
    _write_manifest(_populated_story())
    _capture_notices(monkeypatch)

    unpark_mod.clear_story_fields(_PLAN, _STORY, ["rework_attempts"])

    assert json.loads(_manifest_bytes())["stories"][_STORY]["status"] == "parked"
