"""Shape rules for the ``jira_site`` and ``jira_board`` settings values.

Pure: no I/O, no environment reads, no logging. Each ``accepts_*`` function
returns False for every malformed value and never builds a message from the
value, since a token-shaped value may have been pasted into the wrong field.

The empty dict is the safe value for both keys (the integration is off), so it
is accepted, as ``role_routing`` accepts its empty safe value.
"""

from __future__ import annotations

import re
from typing import Any

from pipeline.secret_refs import SecretRefError, parse_ref

SITE_FIELDS = frozenset({"site", "email", "token_ref", "account_id"})
BOARD_FIELDS = frozenset(
    {"enabled", "project_key", "repo_url", "poll_seconds", "status"}
)
STATUS_FIELDS = frozenset({"in_progress", "in_review", "done", "parent_review"})

POLL_SECONDS_MIN = 60
POLL_SECONDS_MAX = 3600
STATUS_NAME_MAX = 60

# Jira Cloud only: a single DNS label under atlassian.net, no path, no port.
SITE_RE = re.compile(r"https://[a-z0-9][a-z0-9-]{0,62}\.atlassian\.net")
EMAIL_RE = re.compile(r"[^@\s/]+@[^@\s/]+\.[^@\s/]+")
ACCOUNT_ID_RE = re.compile(r"[A-Za-z0-9:_-]{1,128}")
PROJECT_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]{1,9}")
# Plain https only: no userinfo, query, fragment or port, so a URL that is
# later logged or shown cannot carry a credential.
REPO_URL_RE = re.compile(r"https://[A-Za-z0-9.-]+(?:/[A-Za-z0-9._~-]+)+")

JIRA_SITE_TYPE = "jira_site"
JIRA_BOARD_TYPE = "jira_board"


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def _is_plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_status_name(value: Any) -> bool:
    return (
        _is_str(value)
        and 1 <= len(value) <= STATUS_NAME_MAX
        and value.strip() != ""
        and value.isprintable()
    )


def _is_token_ref(value: Any) -> bool:
    if not _is_str(value):
        return False
    try:
        parse_ref(value)
    except SecretRefError:
        return False
    return True


def accepts_jira_site(value: Any) -> bool:
    """True for the empty safe value or a well-formed ``jira_site`` value."""
    if isinstance(value, dict) and not value:
        return True
    if not isinstance(value, dict) or set(value) != SITE_FIELDS:
        return False
    return (
        _is_str(value["site"])
        and SITE_RE.fullmatch(value["site"]) is not None
        and _is_str(value["email"])
        and EMAIL_RE.fullmatch(value["email"]) is not None
        and _is_token_ref(value["token_ref"])
        and _is_str(value["account_id"])
        and ACCOUNT_ID_RE.fullmatch(value["account_id"]) is not None
    )


def accepts_jira_board(value: Any) -> bool:
    """True for the empty safe value or a well-formed ``jira_board`` value."""
    if isinstance(value, dict) and not value:
        return True
    if not isinstance(value, dict) or set(value) != BOARD_FIELDS:
        return False
    status = value["status"]
    if not isinstance(status, dict) or set(status) != STATUS_FIELDS:
        return False
    return (
        isinstance(value["enabled"], bool)
        and _is_str(value["project_key"])
        and PROJECT_KEY_RE.fullmatch(value["project_key"]) is not None
        and _is_str(value["repo_url"])
        and REPO_URL_RE.fullmatch(value["repo_url"]) is not None
        and _is_plain_int(value["poll_seconds"])
        and POLL_SECONDS_MIN <= value["poll_seconds"] <= POLL_SECONDS_MAX
        and all(_is_status_name(status[name]) for name in STATUS_FIELDS)
    )
