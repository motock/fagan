"""Text and once-per-episode state for the "dispatch is waiting" notice.

``pipeline/advance.py`` reports a tick whose dispatch-eligible stories were
held back by the dispatch gate (hrf-04: a misconfigured Ollama endpoint left
ready stories in ``todo`` with no reason anywhere). The reason comes from a
backend's ``resource_status()``, so it can embed a URL and can span lines;
:func:`sanitise_gate_reason` makes it safe to put in a notification.

``_notify_user`` records a ``dedup_key`` but does not suppress on it, so the
once-per-episode guard lives here, in-process, like ``review_gate_notice``.
Unlike that module the key is the reason, because the notice must name the
reason. A reason that embeds a changing measured value will therefore be
announced again whenever the value changes.
"""

from __future__ import annotations

import re

MAX_REASON_CHARS = 200
_FALLBACK_REASON = "dispatch backend unavailable"

_URL_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9+.-]*://)(\S*)")
_WHITESPACE_RE = re.compile(r"\s+")

_ANNOUNCED: dict[str, set[str]] = {}


def _strip_url_credentials_and_query(match: re.Match[str]) -> str:
    """Keep ``scheme://host/path``; drop userinfo, query and fragment."""
    scheme, rest = match.group(1), match.group(2)
    without_query = re.split(r"[?#]", rest, maxsplit=1)[0]
    host, slash, path = without_query.partition("/")
    host = host.rpartition("@")[2]
    return f"{scheme}{host}{slash}{path}"


def sanitise_gate_reason(reason: str | None) -> str:
    """One line, at most ``MAX_REASON_CHARS``, no URL credentials or query."""
    if not reason:
        return ""
    one_line = _WHITESPACE_RE.sub(" ", str(reason)).strip()
    clean = _URL_RE.sub(_strip_url_credentials_and_query, one_line)
    return clean[:MAX_REASON_CHARS].rstrip()


def group_gated_stories(gated: list[tuple[str, str | None]]) -> dict[str, list[str]]:
    """Group ``(story_key, raw_reason)`` pairs by their sanitised reason.

    Insertion order is preserved, so the first gated story sets the first group.
    A story whose gate gave no reason is grouped under a fixed fallback.
    """
    groups: dict[str, list[str]] = {}
    for key, raw_reason in gated:
        reason = sanitise_gate_reason(raw_reason) or _FALLBACK_REASON
        groups.setdefault(reason, []).append(key)
    return groups


def take_new_gate_reasons(plan_name: str, reasons: list[str]) -> list[str]:
    """Return the reasons not yet announced for this plan in this episode.

    ``reasons`` is the full set gated this tick. Reasons that are no longer
    gated are forgotten, so the gate returning later is announced again.
    An empty ``reasons`` (gate up, or nothing eligible) ends the episode.
    """
    announced = _ANNOUNCED.get(plan_name, set())
    fresh = [reason for reason in reasons if reason not in announced]
    if reasons:
        _ANNOUNCED[plan_name] = set(reasons)
    else:
        _ANNOUNCED.pop(plan_name, None)
    return fresh
