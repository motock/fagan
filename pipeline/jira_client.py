"""Jira Cloud REST client for the planning side of the Jira integration.

Only ``https://<name>.atlassian.net`` sites are accepted. The API token is a
``secret://`` reference resolved at call time and never stored on the client,
logged, or put in an exception. Redirects are not followed.

Jira text (summaries, descriptions, comments) is untrusted input. It reaches
only the decomposer, and the plan it produces still needs a person's approval
before anything runs (ADR 0007). Nothing here logs Jira text.

Argument errors (a malformed key, an empty JQL) raise ``ValueError`` before any
request. Failures from Jira or the network raise :class:`JiraError`, whose
message carries the kind and HTTP status only.
"""

from __future__ import annotations

import base64
import logging
import re
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.credentials import CredentialError, CredentialRef, resolve_for_injection
from pipeline.secret_refs import parse_ref
from pipeline.settings_registry import ACCOUNT_ID_RE, EMAIL_RE, PROJECT_KEY_RE, SITE_RE

_LOGGER = logging.getLogger("pipeline.jira_client")

ERROR_KINDS = ("auth", "not_found", "rate_limited", "unavailable", "invalid")
TIMEOUT_SECONDS = 10.0
MAX_SEARCH_PAGES = 5
MAX_TEXT_CHARS = 20000
MAX_ADF_DEPTH = 50
SUBTASK_ISSUE_TYPE = "Sub-task"
SUMMARY_MAX = 255

_ISSUE_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]+-[0-9]+")
_LABEL_RE = re.compile(r"[A-Za-z0-9_.:-]{1,255}")
_LIST_KINDS = frozenset({"bulletList", "orderedList"})
_LINE_KINDS = frozenset({"paragraph", "heading", "codeBlock"})


class JiraError(Exception):
    """A Jira request failed. ``kind`` is one of :data:`ERROR_KINDS`."""

    def __init__(self, kind: str, status: int | None = None) -> None:
        if kind not in ERROR_KINDS:
            raise ValueError("unknown Jira error kind")
        self.kind = kind
        self.status = status
        detail = f" (HTTP {status})" if status is not None else ""
        super().__init__(f"Jira request failed: {kind}{detail}")


def _error(
    operation: str,
    kind: str,
    status: int | None = None,
    error_type: str | None = None,
) -> JiraError:
    """Log the failure's identifiers and return the error to raise."""
    _LOGGER.warning(
        "jira request failed",
        extra={
            "jira_operation": operation,
            "error_kind": kind,
            "http_status": status,
            "error_type": error_type,
        },
    )
    return JiraError(kind, status)


def _kind_for_status(status: int) -> str:
    if status in (401, 403):
        return "auth"
    if status == 404:
        return "not_found"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "unavailable"
    return "invalid"


def _check_issue_key(key: object) -> str:
    if not isinstance(key, str) or _ISSUE_KEY_RE.fullmatch(key) is None:
        raise ValueError("invalid Jira issue key")
    return key


def _is_https_link(value: object) -> bool:
    if not isinstance(value, str) or not value.isprintable() or " " in value:
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme == "https" and bool(parts.hostname) and "@" not in parts.netloc


def _paragraph(text: str) -> dict[str, Any]:
    if text == "":
        return {"type": "paragraph", "content": []}
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


def _doc(lines: list[str]) -> dict[str, Any]:
    return {"type": "doc", "version": 1, "content": [_paragraph(line) for line in lines]}


def _children(node: dict[str, Any]) -> list[dict[str, Any]]:
    content = node.get("content")
    if not isinstance(content, list):
        return []
    return [child for child in content if isinstance(child, dict)]


def _node_text(node: dict[str, Any], depth: int) -> str:
    if depth > MAX_ADF_DEPTH:
        return ""
    kind = node.get("type")
    if kind == "text":
        text = node.get("text")
        return text if isinstance(text, str) else ""
    if kind == "hardBreak":
        return "\n"
    if kind in _LIST_KINDS:
        return _list_text(_children(node), ordered=kind == "orderedList", depth=depth)
    body = "".join(_node_text(child, depth + 1) for child in _children(node))
    if kind in _LINE_KINDS:
        return body + "\n"
    return body


def _list_text(items: list[dict[str, Any]], ordered: bool, depth: int) -> str:
    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        marker = f"{index}. " if ordered else "- "
        body = _node_text(item, depth + 1).rstrip("\n")
        first, *rest = body.split("\n")
        lines.append(marker + first)
        lines.extend(" " * len(marker) + line for line in rest)
    return "\n".join(lines) + "\n" if lines else ""


def adf_to_text(adf: Any) -> str:
    """Flatten an Atlassian Document Format tree to plain text.

    Paragraphs, headings and code blocks become lines; lists keep their markers
    and nesting; text marks are dropped; an unknown node contributes its text
    children. Anything that is not a document yields "".
    """
    if not isinstance(adf, dict):
        return ""
    return _node_text(adf, 0).rstrip()[:MAX_TEXT_CHARS]


class JiraClient:
    """Read and write the Jira Cloud issues a team's plans map onto."""

    def __init__(
        self,
        site: str,
        email: str,
        token_ref: str,
        account_id: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not isinstance(site, str) or SITE_RE.fullmatch(site) is None:
            raise ValueError("invalid Jira site")
        if not isinstance(email, str) or EMAIL_RE.fullmatch(email) is None:
            raise ValueError("invalid Jira service account email")
        if not isinstance(account_id, str) or ACCOUNT_ID_RE.fullmatch(account_id) is None:
            raise ValueError("invalid Jira account id")
        try:
            ref = parse_ref(token_ref)
        except ValueError:
            raise ValueError("Jira token must be a secret:// reference") from None
        self._site = site
        self._email = email
        self._credential_name = f"{ref.provider}/{ref.name}"
        self.account_id = account_id
        self._transport = transport

    def _authorization(self) -> str:
        try:
            token = resolve_for_injection(
                CredentialRef(purpose="secret", name=self._credential_name)
            )
        except CredentialError:
            raise _error("authorize", "auth") from None
        raw = f"{self._email}:{token}".encode()
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def _request(
        self, method: str, path: str, operation: str, json_body: Any = None
    ) -> httpx.Response:
        headers = {"Accept": "application/json", "Authorization": self._authorization()}
        try:
            with httpx.Client(
                timeout=TIMEOUT_SECONDS,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = client.request(
                    method, f"{self._site}{path}", headers=headers, json=json_body
                )
        except httpx.TransportError as exc:
            raise _error(operation, "unavailable", error_type=type(exc).__name__) from None
        if not response.is_success:
            raise _error(
                operation, _kind_for_status(response.status_code), response.status_code
            ) from None
        return response

    def _json(
        self, method: str, path: str, operation: str, json_body: Any = None
    ) -> dict[str, Any]:
        response = self._request(method, path, operation, json_body)
        if not response.content:
            return {}
        try:
            data = response.json()
        except ValueError:
            raise _error(operation, "invalid", response.status_code) from None
        if not isinstance(data, dict):
            raise _error(operation, "invalid", response.status_code)
        return data

    def search(
        self, jql: str, fields: Sequence[str], max_results: int = 50
    ) -> list[dict[str, Any]]:
        """Return the issues matching ``jql``, following at most five pages."""
        if not isinstance(jql, str) or not jql.strip():
            raise ValueError("jql must be a non-empty string")
        if not isinstance(fields, (list, tuple)) or not all(
            isinstance(field, str) for field in fields
        ):
            raise ValueError("fields must be a list of strings")
        if (
            isinstance(max_results, bool)
            or not isinstance(max_results, int)
            or not 1 <= max_results <= 100
        ):
            raise ValueError("max_results must be between 1 and 100")
        issues: list[dict[str, Any]] = []
        token: str | None = None
        for _ in range(MAX_SEARCH_PAGES):
            body: dict[str, Any] = {
                "jql": jql,
                "fields": list(fields),
                "maxResults": max_results,
            }
            if token is not None:
                body["nextPageToken"] = token
            data = self._json("POST", "/rest/api/3/search/jql", "search", body)
            page = data.get("issues")
            if not isinstance(page, list) or not all(isinstance(i, dict) for i in page):
                raise _error("search", "invalid")
            issues.extend(page)
            token = data.get("nextPageToken")
            if token is None:
                break
            if not isinstance(token, str) or not token:
                raise _error("search", "invalid")
        return issues

    def get_issue(self, key: str) -> dict[str, Any]:
        """Return the raw issue document for ``key``."""
        _check_issue_key(key)
        return self._json("GET", f"/rest/api/3/issue/{key}", "get_issue")

    def create_subtask(
        self,
        parent_key: str,
        project_key: str,
        summary: str,
        description_text: str,
        labels: Sequence[str],
    ) -> str:
        """Create a sub-task under ``parent_key`` and return its new key."""
        _check_issue_key(parent_key)
        if not isinstance(project_key, str) or PROJECT_KEY_RE.fullmatch(project_key) is None:
            raise ValueError("invalid Jira project key")
        if (
            not isinstance(summary, str)
            or not summary.strip()
            or len(summary) > SUMMARY_MAX
        ):
            raise ValueError("summary must be 1 to 255 characters")
        if not isinstance(description_text, str):
            raise TypeError("description must be a string")
        _check_labels(labels)
        body = {
            "fields": {
                "project": {"key": project_key},
                "parent": {"key": parent_key},
                "issuetype": {"name": SUBTASK_ISSUE_TYPE},
                "summary": summary,
                "description": _doc(description_text.splitlines() or [""]),
                "labels": list(labels),
            }
        }
        data = self._json("POST", "/rest/api/3/issue", "create_subtask", body)
        key = data.get("key")
        if not isinstance(key, str) or _ISSUE_KEY_RE.fullmatch(key) is None:
            raise _error("create_subtask", "invalid")
        return key

    def add_labels(self, key: str, labels: Sequence[str]) -> None:
        """Add ``labels`` to the issue without removing existing ones."""
        _check_issue_key(key)
        _check_labels(labels)
        if not labels:
            return
        body = {"update": {"labels": [{"add": label} for label in labels]}}
        self._request("PUT", f"/rest/api/3/issue/{key}", "add_labels", body)

    def add_comment(self, key: str, text: str, link: str | None = None) -> None:
        """Comment on the issue; ``link`` adds a paragraph holding an https link."""
        _check_issue_key(key)
        if not isinstance(text, str):
            raise TypeError("comment text must be a string")
        content = _doc(text.splitlines() or [""])["content"]
        if link is not None:
            if not _is_https_link(link):
                raise ValueError("comment link must be a plain https URL")
            content.append(
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": link,
                            "marks": [{"type": "link", "attrs": {"href": link}}],
                        }
                    ],
                }
            )
        body = {"body": {"type": "doc", "version": 1, "content": content}}
        self._request("POST", f"/rest/api/3/issue/{key}/comment", "add_comment", body)

    def transition_to(self, key: str, status_name: str) -> bool:
        """Move the issue to the status named ``status_name``.

        Returns False, without posting, when no transition leads to a status
        with that name (compared case-insensitively).
        """
        _check_issue_key(key)
        if not isinstance(status_name, str) or not status_name.strip():
            raise ValueError("status name must be a non-empty string")
        path = f"/rest/api/3/issue/{key}/transitions"
        data = self._json("GET", path, "transition_list")
        transitions = data.get("transitions", [])
        if not isinstance(transitions, list):
            raise _error("transition_list", "invalid")
        wanted = status_name.casefold()
        for transition in transitions:
            if not isinstance(transition, dict):
                continue
            target = transition.get("to")
            name = target.get("name") if isinstance(target, dict) else None
            if not isinstance(name, str) or name.casefold() != wanted:
                continue
            transition_id = transition.get("id")
            if not isinstance(transition_id, str) or not transition_id:
                raise _error("transition", "invalid")
            self._request("POST", path, "transition", {"transition": {"id": transition_id}})
            return True
        return False


def _check_labels(labels: object) -> None:
    if not isinstance(labels, (list, tuple)) or not all(
        isinstance(label, str) and _LABEL_RE.fullmatch(label) for label in labels
    ):
        raise ValueError("labels must be a list of plain label names")
