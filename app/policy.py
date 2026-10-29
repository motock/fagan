"""Deny-by-default authorization policy for the tenancy seam (ADR 0009).

This is the authorization half of the ADR 0009 seam: a pure, stateless policy
over a verified :class:`app.identity.Principal`. It is inert in this plan —
nothing calls it yet — so it is written to be strictly deny-by-default and to
fail closed, because the enforcement plan will trust it as written.

It imports ``app.identity`` only: never ``app.auth`` (an adapter concern) and
never ``pipeline.*`` (the service layer that will call it).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from app.identity import (
    LOCAL_OPERATOR,
    SYSTEM_AUTH_METHOD,
    SYSTEM_SCHEDULER,
    SYSTEM_SUBJECT_PREFIX,
    SYSTEM_WORKER,
    Principal,
)

logger = logging.getLogger(__name__)

#: The subject of the local single-operator deployment's principal.
LOCAL_OPERATOR_SUBJECT = LOCAL_OPERATOR.subject


class Action(str, Enum):
    """The mutating actions ADR 0009 authorizes."""

    PLAN_INGEST = "plan:ingest"
    PLAN_APPROVE = "plan:approve"
    PLAN_REJECT = "plan:reject"
    PLAN_DISPATCH = "plan:dispatch"
    PLAN_PAUSE = "plan:pause"
    STORY_PATCH = "story:patch"
    STORY_INTERRUPT = "story:interrupt"
    DECISION_ANSWER = "decision:answer"
    STORY_MERGE = "story:merge"
    STORY_MARK_DONE = "story:mark_done"
    CONFIG_WRITE = "config:write"


#: The actions each reserved ``system:`` principal holds, and nothing more.
#:
#: The rows are the actions the code those principals run actually reaches:
#: the scheduler tick (``advance_all_plans``/``scan_all_plans``) dispatches
#: (``plan:dispatch``), merges (``story:merge``) and interrupts
#: (``story:interrupt``); a dispatched worker only patches its own story
#: (``story:patch``). The tick's merge path writes ``status="done"`` itself, so
#: ``story:mark_done`` — which only the human ``mark_story_done`` tool asks for
#: — is absent, as are ``plan:approve``, ``plan:reject``, ``plan:ingest``,
#: ``config:write`` and ``decision:answer``: a system principal never holds
#: human authority, and ADR 0009 forbids a scheduler approving its own work.
#: ``plan:pause`` (pause / resume tools) and the scheduler's ``story:patch`` are
#: likewise absent because no system code path asks for them.
#:
#: Keyed by subject, not auth method: a reserved subject stays confined to its
#: row whatever ``auth_method`` it arrives with. The values are frozensets, so
#: a decision can never add an action to a row.
SYSTEM_PRINCIPAL_GRANTS: dict[str, frozenset[Action]] = {
    SYSTEM_SCHEDULER.subject: frozenset(
        {
            Action.PLAN_DISPATCH,
            Action.STORY_MERGE,
            Action.STORY_INTERRUPT,
        }
    ),
    SYSTEM_WORKER.subject: frozenset({Action.STORY_PATCH}),
}


@dataclass(frozen=True)
class Resource:
    """The ADR 0009 scope chain: principal → team → repo → plan → story."""

    tenant: str = ""
    team: str = ""
    repo: str = ""
    plan: str = ""
    story: str = ""
    owner: str = ""
    delegates: tuple[str, ...] = ()


class Authorizer(Protocol):
    """Anything that can answer ``allow(principal, action, resource)``."""

    def allow(
        self,
        principal: Principal | None,
        action: Action | str,
        resource: Resource | None = None,
    ) -> bool: ...


def _coerce_action(action: Action | str) -> Action | None:
    """Return ``action`` as an :class:`Action`, or ``None`` if it is not one.

    A raw string is accepted only when it is exactly an ``Action`` value; any
    other type — including ``None`` and non-string objects — is rejected
    without raising.
    """
    if isinstance(action, Action):
        return action
    if isinstance(action, str):
        try:
            return Action(action)
        except ValueError:
            return None
    return None


class LocalPolicy:
    """The local single-operator policy: the operator may do everything.

    Every other principal is denied every action, with two exceptions. A plan's
    ``owner`` and its ``delegates`` are allowed ``plan:approve``,
    ``plan:reject`` and ``story:merge``; ownership is additive only — it can
    allow a principal the role rule denies, never deny one it allows — and a
    plan with no owner (every plan written before OWN-01) falls through to the
    role rule unchanged. Ownership never applies to a ``system:`` subject: ADR
    0009 requires that a system principal never approve its own plan.

    The second exception is the reserved ``system:`` principals, which hold
    exactly their row of :data:`SYSTEM_PRINCIPAL_GRANTS` and nothing else. That
    branch is decided before ownership is read, so naming a system subject as
    ``owner`` or ``delegate`` in a manifest grants it nothing.
    """

    def allow(
        self,
        principal: Principal | None,
        action: Action | str,
        resource: Resource | None = None,
    ) -> bool:
        if principal is None:
            return False

        resolved_action = _coerce_action(action)
        if resolved_action is None:
            return False

        # A reserved system principal is decided by its grant row alone, before
        # any manifest is consulted: ownership must never widen a `system:`
        # subject, or a scheduler could approve the plan it dispatches.
        if isinstance(principal, Principal) and (
            principal.subject.startswith(SYSTEM_SUBJECT_PREFIX)
            or principal.auth_method == SYSTEM_AUTH_METHOD
        ):
            return resolved_action in SYSTEM_PRINCIPAL_GRANTS.get(
                principal.subject, frozenset()
            )

        # Ownership is an ADDITIONAL authority for exactly the three actions a
        # plan's owner governs: it can only allow a principal the role rules
        # below would deny, never deny one they would allow. The `resource.owner
        # and` guard keeps a pre-OWN-01 plan (no owner) behaving as it does
        # today, rather than matching a principal whose subject is empty.
        if (
            resource is not None
            and isinstance(principal, Principal)
            and resolved_action
            in (Action.PLAN_APPROVE, Action.PLAN_REJECT, Action.STORY_MERGE)
            and (
                (resource.owner and principal.subject == resource.owner)
                or principal.subject in resource.delegates
            )
        ):
            return True

        return (
            isinstance(principal, Principal)
            and principal.subject == LOCAL_OPERATOR_SUBJECT
        )


def default_authorizer() -> Authorizer:
    """Return the authorizer for this deployment."""
    return LocalPolicy()


def _action_label(action: object) -> str:
    """Return a log-safe label for ``action``: its value, or its type name."""
    if isinstance(action, Action):
        return action.value
    if isinstance(action, str):
        return action
    return type(action).__name__


def can_view(principal: Principal | None, resource: Resource | None) -> bool:
    """Return whether *principal* may see the plan *resource* scopes.

    Deny by default: the local operator sees every plan; a principal sees a plan
    it owns or is a delegate of; every other plan -- including one with no owner
    at all -- is invisible to a remote principal. Ownership is matched against
    ``resource.owner`` only when it is non-empty, so a pre-OWN-01 plan with no
    owner never matches a principal whose subject is empty.

    Fails closed on any error, exactly as :func:`is_allowed` does: an
    unreadable resource hides the plan rather than showing it. The warning
    names the decision and the principal subject only -- never the plan name or
    the exception text.

    The local-operator branch compares subjects, so a remote caller claiming
    that subject would match here; ``pipeline.remote_tool_policy`` refuses any
    such identity before this check runs, and identity can only narrow access.
    """
    try:
        if not isinstance(principal, Principal) or resource is None:
            return False
        if principal.subject == LOCAL_OPERATOR_SUBJECT:
            return True
        if resource.owner and principal.subject == resource.owner:
            return True
        return principal.subject in resource.delegates
    except Exception:  # noqa: BLE001 - fail closed: any policy error hides the plan
        logger.warning(
            "plan visibility check failed; denying view principal=%s",
            getattr(principal, "subject", None),
        )
        return False


def is_allowed(
    authorizer: Authorizer,
    principal: Principal | None,
    action: Action | str,
    resource: Resource | None = None,
) -> bool:
    """Ask ``authorizer`` and fail closed on any error.

    This is the wrapper every future call site uses: a policy that raises must
    land denied, never propagate. The warning names the action and the
    principal subject only — never a credential, token, resource or the
    exception text.
    """
    try:
        return authorizer.allow(principal, action, resource)
    except Exception:  # noqa: BLE001 - fail closed: any policy error lands denied
        logger.warning(
            "authorization policy failed; denying action=%s principal=%s",
            _action_label(action),
            getattr(principal, "subject", None),
        )
        return False
