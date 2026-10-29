"""FU-04 (ingest boundary): a hostile plan owner is refused where it enters.

``ownership_from_plan`` is the ingest boundary that decides which owner is
persisted onto the manifest. It used to accept any non-empty string, so an owner
carrying a newline, an ANSI escape, or 10 KB of text was persisted and later
interpolated into the line-oriented notifications log.

The bound is the same subject shape ``_routing_target`` applies on the way out -
``[A-Za-z0-9][A-Za-z0-9._@:-]{0,63}`` - and the refusal is the exact existing
structured refusal (``ValueError``) an empty owner already gets, because the
ingest path turns only ``ValueError`` into its ``{"ok": False}`` refusal.
"""

import pytest

from app.identity import LOCAL_OPERATOR
from pipeline.ownership import ownership_from_plan

HOSTILE_OWNERS = ["a\nb", "\x1b[2J", "x" * 10000, {"x": 1}, 123, "alice\n"]
VALID_OWNERS = ["alice", "team@example.com", "ops:oncall"]

INGESTED_BY = LOCAL_OPERATOR.subject


def _refusal(owner):
    """Return the exact refusal *owner* produces, or None when accepted."""
    try:
        ownership_from_plan({"owner": owner}, INGESTED_BY)
    except ValueError as exc:
        return str(exc)
    return None


#: The refusal an already-rejected owner (a non-string) gets today. A hostile
#: subject must reuse it verbatim rather than introduce a new shape, because the
#: ingest path turns only this ValueError into its structured {"ok": False}.
PRE_EXISTING_REFUSAL = "invalid owner: must be a non-empty string"


@pytest.mark.parametrize("owner", HOSTILE_OWNERS)
def test_hostile_owner_is_refused_with_the_existing_structured_refusal(owner):
    """A hostile owner is refused exactly like an already-invalid one."""
    assert _refusal({"x": 1}) == PRE_EXISTING_REFUSAL
    assert _refusal(owner) == PRE_EXISTING_REFUSAL


def test_empty_owner_and_ingester_is_the_same_refusal():
    """The empty-owner case keeps refusing with the same message."""
    with pytest.raises(ValueError) as excinfo:
        ownership_from_plan({"owner": ""}, "")
    assert str(excinfo.value) == PRE_EXISTING_REFUSAL


@pytest.mark.parametrize("owner", VALID_OWNERS)
def test_valid_owner_takes_the_unchanged_success_path(owner):
    """A valid subject is still persisted verbatim and unchanged."""
    assert ownership_from_plan({"owner": owner}, INGESTED_BY) == {
        "owner": owner,
        "delegates": [],
    }


def test_default_ingester_owner_still_applies():
    """A plan naming no owner still defaults to the ingester."""
    assert ownership_from_plan({}, INGESTED_BY) == {
        "owner": INGESTED_BY,
        "delegates": [],
    }


def test_hostile_owner_is_never_persisted_before_a_valid_one():
    """Worked example B: nothing hostile is handed to the persistence path."""
    assert _refusal("a\nb") == PRE_EXISTING_REFUSAL

    accepted = ownership_from_plan({"owner": "team@example.com"}, INGESTED_BY)

    assert accepted == {"owner": "team@example.com", "delegates": []}
