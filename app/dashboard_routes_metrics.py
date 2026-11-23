"""``GET /api/metrics``: the operator-only Prometheus scrape surface.

The route is mounted under ``/api`` (see :func:`app.dashboard_mount.mount_routes`)
so the dashboard's existing middleware authenticates the caller: an OIDC
session or bearer token, or the shared-secret key. Authentication is not
authorization, so the handler additionally requires an **operator**:

* the local operator principal (:data:`app.identity.LOCAL_OPERATOR`, which the
  shared-secret provider attaches) -- under the shared-secret provider the
  local operator *is* the administrator; or
* any principal holding :data:`app.identity.ROLE_ADMIN`; or
* any principal whose token scopes contain exactly :data:`METRICS_READ_SCOPE`
  (``metrics-read``), so a machine scraper needs no admin role. The scope is
  matched exactly, never by substring, and grants nothing else.

The local operator is allowed everything (``app.policy.LocalPolicy``) and the
admin role is the one role that carries that authority; the ``metrics-read``
scope is a narrow read-only grant for this route alone. The identity module
owns the role and the operator principal, so this route adds no second roles.
Every refusal is the same fixed body, which names neither the caller nor the
reason detail.

The body is the Prometheus text exposition from
:func:`pipeline.metrics_exposition.render`, with the ``text/plain;
version=0.0.4`` media type a scraper expects. It carries aggregates only: no
plan name, story key, file path, subject or exception text (see that module's
docstring).

This surface is deliberately *not* served at ``/livez``/``/readyz``: those
probes are unauthenticated on purpose because an orchestrator has no
credential, and a metrics payload is operator data.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from app.identity import LOCAL_OPERATOR, ROLE_ADMIN
from pipeline.metrics_exposition import collect, render

router = APIRouter()

#: The one refusal body this route returns, for every non-operator caller.
_NOT_AUTHORIZED_DETAIL = "not authorized: operator"

#: The token scope that admits a machine client to this route and nothing else.
METRICS_READ_SCOPE = "metrics-read"

#: The exposition media type: format 0.0.4, the version Prometheus scrapes.
MEDIA_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _may_read_metrics(principal) -> bool:
    """Whether *principal* may read the metrics exposition.

    Deny by default: no principal (an unauthenticated caller that somehow
    reached the handler) may read it. The admin role is the ADR 0009 role that
    carries operator authority; the local operator principal holds it too, and
    is named explicitly so a future narrowing of the local role set cannot
    quietly close the local deployment. A principal without a ``scopes`` value,
    or with an empty one, is refused; the scope must match exactly.
    """
    if principal is None:
        return False
    if principal == LOCAL_OPERATOR:
        return True
    if ROLE_ADMIN in getattr(principal, "roles", frozenset()):
        return True
    scopes = getattr(principal, "scopes", ())
    # A bare string would match by substring, so only a real collection counts.
    return isinstance(scopes, (tuple, list, frozenset)) and METRICS_READ_SCOPE in scopes


@router.get(
    "/metrics",
    responses={
        200: {
            "description": "Prometheus text exposition format 0.0.4.",
            "content": {"text/plain": {"schema": {"type": "string"}}},
        }
    },
)
def get_metrics(request: Request) -> Response:
    """Return the metrics exposition to an operator or a ``metrics-read`` caller, or a fixed 403."""
    if not _may_read_metrics(getattr(request.state, "principal", None)):
        raise HTTPException(status_code=403, detail=_NOT_AUTHORIZED_DETAIL)
    return Response(content=render(collect()), media_type=MEDIA_TYPE)
