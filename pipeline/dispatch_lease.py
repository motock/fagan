"""Dispatch lease primitive for pipeline stories (LOCKSTARVE-B2).

Today ``advance_pipeline`` holds the plan lock for the whole tick, so a
story cannot be dispatched twice. Stories B3/B4 release that lock across
the synchronous model phases, which opens a window where a second tick
(or a second MCP server process) could observe the story still in
``todo``/``interrupted`` and dispatch it again — two agents in one
worktree, the documented cause of repeated zero-output agent deaths (see
``_plan_lock``'s docstring in pipeline/concurrency.py).

The lease is the cross-tick / cross-process guard: a story carries

- ``dispatch_lease_expires_at`` — ISO-8601 UTC timestamp string,
- ``dispatch_lease_owner_pid`` — ``os.getpid()`` of the claimer,

and a second claimer must see the live lease and back off. This module
provides ONLY the primitive; B3 wires it into ``advance_pipeline``.

Fail secure, in both directions:

- A live lease is never silently stolen: ``claim_dispatch_lease``
  returns False and leaves the story dict untouched.
- A missing, malformed, or expired lease never withholds work forever:
  it reads as not live and is re-claimable. A naive (timezone-less)
  timestamp is malformed — never guess a timezone.

``now`` / ``ttl_s`` are injectable purely so tests are deterministic;
production callers pass neither. No logging: manifests carry user work.
"""

import os
from datetime import datetime, timedelta, timezone

_DEFAULT_TTL_S = 1800
_TTL_ENV_VAR = "PIPELINE_DISPATCH_LEASE_TTL_SECONDS"


def claim_dispatch_lease(
    story: dict,
    *,
    now: datetime | None = None,
    ttl_s: int | None = None,
) -> bool:
    """Claim the dispatch lease on ``story`` in place.

    Returns True iff the claim succeeded. If a live lease is already
    held, returns False WITHOUT mutating the story — somebody else owns
    the dispatch. Otherwise writes ``dispatch_lease_expires_at`` (ISO-8601
    UTC, ``now + ttl``) and ``dispatch_lease_owner_pid`` (``os.getpid()``).

    ``ttl_s`` defaults from ``PIPELINE_DISPATCH_LEASE_TTL_SECONDS``
    (default 1800); a malformed value degrades to the default, never
    raising. The env var is read at call time, never cached at import.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if ttl_s is not None:
        ttl = ttl_s
    else:
        raw = os.environ.get(_TTL_ENV_VAR)
        try:
            ttl = int(raw)
        except (TypeError, ValueError):
            ttl = _DEFAULT_TTL_S

    # Guard first, mutate last: a rejected claim must leave both lease
    # fields byte-identical (no refresh, no owner change, no partial
    # write), otherwise a losing poller could push the expiry forward
    # forever and the story would never be re-dispatched.
    if lease_is_live(story, now=now):
        return False

    story["dispatch_lease_expires_at"] = (
        now + timedelta(seconds=ttl)
    ).isoformat()
    story["dispatch_lease_owner_pid"] = os.getpid()
    return True


def lease_is_live(story: dict, *, now: datetime | None = None) -> bool:
    """Return True only when ``story`` holds a live dispatch lease.

    The lease is live iff ``dispatch_lease_expires_at`` is present,
    parses as a timezone-aware ISO-8601 timestamp, and is strictly in
    the future relative to ``now`` (expiry is exclusive). Anything else
    — missing field, non-string, unparseable, naive timestamp, expired —
    is NOT live, so a corrupt field can never withhold work forever.
    This function never mutates ``story``.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    value = story.get("dispatch_lease_expires_at")
    if not isinstance(value, str) or not value:
        return False

    try:
        expires = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return False

    # A naive (timezone-less) timestamp is malformed: never guess a
    # timezone, and never compare naive to aware (that raises TypeError).
    if expires.tzinfo is None or expires.utcoffset() is None:
        return False

    # Expiry is exclusive: a lease expiring at exactly ``now`` is dead.
    return expires > now