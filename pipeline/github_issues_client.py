"""GitHub REST client for the GitHub Issues intake (github.com only).

The API base is the constant ``https://api.github.com``. No caller, setting or
environment variable can change it, and GitHub Enterprise Server is not
supported. Redirects are not followed. The token comes from a zero-argument
``token_source`` called once per request, so it is never stored on the client,
logged, or put in an exception.

Issue titles, bodies and comments are untrusted input. Nothing here logs them,
and a repository URL, an issue number, or a token never reaches a log record.

Argument errors (a malformed owner, repo, label or issue number) raise
``ValueError`` or ``TypeError`` before any request. Failures from GitHub or the
network raise :class:`GitHubIssuesError`, whose message carries the kind and
HTTP status only.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.credentials import (
    CredentialError,
    CredentialMissing,
    CredentialRef,
    resolve_for_injection,
)
from pipeline.secret_refs import SecretRefError, parse_ref
from pipeline.settings_registry import GITHUB_ISSUE_LABEL_RE

_LOGGER = logging.getLogger("pipeline.github_issues_client")

API_BASE = "https://api.github.com"
API_VERSION = "2022-11-28"
ERROR_KINDS = ("auth", "not_found", "rate_limited", "unavailable", "invalid")
TIMEOUT_SECONDS = 10.0
MAX_LIST_RESULTS = 100
FORGE_TOKEN_NAME = "github_token"
TOKEN_NAME_PREFIX = "github-issues"

_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")


class GitHubIssuesError(Exception):
    """A GitHub request failed. ``kind`` is one of :data:`ERROR_KINDS`."""

    def __init__(self, kind: str, status: int | None = None) -> None:
        if kind not in ERROR_KINDS:
            raise ValueError("unknown GitHub Issues error kind")
        self.kind = kind
        self.status = status
        detail = f" (HTTP {status})" if status is not None else ""
        super().__init__(f"GitHub Issues request failed: {kind}{detail}")


def _error(
    operation: str,
    kind: str,
    status: int | None = None,
    error_type: str | None = None,
) -> GitHubIssuesError:
    """Log the failure's identifiers and return the error to raise."""
    _LOGGER.warning(
        "github issues request failed",
        extra={
            "github_operation": operation,
            "error_kind": kind,
            "http_status": status,
            "error_type": error_type,
        },
    )
    return GitHubIssuesError(kind, status)


def _kind_for_response(response: httpx.Response) -> str:
    status = response.status_code
    # GitHub answers 403 for both a bad token and a rate limit; the headers tell them apart.
    if status == 403 and (
        response.headers.get("X-RateLimit-Remaining") == "0"
        or "Retry-After" in response.headers
    ):
        return "rate_limited"
    if status in (401, 403):
        return "auth"
    if status == 404:
        return "not_found"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "unavailable"
    return "invalid"


def _is_name(value: object) -> bool:
    """True for an owner or repository name that cannot change the URL's path."""
    return (
        isinstance(value, str)
        and _NAME_RE.fullmatch(value) is not None
        and value.strip(".") != ""
    )


def _is_label(value: object) -> bool:
    return (
        isinstance(value, str)
        and GITHUB_ISSUE_LABEL_RE.fullmatch(value) is not None
        and value.strip(".") != ""
    )


def _check_id(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{what} must be a positive integer")
    return value


def _check_limit(max_results: object) -> int:
    if (
        isinstance(max_results, bool)
        or not isinstance(max_results, int)
        or not 1 <= max_results <= MAX_LIST_RESULTS
    ):
        raise ValueError("max_results must be between 1 and 100")
    return max_results


def _check_labels(labels: object) -> list[str]:
    if not isinstance(labels, (list, tuple)) or not all(
        _is_label(label) for label in labels
    ):
        raise ValueError("labels must be a list of plain label names")
    return list(labels)


def _is_https_link(value: object) -> bool:
    if not isinstance(value, str) or not value.isprintable() or " " in value:
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme == "https" and bool(parts.hostname) and "@" not in parts.netloc


def _parse(response: httpx.Response, operation: str) -> Any:
    try:
        return response.json()
    except ValueError:
        raise _error(operation, "invalid", response.status_code) from None


def _objects(data: Any, operation: str, status: int) -> list[dict[str, Any]]:
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise _error(operation, "invalid", status)
    return data


class GitHubIssuesClient:
    """Read and write the GitHub issues a team's plans map onto."""

    def __init__(
        self,
        owner: str,
        repo: str,
        token_source: Callable[[], str | None],
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not _is_name(owner):
            raise ValueError("invalid GitHub owner")
        if not _is_name(repo):
            raise ValueError("invalid GitHub repository")
        if not callable(token_source):
            raise TypeError("token_source must be callable")
        self._repo_path = f"/repos/{owner}/{repo}"
        self._token_source = token_source
        self._transport = transport

    def _authorization(self, operation: str) -> str:
        try:
            token = self._token_source()
        except CredentialError:
            raise _error(operation, "auth") from None
        if not token:
            raise _error(operation, "auth")
        return f"Bearer {token}"

    def _request(
        self,
        method: str,
        path: str,
        operation: str,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": self._authorization(operation),
            "X-GitHub-Api-Version": API_VERSION,
        }
        try:
            with httpx.Client(
                timeout=TIMEOUT_SECONDS,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = client.request(
                    method,
                    f"{API_BASE}{path}",
                    headers=headers,
                    json=json_body,
                    params=params,
                )
        except httpx.TransportError as exc:
            raise _error(
                operation, "unavailable", error_type=type(exc).__name__
            ) from None
        if not response.is_success:
            raise _error(
                operation, _kind_for_response(response), response.status_code
            ) from None
        return response

    def list_open_issues(self, label: str, max_results: int) -> list[dict[str, Any]]:
        """Return up to ``max_results`` open issues carrying ``label``.

        The API lists pull requests as issues, so they are dropped here and the
        page may hold fewer than ``max_results`` entries.
        """
        if not _is_label(label):
            raise ValueError("label must be a plain label name")
        limit = _check_limit(max_results)
        params = {"state": "open", "labels": label, "per_page": limit}
        response = self._request(
            "GET", f"{self._repo_path}/issues", "list_open_issues", params=params
        )
        issues = _objects(
            _parse(response, "list_open_issues"),
            "list_open_issues",
            response.status_code,
        )
        return [issue for issue in issues if "pull_request" not in issue]

    def get_issue(self, number: int) -> dict[str, Any]:
        """Return the raw issue document for ``number``."""
        number = _check_id(number, "issue number")
        response = self._request(
            "GET", f"{self._repo_path}/issues/{number}", "get_issue"
        )
        data = _parse(response, "get_issue")
        if not isinstance(data, dict):
            raise _error("get_issue", "invalid", response.status_code)
        return data

    def add_labels(self, number: int, labels: Sequence[str]) -> None:
        """Add ``labels`` to the issue without removing existing ones."""
        number = _check_id(number, "issue number")
        names = _check_labels(labels)
        if not names:
            return
        path = f"{self._repo_path}/issues/{number}/labels"
        self._request("POST", path, "add_labels", {"labels": names})

    def remove_label(self, number: int, label: str) -> None:
        """Remove ``label`` from the issue; GitHub answers 404 when it is not set."""
        number = _check_id(number, "issue number")
        if not _is_label(label):
            raise ValueError("label must be a plain label name")
        path = f"{self._repo_path}/issues/{number}/labels/{label}"
        self._request("DELETE", path, "remove_label")

    def add_comment(self, number: int, text: str, link: str | None = None) -> int:
        """Comment on the issue and return the new comment's id.

        ``link`` is appended as its own paragraph and must be a plain https URL.
        """
        number = _check_id(number, "issue number")
        if not isinstance(text, str):
            raise TypeError("comment text must be a string")
        body = text
        if link is not None:
            if not _is_https_link(link):
                raise ValueError("comment link must be a plain https URL")
            body = f"{text}\n\n{link}"
        path = f"{self._repo_path}/issues/{number}/comments"
        response = self._request("POST", path, "add_comment", {"body": body})
        data = _parse(response, "add_comment")
        comment_id = data.get("id") if isinstance(data, dict) else None
        if isinstance(comment_id, bool) or not isinstance(comment_id, int):
            raise _error("add_comment", "invalid", response.status_code)
        return comment_id

    def update_comment(self, comment_id: int, text: str) -> None:
        """Replace the text of an existing comment on the repository."""
        comment_id = _check_id(comment_id, "comment id")
        if not isinstance(text, str):
            raise TypeError("comment text must be a string")
        path = f"{self._repo_path}/issues/comments/{comment_id}"
        self._request("PATCH", path, "update_comment", {"body": text})

    def list_comments(self, number: int, max_results: int) -> list[dict[str, Any]]:
        """Return up to ``max_results`` comments on the issue, oldest first."""
        number = _check_id(number, "issue number")
        limit = _check_limit(max_results)
        path = f"{self._repo_path}/issues/{number}/comments"
        response = self._request(
            "GET", path, "list_comments", params={"per_page": limit}
        )
        return _objects(
            _parse(response, "list_comments"), "list_comments", response.status_code
        )


def token_source_for(namespace: str, token_ref: str) -> Callable[[], str | None]:
    """Return the zero-argument token source for a team's ``github_issues`` setting.

    An empty ``token_ref`` reads the namespace's forge ``github_token``. Any
    other value must be a ``secret://`` reference whose name starts with
    ``github-issues``, so this integration cannot read an arbitrary secret.
    Refusals name the rule only, never the reference.
    """
    if token_ref == "":
        ref = CredentialRef("forge", FORGE_TOKEN_NAME, namespace=namespace)

        def forge_token() -> str | None:
            try:
                return resolve_for_injection(ref)
            except CredentialMissing:
                return None

        return forge_token
    try:
        parsed = parse_ref(token_ref)
    except SecretRefError:
        raise ValueError("GitHub Issues token must be a secret:// reference") from None
    if not parsed.name.lower().startswith(TOKEN_NAME_PREFIX):
        raise ValueError("GitHub Issues token name must start with github-issues")
    ref = CredentialRef("secret", f"{parsed.provider}/{parsed.name}")
    return lambda: resolve_for_injection(ref)
