"""Recognise plans whose advance pass has no dispatch, review or merge work.

The scheduler advances every plan it can list, one child process per plan, and
each child pays the cost of importing the pipeline before it can discover that
there was nothing to do. A plan whose stories are all ``done``, a plan paused
with no agent left to interrupt, and a plan that is not approved for activation
all fall into that group.

``is_inert`` is the single authority for that decision, shared by the pooled and
the serial advance passes so both skip exactly the same plans. Its answer is
one-sided by design: a manifest it cannot positively classify is advanced,
because an unexpected advance costs one tick while an unexpected skip can strand
a plan indefinitely. Plan completion is unaffected by a skip -- the completion
notification is emitted from the merge, CI and advance-merge paths, not from the
advance pass.

A plan with no stories at all is NOT inert: an empty plan has not finished, it
has not started, and pre-existing callers rely on it being advanced.
"""

from __future__ import annotations

import logging

from pipeline import plan_approval

__all__ = ["is_inert"]

logger = logging.getLogger(__name__)


def _has_live_agent(stories: dict) -> bool:
    """Return whether any story holds a dispatched agent's ``pid``.

    Presence of the ``pid`` key is the test, never its value: ``0`` and ``None``
    are still records of a dispatch that has to be interrupted.
    """
    return any(
        isinstance(story, dict)
        and story.get("status") == "in_progress"
        and "pid" in story
        for story in stories.values()
    )


def is_inert(manifest: dict) -> bool:
    """Return whether *manifest* has no work for an advance pass to do.

    True for a plan whose stories are all ``done``, for a paused plan with no
    live agent to interrupt, and for a plan ``plan_approval.is_activatable``
    rejects (the advance pass skips it before any work). False for everything
    else, including every manifest this function cannot read: unknown means
    advance it.
    """
    plan_name = None
    try:
        if not isinstance(manifest, dict):
            return False
        plan_name = manifest.get("name")
        stories = manifest.get("stories")
        if not isinstance(stories, dict) or not stories:
            return False
        # Pause is decided before the approval gate: the advance pass interrupts
        # a paused plan's running agents, and an unapproved plan paused after
        # dispatch must still get that pass or its agent runs forever. A paused
        # plan holding a live agent is therefore never inert.
        if manifest.get("paused"):
            return not _has_live_agent(stories)
        if not plan_approval.is_activatable(manifest):
            return True
        return all(
            isinstance(story, dict) and story.get("status") == "done"
            for story in stories.values()
        )
    except Exception:  # noqa: BLE001 (unknown manifests must be advanced, not fatal)
        # Only the class name and the plan are recorded: exception text can
        # carry filesystem paths and manifest payloads.
        logger.debug(
            "plan activity unreadable; treating as active",
            extra={"plan": plan_name},
            exc_info=False,
        )
        return False
