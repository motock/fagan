"""``review_story(force=True)``: the explicit, audited way to re-run a review.

``review_story`` deliberately declines to re-review a story whose branch has not
moved since the rejection (``skipped: "unchanged_since_last_review"``), and
declines a story that is not at ``tests_passed``
(``skipped: "not_reviewable_state"``). Both are correct defaults, but neither is
an absolute prohibition: an operator who wants the reviewer to look again at the
same commit -- a suspect verdict, a re-run after a backend hiccup -- needs a
route that is allowed, that takes the plan lock, and that leaves a trace.

``force=True`` is that route. It is deliberately narrow:

* It only clears the three "this exact commit was already reviewed" markers --
  ``last_reviewed_sha``, ``last_reviewed_logic_fingerprint``
  (:data:`REVIEW_LOGIC_FINGERPRINT_KEY`) and ``skip_notified_sha``. Fields are
  *deleted*, not zeroed, matching :mod:`pipeline.unpark`: an absent key is the
  honest "not recorded" state.
* It never clears ``review_verdict``, ``review_feedback``, ``rework_attempts``
  or any review budget counter. A forced review that lands
  ``REQUEST_CHANGES`` therefore counts against the rework budget exactly like
  any other review, so ``force`` cannot be used to loop past
  ``PIPELINE_REWORK_MAX_ATTEMPTS`` (and the no-progress guard in
  ``pipeline/story_status.py`` still applies).
* It refuses while the story carries a ``review_job`` handle: a detached review
  is live, and a second review would race it.
* It refuses any story that is not at ``tests_passed`` -- the sole legitimate
  entry state -- rather than reviewing a stale story.
* It records exactly one ``story:force_review`` audit entry, and only when the
  markers were actually cleared.

Because the first review after a force has no ``last_reviewed_sha``, it is a
FIRST-style full-branch review of the current HEAD, not a ``since_sha`` rework
diff. ``force`` never merges anything: the merged-or-not decision stays with
``advance_pipeline``'s risk adjudication.

The caller owns the lock: :func:`prepare_forced_review` performs no
``transaction`` of its own, because ``PipelineService.review_story`` calls it
inside the plan transaction it already holds. It is only ever called there, with
both keys already validated.
"""

from __future__ import annotations

import logging
from typing import Any

from .audit import record_audit
from .review_logic import REVIEW_LOGIC_FINGERPRINT_KEY
from .store import build_store

logger = logging.getLogger(__name__)

__all__ = ["FORCED_REVIEW_CLEARED_FIELDS", "prepare_forced_review"]

#: The "this exact commit was already reviewed" markers a force clears, so the
#: next review call is not skipped. Nothing else -- least of all a verdict, the
#: reviewer's feedback, or a budget counter -- is in this list.
FORCED_REVIEW_CLEARED_FIELDS: tuple[str, ...] = (
    "last_reviewed_sha",
    REVIEW_LOGIC_FINGERPRINT_KEY,
    "skip_notified_sha",
)


def prepare_forced_review(plan_name: str, story_key: str) -> dict[str, Any]:
    """Clear *story_key*'s last-reviewed markers so its next review runs.

    Must be called inside the plan transaction (it neither locks nor re-reads
    under a lock of its own). Returns ``{"ok": True}`` once the markers are
    cleared and the ``story:force_review`` audit entry is written.

    Returns -- changing nothing -- ``{"ok": False, "error": "review_job_running"}``
    while the story carries a ``review_job`` handle (a detached review is live
    and a second one would race it), ``{"ok": False, "error":
    "not_reviewable_state"}`` unless its status is ``tests_passed``, and the
    same ``{"ok": False, "error": "No such story ..."}`` result the unforced
    path returns when the story does not exist. The ``review_job`` check runs
    first: it is the honest reason, and a running job outranks a stale status.

    ``review_verdict``, ``review_feedback``, ``rework_attempts`` and every
    review budget counter are left exactly as they were, so the forced review
    is judged and budgeted like any other.
    """
    store = build_store()
    manifest = store.get_manifest(plan_name)
    story = manifest["stories"].get(story_key)
    if not story:
        return {"ok": False, "error": f"No such story {story_key}"}
    if story.get("review_job"):
        return {"ok": False, "error": "review_job_running"}
    if story.get("status") != "tests_passed":
        return {"ok": False, "error": "not_reviewable_state"}

    for field in FORCED_REVIEW_CLEARED_FIELDS:
        story.pop(field, None)
    store.save_manifest(plan_name, manifest)
    # Only after the save returns: a failed write must never claim the force
    # happened, and a refused force must never leave an audit record behind.
    record_audit(
        plan_name,
        action="story:force_review",
        outcome="forced",
        target={"story": story_key},
    )
    logger.info(
        "forced review prepared plan=%s story=%s - last-reviewed markers cleared",
        plan_name,
        story_key,
    )
    return {"ok": True}
