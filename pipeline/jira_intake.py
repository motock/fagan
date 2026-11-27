"""Plan the Jira stories assigned to the Fagan service account.

Called once per scheduler tick. Off unless ``PIPELINE_JIRA_ENABLED`` is exactly
``1``, read on every call. Intake also requires the plan-approval gate
(``PIPELINE_PLAN_APPROVAL``) to be on and refuses to run otherwise: an ingested
plan would run on the next tick without a person's approval, and Jira text is
attacker-controllable. For each team whose ``jira_board`` is enabled, the
team's ``jira_site`` is resolved through the settings layers, the site and the
token name must be allowed by the operator (``PIPELINE_JIRA_SITES``), the
assigned stories are searched, and each new one is decomposed, saved and
ingested as the plan ``jira-<issue key>``. Jira text is untrusted: it reaches
the decomposer only and is never logged.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any
from urllib.parse import quote

from pipeline import notification_links
from pipeline.actor import bind_namespace, current_namespace
from pipeline.intake_guidance import take_questions
from pipeline.jira_client import JiraClient, JiraError, adf_to_text
from pipeline.live_ref import LiveRef
from pipeline.plan_approval import approval_enabled
from pipeline.plan_guidance import raise_guidance
from pipeline.secret_refs import SecretRefError, parse_ref
from pipeline.service import PipelineService
from pipeline.settings_registry import PROJECT_KEY_RE, SITE_RE
from pipeline.settings_store import effective_setting
from pipeline.store_namespaces import namespaces_with_plans

logger = logging.getLogger("pipeline.jira_intake")

ENABLE_ENV = "PIPELINE_JIRA_ENABLED"
ENABLED_VALUE = "1"
SITES_ENV = "PIPELINE_JIRA_SITES"
TOKEN_NAME_PREFIX = "jira"
PLANNED_LABEL = "fagan-planned"
FAILED_LABEL = "fagan-intake-failed"
PLAN_PREFIX = "jira-"
MAX_ISSUES_PER_RUN = 5
DEFAULT_POLL_SECONDS = 300
PLANNED_COMMENT = (
    "Fagan planned this story into {count} stories. "
    "It is waiting for team lead approval."
)
GUIDANCE_COMMENT = (
    "Fagan drafted this story into {count} stories and has {questions} questions "
    "before it can be approved. Answer them in Fagan."
)
MAX_EXCERPT_CHARS = 2000
FAILED_COMMENT = (
    "Fagan could not plan this story. A team member can fix the story and "
    "remove the label fagan-intake-failed to retry."
)
# Kinds that end a team's run: the credential, the quota, or the site is the
# problem for every issue of that team, so the rest of the run would fail too.
_STOP_KINDS = frozenset({"auth", "rate_limited", "unavailable"})
_ISSUE_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]+-[0-9]+")
_FIELDS = ["summary", "description", "status", "labels"]

# The store is resolved per access, through the same ``pipeline.server`` binding
# the settings resolver reads, so the intake sees what the rest of the pipeline sees.
_store = LiveRef("_store")

# Last run per (namespace, team), in memory: a restart polls once immediately.
_LAST_RUN: dict[tuple[str, str], float] = {}


class _PlanFailed(Exception):
    """Decompose, save or ingest did not produce a usable plan."""


def run_jira_intake(now: float) -> dict[str, int]:
    """Plan the assigned Jira stories for every enabled team. Returns outcome counts.

    Returns ``{}`` without reading settings or opening a connection unless
    ``PIPELINE_JIRA_ENABLED`` is exactly ``1``.
    """
    if os.environ.get(ENABLE_ENV) != ENABLED_VALUE:
        return {}
    if not _approval_gate_on():
        logger.warning("jira intake skipped: plan approval gate is off")
        return {}
    counts = {"planned": 0, "existing": 0, "failed": 0}
    records = _store.records()
    for namespace in sorted({current_namespace(), *namespaces_with_plans(records)}):
        try:
            with bind_namespace(namespace):
                _run_namespace(namespace, now, counts)
        except Exception as exc:  # noqa: BLE001 - one namespace never stops the others
            logger.warning(
                "jira intake failed for namespace",
                extra={"namespace": namespace, "error_type": type(exc).__name__},
            )
    return counts


def _approval_gate_on() -> bool:
    """Fail closed: an unrecognised approval setting counts as off."""
    try:
        return approval_enabled()
    except ValueError:
        return False


def _allowed_sites() -> frozenset[str]:
    """The operator's exact site allowlist, read per call. Malformed entries are ignored."""
    entries = [entry.strip() for entry in os.environ.get(SITES_ENV, "").split(",")]
    return frozenset(entry for entry in entries if SITE_RE.fullmatch(entry))


def _run_namespace(namespace: str, now: float, counts: dict[str, int]) -> None:
    records = _store.records()
    settings = records.list_settings(namespace)
    org_id = _single_org(settings)
    if org_id is None:
        logger.warning(
            "jira intake skipped: more than one org holds a jira_site",
            extra={"namespace": namespace},
        )
        return
    for record in settings:
        if record.get("scope") != "team" or record.get("repo") != "":
            continue
        board = (record.get("values") or {}).get("jira_board")
        if not isinstance(board, dict) or board.get("enabled") is not True:
            continue
        try:
            _run_team(namespace, record["scope_id"], board, org_id, now, counts)
        except Exception as exc:  # noqa: BLE001 - one team never stops the others
            logger.warning(
                "jira intake failed for team",
                extra={
                    "namespace": namespace,
                    "team": record["scope_id"],
                    "error_type": type(exc).__name__,
                },
            )


def _single_org(settings: list[dict]) -> str | None:
    """Return the one org that holds a ``jira_site``, ``""`` for none, ``None`` for many."""
    orgs = [
        record["scope_id"]
        for record in settings
        if record.get("scope") == "org"
        and (record.get("values") or {}).get("jira_site")
    ]
    if len(orgs) > 1:
        return None
    return orgs[0] if orgs else ""


def _run_team(
    namespace: str,
    team: str,
    board: dict[str, Any],
    org_id: str,
    now: float,
    counts: dict[str, int],
) -> None:
    slot = (namespace, team)
    last = _LAST_RUN.get(slot)
    poll = board.get("poll_seconds", DEFAULT_POLL_SECONDS)
    if last is not None and now - last < poll:
        return
    _LAST_RUN[slot] = now

    project_key = board.get("project_key")
    if (
        not isinstance(project_key, str)
        or PROJECT_KEY_RE.fullmatch(project_key) is None
    ):
        _skip_team(namespace, team, "invalid_board")
        return
    site = effective_setting(
        "jira_site", namespace=namespace, team=team, repo="", org=org_id
    )["value"]
    if not isinstance(site, dict) or not site:
        logger.info("jira intake skipped: no jira_site", extra={"team": team})
        return
    if not _site_allowed(site):
        _skip_team(namespace, team, "site_not_allowed")
        return
    if not _token_name_allowed(site):
        _skip_team(namespace, team, "token_ref_not_allowed")
        return
    client = _make_client(site)
    try:
        issues = client.search(
            _jql(project_key, site["account_id"]),
            _FIELDS,
            max_results=MAX_ISSUES_PER_RUN,
        )
    except JiraError as exc:
        _stop_team(team, exc)
        return
    for issue in issues[:MAX_ISSUES_PER_RUN]:
        try:
            _intake_issue(client, issue, team, board, counts)
        except JiraError as exc:
            if exc.kind in _STOP_KINDS:
                _stop_team(team, exc)
                return
            logger.warning(
                "jira write failed", extra={"team": team, "error_kind": exc.kind}
            )


def _site_allowed(site: dict[str, Any]) -> bool:
    return isinstance(site.get("site"), str) and site["site"] in _allowed_sites()


def _token_name_allowed(site: dict[str, Any]) -> bool:
    """Only a credential whose name starts with ``jira`` may authenticate Jira."""
    try:
        name = parse_ref(site.get("token_ref")).name
    except SecretRefError:
        return False
    return name.lower().startswith(TOKEN_NAME_PREFIX)


def _skip_team(namespace: str, team: str, reason: str) -> None:
    """Identifiers and the reason only: never the site, token reference or Jira text."""
    logger.warning(
        "jira team skipped",
        extra={"namespace": namespace, "team": team, "reason": reason},
    )


def _make_client(site: dict[str, Any]) -> JiraClient:
    return JiraClient(
        site["site"], site["email"], site["token_ref"], site["account_id"]
    )


def _jql(project_key: str, account_id: str) -> str:
    return (
        f'project = "{project_key}" AND issuetype = Story AND assignee = "{account_id}" '
        f"AND labels not in ({PLANNED_LABEL}, {FAILED_LABEL}) ORDER BY created ASC"
    )


def _stop_team(team: str, exc: JiraError) -> None:
    logger.warning(
        "jira team run stopped", extra={"team": team, "error_kind": exc.kind}
    )


def _intake_issue(
    client: JiraClient,
    issue: dict[str, Any],
    team: str,
    board: dict[str, Any],
    counts: dict[str, int],
) -> None:
    key = issue.get("key")
    if not isinstance(key, str) or _ISSUE_KEY_RE.fullmatch(key) is None:
        logger.warning("jira issue skipped: malformed key", extra={"team": team})
        return
    plan_name = PLAN_PREFIX + key.lower()
    if _store.records().get_manifest(current_namespace(), plan_name) is not None:
        client.add_labels(key, [PLANNED_LABEL])
        counts["existing"] += 1
        return

    raw_fields = issue.get("fields")
    fields: dict[str, Any] = raw_fields if isinstance(raw_fields, dict) else {}
    summary = fields.get("summary") if isinstance(fields.get("summary"), str) else ""
    request_text = f"Jira {key}: {summary}\n\n{adf_to_text(fields.get('description'))}"
    try:
        story_count, questions = _plan_story(
            plan_name, request_text, team, board["repo_url"]
        )
    except Exception as exc:  # noqa: BLE001 - any decompose/save/ingest failure parks the story
        logger.warning(
            "jira story could not be planned",
            extra={"issue": key, "team": team, "error_type": type(exc).__name__},
        )
        client.add_labels(key, [FAILED_LABEL])
        client.add_comment(key, FAILED_COMMENT)
        counts["failed"] += 1
        return

    needs_guidance = bool(questions) and _raise_guidance(
        plan_name, key, questions, request_text
    )
    client.add_labels(key, [PLANNED_LABEL])
    client.transition_to(key, board["status"]["in_progress"])
    if needs_guidance:
        comment = GUIDANCE_COMMENT.format(count=story_count, questions=len(questions))
    else:
        comment = PLANNED_COMMENT.format(count=story_count)
    client.add_comment(key, comment, link=_plan_link(plan_name))
    counts["planned"] += 1


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
    plan_name: str, key: str, questions: list[str], request_text: str
) -> bool:
    """Mark the planned ``plan_name`` as needing guidance. ``False`` when it did not.

    A refusal or a failure is logged with its reason token only and the issue is
    planned normally: the questions and the request text never reach a log.
    """
    try:
        result = raise_guidance(
            plan_name, questions, "jira", key, request_text[:MAX_EXCERPT_CHARS]
        )
    except Exception as exc:  # noqa: BLE001 - a guidance failure never fails the intake
        logger.warning(
            "jira guidance not raised",
            extra={"issue": key, "error_type": type(exc).__name__},
        )
        return False
    if result.get("ok") is True:
        return True
    logger.warning(
        "jira guidance not raised",
        extra={"issue": key, "reason": result.get("error")},
    )
    return False


def _plan_link(plan_name: str) -> str | None:
    """The plan-approval link, or ``None`` when no public URL is configured."""
    origin = notification_links._origin()
    path = notification_links._link_path()
    if origin is None or path is None:
        return None
    return f"{origin}{path}?plan={quote(plan_name, safe='')}"
