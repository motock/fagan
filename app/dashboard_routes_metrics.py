"""``GET /api/metrics``: the operator-only Prometheus scrape surface.

The route is mounted under ``/api`` (see :func:`app.dashboard_mount.mount_routes`)
so the dashboard's existing middleware authenticates the caller: an OIDC
session or bearer token, or the shared-secret key. Authentication is not
authorization, so the handler additionally requires an **operator**:

* the local operator principal (:data:`app.identity.LOCAL_OPERATOR`, which the
  shared-secret provider attaches) -- under the shared-secret provider the
  local operator *is* the administrator; or
* any principal holding :data:`app.identity.ROLE_ADMIN`.

That is the same rule the rest of the dashboard's operator-only surfaces use:
the local operator is allowed everything (``app.policy.LocalPolicy``) and the
admin role is the one role that carries that authority, so this route adds no
second authorization concept -- it asks the identity module that owns both.
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

#: The exposition media type: format 0.0.4, the version Prometheus scrapes.
MEDIA_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _is_operator(principal) -> bool:
    """Whether *principal* may read the operator-only metrics.

    Deny by default: no principal (an unauthenticated caller that somehow
    reached the handler) is not an operator. The admin role is the ADR 0009
    role that carries operator authority; the local operator principal holds
    it too, and is named explicitly so a future narrowing of the local role set
    cannot quietly close the local deployment.
    """
    if principal is None:
        return False
    if principal == LOCAL_OPERATOR:
        return True
    return ROLE_ADMIN in getattr(principal, "roles", frozenset())


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
    """Return the metrics exposition to an operator, or a fixed 403."""
    if not _is_operator(getattr(request.state, "principal", None)):
        raise HTTPException(status_code=403, detail=_NOT_AUTHORIZED_DETAIL)
    return Response(content=render(collect()), media_type=MEDIA_TYPE)
