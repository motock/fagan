"""Request-scoped context: who is asking, and which request it is.

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

The same request lifetime also carries the correlation id -- *which* request
this is, as opposed to *who* is asking -- so a transport binds both with the
same ``with`` nesting and a recording site reads ``actor_id()`` /
``current_correlation_id()`` and needs nothing else to say who did what, in
which request, on which log lines.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from app.identity import (
    LOCAL_OPERATOR,
    SYSTEM_SCHEDULER,  # noqa: F401 (re-exported)
    Principal,
)

#: A caller whose transport refused to identify them. Distinct from "no
#: transport at all": this one must never be treated as the local operator.
ANONYMOUS = Principal(subject="anonymous", auth_method="none")

#: An inbound correlation id is caller-controlled, so it is only accepted when
#: it has this shape: echoing an arbitrary header into a response header and
#: into logs is log forging.
CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

_BOUND: ContextVar[Principal | None] = ContextVar("pipeline_actor", default=None)

_CORRELATION: ContextVar[str | None] = ContextVar(
    "pipeline_correlation", default=None
)


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


def new_correlation_id() -> str:
    """Mint a correlation id: the 12-hex shape dispatch already uses per story."""
    return uuid.uuid4().hex[:12]


def request_correlation_id(candidate: str | None) -> str:
    """Return *candidate* when well formed, else a freshly minted id."""
    if candidate and CORRELATION_ID_RE.fullmatch(candidate):
        return candidate
    return new_correlation_id()


@contextmanager
def bind_correlation_id(value: str) -> Iterator[None]:
    """Bind *value* as the correlation id for the current request."""
    token = _CORRELATION.set(value)
    try:
        yield
    finally:
        _CORRELATION.reset(token)


def current_correlation_id() -> str:
    """Return the bound correlation id, or "" outside a request."""
    return _CORRELATION.get() or ""
