"""PipelineService.request_decision's body, moved out of pipeline/service.py
(line-count target). The method keeps its signature and docstring and
delegates here. Server-owned names are _ServerRef bindings, exactly as in
pipeline.service, so tests patching pipeline.server still land.
"""

import logging
import re
from datetime import UTC, datetime

from app.identity import LOCAL_OPERATOR

from .actor import actor_id
from .authorization import _load_manifest

#: A routing subject: one line, 1-64 characters, no control characters. This is
#: the same 1-64 character bound and one-line intent as
#: ``actor.CORRELATION_ID_RE``, with a wider charset because a subject may be an
#: email address or ``ops:oncall``. A subject is interpolated into the
#: line-oriented notifications log, so a newline there would forge a line.
SUBJECT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@:-]{0,63}")

#: Cap on the delegates a manifest may route to, so a hostile list cannot bury
#: neighbouring records or grow the routing record without bound.
MAX_DELEGATES = 64

#: Parsed ACTION values that mean a human must answer: the overlord parks the
#: story for one. A ruling with a truthy ``notify_user`` escalates too - the
#: contract asks the user to read and answer it.
_ESCALATING_ACTIONS = frozenset({"park_for_human"})


def _escalates_to_human(ruling: dict) -> bool:
    """Return whether *ruling* hands the decision to a human.

    ``_parse_ruling`` yields ``action`` (normalized, so unparseable payloads
    land on ``park_for_human``) and ``notify_user`` (a bool). Either one saying
    "a human is needed" is enough; anything else is a ruling the overlord
    settled itself.
    """
    return ruling.get("action") in _ESCALATING_ACTIONS or bool(ruling.get("notify_user"))


def _valid_subject(value: object) -> str | None:
    """Return *value* as a routing subject, or ``None`` when it is not one.

    A subject is a single line of ``SUBJECT_RE``. The raw value is checked for
    newlines, carriage returns and ANSI escapes *before* stripping, because
    stripping would otherwise hide a trailing newline and accept ``"alice\n"``
    as ``"alice"``. Non-strings are rejected outright rather than coerced:
    ``str()`` would turn ``123`` into a routable ``"123"``.
    """
    if not isinstance(value, str):
        return None
    if "\n" in value or "\r" in value or "\x1b" in value:
        return None
    candidate = value.strip()
    return candidate if SUBJECT_RE.fullmatch(candidate) else None


def _routing_target(plan_name: str) -> dict:
    """Return who must answer a decision escalated to a human (ADR 0006).

    The target is the plan manifest's ``owner`` plus the ``delegates`` the
    owner named. A manifest that names no owner routes to the local operator,
    which is the deployment's single human. The result is descriptive: it says
    who the answer belongs to, and this module never delivers it.

    An unreadable manifest must not turn a routable escalation into a tool
    error, so any read failure yields the local-operator default. Only the plan
    name is logged - never the exception text or a path (Secure-by-Design).

    The same holds for an owner that is not a subject: this value is
    interpolated into the line-oriented notifications log, so a newline would
    forge a line there and an unbounded value would bury neighbouring records.
    A value that is not a subject falls back to the local operator - the same
    deny-to-safe-default as an unreadable manifest - and the notification is
    still sent. That includes a real owner whose subject is merely outside the
    charset (a plus-addressed email, an OIDC ``issuer#sub``): it cannot be told
    apart from a hostile value here, so the local operator answers instead
    (ADR 0006).
    """
    default = {"owner": LOCAL_OPERATOR.subject, "delegates": []}
    try:
        manifest = _load_manifest(plan_name)
    except Exception:  # noqa: BLE001 - an unreadable manifest must not raise
        logging.getLogger("pipeline").warning(
            "could not read the plan manifest for routing plan=%s", plan_name
        )
        return default
    if not isinstance(manifest, dict):
        return default
    raw_delegates = manifest.get("delegates")
    delegates: list[str] = []
    if isinstance(raw_delegates, list):
        # Filter first, then cap, so a hostile entry never consumes a slot; the
        # manifest's list is never mutated.
        for item in raw_delegates:
            subject = _valid_subject(item)
            if subject is None:
                continue
            delegates.append(subject)
            if len(delegates) == MAX_DELEGATES:
                break
    return {
        "owner": _valid_subject(manifest.get("owner")) or LOCAL_OPERATOR.subject,
        "delegates": delegates,
    }


def _request_decision_impl(self, plan_name: str, story_key: str, question: str, options: list[str], context: str=''):
    _validate_key(plan_name)
    _validate_key(story_key)
    with _scoped_repo_root(plan_name):
        policy = _load_policy()
    opts = "\n".join(f"  - {o}" for o in options)
    prompt = (
        f"A pipeline agent working on story {story_key} is blocked on a decision.\n\n"
        f"QUESTION: {question}\n\n"
        f"OPTIONS:\n{opts}\n\n"
        f"CONTEXT: {context}\n\n"
        f"DECISION POLICY:\n{policy}\n\n"
        f"Rule now, using your output contract exactly."
    )
    try:
        ruling = _parse_ruling(
            _invoke_overlord(prompt, plan_role_config=_plan_role_config(plan_name))
        )
    except Exception as exc:  # noqa: BLE001 - fail open: an overlord error must not reach the agent
        # Fail open: an overlord backend error must never surface to the
        # calling agent as a raw tool error. Park the story for a human and
        # return an actionable message instead (mirrors triage.rule_on_story).
        # Only the exception class name is recorded - never the traceback,
        # file paths or payload data (Secure-by-Design).
        summary = type(exc).__name__
        logging.getLogger("pipeline").warning(
            "request_decision overlord call failed open for story %s: %s",
            story_key,
            summary,
        )
        routed_to = _routing_target(plan_name)
        record = {
            "story_key": story_key,
            "question": question,
            "options": list(options),
            "ruling": "",
            "tier": "",
            "risk": "",
            "rationale": f"overlord call failed open: {summary}",
            "action": "park_for_human",
            "notify_user": True,
            "split": [],
            "failed_open": True,
            "summary": summary,
            "decided_by": "overlord",
            "requested_by": actor_id(),
            "routed_to": routed_to,
            "decided_at": datetime.now(UTC).isoformat(),
        }
        try:
            _store.append_decision(plan_name, record)
        except Exception:  # noqa: BLE001 - persistence failure must not crash the tool
            logging.getLogger("pipeline").warning(
                "Failed to append fail-open decision for story %s", story_key
            )
        try:
            _store.update_story(
                plan_name,
                story_key,
                {
                    "status": "parked",
                    "parked_reason": f"overlord failure: {summary}",
                },
            )
        except Exception:  # noqa: BLE001 - an already-parked story must not crash the tool
            logging.getLogger("pipeline").warning(
                "Failed to park story %s after overlord failure", story_key
            )
        try:
            # Make the park visible to the operator (mirrors triage._park), and
            # name who the answer now belongs to, so the notification is
            # actionable on its own. Only the exception class name is included -
            # no payload data - and the owner is a name, never the raw target.
            _notify_user(
                plan_name,
                f"request_decision failed open for {story_key}: {summary}; "
                f"see decisions log (routed to {routed_to['owner']})",
                story_key=story_key,
                severity="warning",
                event="overlord_failure",
            )
        except Exception:  # noqa: BLE001 - notification failure must not crash the tool
            logging.getLogger("pipeline").warning(
                "Failed to notify user about fail-open decision for story %s", story_key
            )
        return "decision escalated to human: story parked, see decisions log"
    record = {
        "story_key": story_key,
        "question": question,
        "options": list(options),
        **ruling,
        "decided_by": "overlord",
        "requested_by": actor_id(),
        "decided_at": datetime.now(UTC).isoformat(),
    }
    if _escalates_to_human(ruling):
        record["routed_to"] = _routing_target(plan_name)
    _store.append_decision(plan_name, record)
    return record


# must follow the def: service.py back-imports _request_decision_impl (line 163), so defining it first keeps both import directions cycle-free
from .service import _ServerRef

_invoke_overlord = _ServerRef("_invoke_overlord")
_load_policy = _ServerRef("_load_policy")
_notify_user = _ServerRef("_notify_user")
_parse_ruling = _ServerRef("_parse_ruling")
_plan_role_config = _ServerRef("_plan_role_config")
_scoped_repo_root = _ServerRef("_scoped_repo_root")
_store = _ServerRef("_store")
_validate_key = _ServerRef("_validate_key")
