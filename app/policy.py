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

from app.identity import LOCAL_OPERATOR, Principal

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


#: The three actions a plan's owner and its delegates govern (OWN-02).
_OWNER_GATED_ACTIONS = frozenset(
    {Action.PLAN_APPROVE, Action.PLAN_REJECT, Action.STORY_MERGE}
)


def _is_owner_or_delegate(
    principal: Principal, action: Action, resource: Resource | None
) -> bool:
    """Return whether *principal* owns or is delegated *action* on *resource*.

    Ownership is an ADDITIONAL authority for the owner-gated actions only, so
    every other action is untouched. A resource with no owner (every plan
    written before OWN-01) grants nothing: the ``resource.owner`` guard keeps
    an empty owner from matching any principal.
    """
    if resource is None or action not in _OWNER_GATED_ACTIONS:
        return False
    if resource.owner and principal.subject == resource.owner:
        return True
    return principal.subject in resource.delegates


class LocalPolicy:
    """The local single-operator policy: the operator may do everything.

    Every other principal — including any future system principal — is denied
    every action. ADR 0009 requires that a system principal never approve its
    own plan, so the deny is not special-cased per action.
    """

    def allow(
        self,
        principal: Principal | None,
        action: Action | str,
        resource: Resource | None = None,
    ) -> bool:
        if principal is None:
            return False

        coerced = _coerce_action(action)
        if coerced is None:
            return False

        if not isinstance(principal, Principal):
            return False

        if principal.subject == LOCAL_OPERATOR_SUBJECT:
            return True

        return _is_owner_or_delegate(principal, coerced, resource)


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
