"""Tests for the dead-owner rule in pipeline/dispatch_lease.py (LOCKSTARVE-B2
follow-up): a dispatch lease whose recorded owner process is gone must read
as NOT live, so a story is re-dispatchable right after the scheduler restarts
mid-dispatch instead of waiting out the 30-minute TTL.

The contract under test (public API of ``pipeline.dispatch_lease`` only):

- ``lease_is_live(story, *, now=None)`` — a lease whose
  ``dispatch_lease_expires_at`` is live BUT whose
  ``dispatch_lease_owner_pid`` is a plain positive int naming a process that
  no longer exists reads as NOT live (``os.kill(pid, 0)`` raising
  ``ProcessLookupError``). ``PermissionError`` means the owner exists (owned
  by someone else), so the lease stays live.
- An owner pid that is not a plain int (``None``, a string, a ``bool``) or
  is ``<= 0`` is an UNKNOWN owner: fall back to expiry-only (today's
  behavior). The pid is trusted ONLY to prove an owner is dead, never to
  prove it alive — pid reuse can only keep a lease live until expiry, which
  is the safe direction.
- ``claim_dispatch_lease`` needs no new logic: it already consults
  ``lease_is_live``, so a dead-owner lease is re-claimable and the claim
  rewrites ``dispatch_lease_owner_pid`` to ``os.getpid()``.
- ``lease_is_live`` never mutates ``story``; a rejected claim leaves the
  story dict byte-identical.

``now`` is injected purely for determinism; the TTL env var is stubbed out
per-test so the documented 1800s default applies (the unit conftest also
clears PIPELINE_* per test — this makes the intent explicit and keeps the
file self-contained).
"""

import copy
import os
import subprocess
import sys
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
    default (1800) unless it explicitly stubs a value."""
    monkeypatch.delenv(TTL_ENV, raising=False)


def _future_iso():
    return (NOW + timedelta(hours=1)).isoformat()


def _past_iso():
    return (NOW - timedelta(hours=1)).isoformat()


def _exited_child_pid():
    """Fork a real child process, wait for it to exit, and return its pid.

    After ``wait()`` the child is reaped, so ``os.kill(pid, 0)`` raises
    ``ProcessLookupError`` — a genuinely dead owner pid. No monkeypatching of
    the liveness probe: the test exercises the real syscall path.
    """
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


class TestDeadOwnerMakesLeaseNotLive:
    def test_live_expiry_but_dead_owner_pid_is_not_live(self):
        story = {
            EXPIRES_KEY: _future_iso(),
            OWNER_PID_KEY: _exited_child_pid(),
        }
        snapshot = copy.deepcopy(story)

        assert dl.lease_is_live(story, now=NOW) is False
        assert story == snapshot, "lease_is_live must never mutate story"

    def test_claim_after_owner_death_rewrites_owner_pid(self):
        story = {
            EXPIRES_KEY: _future_iso(),
            OWNER_PID_KEY: _exited_child_pid(),
        }

        assert dl.claim_dispatch_lease(story, now=NOW) is True
        assert story[OWNER_PID_KEY] == os.getpid()
        assert dl.lease_is_live(story, now=NOW) is True


class TestLiveOrUnknownOwnerKeepsExpiryOnlySemantics:
    def test_live_owner_pid_keeps_lease_live_and_claim_rejected(self):
        story = {
            EXPIRES_KEY: _future_iso(),
            OWNER_PID_KEY: os.getpid(),
        }
        snapshot = copy.deepcopy(story)

        assert dl.lease_is_live(story, now=NOW) is True
        assert dl.claim_dispatch_lease(story, now=NOW) is False
        assert story == snapshot, (
            "a rejected claim must leave the story dict byte-identical"
        )

    def test_missing_owner_pid_falls_back_to_expiry_only(self):
        story = {EXPIRES_KEY: _future_iso()}

        assert dl.lease_is_live(story, now=NOW) is True

    def test_non_int_owner_pid_falls_back_to_expiry_only(self):
        # None, a numeric string, and a bool are not plain ints: unknown
        # owner, expiry-only. The bool case pins that a bool is rejected
        # even though it is an int subclass — True is not a pid.
        story = {EXPIRES_KEY: _future_iso(), OWNER_PID_KEY: None}
        assert dl.lease_is_live(story, now=NOW) is True
        story = {EXPIRES_KEY: _future_iso(), OWNER_PID_KEY: "123"}
        assert dl.lease_is_live(story, now=NOW) is True
        story = {EXPIRES_KEY: _future_iso(), OWNER_PID_KEY: True}
        assert dl.lease_is_live(story, now=NOW) is True

    def test_non_positive_owner_pid_falls_back_to_expiry_only(self):
        story = {EXPIRES_KEY: _future_iso(), OWNER_PID_KEY: 0}
        assert dl.lease_is_live(story, now=NOW) is True
        story = {EXPIRES_KEY: _future_iso(), OWNER_PID_KEY: -1}
        assert dl.lease_is_live(story, now=NOW) is True

    def test_expired_lease_with_alive_owner_is_not_live(self):
        story = {
            EXPIRES_KEY: _past_iso(),
            OWNER_PID_KEY: os.getpid(),
        }

        assert dl.lease_is_live(story, now=NOW) is False

    def test_permission_error_means_owner_exists_so_lease_is_live(
        self, monkeypatch
    ):
        # os.kill(pid, 0) raising PermissionError means the process exists
        # but is owned by someone else: the owner is alive, the lease stays
        # live, and a claim is rejected without mutating the story.
        def _raise_permission_error(pid, sig):
            raise PermissionError(
                f"pid {pid} exists but is owned by another user"
            )

        monkeypatch.setattr(os, "kill", _raise_permission_error)
        story = {
            EXPIRES_KEY: _future_iso(),
            OWNER_PID_KEY: os.getpid(),
        }
        snapshot = copy.deepcopy(story)

        assert dl.lease_is_live(story, now=NOW) is True
        assert dl.claim_dispatch_lease(story, now=NOW) is False
        assert story == snapshot
