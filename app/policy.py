"""Deny-by-default authorization policy for the tenancy seam (ADR 0009).

This is the authorization half of the ADR 0009 seam: a policy over a verified
:class:`app.identity.Principal`. It is written to be strictly deny-by-default
and to fail closed, because the enforcement path trusts it as written.

Trust boundary, stated honestly: the authorizer is selected by the
operator-controlled ``PIPELINE_AUTHORIZER_PLUGIN=module:attr`` (see
:mod:`app.plugin_loader`), so whoever can set that variable chooses the code
that defines every ADR 0009 allow/deny decision — the same "setting the
variable is code execution in the orchestrator" boundary the plugin loader
documents, extended to the policy itself. The resolved authorizer may be an
arbitrary operator-supplied, stateful object, not just :class:`LocalPolicy`.
Unlike the other seams, the identity and authorizer seams both refuse to load a
plugin unless ``PIPELINE_PLUGIN_MODULES`` is set: for these two the module
allowlist is required, not merely recommended.

Once an operator-supplied authorizer has been built it is kept for the life of
the process, so a later write to ``PIPELINE_AUTHORIZER_PLUGIN`` (or clearing it)
cannot swap or downgrade a running process's decisions.
It imports ``app.identity`` and ``app.plugin_loader`` only: never ``app.auth``
(an adapter concern) and never ``pipeline.*`` (the service layer that calls it).
"""

from __future__ import annotations

import logging
import threading
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
from app.plugin_loader import (
    PluginError,
    load_plugin,
    plugin_spec,
    require_allowlist,
    require_methods,
)

logger = logging.getLogger(__name__)

#: The subject of the local single-operator deployment's principal.
LOCAL_OPERATOR_SUBJECT = LOCAL_OPERATOR.subject

#: ``auth_method`` values that mean "nobody authenticated". ``pipeline.actor``
#: defines ``ANONYMOUS`` this way, and this module may not import that one (the
#: dependency runs the other way), so the rule is stated here: an unauthenticated
#: caller may never create a plan, whatever subject it presents.
UNAUTHENTICATED_AUTH_METHODS = frozenset({"", "none"})


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
    DECISION_RECORD = "decision:record"


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

    Every other principal is denied every action, with three exceptions. A plan's
    ``owner`` and its ``delegates`` are allowed ``plan:approve``,
    ``plan:reject``, ``plan:dispatch``, ``story:merge``, ``decision:answer``,
    ``story:patch``, ``story:interrupt``, ``story:mark_done``, ``plan:pause``
    and plan-scoped ``config:write``; ownership is additive only — it can allow a principal
    the role rule denies, never deny one it allows — and a plan with no owner
    (every plan written before OWN-01) falls through to the role rule
    unchanged. Ownership never applies to a ``system:`` subject: ADR 0009
    requires that a system principal never approve its own plan.

    The second exception is the reserved ``system:`` principals, which hold
    exactly their row of :data:`SYSTEM_PRINCIPAL_GRANTS` and nothing else. That
    branch is decided before ownership is read, so naming a system subject as
    ``owner`` or ``delegate`` in a manifest grants it nothing.

    The third exception is ``plan:ingest``, which is how a plan acquires an
    owner in the first place (ADR 0006). An *authenticated* principal — one
    whose ``auth_method`` is neither empty nor ``"none"``, so never
    ``ANONYMOUS`` — may ingest a plan with a present resource and no owner, and
    becomes its owner; the same principal may re-ingest a plan it already owns.
    A missing resource is denied: ``resource is None`` is not evidence that the
    plan is new, so a call site that forgets the resource is refused rather than
    silently granted. The local operator is unaffected, because the operator
    check runs first. A plan owned by someone else stays closed, and nothing
    here widens any action beyond ``plan:ingest``.

    The fourth exception is ``decision:record``: every *authenticated* human or
    agent principal may record a judgment call on a plan it may view
    (:func:`can_view`), whether or not it owns the plan. Recording a judgment is
    not an authority grant -- it widens nothing the caller may do next -- but it
    stays inside the tenancy seam: a plan the caller cannot see is denied exactly
    as a missing plan is denied. A reserved ``system:`` principal holds no
    ``decision:record`` because its grant row does not list it, and an
    unauthenticated caller holds nothing.
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

        # ADR 0006 makes the *authenticated* caller a plan's owner at ingest.
        # `ANONYMOUS` is a Principal too (auth_method "none"), and it is not an
        # owner-in-waiting: an unauthenticated caller may never create a plan.
        if (
            resolved_action is Action.PLAN_INGEST
            and isinstance(principal, Principal)
            and principal.auth_method not in UNAUTHENTICATED_AUTH_METHODS
        ):
            if principal.subject == LOCAL_OPERATOR_SUBJECT:
                return True
            if resource is None:
                # A missing resource is not evidence that the plan is new.
                return False
            if not resource.owner:
                # A present resource with no owner: a legacy ownerless plan, so
                # the caller becomes its owner (resolve_ownership).
                return True
            return principal.subject == resource.owner

        # Ownership is an ADDITIONAL authority for exactly the ten actions a
        # plan's owner governs: it can only allow a principal the role rules
        # below would deny, never deny one they would allow. The `resource.owner
        # and` guard keeps a plan with no owner behaving as it does today,
        # rather than matching a principal whose subject is empty.
        if (
            resource is not None
            and isinstance(principal, Principal)
            and resolved_action
            in (
                Action.PLAN_APPROVE,
                Action.PLAN_REJECT,
                Action.PLAN_DISPATCH,
                Action.STORY_MERGE,
                Action.DECISION_ANSWER,
                Action.STORY_PATCH,
                Action.STORY_INTERRUPT,
                Action.STORY_MARK_DONE,
                Action.PLAN_PAUSE,
                Action.CONFIG_WRITE,
            )
            and (
                (resource.owner and principal.subject == resource.owner)
                or principal.subject in resource.delegates
            )
        ):
            return True

        # ``decision:record`` (the ``record_decision`` tool) is the one action
        # every authenticated human or agent holds on a plan it may view:
        # recording a judgment call is not an authority grant, so it needs no
        # owner/delegate status, only visibility. The reserved ``system:``
        # branch above already decided those principals by their grant row, so
        # a ``system:worker`` never reaches this rule and never records a
        # judgment. ``can_view`` is the same visibility rule the remote read
        # path uses, so a plan the caller may not see is denied exactly as a
        # plan that does not exist is denied -- the answer cannot be used to
        # probe which plans are on disk. An unauthenticated caller is not an
        # agent of the operator and records nothing.
        if resolved_action is Action.DECISION_RECORD:
            if not isinstance(principal, Principal):
                return False
            if principal.subject == LOCAL_OPERATOR_SUBJECT:
                return True
            return (
                principal.principal_type in ("human", "agent")
                and principal.auth_method not in UNAUTHENTICATED_AUTH_METHODS
                and can_view(principal, resource)
            )

        return (
            isinstance(principal, Principal)
            and principal.subject == LOCAL_OPERATOR_SUBJECT
        )


#: The operator-controlled variable that selects the authorizer seam.
AUTHORIZER_PLUGIN_ENV = "PIPELINE_AUTHORIZER_PLUGIN"

#: Built plugin authorizers, keyed by their spec. This keeps a stateful
#: authorizer (for example one holding a database connection) from being
#: rebuilt on every authorization check. Entries are written only after the
#: object passed :func:`app.plugin_loader.require_methods`, so a rejected
#: object is never handed to a later decision.
_PLUGIN_AUTHORIZERS: dict[str, Authorizer] = {}
#: Re-entrant: a plugin factory may itself ask for the authorizer while it is built.
_AUTHORIZER_LOCK = threading.RLock()


def default_authorizer() -> Authorizer:
    """Return the authorizer for this deployment.

    ``PIPELINE_AUTHORIZER_PLUGIN=module:attr`` selects an operator-supplied
    authorizer in place of :class:`LocalPolicy`; a bad spec raises
    :class:`~app.plugin_loader.PluginError` rather than falling back, because
    silently keeping ``LocalPolicy`` would change who is allowed. The first
    plugin authorizer built wins for the life of the process.
    """
    with _AUTHORIZER_LOCK:
        for live in _PLUGIN_AUTHORIZERS.values():
            return live
        spec = plugin_spec("authorizer")
        if spec is None:
            logger.warning(
                "no plugin configured for a security-sensitive seam",
                extra={
                    "plugin_seam": "authorizer",
                    "plugin_env": AUTHORIZER_PLUGIN_ENV,
                },
            )
            return LocalPolicy()
        # Deny by default for this seam: the authorizer decides every ADR 0009
        # action, so a tree-writer must not be able to install one without the
        # loader's module allowlist. Raises before anything is imported.
        require_allowlist("authorizer")
        factory = load_plugin("authorizer")
        if factory is None:
            # Unreachable outside monkeypatch: both reads take the same
            # stripped variable, so a non-None spec cannot yield no factory.
            # Kept as defence in depth instead of calling ``None``.
            raise PluginError(
                f"{AUTHORIZER_PLUGIN_ENV} is set but no authorizer plugin "
                "factory could be resolved"
            )
        built = factory()
        # Validate before caching: an unvalidated object must never be handed
        # back to a later decision (that would run with no policy at all).
        require_methods(built, ("allow",), "authorizer")
        _PLUGIN_AUTHORIZERS[spec] = built
        logger.info(
            "authorizer plugin loaded",
            extra={
                "plugin_seam": "authorizer",
                "plugin_module": spec.partition(":")[0],
            },
        )
        return built


def activate_authorizer() -> Authorizer:
    """Resolve the authorizer seam once at start-up and log which one is live.

    Resolution belongs to process start-up, not to the first authorization
    decision: a missing or rejected authorizer plugin must stop the process
    before it serves anything, and the operator needs a record of which
    authorizer is in force. :func:`default_authorizer` already keeps a built
    plugin for the life of the process, so calling this again neither rebuilds
    the object nor changes the decision -- it only records the seam again.

    :class:`~app.plugin_loader.PluginError` propagates on purpose: falling back
    to :class:`LocalPolicy` would change who is allowed.
    """
    authorizer = default_authorizer()
    # ``default_authorizer`` returns either a cached plugin object or a fresh
    # LocalPolicy, so identity against the cache is what distinguishes them --
    # more honest than ``isinstance``, which a plugin could satisfy.
    with _AUTHORIZER_LOCK:
        specs = [
            spec for spec, live in _PLUGIN_AUTHORIZERS.items() if live is authorizer
        ]
    # Only the module part of ``module:attr``: never the attribute, the raw
    # spec, a repr of the object, or any other value taken from the
    # environment.
    module = specs[0].partition(":")[0] if specs else "builtin"
    logger.info(
        "authorizer active",
        extra={"plugin_seam": "authorizer", "plugin_module": module},
    )
    return authorizer


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
    land denied, never propagate. Only the literal ``True`` authorizes, because
    the authorizer may now be arbitrary operator-supplied code and
    ``pipeline.authorization.authorize`` grades the answer with ``if allowed:``
    — a denial string, ``{"ok": False}``, ``1`` or an un-awaited coroutine are
    all truthy and would otherwise be read as ALLOW. ``bool(result)`` is not
    enough for the same reason, so the check is identity against ``True``.

    The warnings name the action and the principal subject only — never a
    credential, token, resource or the exception/return value.
    """
    try:
        result = authorizer.allow(principal, action, resource)
    except Exception:  # noqa: BLE001 - fail closed: any policy error lands denied
        logger.warning(
            "authorization policy failed; denying action=%s principal=%s",
            _action_label(action),
            getattr(principal, "subject", None),
        )
        return False
    if result is True:
        return True
    logger.warning(
        "authorization policy returned a non-boolean result; denying action=%s principal=%s",
        _action_label(action),
        getattr(principal, "subject", None),
    )
    return False
