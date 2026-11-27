"""Detect plan completion exactly once and emit a ``plan_completed`` notice.

``notify_if_plan_completed`` is called from a scheduler tick after a story
transition.  It checks whether every story in the plan manifest has reached
``done`` and, if so, emits exactly one ``plan_completed`` notification whose
message is the plan summary rendered by :mod:`pipeline.plan_summary`.

Once-only is enforced by the store, not by ``dedup_key``: the dedup key is
captured at write time and only collapsed later by the dashboard, so it cannot
suppress a second write.  The ``<plan>.plan_completed`` marker record is created
only AFTER the notification has been emitted, so a failed emission can be
retried by a later tick.

The function never raises: it runs inside a scheduler tick and must not break
the story transition that triggered it.

The summary is also stored as the plan's report record, the
``<plan>.report.md`` file in ``PLAN_DIR``; this is best effort: a write failure
is logged and does not block the notification.
"""

import datetime
import logging
from typing import Any, cast

from . import server as _server
from .actor import current_namespace
from .persistence import _notify_user
from .plan_summary import format_plan_summary
from .store import build_store
from .store_records import InvalidRecordKeyError

logger = logging.getLogger(__name__)


def _with_journals(plan_name: str, manifest: dict) -> dict:
    """Shallow copy of ``manifest`` whose stories carry their journal entries.

    Journals are read from the record store (the same view the writers write
    through), one lookup per story against a store built once per call.

    Fail open: an unreachable store, a rejected story key or a journal that is
    absent/empty means no journal for that story - the store cannot tell an
    empty journal from an absent one, and both classify the same way. The
    caller's manifest is not mutated; it is returned as-is when no story has a
    journal.
    """
    try:
        records = cast(Any, build_store()).records()  # cast: Protocol omits records()
    except Exception:
        logger.warning(
            "journal read skipped for %s: record store unavailable",
            plan_name,
            exc_info=True,
        )
        return manifest
    stories = {}
    for key, story in manifest["stories"].items():
        story = dict(story)
        try:
            entries = records.get_journal(current_namespace(), plan_name, key)
        except InvalidRecordKeyError:
            logger.warning(
                "journal read skipped for %s: rejected story key %r", plan_name, key
            )
            entries = None
        if isinstance(entries, list) and entries:
            story["journal"] = [e for e in entries if isinstance(e, dict)]
        stories[key] = story
    if not any("journal" in story for story in stories.values()):
        return manifest
    return {**manifest, "stories": stories}


def _with_just_merged(
    plan_name: str, manifest: dict, history: list[dict], just_merged: str | None
) -> list[dict]:
    """``history`` plus an in-memory ``story_merged`` record for ``just_merged``.

    A durable bus consumer writes history rows asynchronously, so the merge the
    caller just announced may not be readable yet. The record is only handed to
    the summary, never stored; nothing is added when history already has it.
    """
    if not just_merged:
        return history
    if any(
        isinstance(r, dict)
        and r.get("event") == "story_merged"
        and r.get("story_key") == just_merged
        for r in history
    ):
        return history
    record = {
        "ts": datetime.datetime.now(datetime.UTC).isoformat(),
        "plan": plan_name,
        "message": f"{just_merged} merged",
        "story_key": just_merged,
        "severity": "info",
        "event": "story_merged",
        "dedup_key": None,
    }
    story = (manifest.get("stories") or {}).get(just_merged)
    if isinstance(story, dict) and story.get("correlation_id"):
        record["correlation_id"] = story["correlation_id"]
    return [*history, record]


def notify_if_plan_completed(
    plan_name: str, manifest: dict, *, just_merged: str | None = None
) -> bool:
    """Emit one ``plan_completed`` notification if every story is done.

    ``just_merged`` names a story whose ``story_merged`` the caller has just
    recorded; the summary counts it even if the history read has not caught up.

    Returns ``True`` if the notification was emitted on this call, ``False``
    otherwise (plan not complete, already notified, or any failure).
    """
    try:
        records = cast(Any, _server._store).records()  # cast: Protocol omits records()
        if records.is_plan_completed(current_namespace(), plan_name):
            return False

        stories = manifest.get("stories") or {}
        if not stories:
            # An empty or missing ``stories`` dict is an empty/malformed
            # manifest, not a finished plan (``all([])`` is vacuously True).
            return False

        if not all(
            story.get("status") == "done" for story in stories.values()
        ):
            return False

        history = _with_just_merged(
            plan_name,
            manifest,
            records.get_notification_history(current_namespace(), plan_name)[0],
            just_merged,
        )
        summary = format_plan_summary(
            plan_name, _with_journals(plan_name, manifest), history
        )
        # A durable copy for retros; the notification is the delivery, this
        # record is the file. Never blocks the notification.
        try:
            records.put_plan_report(current_namespace(), plan_name, summary + "\n")
        except Exception:
            logger.warning("plan report write failed for %s", plan_name, exc_info=True)
        _notify_user(
            plan_name,
            summary,
            event="plan_completed",
            severity="info",
            dedup_key=f"plan_completed:{plan_name}",
        )
        # Only after the notice: a failed emission must stay retryable, and the
        # atomic create is what makes exactly one caller announce the plan.
        records.mark_plan_completed(current_namespace(), plan_name)
        return True
    except Exception:
        logger.exception("plan_completed check failed for %s", plan_name)
        return False