"""FU-04 rework: one shared subject bound, and a log-safe ingester default.

Two invariants this file pins, both from the review of the first FU-04 attempt:

* the subject shape is defined **once**. The ingest boundary
  (``pipeline.ownership``) and the routing read-back
  (``pipeline.service_decisions``) must accept exactly the same subjects, so
  ``service_decisions`` imports ``subject_or_none`` instead of keeping a copy
  that only a comment keeps in sync.
* the ingester-derived owner - which is *not* held to the subject charset,
  because an OIDC principal is ``f"{issuer}#{sub}"`` - is still bounded for a
  line-oriented log: no C0/C1 control character, no DEL, no Unicode line
  separator, and a length cap. Otherwise the notifications-log write site would
  be the only guard, and a hostile IdP could still persist a value that no
  single-line record can hold.
"""

import pytest

from app.identity import LOCAL_OPERATOR
from pipeline import ownership, service_decisions
from pipeline.ownership import MAX_LENIENT_OWNER, ownership_from_plan, subject_or_none

PLAN = "payments-rollout"

#: Every character a single-line record must never contain: C0 controls (which
#: include the classic newline/CR/ANSI trio and the vertical-tab/form-feed line
#: breaks), DEL, the C1 control range (``\x85`` is NEL, a line break in Latin-1),
#: and the Unicode line/paragraph separators.
LINE_BREAKING_OR_CONTROL = [
    "\n",
    "\r",
    "\x0b",  # vertical tab: a line break to many log readers
    "\x0c",  # form feed
    "\x00",  # NUL
    "\x1b",  # ANSI escape
    "\x1b[2J",
    "\x7f",  # DEL
    "\x85",  # NEL
    "\u2028",  # LINE SEPARATOR
    "\u2029",  # PARAGRAPH SEPARATOR
]


def _refusal(owner):
    """Return the refusal *owner* gets from ingest, or ``None`` when accepted."""
    try:
        ownership_from_plan({"owner": owner}, "operator")
    except ValueError as exc:
        return str(exc)
    return None


def test_the_subject_bound_is_defined_once_and_shared():
    """Routing reuses the ingest helper rather than a copy of the pattern."""
    assert service_decisions.subject_or_none is ownership.subject_or_none
    assert not hasattr(service_decisions, "SUBJECT_RE"), (
        "service_decisions must not keep its own SUBJECT_RE: two copies drift"
    )


@pytest.mark.parametrize("raw", LINE_BREAKING_OR_CONTROL)
def test_subject_or_none_rejects_every_line_breaking_or_control_character(raw):
    """A subject carrying any control or line-separator character is not one."""
    assert subject_or_none(f"alice{raw}") is None
    assert subject_or_none(f"al{raw}ice") is None


@pytest.mark.parametrize("raw", LINE_BREAKING_OR_CONTROL)
def test_ingest_refuses_a_named_owner_with_a_control_character(raw):
    """The ingest boundary refuses the same characters, with the same refusal."""
    assert _refusal(f"alice{raw}") == ownership.OWNER_REFUSAL


def test_subject_length_boundary_is_64_characters():
    """63/64 characters are subjects; 65 and 10,000 are not."""
    assert subject_or_none("a" * 64) == "a" * 64
    assert subject_or_none("a" * 65) is None
    assert subject_or_none("a" * 10000) is None


def test_ingester_default_still_accepts_an_oidc_principal():
    """``issuer#sub`` is outside the subject charset but must keep ingesting."""
    principal = "https://idp.example.com/realms/pipeline#0f8c1d2e-user"
    result = ownership_from_plan({}, principal)
    assert result == {"owner": principal, "delegates": []}


def test_ingester_default_is_capped_for_log_safety():
    """The lenient owner is bounded in length, not just in charset."""
    assert ownership_from_plan({}, "o" * MAX_LENIENT_OWNER)["owner"] == "o" * MAX_LENIENT_OWNER
    with pytest.raises(ValueError) as excinfo:
        ownership_from_plan({}, "o" * (MAX_LENIENT_OWNER + 1))
    assert str(excinfo.value) == ownership.OWNER_REFUSAL


@pytest.mark.parametrize("raw", LINE_BREAKING_OR_CONTROL)
def test_ingester_default_refuses_a_control_character(raw):
    """A hostile IdP cannot place a control character in the stored owner."""
    with pytest.raises(ValueError) as excinfo:
        ownership_from_plan({}, f"issuer#sub{raw}")
    assert str(excinfo.value) == ownership.OWNER_REFUSAL


def test_a_hostile_ingester_default_is_not_persisted():
    """Nothing is written when the ingester-derived owner is refused."""
    with pytest.raises(ValueError) as excinfo:
        ownership_from_plan({"delegates": []}, "issuer#sub\nforged")
    assert str(excinfo.value) == ownership.OWNER_REFUSAL


def test_ingester_default_still_rejects_a_non_string():
    """``str()`` coercion stays forbidden: 123 is not a principal."""
    with pytest.raises(ValueError) as excinfo:
        ownership_from_plan({}, 123)
    assert str(excinfo.value) == ownership.OWNER_REFUSAL


@pytest.mark.parametrize("raw", ["\u2028", "\u2029", "\x85", "\x0b", "\x00", "x" * 10000])
def test_routing_falls_back_for_a_legacy_manifest_owner_nothing_can_sanitise(monkeypatch, raw):
    """A manifest already on disk still routes to the local operator.

    The read-back guard cannot be retired just because ingest refuses these
    values now: a manifest written before the ingest bound, or by any other
    writer, is still read.
    """
    monkeypatch.setattr(
        service_decisions,
        "_load_manifest",
        lambda name: {"owner": raw, "delegates": [raw, "bob"]},
    )
    routed = service_decisions._routing_target(PLAN)
    assert routed["owner"] == LOCAL_OPERATOR.subject
    assert routed["delegates"] == ["bob"]


def test_routing_uses_the_shared_helper_not_a_second_pattern(monkeypatch):
    """A subject the shared helper accepts is exactly what routing accepts."""
    monkeypatch.setattr(
        service_decisions,
        "_load_manifest",
        lambda name: {"owner": "ops:oncall", "delegates": ["team@example.com"]},
    )
    routed = service_decisions._routing_target(PLAN)
    assert routed == {"owner": subject_or_none("ops:oncall"), "delegates": ["team@example.com"]}
