"""TDD tests for ``pipeline/plan_conflict_ruling.py``.

A *plan conflict* is a red grade whose only failing tests are pre-existing
tests the story never touched: the brief and those tests contradict each other.
This module owns the ruling machinery for that situation:

* ``_parse_plan_conflict_reply`` -- PURE parser for the overlord's reply.  It
  fails SECURE: anything malformed becomes ``{"ruling": "PARK", "reason": ...}``
  naming the defect, never a silent pre-authorization.
* ``rule_on_plan_conflict`` -- builds the prompt (brief + conflicting files +
  failing node ids + the last 4000 chars of test output), invokes the
  ``overlord`` role exactly the way ``triage.rule_on_story`` does, and parses
  the reply.  A backend exception returns ``None`` (infrastructure is never
  charged to the story).
* ``apply_plan_conflict_ruling`` -- mutates the story in place and returns the
  outcome string.  It never writes the manifest (the caller owns the write) and
  never touches the rework counters.

These tests are RED until the implementation lands: the module does not exist
yet, so the import below raises ImportError.
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path

import pytest

# pipeline.server is imported explicitly; import order is not required for
# cold-importability (every pipeline module imports cold, see
# tests/unit/test_hub_satellite_cold_imports.py).
import pipeline.server
from pipeline import plan_conflict_ruling as pcr
from pipeline.service import _ServerRef

# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE = REPO_ROOT / "REFERENCE.md"

CONFLICT_FILES = ["tests/unit/test_alpha.py", "tests/unit/test_beta.py"]

_FIELD_ORDER = ["FILE", "TEST", "BEFORE", "AFTER", "REPLACEMENT_ASSERTION", "JUSTIFICATION"]
_BLOCK_FIELDS = {"BEFORE", "AFTER", "REPLACEMENT_ASSERTION"}


def _reply(ruling: str = "PREAUTHORIZE_TEST_EDIT", **fields: str) -> str:
    """Render a reply in the exact format the prompt publishes."""
    lines = [f"RULING: {ruling}"]
    for name in _FIELD_ORDER:
        if name not in fields:
            continue
        value = fields[name]
        if name in _BLOCK_FIELDS:
            lines += [f"{name}:", "<<<", value, ">>>"]
        else:
            lines.append(f"{name}: {value}")
    return "\n".join(lines) + "\n"


def _preauth_fields(**overrides: str) -> dict:
    base = {
        "FILE": "tests/unit/test_alpha.py",
        "TEST": "test_alpha",
        "BEFORE": "assert a == 1",
        "AFTER": "assert a == 2",
        "REPLACEMENT_ASSERTION": "assert deliverable() is True",
        "JUSTIFICATION": "grade the real deliverable",
    }
    base.update(overrides)
    return base


def _preauth_reply(**overrides: str) -> str:
    return _reply(**_preauth_fields(**overrides))


def _ruling(**overrides) -> dict:
    base = {
        "ruling": "PREAUTHORIZE_TEST_EDIT",
        "file": "tests/unit/test_alpha.py",
        "test": "test_alpha",
        "before": "assert a == 1",
        "after": "assert a == 2",
        "replacement_assertion": "assert deliverable() is True",
        "justification": "grade the real deliverable",
    }
    base.update(overrides)
    return base


def _assert_utc_iso(ts) -> None:
    assert isinstance(ts, str) and ts, f"ts must be a non-empty ISO string, got {ts!r}"
    parsed = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None, f"ts must carry a UTC offset: {ts!r}"
    assert parsed.utcoffset() == _dt.timedelta(0), f"ts must be UTC: {ts!r}"


@pytest.fixture
def notifications(monkeypatch):
    """Capture ``_notify_user`` calls made through the pipeline.server binding."""
    calls: list[dict] = []

    def fake(plan_name, message, **kwargs):
        calls.append({"plan_name": plan_name, "message": message, **kwargs})

    monkeypatch.setattr(pipeline.server, "_notify_user", fake)
    return calls


@pytest.fixture
def overlord_calls(monkeypatch):
    """Capture ``_invoke_overlord`` prompts and return a canned reply."""
    calls: list[tuple] = []

    def fake(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return _preauth_reply()

    monkeypatch.setattr(pipeline.server, "_invoke_overlord", fake)
    return calls


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_plan_conflict_header_constant_is_exact():
    assert pcr.PLAN_CONFLICT_HEADER == "=== PLAN-CONFLICT RULING (pre-authorized test edit) ==="


def test_module_exposes_the_three_functions():
    for name in (
        "_parse_plan_conflict_reply",
        "rule_on_plan_conflict",
        "apply_plan_conflict_ruling",
    ):
        assert callable(getattr(pcr, name)), f"{name} must be a callable"


def test_notify_and_backend_are_server_ref_bindings():
    """The seams must resolve through pipeline.server so tests can patch them."""
    assert isinstance(pcr._notify_user, _ServerRef)
    assert isinstance(pcr._invoke_overlord, _ServerRef)


# ---------------------------------------------------------------------------
# _parse_plan_conflict_reply -- happy paths, one per ruling
# ---------------------------------------------------------------------------


def test_parse_preauthorize_well_formed():
    out = pcr._parse_plan_conflict_reply(_preauth_reply(), CONFLICT_FILES)
    assert out["ruling"] == "PREAUTHORIZE_TEST_EDIT"
    assert out["file"] == "tests/unit/test_alpha.py"
    assert out["test"] == "test_alpha"
    assert out["before"] == "assert a == 1"
    assert out["after"] == "assert a == 2"
    assert out["replacement_assertion"] == "assert deliverable() is True"
    assert out["justification"] == "grade the real deliverable"


def test_parse_preauthorize_preserves_multiline_blocks():
    out = pcr._parse_plan_conflict_reply(
        _preauth_reply(BEFORE="assert a == 1\nassert b == 2", AFTER="assert a == 3\nassert b == 4"),
        CONFLICT_FILES,
    )
    assert out["ruling"] == "PREAUTHORIZE_TEST_EDIT"
    assert "assert a == 1" in out["before"]
    assert "assert b == 2" in out["before"]
    assert "assert a == 3" in out["after"]
    assert "assert b == 4" in out["after"]
    assert ">>>" not in out["before"]
    assert ">>>" not in out["after"]


def test_parse_park_well_formed():
    out = pcr._parse_plan_conflict_reply(
        "RULING: PARK\nREASON: the pre-existing test encodes the old contract\n",
        CONFLICT_FILES,
    )
    assert out["ruling"] == "PARK"
    assert isinstance(out["reason"], str) and out["reason"]
    assert "old contract" in out["reason"]


def test_parse_regression_well_formed():
    out = pcr._parse_plan_conflict_reply(
        "RULING: REGRESSION\nRATIONALE: the brief itself is wrong\n",
        CONFLICT_FILES,
    )
    assert out["ruling"] == "REGRESSION"
    assert isinstance(out["rationale"], str) and out["rationale"]
    assert "brief itself is wrong" in out["rationale"]


# ---------------------------------------------------------------------------
# _parse_plan_conflict_reply -- fail secure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reply", ["", "   \n", "no ruling line here at all\n"])
def test_parse_missing_ruling_line_parks(reply):
    out = pcr._parse_plan_conflict_reply(reply, CONFLICT_FILES)
    assert out["ruling"] == "PARK"
    assert isinstance(out["reason"], str) and out["reason"]
    assert "ruling" in out["reason"].lower()


def test_parse_unknown_ruling_parks():
    out = pcr._parse_plan_conflict_reply("RULING: MAYBE_LATER\n", CONFLICT_FILES)
    assert out["ruling"] == "PARK"
    assert isinstance(out["reason"], str) and out["reason"]
    assert "ruling" in out["reason"].lower()


@pytest.mark.parametrize("field", _FIELD_ORDER)
def test_parse_preauthorize_missing_required_field_parks(field):
    fields = _preauth_fields()
    del fields[field]
    out = pcr._parse_plan_conflict_reply(_reply(**fields), CONFLICT_FILES)
    assert out["ruling"] == "PARK", f"missing {field} must fail secure"
    assert isinstance(out["reason"], str) and out["reason"]
    assert field.lower() in out["reason"].lower(), f"reason must name {field}: {out['reason']!r}"


@pytest.mark.parametrize("field", _FIELD_ORDER)
def test_parse_preauthorize_blank_required_field_parks(field):
    out = pcr._parse_plan_conflict_reply(_reply(**_preauth_fields(**{field: ""})), CONFLICT_FILES)
    assert out["ruling"] == "PARK", f"blank {field} must fail secure"
    assert isinstance(out["reason"], str) and out["reason"]
    assert field.lower() in out["reason"].lower(), f"reason must name {field}: {out['reason']!r}"


def test_parse_file_outside_conflict_files_parks():
    out = pcr._parse_plan_conflict_reply(
        _preauth_reply(FILE="tests/unit/test_someone_elses.py"), CONFLICT_FILES
    )
    assert out["ruling"] == "PARK"
    assert isinstance(out["reason"], str) and out["reason"]
    assert "tests/unit/test_someone_elses.py" in out["reason"]


def test_parse_file_outside_conflict_files_parks_even_when_conflict_files_empty():
    out = pcr._parse_plan_conflict_reply(_preauth_reply(), [])
    assert out["ruling"] == "PARK"
    assert isinstance(out["reason"], str) and out["reason"]


def test_parse_before_equal_to_after_parks():
    out = pcr._parse_plan_conflict_reply(
        _preauth_reply(BEFORE="assert a == 1", AFTER="assert a == 1"), CONFLICT_FILES
    )
    assert out["ruling"] == "PARK"
    assert isinstance(out["reason"], str) and out["reason"]
    low = out["reason"].lower()
    assert "before" in low or "after" in low


def test_parse_does_not_mutate_conflict_files():
    files = list(CONFLICT_FILES)
    pcr._parse_plan_conflict_reply(_preauth_reply(), files)
    assert files == CONFLICT_FILES


# ---------------------------------------------------------------------------
# rule_on_plan_conflict
# ---------------------------------------------------------------------------


def test_rule_on_plan_conflict_prompt_carries_brief_files_nodes_and_output(overlord_calls):
    story = {"agent_instructions": "BRIEFMARKER: implement the widget"}
    node_ids = ["tests/unit/test_alpha.py::test_alpha", "tests/unit/test_beta.py::test_beta"]
    out = pcr.rule_on_plan_conflict(story, CONFLICT_FILES, node_ids, "OUTPUTMARKER tail", None)

    assert len(overlord_calls) == 1
    prompt = overlord_calls[0][0]
    assert "BRIEFMARKER" in prompt
    for path in CONFLICT_FILES:
        assert path in prompt
    for node in node_ids:
        assert node in prompt
    assert "OUTPUTMARKER" in prompt
    assert out["ruling"] == "PREAUTHORIZE_TEST_EDIT"
    assert out["file"] == "tests/unit/test_alpha.py"


def test_rule_on_plan_conflict_prompt_states_the_three_rulings(overlord_calls):
    pcr.rule_on_plan_conflict({"agent_instructions": "b"}, CONFLICT_FILES, [], "out", None)
    prompt = overlord_calls[0][0]
    assert "PREAUTHORIZE_TEST_EDIT" in prompt
    assert "PARK" in prompt
    assert "REGRESSION" in prompt


def test_rule_on_plan_conflict_prompt_forbids_reverting_the_brief(overlord_calls):
    pcr.rule_on_plan_conflict({"agent_instructions": "b"}, CONFLICT_FILES, [], "out", None)
    prompt = overlord_calls[0][0]
    low = prompt.lower()
    assert "never" in low, "prompt must forbid reverting the brief's required change"
    assert "revert" in low, "prompt must forbid reverting the brief's required change"
    assert "required change" in low, "prompt must name the brief's required change"


def test_rule_on_plan_conflict_prompt_uses_only_the_last_4000_chars(overlord_calls):
    tail = "ZZHEADZZ" + ("x" * 5000) + "ZZTAILMARKERZZ"
    pcr.rule_on_plan_conflict({"agent_instructions": "b"}, CONFLICT_FILES, [], tail, None)
    prompt = overlord_calls[0][0]
    assert "ZZTAILMARKERZZ" in prompt
    assert "ZZHEADZZ" not in prompt


def test_rule_on_plan_conflict_passes_plan_role_config_through(overlord_calls):
    cfg = {"role": "overlord", "model": "some-model"}
    pcr.rule_on_plan_conflict({"agent_instructions": "b"}, CONFLICT_FILES, [], "out", cfg)
    _args, kwargs = overlord_calls[0]
    assert kwargs.get("plan_role_config") == cfg, (
        "the overlord must be invoked the way triage.rule_on_story does: "
        "_invoke_overlord(prompt, plan_role_config=...)"
    )


def test_rule_on_plan_conflict_backend_exception_returns_none(monkeypatch):
    def boom(prompt, **kwargs):
        raise RuntimeError("backend down")

    monkeypatch.setattr(pipeline.server, "_invoke_overlord", boom)
    assert pcr.rule_on_plan_conflict({"agent_instructions": "b"}, CONFLICT_FILES, [], "out", None) is None


@pytest.mark.parametrize("raw", ["garbage reply", "", None])
def test_rule_on_plan_conflict_malformed_reply_parks(monkeypatch, raw):
    monkeypatch.setattr(pipeline.server, "_invoke_overlord", lambda prompt, **kw: raw)
    out = pcr.rule_on_plan_conflict({"agent_instructions": "b"}, CONFLICT_FILES, [], "out", None)
    assert out is not None
    assert out["ruling"] == "PARK"


# ---------------------------------------------------------------------------
# apply_plan_conflict_ruling -- PREAUTHORIZE_TEST_EDIT
# ---------------------------------------------------------------------------


def test_apply_preauthorize_returns_outcome_and_sets_story_fields(notifications):
    story = {"agent_instructions": "brief text"}
    out = pcr.apply_plan_conflict_ruling("planx", "PX-1", story, _ruling(), CONFLICT_FILES)

    assert out == "preauthorized"
    assert story["status"] == "changes_requested"
    assert isinstance(story["review_feedback"], str) and story["review_feedback"]
    feedback = story["review_feedback"]
    assert pcr.PLAN_CONFLICT_HEADER in feedback or "conflict" in feedback.lower(), (
        "review_feedback must point at the plan-conflict block"
    )

    instr = story["agent_instructions"]
    assert pcr.PLAN_CONFLICT_HEADER in instr
    assert instr.count(pcr.PLAN_CONFLICT_HEADER) == 1
    assert "brief text" in instr
    assert "tests/unit/test_alpha.py" in instr
    assert "test_alpha" in instr
    assert "assert a == 1" in instr
    assert "assert a == 2" in instr
    assert "assert deliverable() is True" in instr
    assert "grade the real deliverable" in instr

    record = story["plan_conflict_ruling"]
    assert record["ruling"] == "PREAUTHORIZE_TEST_EDIT"
    assert record["files"] == CONFLICT_FILES
    _assert_utc_iso(record["ts"])


def test_apply_preauthorize_notifies_with_the_preauthorized_event(notifications):
    story = {"agent_instructions": "brief text"}
    pcr.apply_plan_conflict_ruling("planx", "PX-1", story, _ruling(), CONFLICT_FILES)

    assert len(notifications) == 1
    note = notifications[0]
    assert note["plan_name"] == "planx"
    assert note["event"] == "plan_conflict_preauthorized"
    assert note["story_key"] == "PX-1"
    assert "tests/unit/test_alpha.py::test_alpha" in note["message"]


def test_apply_preauthorize_twice_replaces_and_never_stacks(notifications):
    story = {"agent_instructions": "brief text"}
    first = _ruling(before="OLD_BEFORE_MARKER", after="OLD_AFTER_MARKER")
    second = _ruling(before="NEW_BEFORE_MARKER", after="NEW_AFTER_MARKER")

    pcr.apply_plan_conflict_ruling("planx", "PX-1", story, first, CONFLICT_FILES)
    pcr.apply_plan_conflict_ruling("planx", "PX-1", story, second, CONFLICT_FILES)

    instr = story["agent_instructions"]
    assert instr.count(pcr.PLAN_CONFLICT_HEADER) == 1
    assert "NEW_BEFORE_MARKER" in instr
    assert "NEW_AFTER_MARKER" in instr
    assert "OLD_BEFORE_MARKER" not in instr
    assert "OLD_AFTER_MARKER" not in instr
    assert "brief text" in instr


@pytest.mark.parametrize("initial", [{}, {"rework_attempts": 2}])
def test_apply_preauthorize_leaves_rework_attempts_alone(notifications, initial):
    story = {"agent_instructions": "brief text", **initial}
    pcr.apply_plan_conflict_ruling("planx", "PX-1", story, _ruling(), CONFLICT_FILES)
    if "rework_attempts" in initial:
        assert story["rework_attempts"] == 2
    else:
        assert "rework_attempts" not in story


def test_apply_preauthorize_does_not_write_the_manifest(monkeypatch, notifications):
    def boom(*args, **kwargs):
        raise AssertionError("apply_plan_conflict_ruling must not write the manifest")

    monkeypatch.setattr(pipeline.server, "_atomic_write_json", boom, raising=False)
    story = {"agent_instructions": "brief text"}
    assert pcr.apply_plan_conflict_ruling("planx", "PX-1", story, _ruling(), CONFLICT_FILES) == "preauthorized"


# ---------------------------------------------------------------------------
# apply_plan_conflict_ruling -- PARK
# ---------------------------------------------------------------------------


def test_apply_park_sets_status_reason_and_record(notifications):
    story = {"agent_instructions": "brief text", "status": "changes_requested", "rework_attempts": 2}
    ruling = {"ruling": "PARK", "reason": "the pre-existing test encodes the old contract"}
    out = pcr.apply_plan_conflict_ruling("planx", "PX-1", story, ruling, CONFLICT_FILES)

    assert out == "parked"
    assert story["status"] == "parked"
    assert story["park_reason"] == (
        "plan conflict: pre-existing tests "
        + ", ".join(CONFLICT_FILES)
        + " contradict the brief - the pre-existing test encodes the old contract"
    )
    record = story["plan_conflict_ruling"]
    assert record["ruling"] == "PARK"
    assert record["files"] == CONFLICT_FILES
    _assert_utc_iso(record["ts"])
    assert story["rework_attempts"] == 2
    assert story["agent_instructions"] == "brief text"


def test_apply_park_notifies_with_story_parked_and_names_the_files(notifications):
    story = {"agent_instructions": "brief text"}
    ruling = {"ruling": "PARK", "reason": "contradiction"}
    pcr.apply_plan_conflict_ruling("planx", "PX-1", story, ruling, CONFLICT_FILES)

    assert len(notifications) == 1
    note = notifications[0]
    assert note["plan_name"] == "planx"
    assert note["event"] == "story_parked"
    assert note["story_key"] == "PX-1"
    for path in CONFLICT_FILES:
        assert path in note["message"]


# ---------------------------------------------------------------------------
# apply_plan_conflict_ruling -- REGRESSION
# ---------------------------------------------------------------------------


def test_apply_regression_records_ruling_and_changes_nothing_else(notifications):
    story = {
        "agent_instructions": "brief text",
        "status": "changes_requested",
        "review_feedback": "old feedback",
        "rework_attempts": 2,
    }
    ruling = {"ruling": "REGRESSION", "rationale": "the brief itself is wrong"}
    out = pcr.apply_plan_conflict_ruling("planx", "PX-1", story, ruling, CONFLICT_FILES)

    assert out == "regression"
    assert story["status"] == "changes_requested"
    assert story["agent_instructions"] == "brief text"
    assert story["review_feedback"] == "old feedback"
    assert story["rework_attempts"] == 2
    record = story["plan_conflict_ruling"]
    assert record["ruling"] == "REGRESSION"
    assert record["files"] == CONFLICT_FILES
    _assert_utc_iso(record["ts"])
    assert notifications == []


# ---------------------------------------------------------------------------
# REFERENCE.md documentation
# ---------------------------------------------------------------------------


def test_reference_documents_plan_conflict_rulings_before_notification_records():
    text = REFERENCE.read_text(encoding="utf-8")
    assert "## Plan-conflict rulings" in text
    assert "## Notification records" in text

    headings = re.findall(r"^## .*$", text, re.MULTILINE)
    idx = headings.index("## Plan-conflict rulings")
    assert headings[idx + 1] == "## Notification records", (
        "the new section must sit immediately before '## Notification records'"
    )


def test_reference_plan_conflict_section_covers_the_contract():
    text = REFERENCE.read_text(encoding="utf-8")
    start = text.index("## Plan-conflict rulings")
    end = text.index("## Notification records", start)
    section = text[start:end]

    assert "plan_conflict_preauthorized" in section
    assert "story_parked" in section
    assert "PREAUTHORIZE_TEST_EDIT" in section
    assert "PARK" in section
    assert "REGRESSION" in section
    assert "rework" in section.lower()
