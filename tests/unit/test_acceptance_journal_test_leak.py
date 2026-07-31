"""Acceptance oracle: the unit suite must not write into the operator's real
plans directory.

pipeline/persistence.py reads its own module-level PLAN_DIR binding, so tests
that patch only pipeline.server.PLAN_DIR leak journal writes to
~/.claude/plans/ (344 records accumulated in cap1.S1.journal.json). The fix
belongs in conftest.py so it covers the whole class, not one test.
"""
import json

from pipeline import persistence


def test_persistence_plan_dir_is_redirected_away_from_the_real_one():
    real = str(persistence.Path.home() / ".claude" / "plans")
    assert str(persistence.PLAN_DIR) != real, (
        "pipeline.persistence.PLAN_DIR must be redirected to a tmp dir for the "
        "whole unit suite"
    )


def test_appending_a_journal_entry_does_not_touch_the_real_plans_dir():
    real_journal = persistence.Path.home() / ".claude" / "plans" / "zz_leak_probe.S1.journal.json"
    assert not real_journal.exists()
    persistence._append_journal("zz_leak_probe", "S1", {"step": "probe"})
    assert not real_journal.exists(), (
        f"journal write escaped to {real_journal}"
    )
    written = persistence._journal_path("zz_leak_probe", "S1")
    assert written.exists()
    assert json.loads(written.read_text())[0]["step"] == "probe"


def test_notifications_also_stay_out_of_the_real_plans_dir():
    real_log = persistence.Path.home() / ".claude" / "plans" / "zz_leak_probe.notifications.log"
    existed = real_log.exists()
    before = real_log.read_text() if existed else ""
    persistence._notify_user("zz_leak_probe", "probe")
    after = real_log.read_text() if real_log.exists() else ""
    assert after == before, "notification write escaped to the real plans dir"
