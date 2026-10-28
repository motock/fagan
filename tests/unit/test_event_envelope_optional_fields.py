"""TDD tests for the optional envelope fields on ``make_event``.

``make_event`` accepts three keyword-only fields -- ``event_id``,
``schema_version`` and ``idempotency_key`` -- each of which is added to the
returned envelope ONLY when supplied. The legacy envelope shape (``type``,
``plan``, ``story_key``, ``payload``, ``ts``) is unchanged when none of them are
given, so every existing caller keeps working untouched.

Validation is shape-only and raises ``ValueError`` whose message names the
offending field (never the value):
  * ``event_id``         non-empty ``str`` of at most 64 characters
  * ``schema_version``   ``int`` >= 1 (``bool`` is not accepted as a version)
  * ``idempotency_key``  non-empty ``str`` of at most 128 characters
"""

from __future__ import annotations

import json

import pytest

import pipeline.server  # noqa: F401  (must precede other pipeline imports: import cycle)
from pipeline.events import JsonlEventBus, make_event

EVENT_TYPE = "tests_passed"
PLAN = "envelope-optional-fields"

LEGACY_KEYS = ["type", "plan", "story_key", "payload", "ts"]
NEW_FIELDS = ("event_id", "schema_version", "idempotency_key")


def _event(**kwargs) -> dict:
    return make_event(EVENT_TYPE, PLAN, **kwargs)


# ---------------------------------------------------------------------------
# (a) legacy call shape is byte-identical
# ---------------------------------------------------------------------------
def test_legacy_call_shape_unchanged():
    event = make_event(EVENT_TYPE, PLAN)

    assert list(event.keys()) == LEGACY_KEYS
    assert set(event) == set(LEGACY_KEYS)
    for field in NEW_FIELDS:
        assert field not in event, f"{field} must be absent on a legacy call"


def test_legacy_call_keeps_default_values():
    event = make_event(EVENT_TYPE, PLAN)

    assert event["type"] == EVENT_TYPE
    assert event["plan"] == PLAN
    assert event["story_key"] is None
    assert event["payload"] == {}
    assert isinstance(event["ts"], str) and event["ts"]


# ---------------------------------------------------------------------------
# (b) each field present only when given
# ---------------------------------------------------------------------------
def test_event_id_present_only_when_given():
    without = make_event(EVENT_TYPE, PLAN)
    assert "event_id" not in without

    with_event_id = make_event(EVENT_TYPE, PLAN, event_id="evt-1")
    assert with_event_id["event_id"] == "evt-1"
    assert list(with_event_id.keys()) == [*LEGACY_KEYS, "event_id"]
    assert "schema_version" not in with_event_id
    assert "idempotency_key" not in with_event_id


def test_schema_version_present_only_when_given():
    without = make_event(EVENT_TYPE, PLAN)
    assert "schema_version" not in without

    with_version = make_event(EVENT_TYPE, PLAN, schema_version=2)
    assert with_version["schema_version"] == 2
    assert list(with_version.keys()) == [*LEGACY_KEYS, "schema_version"]
    assert "event_id" not in with_version
    assert "idempotency_key" not in with_version


def test_idempotency_key_present_only_when_given():
    without = make_event(EVENT_TYPE, PLAN)
    assert "idempotency_key" not in without

    with_key = make_event(EVENT_TYPE, PLAN, idempotency_key="idem-1")
    assert with_key["idempotency_key"] == "idem-1"
    assert list(with_key.keys()) == [*LEGACY_KEYS, "idempotency_key"]
    assert "event_id" not in with_key
    assert "schema_version" not in with_key


def test_all_three_fields_together_in_declared_order():
    event = make_event(
        EVENT_TYPE,
        PLAN,
        event_id="evt-9",
        schema_version=3,
        idempotency_key="idem-9",
    )

    assert event["event_id"] == "evt-9"
    assert event["schema_version"] == 3
    assert event["idempotency_key"] == "idem-9"
    assert list(event.keys()) == [*LEGACY_KEYS, "event_id", "schema_version", "idempotency_key"]


# ---------------------------------------------------------------------------
# (c) explicit ``None`` behaves exactly like omission
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("field", NEW_FIELDS)
def test_explicit_none_behaves_like_omission(field):
    event = make_event(EVENT_TYPE, PLAN, **{field: None})

    assert field not in event
    assert list(event.keys()) == LEGACY_KEYS


def test_all_explicit_none_matches_legacy_shape():
    event = make_event(
        EVENT_TYPE,
        PLAN,
        event_id=None,
        schema_version=None,
        idempotency_key=None,
    )

    assert list(event.keys()) == LEGACY_KEYS


# ---------------------------------------------------------------------------
# (d) invalid values are rejected, message names the field
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad_event_id", ["", "x" * 65, 123])
def test_invalid_event_id_rejected(bad_event_id):
    with pytest.raises(ValueError, match="event_id"):
        make_event(EVENT_TYPE, PLAN, event_id=bad_event_id)


@pytest.mark.parametrize("bad_schema_version", [0, -1, "1"])
def test_invalid_schema_version_rejected(bad_schema_version):
    with pytest.raises(ValueError, match="schema_version"):
        make_event(EVENT_TYPE, PLAN, schema_version=bad_schema_version)


@pytest.mark.parametrize("bad_idempotency_key", ["", "k" * 129])
def test_invalid_idempotency_key_rejected(bad_idempotency_key):
    with pytest.raises(ValueError, match="idempotency_key"):
        make_event(EVENT_TYPE, PLAN, idempotency_key=bad_idempotency_key)


def test_error_message_does_not_echo_offending_value():
    secret = "sensitive-payload-marker"
    with pytest.raises(ValueError) as excinfo:
        make_event(EVENT_TYPE, PLAN, event_id=secret * 3)

    assert secret not in str(excinfo.value)


# ---------------------------------------------------------------------------
# (e) boundaries are accepted and stored unchanged
# ---------------------------------------------------------------------------
def test_boundaries_accepted_unchanged():
    long_event_id = "x" * 64
    long_idem_key = "k" * 128

    event = make_event(
        EVENT_TYPE,
        PLAN,
        event_id=long_event_id,
        schema_version=1,
        idempotency_key=long_idem_key,
    )

    assert event["event_id"] == long_event_id
    assert event["schema_version"] == 1
    assert event["idempotency_key"] == long_idem_key


# ---------------------------------------------------------------------------
# (f) no state leaks between calls
# ---------------------------------------------------------------------------
def test_optional_fields_do_not_leak_across_calls():
    e1 = make_event(EVENT_TYPE, "plan-t1", event_id="e-1")
    e2 = make_event(EVENT_TYPE, "plan-t2")
    e3 = make_event(EVENT_TYPE, "plan-t3", idempotency_key="k1")

    assert "event_id" in e1
    assert "event_id" not in e2
    assert "idempotency_key" in e3
    assert "event_id" not in e3


def test_raising_call_leaves_no_residue():
    with pytest.raises(ValueError, match="event_id"):
        make_event(EVENT_TYPE, "plan-t4", event_id="x" * 65)

    follow_up = make_event(EVENT_TYPE, "plan-t4")
    assert "event_id" not in follow_up
    assert list(follow_up.keys()) == LEGACY_KEYS


# ---------------------------------------------------------------------------
# (g) JsonlEventBus round-trip
# ---------------------------------------------------------------------------
def test_jsonl_event_bus_round_trips_optional_fields(tmp_path):
    bus = JsonlEventBus(tmp_path)
    event = make_event(
        EVENT_TYPE,
        "round-trip",
        event_id="evt-rt",
        schema_version=7,
        idempotency_key="idem-rt",
    )

    bus.publish(event)

    log_path = tmp_path / "round-trip.events.jsonl"
    assert log_path.exists()
    lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1

    restored = json.loads(lines[0])
    assert restored["event_id"] == "evt-rt"
    assert restored["schema_version"] == 7
    assert restored["idempotency_key"] == "idem-rt"
    assert set(restored) == set(event)


def test_jsonl_event_bus_round_trips_legacy_shape(tmp_path):
    bus = JsonlEventBus(tmp_path)
    bus.publish(make_event(EVENT_TYPE, "legacy-round-trip"))

    log_path = tmp_path / "legacy-round-trip.events.jsonl"
    restored = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])

    assert list(restored.keys()) == LEGACY_KEYS
    for field in NEW_FIELDS:
        assert field not in restored
