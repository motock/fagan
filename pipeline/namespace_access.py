"""Verify the namespace a request asks for, and audit every denial.

``verify_namespace`` decides whether a principal may reach the namespace it named,
using the accesses the namespace resolver reports for it. The check is fail
closed: a malformed request, an empty or unreadable resolver answer, and a
namespace outside the principal's accesses are all denials with a reason token.

Each denial appends one host audit record carrying the subject, the reason and
the requested value. A malformed value is recorded only as a SHA-256 prefix,
because it may be anything. A successful decision writes nothing. An audit
append that fails is logged and never changes the decision.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TypeVar

from app.identity import Principal
from pipeline.actor import bind_namespace
from pipeline.audit import append_host_audit
from pipeline.namespace_resolver import (
    NamespaceResolver,
    active_namespace_resolver,
    checked_accesses,
)
from pipeline.store_records import InvalidRecordKeyError, validate_namespace

logger = logging.getLogger("pipeline.namespace_access")

#: The request header a caller names its namespace in. Every transport reads it.
NAMESPACE_HEADER = "x-fagan-namespace"

#: Reason tokens. Each denial carries exactly one of these.
NAMESPACE_MISSING = "namespace_missing"
NAMESPACE_MALFORMED = "namespace_malformed"
NAMESPACE_NOT_PERMITTED = "namespace_not_permitted"
NAMESPACE_ERROR = "namespace_error"

_AUDIT_ACTION = "namespace:verify"
_DIGEST_CHARS = 16


@dataclass(frozen=True)
class NamespaceDecision:
    """The outcome of one verification. A denial names no namespace and no roles."""

    allowed: bool
    namespace: str
    roles: frozenset[str]
    reason: str


def verify_namespace(
    principal: Principal,
    requested: str | None,
    resolver: NamespaceResolver | None = None,
) -> NamespaceDecision:
    """Return whether *principal* may reach the *requested* namespace.

    An empty *requested* selects the principal's one access, and is a denial
    when there are several. A malformed *requested* is refused before the
    resolver is consulted. An agent principal reaches only the namespace equal
    to its ``team_scope``.
    """
    is_empty = requested is None or requested == ""
    if not is_empty and not _is_valid(requested):
        return _deny(principal, NAMESPACE_MALFORMED, requested, malformed=True)

    try:
        active = resolver if resolver is not None else active_namespace_resolver()
        accesses = checked_accesses(active, principal)
    except Exception:  # noqa: BLE001 - any resolver failure denies, never allows
        logger.error("namespace resolver failed reason=%s", NAMESPACE_ERROR)
        return _deny(principal, NAMESPACE_ERROR, requested)

    if is_empty:
        if len(accesses) != 1:
            return _deny(principal, NAMESPACE_MISSING, requested)
        access = accesses[0]
    else:
        matches = [entry for entry in accesses if entry.namespace == requested]
        if not matches:
            return _deny(principal, NAMESPACE_NOT_PERMITTED, requested)
        access = matches[0]

    if principal.principal_type == "agent" and access.namespace != principal.team_scope:
        return _deny(principal, NAMESPACE_NOT_PERMITTED, requested)
    return NamespaceDecision(True, access.namespace, access.roles, "")


class NamespaceDenied(Exception):
    """A request asked for a namespace it may not reach. Carries the reason token only."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@contextmanager
def enter_request_namespace(
    principal: Principal, requested: str | None
) -> Iterator[NamespaceDecision]:
    """Verify *requested* for *principal*, then bind its namespace for the request.

    A denial raises :class:`NamespaceDenied` before the body runs, so nothing
    namespace-scoped executes unless the namespace was verified.
    """
    decision = verify_namespace(principal, requested)
    if not decision.allowed:
        raise NamespaceDenied(decision.reason)
    with bind_namespace(decision.namespace):
        yield decision


_T = TypeVar("_T")


async def serve_in_request_namespace(
    principal: Principal,
    header_values: list[str],
    serve: Callable[[], Awaitable[_T]],
    refuse: Callable[[str], Awaitable[_T]],
) -> _T:
    """Serve one request inside the namespace its header names.

    *header_values* is every value of ``X-Fagan-Namespace``; none means no
    namespace is named. A repeated header is joined into one value that the
    namespace grammar refuses, so it is a malformed denial, never a choice
    between the two. A denial before *serve* runs is answered by ``refuse``
    with the reason token. A denial raised by *serve* itself propagates.
    """
    requested = ",".join(header_values) or None
    admitted = False
    try:
        with enter_request_namespace(principal, requested):
            admitted = True
            return await serve()
    except NamespaceDenied as denied:
        if admitted:
            raise
        return await refuse(denied.reason)


def _is_valid(requested: object) -> bool:
    """Return whether *requested* is a whole, valid namespace value."""
    try:
        validate_namespace(requested)
    except InvalidRecordKeyError:
        return False
    return True


def _requested_field(requested: object, malformed: bool) -> object:
    """Return the value to record for *requested*, never a raw malformed one."""
    if not malformed:
        return "" if requested is None else requested
    if isinstance(requested, str):
        data = requested.encode("utf-8", "surrogatepass")
    else:
        data = type(requested).__name__.encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:_DIGEST_CHARS]


def _deny(
    principal: Principal,
    reason: str,
    requested: object,
    *,
    malformed: bool = False,
) -> NamespaceDecision:
    """Audit one denial, then return it. The audit write cannot change the outcome."""
    try:
        append_host_audit(
            action=_AUDIT_ACTION,
            outcome="denied",
            detail={
                "subject": principal.subject,
                "reason": reason,
                "requested": _requested_field(requested, malformed),
                "correlation_id": principal.correlation_id,
            },
        )
    except Exception:  # noqa: BLE001 - an audit failure must not turn a denial into an allow
        logger.error("namespace denial audit append failed reason=%s", reason)
    return NamespaceDecision(False, "", frozenset(), reason)
