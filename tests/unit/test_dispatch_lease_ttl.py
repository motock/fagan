"""Regression tests for dispatch-lease TTL validation (LOCKSTARVE-B2 rework).

Reviewer's blocking finding: the resolved TTL is never validated, so a
non-positive TTL (env ``PIPELINE_DISPATCH_LEASE_TTL_SECONDS=0``/negative, or
explicit ``ttl_s <= 0``) parses via ``int()``, persists an already-expired
lease, and ``claim_dispatch_lease`` still returns True — the
double-dispatch guard this module exists to provide is silently disabled.
An absurdly large env value passes ``int()`` but raises ``OverflowError``
from ``timedelta(seconds=ttl)``, contradicting the documented "malformed
value degrades to the default, never raising" contract.

Expected behavior pinned here:

- env values ``"0"``, ``"-5"``, ``"99999999999999999"`` degrade to
  ``_DEFAULT_TTL_S`` (never raise); the persisted lease is born live at
  ``now + _DEFAULT_TTL_S`` and actually blocks a second claimer.
- explicit ``ttl_s`` outside ``(0, _MAX_TTL_S]`` raises ``ValueError``
  BEFORE any state mutation (the lease store is left byte-identical).
- a valid small env value (``"1"``) is still honored exactly, so the
  validation must not over-degrade good values.
"""

import copy
import os
from datetime import datetime, timedelta, timezone

import pytest

from pipeline.dispatch_lease import (
    _DEFAULT_TTL_S,
    _TTL_ENV_VAR,
    claim_dispatch_lease,
)

NOW_1 = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
# A second tick 5 s later, nominally from a different claimer process.
NOW_2 = NOW_1 + timedelta(seconds=5)
OTHER_PID = 424242


def _fresh_story() -> dict:
    return {"id": "LOCKSTARVE-B2", "status": "todo"}


def _parse_iso(value) -> datetime:
    """Parse the persisted lease timestamp as an instant.

    Accepts a trailing ``Z`` so the assertion pins the instant rather than
    one particular ISO-8601 spelling of it.
    """
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


@pytest.mark.parametrize("bad_env", ["0", "-5", "99999999999999999"])
def test_env_malformed_ttl_degrades_to_default(monkeypatch, bad_env):
    """A malformed env TTL degrades to the default and yields a LIVE lease.

    Before the fix: ``"0"``/``"-5"`` persist ``expires_at == now`` (born
    already expired) while still returning True, and the huge value raises
    ``OverflowError`` from ``timedelta`` — both wrong.
    """
    monkeypatch.setenv(_TTL_ENV_VAR, bad_env)
    story = _fresh_story()

    first = claim_dispatch_lease(story, now=NOW_1)

    assert first is True
    expected_expires_at = NOW_1 + timedelta(seconds=_DEFAULT_TTL_S)
    # The lease must be born live at now + default TTL — NOT already
    # expired at ``now``.
    assert _parse_iso(story["dispatch_lease_expires_at"]) == expected_expires_at

    # The state call 1 leaves behind must actually block a second claimer
    # 5 s later (the double-dispatch LOCKSTARVE-B2 forbids).
    monkeypatch.setattr(os, "getpid", lambda: OTHER_PID)
    second = claim_dispatch_lease(story, now=NOW_2)
    assert second is False
    # The rejected claim leaves the live lease byte-identical.
    assert _parse_iso(story["dispatch_lease_expires_at"]) == expected_expires_at


@pytest.mark.parametrize("bad_ttl", [0, -5])
def test_explicit_non_positive_ttl_raises_and_writes_nothing(monkeypatch, bad_ttl):
    """Explicit ``ttl_s <= 0`` fails secure: ValueError, no lease written."""
    story = _fresh_story()
    before = copy.deepcopy(story)

    with pytest.raises(ValueError):
        claim_dispatch_lease(story, now=NOW_1, ttl_s=bad_ttl)

    # The ValueError must fire before any state mutation: the story (the
    # lease store) is byte-identical to before the call.
    assert story == before

    # The store still works afterwards: a valid claim succeeds normally.
    assert claim_dispatch_lease(story, now=NOW_1, ttl_s=900) is True
    assert _parse_iso(story["dispatch_lease_expires_at"]) == NOW_1 + timedelta(seconds=900)


def test_explicit_huge_ttl_raises(monkeypatch):
    """The upper cap applies to the explicit ``ttl_s`` path too."""
    story = _fresh_story()
    before = copy.deepcopy(story)

    with pytest.raises(ValueError):
        claim_dispatch_lease(story, now=NOW_1, ttl_s=99999999999999999)

    assert story == before


def test_env_valid_small_ttl_still_honored_exactly(monkeypatch):
    """A valid env TTL is honored exactly — validation must not over-degrade."""
    monkeypatch.setenv(_TTL_ENV_VAR, "1")
    story = _fresh_story()

    assert claim_dispatch_lease(story, now=NOW_1) is True
    assert _parse_iso(story["dispatch_lease_expires_at"]) == NOW_1 + timedelta(seconds=1)