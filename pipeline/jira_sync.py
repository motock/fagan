"""Mirror a Jira-originated plan's progress back to its Jira issue.

A plan named ``jira-<key>`` was planned from the Jira story ``<KEY>``. Each of
its stories gets one sub-task under that story, and the sub-task's status
follows the story's lifecycle. Subscribed to the bus only when
``PIPELINE_JIRA_ENABLED`` is exactly ``1`` (see
``pipeline.event_wiring._subscribe_handlers``).

Only a story's summary and description are copied to Jira. Jira text is
untrusted and is never logged. A handler never raises into the bus: a failure
is logged by exception type, with the plan and story key.

Stdlib only at import time. Everything that touches settings, credentials or
HTTP is imported inside the handler, so the event wiring can import this module
without a cycle.
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import nullcontext

from pipeline.notification_links import story_link

logger = logging.getLogger("pipeline.jira_sync")

ENABLE_ENV = "PIPELINE_JIRA_ENABLED"
ENABLED_VALUE = "1"
PLAN_PREFIX = "jira-"
PLAN_APPROVED = "plan_approved"
SUBTASK_LABEL_PREFIX = "fagan-story-"
BASE_LABELS = ("fagan",)
PARKED_COMMENT = "Parked: needs a decision in Fagan."
PARENT_DONE_COMMENT = "All Fagan stories are done. Ready for review."
SUBSCRIBED_EVENT_TYPES = ("notification", "agent_dispatched", "pr_open", "story_done", "parked")

# Event type -> the board status key the sub-task moves to. ``parked`` and
# ``story_done`` are handled on their own paths below.
_STATUS_FOR_EVENT = {
    "agent_dispatched": "in_progress",
    "pr_open": "in_review",
    "story_done": "done",
}
_ISSUE_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]+-[0-9]+")
# A story key goes into a JQL string, so only plain identifier characters pass.
_STORY_KEY_RE = re.compile(r"[A-Za-z0-9_.:-]{1,200}")

# (plan, status) pairs already logged as unreachable, so the INFO line appears once.
_MISSING_TRANSITIONS: set[tuple[str, str]] = set()


def enabled() -> bool:
    """True only when ``PIPELINE_JIRA_ENABLED`` is exactly ``1``. Read on every call."""
    return os.environ.get(ENABLE_ENV) == ENABLED_VALUE


def sync_event(event: dict) -> None:
    """Bus handler: mirror one Fagan event to the plan's Jira issue. Never raises."""
    plan = event.get("plan")
    if not isinstance(plan, str) or not plan.startswith(PLAN_PREFIX):
        return
    event_type = event.get("type")
    story_key = event.get("story_key")
    if event_type == "notification":
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("event") != PLAN_APPROVED:
            return
    try:
        _sync(event, plan, event_type, story_key)
    except Exception as exc:  # noqa: BLE001 - a Jira failure must never reach the bus
        logger.warning(
            "jira sync failed",
            extra={
                "plan": plan,
                "story": story_key,
                "event_type": event_type,
                "error_type": type(exc).__name__,
            },
        )


def _sync(event: dict, plan: str, event_type: str, story_key: object) -> None:
    from pipeline import manifest_source
    from pipeline.actor import bind_namespace
    from pipeline.ci import context_from_manifest
    from pipeline.settings_store import effective_setting

    binding = bind_namespace(event["namespace"]) if "namespace" in event else nullcontext()
    with binding:
        manifest = manifest_source.read_plan_manifest(plan, fallback_on_error=True)
        if manifest is None:
            return
        stories = manifest.get("stories")
        if not isinstance(stories, dict):
            return
        context = context_from_manifest(manifest)
        board = effective_setting(
            "jira_board", namespace=context.namespace, team=context.team, repo=context.repo
        )["value"]
        if not isinstance(board, dict) or board.get("enabled") is not True:
            return
        org = manifest.get("org")
        site = effective_setting(
            "jira_site",
            namespace=context.namespace,
            team=context.team,
            repo=context.repo,
            org=org if isinstance(org, str) else "",
        )["value"]
        if not isinstance(site, dict) or not site:
            return
        parent = _parent_key(plan)
        if parent is None:
            logger.warning("jira sync skipped: plan name holds no issue key", extra={"plan": plan})
            return
        action = PLAN_APPROVED if event_type == "notification" else event_type
        _run(action, plan, parent, story_key, stories, board, site)


def _run(action, plan, parent, story_key, stories, board, site) -> None:
    from pipeline.jira_intake import _make_client, _site_allowed, _token_name_allowed

    if not _site_allowed(site) or not _token_name_allowed(site):
        logger.warning("jira sync skipped: site or token not allowed", extra={"plan": plan})
        return
    client = _make_client(site)
    statuses = board["status"]
    if action == PLAN_APPROVED:
        _create_subtasks(client, plan, parent, board["project_key"], stories)
    elif action == "parked":
        _park(client, plan, parent, story_key)
    elif action == "story_done":
        _move_story(client, plan, parent, story_key, statuses["done"])
        if _all_done(stories):
            _move(client, plan, parent, statuses["parent_review"])
            client.add_comment(parent, PARENT_DONE_COMMENT)
    else:
        _move_story(client, plan, parent, story_key, statuses[_STATUS_FOR_EVENT[action]])


def _parent_key(plan: str) -> str | None:
    key = plan[len(PLAN_PREFIX):].upper()
    return key if _ISSUE_KEY_RE.fullmatch(key) else None


def _all_done(stories: dict) -> bool:
    return bool(stories) and all(
        isinstance(story, dict) and story.get("status") == "done" for story in stories.values()
    )


def _subtask_key(client, parent: str, story_key: object) -> str | None:
    """The sub-task for ``story_key`` under ``parent``, or ``None`` when absent."""
    if not isinstance(story_key, str) or _STORY_KEY_RE.fullmatch(story_key) is None:
        logger.warning("jira sync skipped: malformed story key")
        return None
    label = SUBTASK_LABEL_PREFIX + story_key
    issues = client.search(f'parent = {parent} AND labels = "{label}"', ["summary"], max_results=1)
    if not issues:
        return None
    key = issues[0].get("key")
    return key if isinstance(key, str) and _ISSUE_KEY_RE.fullmatch(key) else None


def _create_subtasks(client, plan: str, parent: str, project_key: str, stories: dict) -> None:
    from pipeline.jira_client import SUMMARY_MAX, JiraError

    for story_key, story in stories.items():
        if not isinstance(story, dict) or not isinstance(story_key, str):
            continue
        if _STORY_KEY_RE.fullmatch(story_key) is None:
            logger.warning("jira sync skipped story: malformed story key", extra={"plan": plan})
            continue
        try:
            if _subtask_key(client, parent, story_key) is not None:
                continue
            summary = story.get("summary")
            if not isinstance(summary, str) or not summary.strip():
                summary = story_key
            description = story.get("description")
            client.create_subtask(
                parent,
                project_key,
                summary[:SUMMARY_MAX],
                description if isinstance(description, str) else "",
                [*BASE_LABELS, SUBTASK_LABEL_PREFIX + story_key],
            )
        except JiraError as exc:
            logger.warning(
                "jira sub-task create failed",
                extra={"plan": plan, "story": story_key, "error_kind": exc.kind},
            )


def _move_story(client, plan: str, parent: str, story_key: object, status_name: str) -> None:
    subtask = _subtask_key(client, parent, story_key)
    if subtask is None:
        return
    _move(client, plan, subtask, status_name)


def _move(client, plan: str, issue_key: str, status_name: str) -> None:
    if client.transition_to(issue_key, status_name):
        return
    marker = (plan, status_name)
    if marker in _MISSING_TRANSITIONS:
        return
    _MISSING_TRANSITIONS.add(marker)
    logger.info(
        "jira status has no transition from this issue",
        extra={"plan": plan, "status": status_name},
    )


def _park(client, plan: str, parent: str, story_key: object) -> None:
    subtask = _subtask_key(client, parent, story_key)
    if subtask is None:
        return
    client.add_comment(subtask, PARKED_COMMENT, link=story_link(plan, story_key))
