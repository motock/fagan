"""RLF-4: the give-up rebrief must emit ``brief_patched``.

``pipeline/advance.py``'s give-up path rewrites a dispatched story's
``agent_instructions`` (folding in the diagnosis) exactly like the step-cap
rebrief does, but emitted no ``brief_patched`` record.  ``local_success.py``
sees the rewritten brief and records the story as not a clean first pass,
while ``compute_story_metrics`` sees no disqualifying event and reports it
clean -- one report, two verdicts.  Its unsatisfiable notice was also
unattributed (no ``story_key``), so the record could not be tied to a story.

These tests drive the REAL tick (``_advance_pipeline_locked_impl``) with
``check_story_status`` stubbed to report a give_up, so they grade the wiring
in ``pipeline/advance.py`` rather than the existence of a helper.  No real
backend, git repo or network is ever contacted.
"""

import ast
import json
from pathlib import Path

import pipeline.advance as adv
from pipeline.rebrief import DIAGNOSIS_HEADER
from pipeline.story_metrics import compute_story_metrics

PLAN = "rlf4-giveup-plan"
KEY = "S1"
CORRELATION = "corr-rlf4-1"


# ---------------------------------------------------------------------------
# Tick seams (mirrors tests/unit/test_oa2_giveup_rebrief.py's _wire)
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self, path):
        self.path = path

    def manifest_path(self, plan_name):
        return self.path

    def get_manifest(self, plan_name):
        return json.loads(self.path.read_text())


class _FakeBackend:
    def get_backend(self, *_args, **_kwargs):
        return self

    def resource_status(self, **_kwargs):
        return {"ok": True}


class _FakeAutonomy:
    def __init__(self, value="gated"):
        self._value_ = value

    def _value(self):
        return self._value_


def _story(status="in_progress", **over):
    story = {
        "key": KEY,
        "title": "story",
        "summary": "story summary",
        "status": status,
        "risk": "low",
        "backend": "claude",
        "pid": 4242,
        "worktree": "/nonexistent-rlf4-worktree",
        "agent_instructions": "ORIGINAL BRIEF",
        "dependencies": [],
    }
    story.update(over)
    return story


def _wire(monkeypatch, tmp_path, story, *, notify_calls):
    """Write a one-story manifest and stub every seam the tick touches."""
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"name": PLAN, "paused": False, "stories": {KEY: story}}))

    def _fake_check(plan_name, key):
        manifest = json.loads(path.read_text())
        manifest["stories"][key]["status"] = "failed"
        manifest["stories"][key]["failure_kind"] = "give_up"
        path.write_text(json.dumps(manifest))
        return {"status": "failed", "failure_kind": "give_up"}

    monkeypatch.setattr(adv, "_store", _FakeStore(path))
    monkeypatch.setattr(adv, "backend", _FakeBackend())
    monkeypatch.setattr(adv, "_role_resource_ok", lambda *a, **k: (True, ""))
    monkeypatch.setattr(adv, "_count_on_device_in_progress_agents", lambda: 0)
    monkeypatch.setattr(adv, "PIPELINE_AUTONOMY", _FakeAutonomy("gated"))
    monkeypatch.setattr(adv, "_adjudicate_merges", lambda *a, **k: None)
    monkeypatch.setattr(adv, "interrupt_story", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(adv, "check_story_status", _fake_check)
    monkeypatch.setattr(
        adv,
        "_notify_user",
        lambda plan_name, message, **kwargs: notify_calls.append(
            {"plan_name": plan_name, "message": message, "kwargs": kwargs}
        ),
    )
    return path


def _read_story(path):
    return json.loads(path.read_text())["stories"][KEY]


def _patched(notify_calls):
    return [c for c in notify_calls if c["kwargs"].get("event") == "brief_patched"]


# ---------------------------------------------------------------------------
# 1. Positive: the give-up rebrief emits an attributed brief_patched record
# ---------------------------------------------------------------------------


def test_brief_patched_record_is_emitted_on_the_give_up_rebrief(monkeypatch, tmp_path):
    notify_calls = []
    path = _wire(
        monkeypatch, tmp_path, _story(correlation_id=CORRELATION), notify_calls=notify_calls
    )
    monkeypatch.setattr(
        adv, "collect_failure_evidence", lambda *a, **k: "evidence", raising=False
    )
    monkeypatch.setattr(adv, "detect_unsatisfiable_signal", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(
        adv,
        "diagnose_failure",
        lambda evidence, story, plan_role_config=None: "missing API X; add it",
        raising=False,
    )

    adv._advance_pipeline_locked_impl(PLAN)

    records = _patched(notify_calls)
    assert len(records) == 1, "the give-up rebrief must emit exactly one brief_patched record"
    record = records[0]
    assert record["kwargs"]["story_key"] == KEY
    assert record["kwargs"]["correlation_id"] == CORRELATION
    # The record must describe a change that actually landed on disk.
    story = _read_story(path)
    assert story["agent_instructions"] != "ORIGINAL BRIEF"
    assert DIAGNOSIS_HEADER in story["agent_instructions"]
    # Survivor: the persist still goes through the FRESH read, so the terminal
    # status check_story_status wrote is not reverted to in_progress.
    assert story["status"] == "failed"


# ---------------------------------------------------------------------------
# 2. Negative: a no-op rebrief must not be reported as a patch
# ---------------------------------------------------------------------------


def test_no_brief_patched_record_when_the_rebrief_is_a_noop(monkeypatch, tmp_path):
    notify_calls = []
    path = _wire(monkeypatch, tmp_path, _story(), notify_calls=notify_calls)
    monkeypatch.setattr(adv, "collect_failure_evidence", lambda *a, **k: "", raising=False)
    monkeypatch.setattr(adv, "detect_unsatisfiable_signal", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(
        adv, "diagnose_failure", lambda *a, **k: None, raising=False
    )

    adv._advance_pipeline_locked_impl(PLAN)  # must not raise

    assert _patched(notify_calls) == []
    story = _read_story(path)
    assert story["agent_instructions"] == "ORIGINAL BRIEF"
    assert story["status"] == "failed"
    # Survivor: the agent_gave_up notice is untouched.
    assert [c for c in notify_calls if c["kwargs"].get("event") == "agent_gave_up"]


# ---------------------------------------------------------------------------
# 3. The unsatisfiable notice on the give-up path carries the story key
# ---------------------------------------------------------------------------


def test_unsatisfiable_notice_on_the_give_up_path_carries_the_story_key(
    monkeypatch, tmp_path
):
    notify_calls = []
    _wire(monkeypatch, tmp_path, _story(), notify_calls=notify_calls)
    monkeypatch.setattr(adv, "collect_failure_evidence", lambda *a, **k: "evidence", raising=False)
    monkeypatch.setattr(
        adv,
        "detect_unsatisfiable_signal",
        lambda *a, **k: "the spec requires X and Y, which conflict",
        raising=False,
    )
    monkeypatch.setattr(adv, "diagnose_failure", lambda *a, **k: None, raising=False)

    adv._advance_pipeline_locked_impl(PLAN)

    notices = [c for c in notify_calls if "unsatisfiable" in c["message"]]
    assert len(notices) == 1
    assert notices[0]["kwargs"]["story_key"] == KEY
    # "changing nothing else": the notice gains no event stamp.
    assert "event" not in notices[0]["kwargs"]


# ---------------------------------------------------------------------------
# 4. The record disqualifies a first-pass-clean verdict
# ---------------------------------------------------------------------------


def test_brief_patched_record_disqualifies_first_pass_clean():
    merged = {"event": "story_merged", "story_key": KEY, "correlation_id": CORRELATION}
    patched = {"event": "brief_patched", "story_key": KEY, "correlation_id": CORRELATION}

    alone = compute_story_metrics([dict(merged)])
    assert len(alone) == 1
    payload = next(iter(alone.values()))
    assert payload["first_pass_clean"] is True
    assert payload["disqualifying_events"] == 0

    both = compute_story_metrics([dict(merged), dict(patched)])
    assert len(both) == 1
    payload = next(iter(both.values()))
    assert payload["disqualifying_events"] == 1
    assert payload["first_pass_clean"] is False


# ---------------------------------------------------------------------------
# 5. The new message must not collide with the notification census vocabulary
# ---------------------------------------------------------------------------


def test_new_message_matches_no_vocabulary_entry():
    from tests.unit.test_notification_event_names import (
        _expected_event,
        _notify_calls,
        _static_message,
    )

    patched_calls = [
        call
        for call in _notify_calls()
        if any(
            kw.arg == "event"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value == "brief_patched"
            for kw in call.keywords
        )
    ]
    assert patched_calls, "advance.py must carry an event=\"brief_patched\" literal"
    for call in patched_calls:
        assert _expected_event(_static_message(call)) is None, (
            f"line {call.lineno}: the brief_patched message matches a census "
            f"vocabulary entry, which demands a duplicate event literal"
        )


# ---------------------------------------------------------------------------
# 6. REFERENCE.md documents advance.py as a brief_patched emitter
# ---------------------------------------------------------------------------


def test_reference_names_advance_as_a_brief_patched_emitter():
    lines = Path("REFERENCE.md").read_text().splitlines()
    start = lines.index("## Notification records")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("## "))
    section = "\n".join(lines[start:end])
    assert "`brief_patched`" in section

    paragraph = next(p for p in section.split("\n\n") if "`brief_patched`" in p)
    assert "pipeline/advance.py" in paragraph
    assert "pipeline/dispatch_attempt.py" in paragraph
    # The old claim that advance.py does NOT emit the event must be gone.
    assert "not by `pipeline/advance.py`" not in " ".join(paragraph.split())
