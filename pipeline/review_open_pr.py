"""The bound on a story whose pull request cannot be opened after APPROVE.

See `pipeline.review_orchestrator`, which calls `handle_open_pr_failure` from
the APPROVE branch when `_open_pr` raises.

Live 2026-11-22: a push with no credentials failed on every tick. Each failure
notified "will retry next tick" and left the story in `tests_passed`, so every
advance ran a new paid review of the same commit: five full reviews in eight
minutes, the story never parked. This module owns the bound that turns such a
streak into a park.

The failure text is never echoed. git stderr can carry a remote URL with
embedded credentials, so a park reason, a notification and a log line hold the
exception class name and fixed sentences only.

Kept out of review_orchestrator.py, which already sits at the 1000-line
production cap that scripts/check_line_limit.py enforces.
"""

from __future__ import annotations

import subprocess
from typing import Any

from .review_refs import _ServerRef
from .story_commit import commit_story

_notify_user = _ServerRef("_notify_user")

#: Failed pull-request opens tolerated before parking, for a non-auth failure.
PR_OPEN_OTHER_PARK_AFTER = 2

#: Lower-cased substrings of git/gh stderr or stdout that mean the repository
#: token was refused or is missing. Retrying with the same token cannot help.
_AUTH_MARKERS = (
    "could not read username",
    "authentication failed",
    "gh auth login",
    "gh_token",
    "permission to",
    "403",
)

AUTH_PARK_REASON = (
    "approved but the pull request could not be opened: the repository token "
    "was refused or is missing; check the team's GitHub token and its access "
    "to this repository"
)


def _is_auth_failure(exc: BaseException) -> bool:
    """True when a CalledProcessError's output names a refused or missing token.

    Only CalledProcessError carries the process output. A missing or non-string
    value counts as empty text, so bytes or None never raise here.
    """
    if not isinstance(exc, subprocess.CalledProcessError):
        return False
    text = "\n".join(
        value
        for value in (getattr(exc, "stderr", None), getattr(exc, "stdout", None))
        if isinstance(value, str)
    ).lower()
    return any(marker in text for marker in _AUTH_MARKERS)


def handle_open_pr_failure(
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    exc: BaseException,
    **cid_kwargs: Any,
) -> dict[str, Any]:
    """Count a failed pull-request open and park the story once it is hopeless.

    An auth failure parks at once: the same token will be refused again. Any
    other failure gets one retry on the next tick, then parks. Until it parks,
    the story keeps its status so the next advance can try the open again.
    """
    failures = story.get("pr_open_failures", 0) + 1
    story["pr_open_failures"] = failures
    exc_name = exc.__class__.__name__

    if _is_auth_failure(exc):
        reason = AUTH_PARK_REASON
    elif failures >= PR_OPEN_OTHER_PARK_AFTER:
        reason = (
            "approved but the pull request could not be opened after "
            f"{PR_OPEN_OTHER_PARK_AFTER} attempts ({exc_name})"
        )
    else:
        _notify_user(
            plan_name,
            f"{story_key}: review APPROVEd but could not open PR "
            f"({exc_name}); will retry next tick.",
            **cid_kwargs,
        )
        commit_story(plan_name, story_key, story)
        return {"ok": True, "verdict": "APPROVE", "status": story["status"]}

    story["status"] = "parked"
    story["parked_reason"] = reason
    _notify_user(
        plan_name,
        f"{story_key} parked: {reason}.",
        severity="warning",
        event="story_parked",
        **cid_kwargs,
    )
    commit_story(plan_name, story_key, story)
    return {"ok": False, "verdict": "APPROVE", "status": "parked"}
