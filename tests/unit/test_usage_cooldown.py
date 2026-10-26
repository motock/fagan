"""Tests for pipeline.usage_cooldown (UC-02).

The cooldown is a small latch file beside USAGE_STATE_PATH: `trip()` writes a
deadline, `active()` reports whether that deadline is still in the future.
It exists because the polled usage snapshot lags the real cap (see
resource_status()'s docstring in app/backend_claude.py, live 2026-10-06): an
agent can die on "hit your session limit" before the poller trips the usage
gate, and the scheduler would otherwise redispatch into the same wall every
tick.

Every read must fail OPEN - a missing, corrupt or unparsable file is NOT
active, because a bad file must never block dispatch forever.
"""
import json
from datetime import UTC, datetime, timedelta

import pytest

from app import backend as b
from app import backend_claude as bc
from pipeline import usage_cooldown

_NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def clock(monkeypatch, tmp_path):
    """Point the cooldown file at tmp_path and freeze the module clock.

    The clock is a mutable dict so a test can advance time without
    re-patching; `_utcnow` is the module's single time source.
    """
    monkeypatch.setattr(
        usage_cooldown, "USAGE_STATE_PATH", str(tmp_path / "usage_state.json")
    )
    now = {"now": _NOW}
    monkeypatch.setattr(usage_cooldown, "_utcnow", lambda: now["now"])
    monkeypatch.delenv("PIPELINE_USAGE_COOLDOWN_SECONDS", raising=False)
    return now


def _cooldown_file(tmp_path):
    return tmp_path / "usage_cooldown.json"


def _until(tmp_path):
    return json.loads(_cooldown_file(tmp_path).read_text())["until"]


# ---------- trip() / active() round trip ----------
def test_trip_then_active_is_true(clock, tmp_path):
    usage_cooldown.trip()

    assert usage_cooldown.active() is True
    assert _until(tmp_path) == "2026-10-25T12:10:00+00:00"


def test_active_is_false_after_until_passes_and_retrip_uses_fresh_deadline(clock, tmp_path):
    usage_cooldown.trip(600)
    assert usage_cooldown.active() is True

    clock["now"] = _NOW + timedelta(seconds=599)
    assert usage_cooldown.active() is True

    # Strict `>`: equality with the deadline is already expired.
    clock["now"] = _NOW + timedelta(seconds=600)
    assert usage_cooldown.active() is False

    clock["now"] = _NOW + timedelta(seconds=601)
    assert usage_cooldown.active() is False

    # A second trip must read the clock again, not a deadline cached from the
    # first call - otherwise the latch would stay stuck on the stale 12:10:00.
    clock["now"] = _NOW + timedelta(seconds=605)
    usage_cooldown.trip(60)
    assert _until(tmp_path) == "2026-10-25T12:11:05+00:00"

    clock["now"] = _NOW + timedelta(seconds=606)
    assert usage_cooldown.active() is True

    clock["now"] = _NOW + timedelta(seconds=666)
    assert usage_cooldown.active() is False


# ---------- fail open on a bad or absent file ----------
def test_missing_file_is_not_active_and_read_has_no_side_effects(clock, tmp_path):
    assert usage_cooldown.active() is False
    assert not _cooldown_file(tmp_path).exists()


def test_corrupt_file_is_not_active(clock, tmp_path):
    _cooldown_file(tmp_path).write_text("not json")

    assert usage_cooldown.active() is False


def test_non_string_until_is_not_active(clock, tmp_path):
    _cooldown_file(tmp_path).write_text(json.dumps({"until": 12345}))

    assert usage_cooldown.active() is False


def test_unparsable_until_is_not_active(clock, tmp_path):
    _cooldown_file(tmp_path).write_text(json.dumps({"until": "not-a-timestamp"}))

    assert usage_cooldown.active() is False


def test_trip_never_raises_when_the_path_is_unwritable(clock, tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    monkeypatch.setattr(
        usage_cooldown, "USAGE_STATE_PATH", str(blocker / "usage_state.json")
    )

    usage_cooldown.trip()  # must not raise

    assert usage_cooldown.active() is False


# ---------- env override ----------
def test_env_override_is_honoured(clock, tmp_path, monkeypatch):
    monkeypatch.setenv("PIPELINE_USAGE_COOLDOWN_SECONDS", "900")

    usage_cooldown.trip()

    assert _until(tmp_path) == "2026-10-25T12:15:00+00:00"


@pytest.mark.parametrize("raw", ["0", "-30"])
def test_non_positive_env_falls_back_to_default(clock, tmp_path, monkeypatch, raw):
    monkeypatch.setenv("PIPELINE_USAGE_COOLDOWN_SECONDS", raw)

    usage_cooldown.trip()

    assert _until(tmp_path) == "2026-10-25T12:10:00+00:00"


@pytest.mark.parametrize("raw", ["abc", "1.5"])
def test_non_integer_env_falls_back_to_default(clock, tmp_path, monkeypatch, raw):
    monkeypatch.setenv("PIPELINE_USAGE_COOLDOWN_SECONDS", raw)

    usage_cooldown.trip()

    assert _until(tmp_path) == "2026-10-25T12:10:00+00:00"


# ---------- resource_status() wiring ----------
def _stub_usage_gate_open(monkeypatch):
    """Open the normal usage gate and leave the identity check unprobed, so
    the only thing that can block resource_status() is the cooldown."""
    from app import pipeline_mcp_server as p

    monkeypatch.setattr(p, "_read_usage_state", lambda: {"paused": False})
    monkeypatch.setattr(bc, "_claude_identity_status", None)


def test_resource_status_blocks_while_cooldown_active(clock, monkeypatch):
    _stub_usage_gate_open(monkeypatch)
    usage_cooldown.trip()

    result = b.ClaudeCliDriver().resource_status()

    assert result["ok"] is False
    assert result["reason"] == "Claude usage cooldown active"


def test_resource_status_ok_when_no_cooldown(clock, monkeypatch):
    _stub_usage_gate_open(monkeypatch)

    result = b.ClaudeCliDriver().resource_status()

    assert result["ok"] is True
