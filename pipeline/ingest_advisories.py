"""Ingest-time advisories for a story, returned as ``(event, text)`` pairs.

Each advisory is a non-blocking notice: it never stops an ingest, an approval
or a dispatch. The cross-repository check names a repository the brief
mentions but the story does not order with ``blocked_by`` (a dependency that
was noticed but not declared).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pipeline import repo_listing_hooks
from pipeline.repo_clone import RepoUrlError, parse_repo_url
from pipeline.scope_headroom import scope_headroom_warning

logger = logging.getLogger("pipeline")

_MAX_NAMED = 5
_MIN_SHORT_NAME = 3
# A name is bounded by non-name characters, so "acme/api" does not match
# inside "xacme/api", "acme/apiary", "acme/api.py" or "github.com/acme/api".
_BEFORE = r"(?<![\w./-])"
_AFTER = r"(?![\w.-])"


def _patterns(repo: str) -> list[re.Pattern[str]]:
    """Return the patterns that name *repo*: its full form and, when long enough, owner/name."""
    host_and_path = repo.split("/")
    forms = [repo]
    if len(host_and_path) == 3 and len(host_and_path[2]) >= _MIN_SHORT_NAME:
        forms.append("/".join(host_and_path[1:]))
    return [re.compile(_BEFORE + re.escape(form) + _AFTER, re.IGNORECASE) for form in forms]


def cross_repo_warning(story: dict, plan_repo: str | None, repositories: list[str]) -> str | None:
    """Warn when the brief names another repository but the story has no ``blocked_by``.

    Never raises: any failure logs one WARNING with the error type and returns None.
    """
    try:
        if story.get("blocked_by"):
            return None
        brief = story.get("agent_instructions")
        if not isinstance(brief, str) or not brief:
            return None
        named = sorted(
            repo
            for repo in repositories
            if repo != plan_repo
            and any(pattern.search(brief) for pattern in _patterns(repo))
        )
        if not named:
            return None
        listed = ", ".join(named[:_MAX_NAMED])
        return (
            f"the brief names another connected repository ({listed}) but the story "
            "has no blocked_by; if it depends on a change there, order it with blocked_by"
        )
    except Exception as exc:  # noqa: BLE001 - an advisory must never fail an ingest
        logger.warning(
            "cross-repository advisory failed",
            extra={"error_type": type(exc).__name__},
        )
        return None


def _connected_repositories(store: Any) -> list[str]:
    """Return the connected ``host/owner/name`` repositories, or [] when unreadable."""
    try:
        answer = repo_listing_hooks.list_repositories_result(store)
    except Exception as exc:  # noqa: BLE001 - fail quiet: no advisory when listing fails
        logger.warning(
            "repository listing unavailable for advisory",
            extra={"error_type": type(exc).__name__},
        )
        return []
    if not isinstance(answer, dict) or answer.get("ok") is not True:
        return []
    items = answer.get("repositories")
    if not isinstance(items, list):
        return []
    return [item["repo"] for item in items if isinstance(item, dict) and isinstance(item.get("repo"), str)]


def _plan_repository(plan_repo_url: str | None) -> str | None:
    """Return the plan's own ``host/owner/name``, or None when it is absent or does not parse."""
    if plan_repo_url is None:
        return None
    try:
        host, owner, name = parse_repo_url(plan_repo_url)
    except RepoUrlError:
        return None
    return f"{host}/{owner}/{name}"


def story_advisories(
    story: dict, repo_root: str, plan_repo_url: str | None, store
) -> list[tuple[str, str]]:
    """Return the ``(event, text)`` advisories for one story, headroom first."""
    advisories: list[tuple[str, str]] = []
    headroom = scope_headroom_warning(story, repo_root)
    if headroom:
        advisories.append(("scope_headroom", headroom))
    repositories = _connected_repositories(store)
    if repositories:
        warning = cross_repo_warning(story, _plan_repository(plan_repo_url), repositories)
        if warning:
            advisories.append(("cross_repo_unordered", warning))
    return advisories
