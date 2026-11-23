"""Plan the GitHub issues labelled for Fagan.

Called once per scheduler tick. Off unless ``PIPELINE_GITHUB_ISSUES_ENABLED``
is exactly ``1``, read on every call. Intake also requires the plan-approval
gate (``PIPELINE_PLAN_APPROVAL``) to be on and refuses to run otherwise: an
ingested plan would run on the next tick without a person's approval, and
issue text is written by anyone who can open an issue. For each team whose
``github_issues`` is enabled, and whose ``jira_board`` is not (a team has one
ticketing provider), up to 5 open issues carrying the team's label and neither
Fagan label are decomposed, saved and ingested as the plan
``gh-<repo name>-<number>``. Issue text reaches the decomposer only and is
never logged.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from urllib.parse import quote, urlsplit

from pipeline import notification_links
from pipeline.actor import bind_namespace, current_namespace
from pipeline.github_issues_client import (
    MAX_LIST_RESULTS,
    GitHubIssuesClient,
    GitHubIssuesError,
    token_source_for,
)
from pipeline.intake_guidance import take_questions
from pipeline.live_ref import LiveRef
from pipeline.plan_approval import approval_enabled
from pipeline.plan_guidance import raise_guidance
from pipeline.service import PipelineService
from pipeline.store_namespaces import namespaces_with_plans

logger = logging.getLogger("pipeline.github_issues_intake")

ENABLE_ENV = "PIPELINE_GITHUB_ISSUES_ENABLED"
ENABLED_VALUE = "1"
PLANNED_LABEL = "fagan-planned"
FAILED_LABEL = "fagan-intake-failed"
NEEDS_GUIDANCE_LABEL = "fagan-needs-guidance"
PLAN_PREFIX = "gh-"
MAX_ISSUES_PER_RUN = 5
MAX_REQUEST_CHARS = 8000
DEFAULT_POLL_SECONDS = 300
PLANNED_COMMENT = (
    "Fagan planned this issue into {count} stories. "
    "It is waiting for team lead approval."
)
GUIDANCE_COMMENT = (
    "Fagan planned this issue into {count} stories and has {questions} questions "
    "before it can be approved. Answer them in Fagan."
)
MAX_EXCERPT_CHARS = 2000
FAILED_COMMENT = (
    "Fagan could not plan this issue. A team member can fix the issue and "
    "remove the label fagan-intake-failed to retry."
)
# Kinds that end a team's run: the credential, the quota, or the service is the
# problem for every issue of that team, so the rest of the run would fail too.
_STOP_KINDS = frozenset({"auth", "rate_limited", "unavailable"})

# The store is resolved per access, through the same ``pipeline.server`` binding
# the settings resolver reads, so the intake sees what the rest of the pipeline sees.
_store = LiveRef("_store")

# Last run per (namespace, team), in memory: a restart polls once immediately.
_LAST_RUN: dict[tuple[str, str], float] = {}


class _PlanFailed(Exception):
    """Decompose, save or ingest did not produce a usable plan."""


def run_github_issues_intake(now: float) -> dict[str, int]:
    """Plan the labelled GitHub issues for every enabled team. Returns outcome counts.

    Returns ``{}`` without reading settings or opening a connection unless
    ``PIPELINE_GITHUB_ISSUES_ENABLED`` is exactly ``1``.
    """
    if os.environ.get(ENABLE_ENV) != ENABLED_VALUE:
        return {}
    if not _approval_gate_on():
        logger.warning("github issues intake skipped: plan approval gate is off")
        return {}
    counts = {"planned": 0, "existing": 0, "failed": 0}
    records = _store.records()
    for namespace in sorted({current_namespace(), *namespaces_with_plans(records)}):
        try:
            with bind_namespace(namespace):
                _run_namespace(namespace, now, counts)
        except Exception as exc:  # noqa: BLE001 - one namespace never stops the others
            logger.warning(
                "github issues intake failed for namespace",
                extra={"namespace": namespace, "error_type": type(exc).__name__},
            )
    return counts


def _approval_gate_on() -> bool:
    """Fail closed: an unrecognised approval setting counts as off."""
    try:
        return approval_enabled()
    except ValueError:
        return False


def _run_namespace(namespace: str, now: float, counts: dict[str, int]) -> None:
    records = _store.records()
    for record in records.list_settings(namespace):
        if record.get("scope") != "team" or record.get("repo") != "":
            continue
        values = record.get("values") or {}
        setting = values.get("github_issues")
        if not isinstance(setting, dict) or setting.get("enabled") is not True:
            continue
        team = record["scope_id"]
        if _jira_enabled(values):
            _skip_team(namespace, team, "both_configured")
            continue
        try:
            _run_team(namespace, team, setting, now, counts)
        except Exception as exc:  # noqa: BLE001 - one team never stops the others
            logger.warning(
                "github issues intake failed for team",
                extra={
                    "namespace": namespace,
                    "team": team,
                    "error_type": type(exc).__name__,
                },
            )


def _jira_enabled(values: dict[str, Any]) -> bool:
    board = values.get("jira_board")
    return isinstance(board, dict) and board.get("enabled") is True


def _run_team(
    namespace: str,
    team: str,
    setting: dict[str, Any],
    now: float,
    counts: dict[str, int],
) -> None:
    slot = (namespace, team)
    last = _LAST_RUN.get(slot)
    poll = setting.get("poll_seconds", DEFAULT_POLL_SECONDS)
    if last is not None and now - last < poll:
        return
    _LAST_RUN[slot] = now

    repo_url = setting["repo_url"]
    owner_repo = _owner_and_repo(repo_url)
    if owner_repo is None:
        _skip_team(namespace, team, "invalid_repo_url")
        return
    try:
        token_source = token_source_for(namespace, setting.get("token_ref", ""))
    except ValueError:
        _skip_team(namespace, team, "invalid_token_ref")
        return
    owner, repo = owner_repo
    try:
        client = _make_client(owner, repo, token_source)
    except ValueError:
        _skip_team(namespace, team, "invalid_repo_url")
        return

    try:
        issues = client.list_open_issues(setting["label"], MAX_LIST_RESULTS)
    except GitHubIssuesError as exc:
        _stop_team(team, exc)
        return
    # GitHub cannot exclude labels in the query, so the Fagan labels are dropped
    # here, and only the first MAX_ISSUES_PER_RUN unlabelled issues are planned.
    pending = [issue for issue in issues if not _is_marked(issue)]
    for issue in pending[:MAX_ISSUES_PER_RUN]:
        try:
            _intake_issue(client, issue, team, repo_url, owner, repo, counts)
        except GitHubIssuesError as exc:
            if exc.kind in _STOP_KINDS:
                _stop_team(team, exc)
                return
            logger.warning(
                "github write failed",
                extra={"team": team, "error_kind": exc.kind},
            )


def _owner_and_repo(repo_url: str) -> tuple[str, str] | None:
    """The owner and repository of a ``https://github.com/<owner>/<repo>`` URL."""
    parts = urlsplit(repo_url).path.strip("/").split("/")
    if len(parts) != 2:
        return None
    return parts[0], parts[1]


def _make_client(owner: str, repo: str, token_source: Any) -> GitHubIssuesClient:
    return GitHubIssuesClient(owner, repo, token_source)


def _label_names(issue: dict[str, Any]) -> set[str]:
    raw = issue.get("labels")
    if not isinstance(raw, list):
        return set()
    names = set()
    for item in raw:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str):
            names.add(name)
    return names


def _is_marked(issue: dict[str, Any]) -> bool:
    return bool(_label_names(issue) & {PLANNED_LABEL, FAILED_LABEL})


def _skip_team(namespace: str, team: str, reason: str) -> None:
    """Identifiers and the reason only: never the repository, token or issue text."""
    logger.warning(
        "github issues team skipped",
        extra={"namespace": namespace, "team": team, "reason": reason},
    )


def _stop_team(team: str, exc: GitHubIssuesError) -> None:
    logger.warning(
        "github issues team run stopped",
        extra={"team": team, "error_kind": exc.kind},
    )


def _intake_issue(
    client: GitHubIssuesClient,
    issue: dict[str, Any],
    team: str,
    repo_url: str,
    owner: str,
    repo: str,
    counts: dict[str, int],
) -> None:
    number = issue.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        logger.warning("github issue skipped: malformed number", extra={"team": team})
        return
    plan_name = f"{PLAN_PREFIX}{repo}-{number}"
    if _store.records().get_manifest(current_namespace(), plan_name) is not None:
        client.add_labels(number, [PLANNED_LABEL])
        counts["existing"] += 1
        return

    request_text = _request_text(issue)
    try:
        story_count, questions = _plan_story(plan_name, request_text, team, repo_url)
    except Exception as exc:  # noqa: BLE001 - any decompose/save/ingest failure parks the issue
        logger.warning(
            "github issue could not be planned",
            extra={"issue": number, "team": team, "error_type": type(exc).__name__},
        )
        client.add_labels(number, [FAILED_LABEL])
        client.add_comment(number, FAILED_COMMENT)
        counts["failed"] += 1
        return

    needs_guidance = bool(questions) and _raise_guidance(
        plan_name, number, owner, repo, questions, request_text
    )
    if needs_guidance:
        client.add_labels(number, [PLANNED_LABEL, NEEDS_GUIDANCE_LABEL])
        comment = GUIDANCE_COMMENT.format(count=story_count, questions=len(questions))
    else:
        client.add_labels(number, [PLANNED_LABEL])
        comment = PLANNED_COMMENT.format(count=story_count)
    client.add_comment(number, comment, link=_plan_link(plan_name))
    counts["planned"] += 1


def _request_text(issue: dict[str, Any]) -> str:
    """The issue title and body, capped. Both are untrusted text."""
    title = issue.get("title") if isinstance(issue.get("title"), str) else ""
    body = issue.get("body") if isinstance(issue.get("body"), str) else ""
    return f"{title}\n\n{body}"[:MAX_REQUEST_CHARS]


def _plan_story(
    plan_name: str, request_text: str, team: str, repo_url: str
) -> tuple[int, list[str]]:
    """Decompose, save and ingest one plan. Returns its story count and open questions."""
    service = PipelineService()
    decomposed = service.decompose_plan(request_text, repo_url=repo_url)
    if not decomposed.get("ok"):
        raise _PlanFailed("decompose")
    plan = decomposed["plan"]
    questions = take_questions(plan)
    plan["team"] = team
    if not service.save_plan(plan_name, json.dumps(plan)).get("ok"):
        raise _PlanFailed("save")
    if not service.ingest_plan(plan_name).get("ok"):
        raise _PlanFailed("ingest")
    story_count = sum(len(epic.get("stories", [])) for epic in plan["epics"])
    return story_count, questions


def _raise_guidance(
    plan_name: str,
    number: int,
    owner: str,
    repo: str,
    questions: list[str],
    request_text: str,
) -> bool:
    """Mark the planned ``plan_name`` as needing guidance. ``False`` when it did not.

    A refusal or a failure is logged with its reason token only and the issue is
    planned normally: the questions and the issue text never reach a log.
    """
    try:
        result = raise_guidance(
            plan_name,
            questions,
            "github",
            f"{owner}/{repo}#{number}",
            request_text[:MAX_EXCERPT_CHARS],
        )
    except Exception as exc:  # noqa: BLE001 - a guidance failure never fails the intake
        logger.warning(
            "github guidance not raised",
            extra={"issue": number, "error_type": type(exc).__name__},
        )
        return False
    if result.get("ok") is True:
        return True
    logger.warning(
        "github guidance not raised",
        extra={"issue": number, "reason": result.get("error")},
    )
    return False


def _plan_link(plan_name: str) -> str | None:
    """The plan-approval link, or ``None`` when no public URL is configured."""
    origin = notification_links._origin()
    path = notification_links._link_path()
    if origin is None or path is None:
        return None
    return f"{origin}{path}?plan={quote(plan_name, safe='')}"
