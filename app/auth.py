"""Shared-secret API key gate for the dashboard HTTP API.

The dashboard is a single-operator local tool, so authentication is a single
shared secret held in a 0600 file at the repo root rather than a user store.
`require_api_key` is invoked from `app/dashboard.py`'s HTTP middleware for
every request under the "/api/" prefix, so it covers that surface
uniformly — there is no per-route opt-in. The index route ("/") and static
assets are deliberately exempt: "/" is what hands the key to the browser in
the first place, so gating it behind the same header would be an
unreachable chicken-and-egg lock, and static assets carry no secrets.

The rule this module enforces, stated plainly:

* The dashboard shared secret (the 0600 file) is required on every
  ``/api/*`` route. There is no exempt route and no prefix that is ever
  consulted, the chat stream route included.
* An ``X-Pipeline-Api-Key`` header value is compared to that secret. A
  ``k-``-prefixed value is just a wrong key: it is refused with the same
  generic 401 as any other value, before any route handler (and so before
  ``ChatService``) runs. The caller chooses the header value, so no prefix or
  shape check on it can ever be an authentication decision.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import secrets
from functools import lru_cache
from pathlib import Path

from fastapi import Header, HTTPException

# Same repo-root resolution as pipeline/workspace.py's REPO_ROOT.
REPO_ROOT = Path(__file__).resolve().parent.parent
API_KEY_PATH = REPO_ROOT / ".dashboard_api_key"

# Retained name; it no longer grants anything. Nothing consults this prefix:
# a "k-" X-Pipeline-Api-Key value is compared to the dashboard secret like any
# other value.
PIPELINE_KEY_PREFIX = "k-"

# Origin stamping (WAP-1): the chat service stamps ORIGIN_CHAT on its internal
# HTTP client unconditionally — every internal tool call rides that one client,
# so the pipeline API can always tell a model-driven call from a human one.
# Dashboard-origin-gated routes read this header (later stories).
ORIGIN_HEADER = "X-Pipeline-Origin"
ORIGIN_CHAT = "chat"
ORIGIN_UI = "ui"

# Retained name; it no longer grants any exemption. The chat stream route is
# gated by the dashboard shared secret like every other /api/ route.
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

    The dashboard shared secret is required on every ``/api/*`` route. The
    ``X-Pipeline-Api-Key`` value is compared to it with `secrets.compare_digest`
    (never `==`) so the response time does not leak how much of the key a
    caller guessed correctly. The 401 detail is fixed and generic: it must
    not reveal whether the header was missing or merely wrong. A
    ``k-``-prefixed value gets no special treatment; it is compared like any
    other value.

    ``request_path`` is the request URL path the middleware is gating (the
    middleware passes ``request.url.path``). It is accepted for call
    compatibility and no longer selects any exemption: every path is gated
    the same way.

    Kept as a plain callable (rather than a FastAPI `Depends`-only dependency)
    so `app/dashboard.py`'s middleware can call it directly with a header
    value and the request path read off the request; the
    `Header(default=None)` default still lets it be used as a `Depends`
    elsewhere (e.g. in tests) if needed.
    """
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


def refuse_chat_origin(x_pipeline_origin: str | None) -> None:
    """Refuse requests that carry the chat model's client origin.

    The enforced boundary is the chat model's client, which after WAP-1 always
    carries X-Pipeline-Origin: chat. A human key-holder calling the API without
    the header is the trusted single-operator surface, and existing dashboard
    tests/UI send no origin header, so absent must remain allowed on legacy routes.
    """
    if x_pipeline_origin == ORIGIN_CHAT:
        raise HTTPException(status_code=403, detail="origin not permitted")


def require_ui_origin(x_pipeline_origin: str | None) -> None:
    """Refuse anything that is not the UI origin (chat, absent, unknown all refused)."""
    if x_pipeline_origin != ORIGIN_UI:
        raise HTTPException(status_code=403, detail="origin not permitted")


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

ALLOWED_HOSTS_ENV = "PIPELINE_DASHBOARD_ALLOWED_HOSTS"

_HOSTNAME_RE = re.compile(
    r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*"
)


def _is_plain_host(entry: str) -> bool:
    """True when ``entry`` is a bare hostname or IP literal (already lower-cased)."""
    try:
        ipaddress.ip_address(entry)
        return True
    except ValueError:
        return _HOSTNAME_RE.fullmatch(entry) is not None


@lru_cache(maxsize=256)
def _warn_invalid_host_once(entry: str) -> None:
    """Warn about a malformed allowlist entry, once per distinct value.

    ``host_is_allowed`` runs on every request, so without the cache a single
    typo in the environment variable would log on every page load. The entry
    is a hostname, never a secret, so it is safe to log.
    """
    logger.warning(
        "Ignoring invalid dashboard allowed-host entry",
        extra={"env_var": ALLOWED_HOSTS_ENV, "entry": entry},
    )


def _configured_allowed_hosts() -> set[str]:
    """Hostnames from ``PIPELINE_DASHBOARD_ALLOWED_HOSTS``; invalid entries ignored."""
    hosts: set[str] = set()
    for raw in os.environ.get(ALLOWED_HOSTS_ENV, "").split(","):
        entry = raw.strip().lower()
        if not entry:
            continue
        if _is_plain_host(entry):
            hosts.add(entry)
        else:
            _warn_invalid_host_once(entry)
    return hosts


def host_is_allowed(host_header: str | None, server_host: str | None) -> bool:
    """True when the Host header names this server or an allowed alias.

    The index route hands the API key to the browser, so a DNS-rebinding
    page must not be able to read it under an attacker-chosen hostname.
    Compare the header's hostname against the ASGI server host (the
    socket actually bound), the loopback aliases, and any hostnames the
    operator lists in ``PIPELINE_DASHBOARD_ALLOWED_HOSTS`` (for a
    dashboard behind a reverse proxy). That allowlist takes explicit
    names only — an entry with a wildcard, scheme, port or path is
    ignored with a warning — so a wildcard is still never trusted.
    """
    from urllib.parse import urlsplit

    hostname = urlsplit(f"//{host_header or ''}").hostname
    if not hostname:
        return False
    allowed = set(_LOOPBACK_HOSTS)
    if server_host:
        allowed.add(server_host)
    allowed |= _configured_allowed_hosts()
    return hostname in allowed
