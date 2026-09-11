"""Tests for pipeline/dispatch_lease.py — the dispatch lease primitive
(story B4 groundwork; B3 wires it into advance_pipeline).

Today ``advance_pipeline`` holds the plan lock for the whole tick, so a
story cannot be dispatched twice. Releasing that lock across the
synchronous model phases opens a window where a second tick (or a second
MCP server process) could observe the story still in ``todo`` and dispatch
it again — two agents in one worktree, the documented cause of repeated
zero-output agent deaths (see ``_plan_lock``'s docstring in
pipeline/concurrency.py). The lease is the cross-tick / cross-process
guard: a story carries ``dispatch_lease_expires_at`` +
``dispatch_lease_owner_pid`` and a second claimer must see the live lease
and back off.

The contract under test:

- ``claim_dispatch_lease(story, *, now=None, ttl_s=None) -> bool`` —
  mutates ``story`` in place, returns True iff the claim succeeded. A live
  lease is never stolen (False, zero mutation). An absent/malformed/expired
  lease is re-claimable (fail secure in the *other* direction: corrupt
  fields must never withhold work forever).
- ``lease_is_live(story, *, now=None) -> bool`` — True only when
  ``dispatch_lease_expires_at`` is present, parses as a timezone-aware
  ISO-8601 timestamp, and is strictly in the future relative to ``now``.
  Naive timestamps are NOT live (never guess a timezone); expiry is
  exclusive (a lease expiring at exactly ``now`` is not live).

``now`` / ``ttl_s`` are injectable purely for determinism; one test below
also pins that the no-argument production call path works. The TTL env var
is stubbed per-test with monkeypatch — never asserted against the ambient
environment (.claude/rules/testing-config-gates.md).
"""

import inspect
import os
from datetime import datetime, timedelta, timezone

import pytest

from pipeline import dispatch_lease as dl

TTL_ENV = "PIPELINE_DISPATCH_LEASE_TTL_SECONDS"
EXPIRES_KEY = "dispatch_lease_expires_at"
OWNER_PID_KEY = "dispatch_lease_owner_pid"

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clean_ttl_env(monkeypatch):
    """Remove the TTL env var so every test starts from the documented
    default (1800) unless it explicitly stubs a value. The unit conftest
    already clears PIPELINE_* at import time; this makes the intent
    explicit per-test so no assertion depends on the ambient environment.
    """
    monkeypatch.delenv(TTL_ENV, raising=False)


def _parse_iso(value):
    """Parse an ISO-8601 string tolerating both 'Z' and '+00:00' UTC
    suffixes (the spec mandates ISO-8601 UTC but not a specific suffix).
    Returns None when unparseable — mirroring the fail-secure rule.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    if text.endswith("Z"):
        try:
            return datetime.fromisoformat(text[:-1] + "+00:00")
        except ValueError:
            return None
    return None


def _assert_utc_iso_string(stored):
    """Assert the stored lease expiry is an ISO-8601 UTC timestamp string
    and return it parsed."""
    assert isinstance(stored, str), (
        f"dispatch_lease_expires_at must be stored as an ISO-8601 string, "
        f"got {type(stored).__name__}"
    )
    parsed = _parse_iso(stored)
    assert parsed is not None, (
        f"dispatch_lease_expires_at is not a parseable ISO-8601 timestamp: "
        f"{stored!r}"
    )
    assert parsed.tzinfo is not None, (
        f"stored lease expiry must be timezone-aware, got naive {stored!r}"
    )
    assert parsed.utcoffset() == timedelta(0), (
        f"stored lease expiry must be UTC (offset 0), got {stored!r}"
    )
    return parsed


# --------------------------------------------------------------------------
# Positive
# --------------------------------------------------------------------------


def test_claim_fresh_story_sets_both_fields_and_lease_is_live():
    story = {}

    assert dl.claim_dispatch_lease(story, now=NOW) is True

    # Both fields are set, in place, on the caller's dict.
    assert EXPIRES_KEY in story
    assert OWNER_PID_KEY in story
    assert story[OWNER_PID_KEY] == os.getpid()

    # Default TTL (env var absent -> 1800) is honoured.
    expiry = _assert_utc_iso_string(story[EXPIRES_KEY])
    assert expiry == NOW + timedelta(seconds=1800)

    # The freshly claimed lease is live, and stays live until it expires.
    assert dl.lease_is_live(story, now=NOW) is True
    assert dl.lease_is_live(story, now=NOW + timedelta(seconds=1799)) is True
    assert dl.lease_is_live(story, now=NOW + timedelta(seconds=1800)) is False


def test_production_callers_pass_neither_now_nor_ttl():
    """The injectable kwargs exist for tests; the plain production call
    (no kwargs at all) must work and produce a live lease."""
    story = {}

    assert dl.claim_dispatch_lease(story) is True
    assert dl.lease_is_live(story) is True

    expiry = _assert_utc_iso_string(story[EXPIRES_KEY])
    assert expiry > datetime.now(timezone.utc)
    assert story[OWNER_PID_KEY] == os.getpid()


def test_expired_lease_can_be_reclaimed_and_expiry_moves_forward():
    story = {}
    assert dl.claim_dispatch_lease(story, now=NOW, ttl_s=60) is True
    old_expiry = story[EXPIRES_KEY]

    # One second past expiry: the lease is dead, the story is re-claimable.
    later = NOW + timedelta(seconds=61)
    assert dl.lease_is_live(story, now=later) is False
    assert dl.claim_dispatch_lease(story, now=later, ttl_s=60) is True

    new_expiry = _assert_utc_iso_string(story[EXPIRES_KEY])
    assert new_expiry == later + timedelta(seconds=60)
    assert new_expiry > _parse_iso(old_expiry)


def test_ttl_s_is_honoured_exactly():
    story = {}

    assert dl.claim_dispatch_lease(story, now=NOW, ttl_s=60) is True

    expiry = _assert_utc_iso_string(story[EXPIRES_KEY])
    assert expiry == NOW + timedelta(seconds=60)


# --------------------------------------------------------------------------
# Negative / boundary
# --------------------------------------------------------------------------


def test_second_claim_while_live_returns_false_and_leaves_both_fields_identical():
    story = {}
    assert dl.claim_dispatch_lease(story, now=NOW, ttl_s=1800) is True
    expires_before = story[EXPIRES_KEY]
    pid_before = story[OWNER_PID_KEY]

    # A second tick (or second MCP server process) one second later.
    assert dl.claim_dispatch_lease(story, now=NOW + timedelta(seconds=1)) is False

    # Byte-identical: no refresh, no owner change, no partial write.
    assert story[EXPIRES_KEY] == expires_before
    assert story[OWNER_PID_KEY] == pid_before


@pytest.mark.parametrize(
    "lease_value",
    [
        pytest.param(None, id="none"),
        pytest.param("", id="empty-string"),
        pytest.param(12345, id="int"),
        pytest.param(1.5, id="float"),
        pytest.param(["2026-01-01T13:00:00+00:00"], id="list"),
        pytest.param("not-a-date", id="garbage-string"),
        pytest.param(
            (NOW + timedelta(hours=1)).replace(tzinfo=None).isoformat(),
            id="naive-timestamp",
        ),
    ],
)
def test_lease_is_live_false_for_missing_or_malformed_lease(lease_value):
    story = {EXPIRES_KEY: lease_value}

    assert dl.lease_is_live(story, now=NOW) is False, (
        f"a malformed lease must fail secure (not live): {lease_value!r}"
    )


def test_lease_is_live_false_when_field_missing_entirely():
    story = {"status": "todo"}
    assert EXPIRES_KEY not in story
    assert dl.lease_is_live(story, now=NOW) is False


def test_lease_expiring_at_exactly_now_is_not_live():
    """Expiry is exclusive. A lease expiring at EXACTLY ``now`` is not
    live — an off-by-one here is the difference between a double dispatch
    and a stuck story, so pin the boundary from both sides."""
    boundary = NOW + timedelta(seconds=1800)
    story = {EXPIRES_KEY: boundary.isoformat()}

    assert dl.lease_is_live(story, now=boundary) is False
    # One second before the boundary the lease IS still live (the
    # boundary itself is not — asserted above).
    assert dl.lease_is_live(story, now=boundary - timedelta(seconds=1)) is True
    # And the just-expired lease may be re-claimed at the exact boundary.
    assert dl.claim_dispatch_lease(story, now=boundary) is True


def test_malformed_ttl_env_degrades_to_default_1800_without_raising(monkeypatch):
    monkeypatch.setenv(TTL_ENV, "abc")
    story = {}

    # Must not raise, whatever the env holds.
    assert dl.claim_dispatch_lease(story, now=NOW) is True

    expiry = _assert_utc_iso_string(story[EXPIRES_KEY])
    assert expiry == NOW + timedelta(seconds=1800)


def test_valid_ttl_env_is_honoured(monkeypatch):
    monkeypatch.setenv(TTL_ENV, "900")
    story = {}

    assert dl.claim_dispatch_lease(story, now=NOW) is True

    expiry = _assert_utc_iso_string(story[EXPIRES_KEY])
    assert expiry == NOW + timedelta(seconds=900)


def test_claim_leaves_unrelated_keys_untouched():
    story = {
        "id": "S-42",
        "status": "todo",
        "title": "wire the lease",
        "attempts": 2,
        "notes": ["a", "b"],
    }
    unrelated = {k: v for k, v in story.items()}

    assert dl.claim_dispatch_lease(story, now=NOW) is True

    for key, value in unrelated.items():
        assert story[key] == value, f"claim mutated unrelated key {key!r}"
    # Exactly the two lease keys were added — nothing else.
    assert set(story) - set(unrelated) == {EXPIRES_KEY, OWNER_PID_KEY}


def test_malformed_lease_never_withholds_work_forever():
    """Fail secure in the other direction: a corrupt lease field must be
    re-claimable, so a broken manifest can't park a story forever."""
    for corrupt in ["not-a-date", "", None, 12345]:
        story = {EXPIRES_KEY: corrupt}
        assert dl.claim_dispatch_lease(story, now=NOW) is True, (
            f"a corrupt lease ({corrupt!r}) must be re-claimable"
        )
        assert story[EXPIRES_KEY] != corrupt


def test_naive_timestamp_lease_is_reclaimable():
    """A naive (timezone-less) expiry is not live — and must not be
    silently honoured by claiming either."""
    naive = (NOW + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    story = {EXPIRES_KEY: naive}

    assert dl.lease_is_live(story, now=NOW) is False
    assert dl.claim_dispatch_lease(story, now=NOW) is True
    _assert_utc_iso_string(story[EXPIRES_KEY])


# --------------------------------------------------------------------------
# Module surface: exactly the two functions, keyword-only injectables
# --------------------------------------------------------------------------


def test_module_exposes_exactly_the_two_public_functions():
    public_functions = {
        name
        for name, obj in inspect.getmembers(dl, inspect.isfunction)
        if not name.startswith("_") and obj.__module__ == dl.__name__
    }
    assert public_functions == {"claim_dispatch_lease", "lease_is_live"}


def test_injectables_are_keyword_only():
    claim_params = inspect.signature(dl.claim_dispatch_lease).parameters
    live_params = inspect.signature(dl.lease_is_live).parameters

    assert list(claim_params) == ["story", "now", "ttl_s"]
    assert claim_params["now"].kind is inspect.Parameter.KEYWORD_ONLY
    assert claim_params["ttl_s"].kind is inspect.Parameter.KEYWORD_ONLY
    assert claim_params["now"].default is None
    assert claim_params["ttl_s"].default is None

    assert list(live_params) == ["story", "now"]
    assert live_params["now"].kind is inspect.Parameter.KEYWORD_ONLY
    assert live_params["now"].default is None


def test_module_does_not_log_story_contents():
    """The lease primitive must not log story contents (privacy: manifests
    carry user work). Source-level guard: no logging machinery at all."""
    source = inspect.getsource(dl)
    assert "import logging" not in source
    assert "getLogger" not in source