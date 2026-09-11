"""Shared-secret API key gate for the dashboard HTTP API.

The dashboard is a single-operator local tool, so authentication is a single
shared secret held in a 0600 file at the repo root rather than a user store.
`require_api_key` is invoked from `app/dashboard.py`'s HTTP middleware for
every request under the "/api/" prefix, so it covers that surface
uniformly — there is no per-route opt-in. The index route ("/") and static
assets are deliberately exempt: "/" is what hands the key to the browser in
the first place, so gating it behind the same header would be an
unreachable chicken-and-egg lock, and static assets carry no secrets.

Pass-through pipeline credentials (SSE-03): the chat stream route forwards
the ``X-Pipeline-Api-Key`` header value into ``ChatService`` so its internal
tool calls can authenticate against the pipeline API (see ``app/chat.py``'s
``api_key`` plumbing and ``tests/unit/test_chat_api_key_plumbing.py``). That
forwarded value is a caller/operator-supplied UPSTREAM credential, not a
dashboard login, so it is never compared against the dashboard's own shared
secret here.

The rule this module enforces, stated plainly:

* The dashboard shared secret (the 0600 file) is required on every
  ``/api/*`` route EXCEPT the chat stream route. An ``X-Pipeline-Api-Key``
  header value is just an untrusted, caller-chosen string on those routes:
  no prefix or shape check on it can ever be an authentication decision,
  because the caller picks the value.
* On the chat stream route ONLY, a ``k-``-prefixed header value is accepted
  as the pass-through upstream credential and forwarded to the pipeline,
  which validates it against its own key store. The dashboard gate does not
  validate it — a garbage ``k-`` value passes this gate on the chat route
  and is rejected upstream by the pipeline itself (pinned by
  ``tests/unit/test_dashboard_auth.py``). Requests whose header carries the
  dashboard secret (or no header at all) still authenticate through the
  unchanged dashboard path below, on every route including the chat stream
  route.
"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from fastapi import Header, HTTPException

# Same repo-root resolution as pipeline/workspace.py's REPO_ROOT.
REPO_ROOT = Path(__file__).resolve().parent.parent
API_KEY_PATH = REPO_ROOT / ".dashboard_api_key"

# Pipeline-issued upstream credentials are prefixed "k-" (see
# tests/unit/test_chat_stream_endpoint.py's "k-123" pass-through fixture and
# the pipeline's own key generation). The prefix is NOT an authentication
# signal — the caller chooses the header value, so it discriminates nothing
# — it only marks which chat-route requests carry the upstream credential
# the chat handler forwards into ChatService.
PIPELINE_KEY_PREFIX = "k-"

# The one route where the k- pass-through is accepted, matched as an exact
# request path — the same request-URL matching app/dashboard.py's
# middleware uses to scope the gate to "/api/". The chat stream endpoint is
# the only handler that consumes the pass-through credential, so it is the
# only route the exemption covers; every other /api/* route still requires
# the dashboard shared secret.
CHAT_STREAM_PATH = "/api/chat/stream"

logger = logging.getLogger(__name__)


def get_or_create_api_key() -> str:
    """Return the dashboard's shared secret, generating it on first use."""
    if API_KEY_PATH.exists():
        # Tighten an existing file too: O_CREAT's mode applies only when the
        # file is created, so a key written before this control existed (or
        # by a looser umask) would otherwise stay group/world readable.
        os.chmod(API_KEY_PATH, 0o600)
        return API_KEY_PATH.read_text().strip()

    token = secrets.token_urlsafe(32)
    # Created 0600 up front rather than written and then tightened: a plain
    # write_text() would briefly expose the secret at the umask default.
    fd = os.open(API_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(token)
    os.chmod(API_KEY_PATH, 0o600)
    return token


def require_api_key(
    x_pipeline_api_key: str | None = Header(default=None),
    request_path: str | None = None,
) -> None:
    """Authenticate one dashboard API request.

    The gate's job is to decide whether THIS request may proceed, and the
    rule is: the dashboard shared secret is required on every ``/api/*``
    route except the chat stream route, where a ``k-``-prefixed pipeline
    key is passed through for validation by the pipeline itself. Two paths,
    checked in order:

    1. Chat-stream pass-through: ONLY when the request targets the chat
       stream endpoint (``request_path`` equals ``CHAT_STREAM_PATH`` — the
       same request-path matching app/dashboard.py's middleware uses for
       its ``/api/`` scope) AND the header carries a ``k-``-prefixed value,
       the value is accepted as the upstream credential the chat handler
       forwards into ``ChatService`` (which puts it on the internal HTTP
       client's ``X-Pipeline-Api-Key`` header). It is caller-supplied and
       is never compared against the dashboard's own shared secret — the
       two keys protect different surfaces — and it is not validated here
       at all: the pipeline rejects invalid keys when ChatService's
       internal calls reach it. A garbage ``k-`` value therefore passes
       this gate on the chat route by design (pinned by
       ``tests/unit/test_dashboard_auth.py``).
    2. Dashboard shared secret: every other case — any non-chat-stream
       route regardless of header value, or a header value without the
       ``k-`` prefix — is compared against the dashboard's shared secret
       with `secrets.compare_digest` (never `==`) so the response time does
       not leak how much of the key a caller guessed correctly. The 401
       detail is fixed and generic: it must not reveal whether the header
       was missing or merely wrong. In particular, a ``k-`` header alone
       never authenticates a non-chat route: the prefix is caller-chosen,
       so it is never consulted as an authentication decision outside the
       chat-stream exemption.

    ``request_path`` is the request URL path the middleware is gating (the
    middleware passes ``request.url.path``); it exists so the pass-through
    exemption can be scoped to the one route that consumes the credential.
    Callers that omit it get the strict dashboard-secret path, which is the
    safe default.

    Kept as a plain callable (rather than a FastAPI `Depends`-only dependency)
    so `app/dashboard.py`'s middleware can call it directly with a header
    value and the request path read off the request; the
    `Header(default=None)` default still lets it be used as a `Depends`
    elsewhere (e.g. in tests) if needed.
    """
    if (
        request_path == CHAT_STREAM_PATH
        and x_pipeline_api_key
        and x_pipeline_api_key.startswith(PIPELINE_KEY_PREFIX)
    ):
        # Chat-stream pass-through upstream credential: accepted without
        # comparison to the dashboard secret; the chat handler forwards the
        # value into ChatService's internal client, and the pipeline itself
        # validates it (rejecting garbage keys) when its calls arrive.
        return
    expected = get_or_create_api_key()
    presented = x_pipeline_api_key or ""
    # Compare as bytes: compare_digest rejects non-ASCII str, and a header
    # can carry any latin-1 byte — a TypeError here would surface as a 500
    # rather than a denial.
    if not secrets.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        logger.warning(
            "Dashboard API key rejected",
            extra={"header_present": x_pipeline_api_key is not None},
        )
        raise HTTPException(status_code=401, detail="unauthorized")
