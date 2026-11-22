"""Ready stories that the dispatch gate holds back are announced, not silent.

Incident (measured 2026-11-22): with the Ollama endpoint misconfigured, a
story sat in ``todo`` indefinitely with no notification, event or story-level
reason. ``_advance_pipeline_locked_impl`` computes the dispatch gate reason and
used it only for interruption decisions. After the dispatch loop, a tick now
reports the gate when at least one dispatch-eligible story was held back by it:

  - ``summary["dispatch_gated"] = {"reason": ..., "stories": [...]}``;
  - one ``_notify_user`` per distinct sanitised reason, ``event="dispatch_gated"``,
    ``severity="warning"``, announced once per gated episode (not per tick).

Reasons are sanitised first (single line, at most 200 characters, URL
credentials and query strings removed) because a reason can embed a URL.
"""

import os

from pipeline import server as p
from pipeline.dispatch_gate_notice import sanitise_gate_reason
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _read_manifest,
    _write_manifest,
    plan_dir,
)

_DEVICE_TAG = "gpt-oss:20b"
_DOWN_REASON = "Ollama endpoint unreachable at http://localhost:11434"


# ---------------------------------------------------------------------------
# sanitise_gate_reason: pure function


def test_sanitiser_removes_url_credentials_and_query_string():
    out = sanitise_gate_reason(
        "endpoint http://user:secret@host/path?token=abc is unreachable"
    )

    assert "secret" not in out
    assert "abc" not in out
    assert "http://host/path" in out
    assert "is unreachable" in out


def test_sanitiser_cuts_a_500_character_reason_to_200():
    out = sanitise_gate_reason("x" * 500)

    assert len(out) == 200


def test_sanitiser_turns_a_multi_line_reason_into_one_line():
    out = sanitise_gate_reason("line one\nline two\r\nline three")

    assert "\n" not in out
    assert "\r" not in out
    assert out == "line one line two line three"


def test_sanitiser_leaves_a_plain_reason_unchanged():
    assert sanitise_gate_reason("insufficient free memory") == (
        "insufficient free memory"
    )


def test_sanitiser_maps_an_empty_reason_to_empty():
    assert sanitise_gate_reason("") == ""


# ---------------------------------------------------------------------------
# Tick-level behaviour


class _GatedBackend:
    """Stand-in for ``app.backend``: a model tag listed in ``reasons_by_tag``
    reports its gate as down with that reason; every other tag is ok."""

    def __init__(self, reasons_by_tag=None):
        self._reasons = dict(reasons_by_tag or {})

    def get_backend(self, role, name=None):
        return self

    def resource_status(self, model_tag=None):
        reason = self._reasons.get(model_tag)
        if reason is None:
            return {"ok": True, "reason": ""}
        return {"ok": False, "reason": reason}


def _stub_tick(monkeypatch, *, blanket_down_reason=None, reasons_by_tag=None):
    """Stub the tick's collaborators; return (dispatched, notes) recorders.

    ``notes`` collects ``(message, kwargs)`` for every ``_notify_user`` call.
    """
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "MAX_CONCURRENT_AGENTS", 0)

    def _role_ok(role, plan_role_config=None, **_kwargs):
        if role == "dispatch" and blanket_down_reason is not None:
            return (False, blanket_down_reason)
        return (True, "")

    monkeypatch.setattr(p, "_role_resource_ok", _role_ok)
    monkeypatch.setattr(p, "backend", _GatedBackend(reasons_by_tag))
    monkeypatch.setattr(
        p, "check_story_status", lambda plan, key: {"status": "running"},
    )
    dispatched = []
    monkeypatch.setattr(
        p, "dispatch_story", lambda plan, key: dispatched.append(key),
    )
    monkeypatch.setattr(
        p, "interrupt_story", lambda plan, key: {"ok": True},
    )
    notes = []
    monkeypatch.setattr(
        p,
        "_notify_user",
        lambda plan, msg, **kwargs: notes.append((msg, kwargs)),
    )
    return dispatched, notes


def _gate_notes(notes):
    return [(msg, kw) for msg, kw in notes if kw.get("event") == "dispatch_gated"]


def test_gate_down_with_a_ready_story_records_it_and_notifies_once(
    plan_dir, monkeypatch,
):
    _, notes = _stub_tick(
        monkeypatch,
        blanket_down_reason=_DOWN_REASON,
        reasons_by_tag={_DEVICE_TAG: _DOWN_REASON},
    )
    _write_manifest(plan_dir, "gate_once", {
        "S1": {"summary": "ready", "status": "todo",
               "model": _DEVICE_TAG, "dependencies": []},
    })

    result = p.advance_pipeline("gate_once")

    assert result["dispatch_gated"] == {
        "reason": sanitise_gate_reason(_DOWN_REASON),
        "stories": ["S1"],
    }
    gate_notes = _gate_notes(notes)
    assert len(gate_notes) == 1
    msg, kwargs = gate_notes[0]
    assert msg == f"dispatch is waiting: {sanitise_gate_reason(_DOWN_REASON)}"
    assert kwargs["severity"] == "warning"
    assert kwargs["dedup_key"] == (
        f"dispatch_gated:gate_once:{sanitise_gate_reason(_DOWN_REASON)}"
    )


def test_two_ticks_with_the_same_reason_notify_once(plan_dir, monkeypatch):
    _, notes = _stub_tick(
        monkeypatch,
        blanket_down_reason=_DOWN_REASON,
        reasons_by_tag={_DEVICE_TAG: _DOWN_REASON},
    )
    _write_manifest(plan_dir, "gate_twice", {
        "S1": {"summary": "ready", "status": "todo",
               "model": _DEVICE_TAG, "dependencies": []},
    })

    p.advance_pipeline("gate_twice")
    p.advance_pipeline("gate_twice")

    gate_notes = _gate_notes(notes)
    assert len(gate_notes) == 1
    dedup_keys = {kw["dedup_key"] for _, kw in gate_notes}
    assert len(dedup_keys) == 1


def test_gate_up_emits_neither_summary_entry_nor_notification(
    plan_dir, monkeypatch,
):
    dispatched, notes = _stub_tick(monkeypatch)
    _write_manifest(plan_dir, "gate_up", {
        "S1": {"summary": "ready", "status": "todo",
               "model": _DEVICE_TAG, "dependencies": []},
    })

    result = p.advance_pipeline("gate_up")

    assert dispatched == ["S1"]
    assert "dispatch_gated" not in result
    assert _gate_notes(notes) == []


def test_gate_down_with_no_ready_story_emits_nothing(plan_dir, monkeypatch):
    _, notes = _stub_tick(
        monkeypatch,
        blanket_down_reason=_DOWN_REASON,
        reasons_by_tag={_DEVICE_TAG: _DOWN_REASON},
    )
    _write_manifest(plan_dir, "gate_idle", {
        "S1": {"summary": "already running", "status": "in_progress",
               "pid": os.getpid(), "worktree": "/x",
               "model": _DEVICE_TAG, "dependencies": []},
    })

    result = p.advance_pipeline("gate_idle")

    assert "dispatch_gated" not in result
    assert _gate_notes(notes) == []


def test_each_distinct_gate_reason_is_notified_with_its_own_stories(
    plan_dir, monkeypatch,
):
    other_reason = "Claude usage exhausted"
    _, notes = _stub_tick(
        monkeypatch,
        reasons_by_tag={
            _DEVICE_TAG: _DOWN_REASON,
            "other:model": other_reason,
        },
    )
    _write_manifest(plan_dir, "gate_two_reasons", {
        "S1": {"summary": "local", "status": "todo",
               "model": _DEVICE_TAG, "dependencies": []},
        "S2": {"summary": "other", "status": "todo",
               "model": "other:model", "dependencies": []},
    })

    p.advance_pipeline("gate_two_reasons")

    messages = sorted(msg for msg, _ in _gate_notes(notes))
    assert messages == sorted([
        f"dispatch is waiting: {sanitise_gate_reason(_DOWN_REASON)}",
        f"dispatch is waiting: {other_reason}",
    ])


def test_notice_never_carries_url_credentials(plan_dir, monkeypatch):
    leaky = "Ollama at http://user:secret@host/path?token=abc is down"
    _, notes = _stub_tick(
        monkeypatch,
        blanket_down_reason=leaky,
        reasons_by_tag={_DEVICE_TAG: leaky},
    )
    _write_manifest(plan_dir, "gate_leaky", {
        "S1": {"summary": "ready", "status": "todo",
               "model": _DEVICE_TAG, "dependencies": []},
    })

    p.advance_pipeline("gate_leaky")

    gate_notes = _gate_notes(notes)
    assert len(gate_notes) == 1
    msg, kwargs = gate_notes[0]
    for text in (msg, kwargs["dedup_key"]):
        assert "secret" not in text
        assert "token=abc" not in text
