"""Mirror a GitHub-issue plan's progress back to its issue.

A plan named ``gh-<repo name>-<number>`` was planned from GitHub issue
``<number>`` (see ``pipeline.github_issues_intake``). The issue carries one
checklist comment, marked by a first line of ``<!-- fagan-stories -->``, with
one task-list line per story. Every lifecycle event rewrites that comment from
the manifest, so the handler is idempotent and independent of event order.
When a parked story or the last finished story needs attention, one comment is
added, and the last finished story also adds the ``fagan-done`` label. The
issue is never closed. Subscribed to the bus only when
``PIPELINE_GITHUB_ISSUES_ENABLED`` is exactly ``1`` (see
``pipeline.event_wiring._subscribe_handlers``).

Only plans named for a repository whose team has ``github_issues`` enabled are
mirrored, and only to that repository. Story text is Fagan's own, but it is
still cleaned before it is written, and nothing here logs it. A handler never
raises into the bus: a failure is logged once per plan and kind, with the kind
only.

Stdlib only at import time. Everything that touches settings, credentials or
HTTP is imported inside the handler, so the event wiring can import this module
without a cycle.
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import nullcontext
from urllib.parse import urlsplit

from pipeline.github_issues_client import MAX_LIST_RESULTS, GitHubIssuesError

logger = logging.getLogger("pipeline.github_issues_sync")

ENABLE_ENV = "PIPELINE_GITHUB_ISSUES_ENABLED"
ENABLED_VALUE = "1"
PLAN_APPROVED = "plan_approved"
MARKER = "<!-- fagan-stories -->"
DONE_LABEL = "fagan-done"
PARKED_COMMENT = "Parked: {story} needs a decision in Fagan."
DONE_COMMENT = "All Fagan stories are done. Ready for review."
GUIDANCE_RESOLVED_EVENT = "plan_guidance_resolved"
GUIDANCE_RESOLVED_COMMENT = "Questions answered in Fagan. The plan is waiting for approval."
SUBSCRIBED_EVENT_TYPES = (
    "notification",
    "agent_dispatched",
    "pr_open",
    "story_done",
    "parked",
    GUIDANCE_RESOLVED_EVENT,
)
SUMMARY_MAX = 120
# Characters that could open markup in a comment. They are removed, and line
# breaks become spaces so one story can never start a second checklist line.
_CLEAN_TABLE = str.maketrans({"`": None, "<": None, ">": None, "|": None})
_PLAN_RE = re.compile(r"gh-(?P<repo>.+)-(?P<number>[1-9][0-9]*)")
_PR_PATH_RE = re.compile(r"/pull/([1-9][0-9]*)/?")

# (plan, kind) pairs already logged, so a repeating failure logs once.
_LOGGED: set[tuple[str, str]] = set()


def enabled() -> bool:
    """True only when ``PIPELINE_GITHUB_ISSUES_ENABLED`` is exactly ``1``. Read on every call."""
    return os.environ.get(ENABLE_ENV) == ENABLED_VALUE


def sync_event(event: dict) -> None:
    """Bus handler: mirror one Fagan event to the plan's GitHub issue. Never raises."""
    plan = event.get("plan")
    if not isinstance(plan, str) or not plan.startswith("gh-"):
        return
    event_type = event.get("type")
    if event_type == "notification":
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("event") != PLAN_APPROVED:
            return
    try:
        _sync(event, plan, event_type, event.get("story_key"))
    except GitHubIssuesError as exc:
        _log_failure(plan, exc.kind)
    except Exception as exc:  # noqa: BLE001 - a GitHub failure must never reach the bus
        _log_failure(plan, type(exc).__name__)


def _log_failure(plan: str, kind: str) -> None:
    if (plan, kind) in _LOGGED:
        return
    _LOGGED.add((plan, kind))
    logger.warning("github issues sync failed", extra={"error_kind": kind})


def _sync(event: dict, plan: str, event_type: object, story_key: object) -> None:
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
        setting = effective_setting(
            "github_issues", namespace=context.namespace, team=context.team, repo=context.repo
        )["value"]
        if not isinstance(setting, dict) or setting.get("enabled") is not True:
            return
        number = _issue_number(plan, setting)
        if number is None:
            return
        client = _client_for(context.namespace, setting)
        _run(client, number, event_type, story_key, stories)


def _issue_number(plan: str, setting: dict) -> int | None:
    """The issue number named by ``plan``, when its repository is this team's repository."""
    from pipeline.github_issues_intake import _owner_and_repo

    match = _PLAN_RE.fullmatch(plan)
    repo_url = setting.get("repo_url")
    if match is None or not isinstance(repo_url, str):
        return None
    owner_repo = _owner_and_repo(repo_url)
    if owner_repo is None or owner_repo[1] != match["repo"]:
        return None
    return int(match["number"])


def _client_for(namespace: str, setting: dict):
    from pipeline.github_issues_client import GitHubIssuesClient, token_source_for
    from pipeline.github_issues_intake import _owner_and_repo

    owner_repo = _owner_and_repo(setting["repo_url"])
    if owner_repo is None:
        raise TypeError("repo_url is not a <owner>/<repo> repository URL")
    owner, repo = owner_repo
    token_source = token_source_for(namespace, setting.get("token_ref", ""))
    return GitHubIssuesClient(owner, repo, token_source)


def _run(client, number: int, event_type: object, story_key: object, stories: dict) -> None:
    if event_type == GUIDANCE_RESOLVED_EVENT:
        _answer_guidance(client, number)
        return
    comments = client.list_comments(number, MAX_LIST_RESULTS)
    _write_checklist(client, number, comments, stories)
    if event_type == "parked":
        _add_once(client, number, comments, PARKED_COMMENT.format(story=_clean(story_key)))
    elif event_type == "story_done" and _all_done(stories):
        _finish_once(client, number, comments)


def _answer_guidance(client, number: int) -> None:
    """Take the guidance label off and say the plan awaits approval, once.

    The comment is the marker: a replay finds it and changes nothing. The label
    is checked first, so a label already removed by a person is not a failure.
    """
    from pipeline.github_issues_intake import NEEDS_GUIDANCE_LABEL, _label_names

    comments = client.list_comments(number, MAX_LIST_RESULTS)
    if _has_comment(comments, GUIDANCE_RESOLVED_COMMENT):
        return
    if NEEDS_GUIDANCE_LABEL in _label_names(client.get_issue(number)):
        client.remove_label(number, NEEDS_GUIDANCE_LABEL)
    client.add_comment(number, GUIDANCE_RESOLVED_COMMENT)


def _write_checklist(client, number: int, comments: list[dict], stories: dict) -> None:
    body = _checklist(stories)
    for comment in comments:
        if isinstance(comment.get("body"), str) and comment["body"].startswith(MARKER):
            if comment["body"] != body:
                client.update_comment(comment["id"], body)
            return
    client.add_comment(number, body)


def _checklist(stories: dict) -> str:
    lines = [MARKER]
    for key, story in stories.items():
        if isinstance(story, dict) and isinstance(key, str):
            lines.append(_line(key, story))
    return "\n".join(lines)


def _line(key: str, story: dict) -> str:
    summary = _summary(story.get("summary")) or _clean(key)
    box, state = _state(story)
    return f"- [{box}] {_clean(key)}: {summary} ({state})"


def _state(story: dict) -> tuple[str, str]:
    """The checkbox and the state word for a story's manifest status."""
    status = story.get("status")
    if status == "done":
        return "x", "done"
    if status == "in_progress":
        return " ", "in progress"
    if status == "pr_open":
        pr_number = _pr_number(story.get("pr_url"))
        return " ", f"in review, PR #{pr_number}" if pr_number else "in review"
    return " ", "waiting"


def _pr_number(pr_url: object) -> str | None:
    if not isinstance(pr_url, str):
        return None
    match = _PR_PATH_RE.search(urlsplit(pr_url).path)
    return match.group(1) if match else None


def _summary(value: object) -> str:
    return _clean(value)[:SUMMARY_MAX].rstrip()


def _clean(value: object) -> str:
    """Remove markup characters and collapse whitespace, so the text stays on one line."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.translate(_CLEAN_TABLE).split())


def _all_done(stories: dict) -> bool:
    return bool(stories) and all(
        isinstance(story, dict) and story.get("status") == "done" for story in stories.values()
    )


def _has_comment(comments: list[dict], text: str) -> bool:
    return any(comment.get("body") == text for comment in comments)


def _add_once(client, number: int, comments: list[dict], text: str) -> None:
    if not _has_comment(comments, text):
        client.add_comment(number, text)


def _finish_once(client, number: int, comments: list[dict]) -> None:
    if not _has_comment(comments, DONE_COMMENT):
        client.add_labels(number, [DONE_LABEL])
        client.add_comment(number, DONE_COMMENT)
