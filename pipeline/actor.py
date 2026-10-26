"""Request-scoped actor identity (ADR 0009 phase 1: actor propagation).

The process that runs a tool is not the caller. The MCP server is stdio, the
dashboard authenticates a shared secret, and the scheduler daemon is a
launchd job -- in every case the *caller* has to be established by the
transport that received the request, never by an argument to the tool the
caller invokes (a parameter named ``actor`` can be set to anything by whoever
holds the credential).

So a transport binds a ``Principal`` for the duration of one request with
``bind_principal`` and everything downstream reads it through
``current_principal`` / ``actor_id``. In order of authority:

* a principal a transport bound (the dashboard middleware today, the remote
  MCP HTTP transport once it verifies a token);
* ``ANONYMOUS`` when the transport went out of its way to authenticate nobody
  (the chat-stream ``k-`` pass-through);
* nothing bound at all -- a process that *is* the local operator (stdio MCP,
  the launchd daemon, a CLI script), which resolves to ``LOCAL_OPERATOR``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from app.identity import LOCAL_OPERATOR, Principal

#: A caller whose transport refused to identify them. Distinct from "no
#: transport at all": this one must never be treated as the local operator.
ANONYMOUS = Principal(subject="anonymous", auth_method="none")

_BOUND: ContextVar[Principal | None] = ContextVar("pipeline_actor", default=None)


@contextmanager
def bind_principal(principal: Principal) -> Iterator[None]:
    """Bind *principal* as the actor for the current request."""
    token = _BOUND.set(principal)
    try:
        yield
    finally:
        _BOUND.reset(token)


def current_principal() -> Principal:
    """Return the bound principal, or ``LOCAL_OPERATOR`` when nothing is bound."""
    return _BOUND.get() or LOCAL_OPERATOR


def actor_id() -> str:
    """Return the current actor's subject, for stamping onto records."""
    return current_principal().subject
