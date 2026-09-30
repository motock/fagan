"""RH-06: the retro backlog must notify once per threshold crossing.

``pipeline/ci.py::_record_retro_pending`` appends one marker line per completed
pipeline-self-repo plan to ``retros/PENDING.md``. It had no cap and emitted no
notification, so the file grew by 43 markers in the week of Sep 22-29 against
zero removals - a backlog that only grows stops being a to-do list.

The fix: when the marker count crosses ``_RETRO_BACKLOG_NOTIFY_AT``, emit ONE
notification naming the current backlog size, and do not fire again for every
subsequent plan while still over the threshold. The crossing is derived from
the file on every call, so it re-arms automatically once the file is drained
back below the threshold.

Assertions read the throwaway PENDING.md these tests point the writer at; they
never touch the real, shared retros/PENDING.md.
"""

import importlib

import pytest

p = importlib.import_module("pipeline.server")
ci = importlib.import_module("pipeline.ci")

THRESHOLD = 100


def _write_markers(path, count, prefix="drained-plan"):
    """Write ``count`` marker lines, the shape ``_record_retro_pending`` writes."""
    path.write_text(
        "".join(
            f"- {prefix}-{i} \u2014 completed 2026-09-29, 1 story\n" for i in range(count)
        )
    )


def _marker_count(path):
    return sum(1 for line in path.read_text().splitlines() if line.startswith("- "))


@pytest.fixture
def pending(tmp_path, monkeypatch):
    """Point the writer at a throwaway PENDING.md and capture notifications."""
    path = tmp_path / "PENDING.md"
    monkeypatch.setattr(p, "RETRO_PENDING_PATH", path, raising=False)
    notes = []
    monkeypatch.setattr(
        p, "_notify_user", lambda plan, msg, **kwargs: notes.append((plan, msg, kwargs))
    )
    return path, notes


def test_the_threshold_constant_is_module_level_and_greppable():
    assert ci._RETRO_BACKLOG_NOTIFY_AT == THRESHOLD


def test_below_the_threshold_appends_without_notifying(pending):
    path, notes = pending
    _write_markers(path, THRESHOLD - 2)

    ci._record_retro_pending("below-threshold-plan", 1)

    assert notes == []
    assert _marker_count(path) == THRESHOLD - 1


def test_crossing_the_threshold_notifies_once_and_names_the_count(pending):
    path, notes = pending
    _write_markers(path, THRESHOLD - 1)

    ci._record_retro_pending("crossing-plan", 1)

    assert len(notes) == 1, f"expected exactly one notification, got {notes!r}"
    _plan, message, _kwargs = notes[0]
    assert str(THRESHOLD) in message, (
        f"notification must name the current backlog size ({THRESHOLD}): {message!r}"
    )
    assert _marker_count(path) == THRESHOLD


def test_staying_above_the_threshold_does_not_notify_again(pending):
    path, notes = pending
    _write_markers(path, THRESHOLD - 1)

    ci._record_retro_pending("crossing-plan", 1)
    assert len(notes) == 1

    ci._record_retro_pending("next-plan", 1)
    ci._record_retro_pending("another-plan", 1)

    assert len(notes) == 1, f"must fire once per crossing, not per append: {notes!r}"
    assert _marker_count(path) == THRESHOLD + 2


def test_dropping_below_and_crossing_again_notifies_again(pending):
    path, notes = pending
    _write_markers(path, THRESHOLD - 1)
    ci._record_retro_pending("first-crossing-plan", 1)
    assert len(notes) == 1

    # Drain: the file is edited down below the threshold again.
    _write_markers(path, 50)
    ci._record_retro_pending("drained-plan", 1)
    assert len(notes) == 1, "still below the threshold: no notification"
    assert _marker_count(path) == 51

    _write_markers(path, THRESHOLD - 1)
    ci._record_retro_pending("second-crossing-plan", 1)

    assert len(notes) == 2, f"the crossing must re-arm after a drain: {notes!r}"
    assert str(THRESHOLD) in notes[1][1]


def test_a_duplicate_plan_name_returns_early_without_appending_or_notifying(pending):
    path, notes = pending
    _write_markers(path, THRESHOLD - 1)
    ci._record_retro_pending("duplicate-plan", 1)
    assert len(notes) == 1
    count_after_first = _marker_count(path)

    ci._record_retro_pending("duplicate-plan", 1)

    assert len(notes) == 1, "a duplicate must not notify"
    assert _marker_count(path) == count_after_first, "a duplicate must not append"


def test_a_missing_pending_file_is_created_without_notifying(pending):
    path, notes = pending
    assert not path.exists()

    ci._record_retro_pending("fresh-plan", 1)

    assert path.exists()
    assert notes == []
    assert _marker_count(path) == 1


def test_the_threshold_is_read_from_the_constant_not_hardcoded(tmp_path, monkeypatch):
    """The constant must be the single source of truth for the threshold."""
    path = tmp_path / "PENDING.md"
    monkeypatch.setattr(p, "RETRO_PENDING_PATH", path, raising=False)
    notes = []
    monkeypatch.setattr(
        p, "_notify_user", lambda plan, msg, **kwargs: notes.append((plan, msg, kwargs))
    )
    monkeypatch.setattr(ci, "_RETRO_BACKLOG_NOTIFY_AT", 3)

    _write_markers(path, 2)
    ci._record_retro_pending("patched-threshold-plan", 1)

    assert len(notes) == 1, f"the patched threshold (3) must be honoured: {notes!r}"
    assert "3" in notes[0][1]
