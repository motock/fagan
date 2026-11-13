"""Aggregate depth and age of the notification outbox, for health and notices.

The outbox is a set of JSONL spool files: one ``<plan>.outbox.jsonl`` per plan
plus one ``<plan>.outbox.<channel>.jsonl`` per configured delivery channel.
When delivery works they stay near-empty; when it breaks they grow and the
oldest record keeps ageing, which is the signal ``/api/health`` needs and the
one the scheduler turns into a single notice per stall.

Two rules shape this module:

* **Read-only.** Probing the outbox must never create, write, truncate or lock
  anything. The drain holds a per-file lock, and a health probe that touched it
  could stall or race a delivery, so this module does not import the drain.
* **Aggregate only, no payloads.** Each line is parsed with :func:`json.loads`
  and only its top-level ``"ts"`` is read; the parsed object is dropped
  immediately. Message text, recipients and plan names never reach the result,
  so the health response carries nothing tenant-specific. The per-plan view
  (:func:`outbox_summary_for_plan`) selects files by exact name and stays just
  as payload-free.

"Counts as zero" is read literally: an unreadable file contributes neither a
file nor a record, so a permission error cannot make the outbox look healthy at
a glance while hiding records. A line that is not JSON, or whose ``"ts"`` is
missing or unparseable, is still a record -- we counted a line -- but
contributes an unknown age. An unknown age is *not* stale: a spool file we
cannot read is a reason to look, not a reason to page.

Timestamp parsing matches :func:`pipeline.notification_outbox._is_expired`: a
naive timestamp means UTC, and an unparseable one means "unknown", so an odd
clock never silently hides a growing backlog.
"""

from __future__ import annotations

import logging
import math
import os

from .store_file_outbox import (  # noqa: F401 - re-exported for existing callers
    OUTBOX_GLOB,
    _age_seconds,
    outbox_summary,
    outbox_summary_for_plan,
)

logger = logging.getLogger(__name__)

#: Env var carrying the staleness threshold, in seconds.
OUTBOX_STALE_ENV = "PIPELINE_OUTBOX_STALE_SECONDS"

#: Threshold applied when the env var is unset, malformed or not positive.
DEFAULT_OUTBOX_STALE_SECONDS = 900

def outbox_stale_seconds() -> int:
    """Return the outbox staleness threshold, in seconds.

    Re-read on every call, never cached at import: an operator retuning the
    value should not need to restart the scheduler, and caching would also
    ignore whatever a test sets after import. Malformed, non-finite (``inf``,
    ``nan``) or non-positive values fall back to the default rather than
    disabling the check or making every outbox look stale.
    """
    raw = os.environ.get(OUTBOX_STALE_ENV)
    if raw is None:
        return DEFAULT_OUTBOX_STALE_SECONDS
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        return DEFAULT_OUTBOX_STALE_SECONDS
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_OUTBOX_STALE_SECONDS
    # Round a positive fractional threshold up instead of truncating: ``0.5``
    # would otherwise become ``0`` and make every non-empty outbox stale.
    return max(1, math.ceil(value))


def _age_is_stale(oldest_age_s: int | None, threshold: int) -> bool:
    """True when *oldest_age_s* is strictly older than *threshold* seconds.

    The single definition of "stale", so the health verdict and the notice
    clock cannot drift apart. Strictly older, so a record exactly at the
    threshold is not yet stale; a notice should mean "this is past the limit",
    not "this reached it". ``None`` (unknown age) is never stale.
    """
    return oldest_age_s is not None and oldest_age_s > threshold


def outbox_is_stale(summary: dict, threshold: int) -> bool:
    """True when the oldest record is strictly older than *threshold* seconds."""
    return _age_is_stale(summary.get("oldest_age_s"), threshold)


def stale_transition(
    prev_stale: bool, oldest_age_s: int | None, threshold: int
) -> tuple[str | None, bool]:
    """Return ``(event, new_stale)`` for one tick of the staleness clock.

    *prev_stale* is the flag stored by the previous tick. The returned flag is
    what the caller must store -- storing anything else either re-fires the
    same notice on the next tick or swallows the next real transition. The
    event fires once per transition into stale (``outbox_stale``) and once on
    recovery (``outbox_recovered``), and is ``None`` while the state holds, so
    a persistently stale outbox raises one notice rather than one per tick.

    Public because the scheduler tick (another module) owns the stored flag;
    the state machine lives here, next to the predicate it uses.
    """
    is_stale = _age_is_stale(oldest_age_s, threshold)
    if not prev_stale and is_stale:
        return "outbox_stale", True
    if prev_stale and not is_stale:
        return "outbox_recovered", False
    return None, prev_stale


def apply_outbox_health(result: dict, plan_dir, threshold: int | None = None, summary=None) -> dict:
    """Add the aggregate ``outbox`` block to a health payload, in place.

    This is the whole ``/api/health`` contribution: ``result["outbox"]`` is the
    summary, and a stale outbox sets ``healthy`` False and appends
    ``"outbox_stale"`` to ``degraded`` without dropping the verdicts already
    there. *threshold* defaults to :func:`outbox_stale_seconds`, re-read per
    call.

    *summary* lets a caller that already asked the record store for the
    aggregate hand it in, so the health payload grades the store's outbox
    rather than re-walking a directory; when it is ``None`` the summary is
    computed from the second positional argument, which keeps this function's
    own tests working unchanged.

    The block is aggregate only -- counts and one age -- so no plan name, file
    path or message body reaches a response body. Scoped, per-plan views are a
    later tenancy concern.
    """
    if summary is None:
        summary = outbox_summary(plan_dir)
    if threshold is None:
        threshold = outbox_stale_seconds()
    result["outbox"] = summary
    if outbox_is_stale(summary, threshold):
        result["healthy"] = False
        degraded = result.get("degraded")
        if isinstance(degraded, list):
            if "outbox_stale" not in degraded:
                degraded.append("outbox_stale")
        else:
            result["degraded"] = ["outbox_stale"]
    return result
