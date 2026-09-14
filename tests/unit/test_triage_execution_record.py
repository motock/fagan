"""Tests for the execution-outcome decision record appended by
``pipeline.triage._apply_ruling_for_mode``.

Written FIRST (TDD).  Today ``rule_on_story`` appends the RULING record to the
plan's decisions log but nothing records the EXECUTION outcome; this story adds
that second record so every autonomous action is explainable and undoable
post-hoc.  Until the implementation lands these tests must fail for the right
reason (a missing record / missing key), never because of a bug in the test
logic itself.

Design notes
------------
* ``_append_decision`` is bound into ``pipeline.triage``'s namespace, so the
  tests patch the *triage module attribute*.  That also grades the requirement
  that the append goes through the already-imported name rather than reaching
  into ``pipeline.persistence`` directly.
* Every assertion is KEY-based (membership + value of the keys this story
  specifies) so later sibling stories can extend the same record with extra
  fields (``children``, acceptance digests) without breaking this file.
* ``PIPELINE_AUTONOMY`` is read lazily inside ``_apply_ruling_for_mode`` via
  ``from .server import PIPELINE_AUTONOMY``, so the mode is controlled by
  patching ``pipeline.server.PIPELINE_AUTONOMY``.
"""

import copy
import logging
from datetime import datetime, timedelta, timezone

import pytest

from pipeline import server, triage

PLAN = "plan-xyz"
STORY_KEY = "story-abc"

BASE_RULING = {
    "ruling": "the build failed because of a missing import",
    "tier": "hard",
    "risk": "high",
    "rationale": "the agent never added the import the story required",
    "notify_user": True,
    "action": "escalate_model",
}

# The keys this story requires on the execution-outcome record.  Membership is
# asserted (not an exact dict match) so sibling stories can add fields.
REQUIRED_KEYS = (
    "story_key",
    "question",
    "action",
    "result",
    "mode",
    "children",
    "prior_status",
    "prior_parked_reason",
    "decided_by",
    "decided_at",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_notifications(monkeypatch):
    """The dry-run branch notifies the user; never touch the real notifier."""
    monkeypatch.setattr(triage, "_notify_user", lambda *a, **k: None, raising=False)
    yield


def _capture_appends(monkeypatch):
    """Stub ``triage._append_decision`` and return the list of (plan, record)."""
    calls = []

    def fake_append(plan_name, record):
        calls.append((plan_name, record))

    monkeypatch.setattr(triage, "_append_decision", fake_append, raising=False)
    return calls


def _set_mode(monkeypatch, mode):
    monkeypatch.setattr(server, "PIPELINE_AUTONOMY", mode, raising=False)


def _run(monkeypatch, story, ruling=None, mode="gated"):
    """Drive the real ``_apply_ruling_for_mode`` and return (result, calls)."""
    _set_mode(monkeypatch, mode)
    calls = _capture_appends(monkeypatch)
    result = triage._apply_ruling_for_mode(
        PLAN,
        STORY_KEY,
        story,
        dict(BASE_RULING if ruling is None else ruling),
        {},
        None,
    )
    return result, calls


def _only_record(calls):
    assert len(calls) == 1, f"expected exactly one _append_decision call, got {len(calls)}"
    plan_name, record = calls[0]
    assert plan_name == PLAN
    return record


# ---------------------------------------------------------------------------
# Dry-run branch
# ---------------------------------------------------------------------------


def test_dry_run_records_execution_outcome_with_mode_dry_run(monkeypatch):
    story = {"status": "parked", "parked_reason": "build failed"}

    result, calls = _run(monkeypatch, story, mode="dry-run")

    assert result == "dry-run"
    record = _only_record(calls)
    for key in REQUIRED_KEYS:
        assert key in record, f"execution record is missing required key {key!r}"
    assert record["story_key"] == STORY_KEY
    assert record["question"] == "failure triage execution"
    assert record["action"] == "escalate_model"
    assert record["mode"] == "dry-run"
    assert record["children"] == []
    assert record["decided_by"] == "overlord-triage"
    # "result" is the executor's return string; the dry-run branch returns
    # "dry-run" without executing anything.
    assert record["result"] == result == "dry-run"


def test_dry_run_does_not_mutate_the_story(monkeypatch):
    story = {"status": "parked", "parked_reason": "build failed", "key": STORY_KEY}
    before = copy.deepcopy(story)
    executed = []
    monkeypatch.setattr(
        triage,
        "execute_ruling",
        lambda *a, **k: executed.append(a) or "should-not-run",
        raising=False,
    )

    result, calls = _run(monkeypatch, story, mode="dry-run")

    assert result == "dry-run"
    assert executed == [], "the dry-run branch must not invoke the executor"
    assert story == before, "the dry-run branch must leave the story untouched"
    # ...and it must still have recorded the (non-)execution outcome.
    assert _only_record(calls)["mode"] == "dry-run"


def test_dry_run_records_prior_state_from_before_execution(monkeypatch):
    story = {"status": "parked", "parked_reason": "build failed"}

    _, calls = _run(monkeypatch, story, mode="dry-run")

    record = _only_record(calls)
    assert record["prior_status"] == "parked"
    assert record["prior_parked_reason"] == "build failed"


def test_dry_run_records_even_when_the_ruling_has_no_action(monkeypatch):
    """Boundary: a malformed ruling with no ACTION still produces a record."""
    _, calls = _run(monkeypatch, {"status": "parked"}, ruling={}, mode="dry-run")

    record = _only_record(calls)
    assert record["action"] == "unknown"
    assert record["mode"] == "dry-run"


# ---------------------------------------------------------------------------
# Executed (gated / full) branch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["gated", "full"])
def test_executed_ruling_records_the_executor_return_string(monkeypatch, mode):
    story = {"status": "parked", "parked_reason": "build failed"}
    monkeypatch.setattr(
        triage, "execute_ruling", lambda *a, **k: "escalate_model", raising=False
    )

    result, calls = _run(monkeypatch, story, mode=mode)

    assert result == "escalate_model"
    record = _only_record(calls)
    for key in REQUIRED_KEYS:
        assert key in record, f"execution record is missing required key {key!r}"
    assert record["result"] == "escalate_model"
    assert record["mode"] == mode
    assert record["action"] == "escalate_model"
    assert record["story_key"] == STORY_KEY
    assert record["question"] == "failure triage execution"
    assert record["children"] == []
    assert record["decided_by"] == "overlord-triage"
    assert record["prior_status"] == "parked"
    assert record["prior_parked_reason"] == "build failed"


def test_prior_state_is_snapshotted_before_the_executor_mutates_the_story(monkeypatch):
    """The snapshot must be taken BEFORE the executor touches the story dict."""
    story = {"status": "parked", "parked_reason": "old reason"}

    def fake_execute(plan_name, story_key, story_dict, ruling, manifest, manifest_path):
        story_dict["status"] = "in_progress"
        story_dict["parked_reason"] = "new reason"
        return "escalate_model"

    monkeypatch.setattr(triage, "execute_ruling", fake_execute, raising=False)

    result, calls = _run(monkeypatch, story, mode="gated")

    assert result == "escalate_model"
    assert story["status"] == "in_progress", "the stub executor must have run"
    record = _only_record(calls)
    assert record["prior_status"] == "parked"
    assert record["prior_parked_reason"] == "old reason"


def test_executed_park_records_the_park_result_and_prior_state(monkeypatch):
    story = {"status": "in_progress"}

    def fake_execute(plan_name, story_key, story_dict, ruling, manifest, manifest_path):
        story_dict["status"] = "parked"
        story_dict["parked_reason"] = "unhandled ruling action"
        return "park_for_human"

    monkeypatch.setattr(triage, "execute_ruling", fake_execute, raising=False)

    result, calls = _run(monkeypatch, story, mode="gated")

    assert result == "park_for_human"
    record = _only_record(calls)
    assert record["result"] == "park_for_human"
    assert record["prior_status"] == "in_progress"
    assert record["prior_parked_reason"] in (None, "")


def test_record_is_appended_after_the_executor_returns(monkeypatch):
    calls = _capture_appends(monkeypatch)
    _set_mode(monkeypatch, "gated")
    seen = []

    def fake_execute(*a, **k):
        seen.append(len(calls))
        return "park_for_human"

    monkeypatch.setattr(triage, "execute_ruling", fake_execute, raising=False)

    triage._apply_ruling_for_mode(
        PLAN, STORY_KEY, {"status": "parked"}, dict(BASE_RULING), {}, None
    )

    assert seen == [0], "the execution record must be appended only after the executor returns"
    assert len(calls) == 1


def test_mode_reflects_the_autonomy_value_at_call_time(monkeypatch):
    calls = _capture_appends(monkeypatch)
    monkeypatch.setattr(
        triage, "execute_ruling", lambda *a, **k: "escalate_model", raising=False
    )

    _set_mode(monkeypatch, "gated")
    triage._apply_ruling_for_mode(
        PLAN, STORY_KEY, {"status": "parked"}, dict(BASE_RULING), {}, None
    )
    _set_mode(monkeypatch, "dry-run")
    triage._apply_ruling_for_mode(
        PLAN, STORY_KEY, {"status": "parked"}, dict(BASE_RULING), {}, None
    )

    assert [record["mode"] for _, record in calls] == ["gated", "dry-run"]


# ---------------------------------------------------------------------------
# Record shape / boundaries
# ---------------------------------------------------------------------------


def test_prior_parked_reason_is_empty_when_the_story_had_none(monkeypatch):
    _, calls = _run(monkeypatch, {"status": "in_progress"}, mode="dry-run")

    record = _only_record(calls)
    assert "prior_parked_reason" in record
    assert record["prior_parked_reason"] in (None, "")
    assert record["prior_status"] == "in_progress"


def test_children_is_an_empty_list_placeholder(monkeypatch):
    _, calls = _run(monkeypatch, {"status": "parked"}, mode="dry-run")

    children = _only_record(calls)["children"]
    assert isinstance(children, list)
    assert children == []


def test_decided_at_is_iso_utc(monkeypatch):
    before = datetime.now(timezone.utc)

    _, calls = _run(monkeypatch, {"status": "parked"}, mode="dry-run")

    after = datetime.now(timezone.utc)
    raw = _only_record(calls)["decided_at"]
    assert isinstance(raw, str) and raw
    parsed = datetime.fromisoformat(raw)
    assert parsed.tzinfo is not None, "decided_at must be timezone-aware"
    assert parsed.utcoffset() == timedelta(0), "decided_at must be UTC"
    assert before - timedelta(seconds=5) <= parsed <= after + timedelta(seconds=5)


# ---------------------------------------------------------------------------
# Audit-write failures must never escape the executor path
# ---------------------------------------------------------------------------


def _raising_append(plan_name, record):
    raise RuntimeError("audit log is read-only")


def test_append_failure_does_not_propagate_from_the_executor_path(monkeypatch, caplog):
    story = {"status": "parked", "parked_reason": "build failed"}
    monkeypatch.setattr(
        triage, "execute_ruling", lambda *a, **k: "escalate_model", raising=False
    )
    _set_mode(monkeypatch, "gated")
    monkeypatch.setattr(triage, "_append_decision", _raising_append, raising=False)

    with caplog.at_level(logging.WARNING, logger="pipeline"):
        result = triage._apply_ruling_for_mode(
            PLAN, STORY_KEY, story, dict(BASE_RULING), {}, None
        )

    assert result == "escalate_model"
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING and STORY_KEY in record.getMessage()
    ]
    assert messages, "a warning naming the story key must be logged"
    assert all(PLAN not in message for message in messages), (
        "the warning must name only the story key"
    )


def test_append_failure_does_not_propagate_from_the_dry_run_branch(monkeypatch):
    _set_mode(monkeypatch, "dry-run")
    attempts = []

    def raising_append(plan_name, record):
        attempts.append((plan_name, record))
        raise RuntimeError("audit log is read-only")

    monkeypatch.setattr(triage, "_append_decision", raising_append, raising=False)

    result = triage._apply_ruling_for_mode(
        PLAN, STORY_KEY, {"status": "parked"}, dict(BASE_RULING), {}, None
    )

    assert result == "dry-run"
    assert len(attempts) == 1, "the dry-run branch must still attempt the audit write"
    assert attempts[0][1]["mode"] == "dry-run"


# ---------------------------------------------------------------------------
# Optional shared helper (shape note for sibling stories 5/6/7)
# ---------------------------------------------------------------------------


def test_record_execution_helper_forwards_extra_fields_when_present(monkeypatch):
    """If the optional ``_record_execution`` wrapper exists it must stay a thin
    wrapper over ``_append_decision`` that forwards extra fields (children,
    acceptance digests) for the later sibling stories."""
    helper = getattr(triage, "_record_execution", None)
    if helper is None:
        pytest.skip(
            "optional _record_execution helper not implemented; the record may be "
            "appended inline"
        )

    calls = _capture_appends(monkeypatch)
    try:
        helper(
            PLAN,
            STORY_KEY,
            action="split_story",
            result="split",
            mode="gated",
            prior_status="parked",
            prior_parked_reason="build failed",
            children=["S1", "S2"],
            acceptance_digest="abc123",
        )
    except TypeError as exc:  # pragma: no cover - different helper signature
        pytest.skip(f"_record_execution has a different signature: {exc}")

    assert len(calls) == 1
    plan_name, record = calls[0]
    assert plan_name == PLAN
    assert record["story_key"] == STORY_KEY
    assert record["question"] == "failure triage execution"
    assert record["children"] == ["S1", "S2"]
    assert record["acceptance_digest"] == "abc123"
