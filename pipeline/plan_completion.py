"""Detect plan completion exactly once and emit a ``plan_completed`` notice.

``notify_if_plan_completed`` is called from a scheduler tick after a story
transition.  It checks whether every story in the plan manifest has reached
``done`` and, if so, emits exactly one ``plan_completed`` notification whose
message is the plan summary rendered by :mod:`pipeline.plan_summary`.

Once-only is enforced here, not by ``dedup_key``: the dedup key is captured at
write time and only collapsed later by the dashboard, so it cannot suppress a
second write.  A ``<plan>.plan_completed`` marker file under ``PLAN_DIR`` is
created only AFTER the notification has been emitted, so a failed emission can
be retried by a later tick.

The function never raises: it runs inside a scheduler tick and must not break
the story transition that triggered it.

The summary is also written to ``<plan>.report.md`` in ``PLAN_DIR``; this is
best effort: a write failure is logged and does not block the notification.
"""

import logging

from .paths import PLAN_DIR
from .persistence import _notify_user
from .plan_summary import format_plan_summary
from .story_metrics import load_notification_records

logger = logging.getLogger(__name__)


def notify_if_plan_completed(plan_name: str, manifest: dict) -> bool:
    """Emit one ``plan_completed`` notification if every story is done.

    Returns ``True`` if the notification was emitted on this call, ``False``
    otherwise (plan not complete, already notified, or any failure).
    """
    try:
        marker = PLAN_DIR / f"{plan_name}.plan_completed"
        if marker.exists():
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

        records = load_notification_records(
            PLAN_DIR / f"{plan_name}.notifications.jsonl"
        )[0]
        summary = format_plan_summary(plan_name, manifest, records)
        # A durable copy for retros; the notification is the delivery, this
        # file is the record. Never blocks the notification.
        try:
            (PLAN_DIR / f"{plan_name}.report.md").write_text(
                summary + "\n", encoding="utf-8"
            )
        except OSError:
            logger.warning("plan report write failed for %s", plan_name, exc_info=True)
        _notify_user(
            plan_name,
            summary,
            event="plan_completed",
            severity="info",
            dedup_key=f"plan_completed:{plan_name}",
        )
        marker.touch()
        return True
    except Exception:
        logger.exception("plan_completed check failed for %s", plan_name)
        return False