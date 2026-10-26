"""Tests for pipeline/usage_transitions.py's pause/resume announcements.

On 2026-10-24 the Claude session cap (100%) paused dispatch and interrupted
running agents, and nobody was told: the gate state already existed in
_check_usage_impl, only the transition was silent. These tests pin the
announcement contract: exactly one notification per transition, a corrupt
announced-file can never suppress the first pause, and a failing notifier
never propagates into the poller's tick.
"""

import json
from datetime import UTC, datetime

import pytest

from pipeline import server
from pipeline import usage as u
from pipeline import usage_transitions as ut


@pytest.fixture
def state_path(tmp_path, monkeypatch):
    """Isolate the usage state (and therefore the announced sidecar) file."""
    path = tmp_path / "usage_state.json"
    monkeypatch.setattr(ut, "USAGE_STATE_PATH", path)
    return path


@pytest.fixture
def notifications(monkeypatch):
    """Record every _notify_user call instead of writing notification files."""
    calls = []

    def _fake_notify(plan_name, message, **kwargs):
        calls.append({"plan_name": plan_name, "message": message, **kwargs})

    monkeypatch.setattr(server, "_notify_user", _fake_notify)
    return calls


def _paused_state(session_pct=95, week_pct=40, session_reset="2h", week_reset="Mon"):
    return {
        "session_pct": session_pct,
        "week_pct": week_pct,
        "session_reset": session_reset,
        "week_reset": week_reset,
        "paused": True,
    }


def _running_state(session_pct=12, week_pct=8):
    return {
        "session_pct": session_pct,
        "week_pct": week_pct,
        "session_reset": "",
        "week_reset": "",
        "paused": False,
    }


# ---------------------------------------------------------------------------
# Transition detection
# ---------------------------------------------------------------------------

def test_first_pause_announces_once(state_path, notifications):
    event = ut.announce(True, _paused_state())

    assert event == "usage_paused"
    assert len(notifications) == 1
    assert notifications[0]["event"] == "usage_paused"


def test_repeated_paused_polls_announce_nothing(state_path, notifications):
    assert ut.announce(True, _paused_state()) == "usage_paused"
    assert ut.announce(True, _paused_state()) is None
    assert ut.announce(True, _paused_state()) is None

    assert len(notifications) == 1


def test_resume_after_pause_announces_resumed_once(state_path, notifications):
    ut.announce(True, _paused_state())

    assert ut.announce(False, _running_state()) == "usage_resumed"
    assert ut.announce(False, _running_state()) is None

    assert [c["event"] for c in notifications] == ["usage_paused", "usage_resumed"]


def test_running_state_with_no_prior_announcement_is_silent(state_path, notifications):
    # A pipeline that has never been paused must not announce a resume.
    assert ut.announce(False, _running_state()) is None
    assert notifications == []


def test_corrupt_announced_file_still_announces_first_pause(state_path, notifications):
    announced = state_path.parent / "usage_announced.json"
    announced.write_text("{not json at all")

    assert ut.announce(True, _paused_state()) == "usage_paused"
    assert len(notifications) == 1


def test_announced_file_with_wrong_shape_reads_as_not_paused(state_path, notifications):
    announced = state_path.parent / "usage_announced.json"
    announced.write_text(json.dumps({"paused": "yes"}))

    assert ut.announce(True, _paused_state()) == "usage_paused"
    assert len(notifications) == 1


def test_announced_value_is_persisted_next_to_usage_state(state_path, notifications):
    ut.announce(True, _paused_state())

    announced = state_path.parent / "usage_announced.json"
    assert json.loads(announced.read_text())["paused"] is True


# ---------------------------------------------------------------------------
# Message content
# ---------------------------------------------------------------------------

def test_paused_message_names_tripped_window_percentages_and_reset(
    state_path, notifications
):
    ut.announce(True, _paused_state(session_pct=95, week_pct=40,
                                   session_reset="in 2h", week_reset="Mon 09:00"))

    message = notifications[0]["message"]
    assert "session" in message
    assert "95%" in message
    assert "40%" in message
    assert "in 2h" in message
    assert "Mon 09:00" in message


def test_paused_message_names_the_week_window_when_it_trips(
    state_path, notifications
):
    ut.announce(True, _paused_state(session_pct=10, week_pct=97))

    message = notifications[0]["message"]
    assert "week" in message
    assert "97%" in message


def test_paused_message_omits_missing_reset_text(state_path, notifications):
    state = _paused_state(session_reset="", week_reset="")

    ut.announce(True, state)

    message = notifications[0]["message"]
    assert "95%" in message
    assert "40%" in message
    assert "resets" not in message


def test_resumed_message_states_both_percentages(state_path, notifications):
    ut.announce(True, _paused_state())
    ut.announce(False, _running_state(session_pct=12, week_pct=8))

    message = notifications[1]["message"]
    assert "12%" in message
    assert "8%" in message


def test_reason_is_included_when_given(state_path, notifications):
    ut.announce(True, _paused_state(), reason="session cap reached")

    assert "session cap reached" in notifications[0]["message"]


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------

def test_notify_failure_does_not_propagate(state_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(server, "_notify_user", _boom)

    # Must not raise: the poller's tick cannot be taken down by a notifier.
    ut.announce(True, _paused_state())


def test_unwritable_announced_path_does_not_propagate(tmp_path, monkeypatch):
    # A directory where the announced file should be makes the atomic write
    # fail; announce() must swallow it rather than break the usage poll.
    state_path = tmp_path / "usage_state.json"
    (tmp_path / "usage_announced.json").mkdir()
    monkeypatch.setattr(ut, "USAGE_STATE_PATH", state_path)
    monkeypatch.setattr(server, "_notify_user", lambda *a, **k: None)

    ut.announce(True, _paused_state())


# ---------------------------------------------------------------------------
# Integration: _check_usage_impl drives the announcements
# ---------------------------------------------------------------------------

def test_check_usage_impl_announces_pause_then_resume(tmp_path, monkeypatch):
    state_path = tmp_path / "usage_state.json"
    monkeypatch.setattr(u, "USAGE_STATE_PATH", state_path)
    monkeypatch.setattr(ut, "USAGE_STATE_PATH", state_path)

    calls = []
    monkeypatch.setattr(
        server,
        "_notify_user",
        lambda plan_name, message, **kw: calls.append(kw.get("event")),
    )

    def _probe(session_pct, week_pct):
        return {
            "session_pct": session_pct,
            "week_pct": week_pct,
            "session_reset": "in 2h",
            "week_reset": "Mon",
            "checked_at": datetime.now(UTC).isoformat(),
        }

    # Pause, then poll again while still paused, then resume.
    monkeypatch.setattr(server, "_run_usage_probe", lambda: _probe(95, 40))
    u._check_usage_impl()
    u._check_usage_impl()

    monkeypatch.setattr(server, "_run_usage_probe", lambda: _probe(10, 5))
    u._check_usage_impl()

    assert calls == ["usage_paused", "usage_resumed"]


def test_check_usage_impl_announces_pause_on_parse_failure_fallback(
    tmp_path, monkeypatch
):
    """The parse-failure branch also sets `paused`, so it must announce too.

    A frozen prev reading that predates the `paused` key must not raise: the
    poller's tick cannot be taken down by a hand-edited state file.
    """
    state_path = tmp_path / "usage_state.json"
    monkeypatch.setattr(u, "USAGE_STATE_PATH", state_path)
    monkeypatch.setattr(ut, "USAGE_STATE_PATH", state_path)

    calls = []
    monkeypatch.setattr(
        server,
        "_notify_user",
        lambda plan_name, message, **kw: calls.append(kw.get("event")),
    )

    # A prev reading with no "paused" key, already blind long enough that the
    # fallback branch fails closed and sets paused=True.
    state_path.write_text(
        json.dumps(
            {
                "session_pct": 95,
                "week_pct": 40,
                "checked_at": "2020-01-01T00:00:00+00:00",
                "measured_at": "2020-01-01T00:00:00+00:00",
                "gate_blind": True,
                "blind_since": "2020-01-01T00:00:00+00:00",
            }
        )
    )

    def _unparseable():
        raise ValueError("no percentage summary")

    monkeypatch.setattr(server, "_run_usage_probe", _unparseable)

    state = u._check_usage_impl()

    assert state["paused"] is True
    assert calls == ["usage_paused"]
