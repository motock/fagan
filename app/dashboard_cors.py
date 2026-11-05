"""Opt-in CORS for a dashboard UI hosted on a different origin.

The dashboard's supported cross-origin model is **header credentials only**.
In OIDC mode the browser session is a ``__Host-`` cookie that is same-origin
by design (``app/oidc_login.py``), and state-changing cookie requests must
prove same origin; the bearer/API-key header paths work from any HTTP client.
So the middleware installed here sets ``allow_credentials=False``: a CORS
response can never make a cookie-bearing request succeed from another origin.

CORS is off unless the operator opts in with
``PIPELINE_DASHBOARD_CORS_ORIGINS``: an unset or blank value installs no
middleware at all and adds no headers -- a browser on another origin sees
nothing, exactly as before. When the variable lists exact origins, those
origins may call the API with an API key or bearer token, and the correlation
id header is exposed so their client can log it.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from starlette.middleware.cors import CORSMiddleware

CORS_ENV = "PIPELINE_DASHBOARD_CORS_ORIGINS"

#: Bound on the list so a pasted blob cannot turn into a huge middleware config.
_MAX_ORIGINS = 20

#: Plain http is only for local development; everywhere else it must be https.
_LOCAL_HTTP_HOSTS = frozenset({"localhost", "127.0.0.1"})

_ALLOW_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
_ALLOW_HEADERS = [
    "Authorization",
    "Content-Type",
    "X-Pipeline-API-Key",
    "X-Pipeline-Origin",
    "X-Correlation-ID",
]
_EXPOSE_HEADERS = ["X-Correlation-ID"]
_MAX_AGE = 600

# A DNS name: dot-separated labels of letters, digits and hyphens, never
# starting or ending with a hyphen.
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOST_RE = re.compile(rf"^(?:{_LABEL})(?:\.(?:{_LABEL}))*$")

_INVALID = f"{CORS_ENV} has an invalid entry"


def _is_valid_origin(entry: str) -> bool:
    """True when *entry* is an exact ``scheme://host[:port]`` origin we serve."""
    if "*" in entry:
        return False
    try:
        parts = urlsplit(entry)
        port = parts.port
    except ValueError:
        # e.g. a port above 65535 raises instead of returning a number.
        return False
    if parts.scheme not in ("http", "https"):
        return False
    if parts.username or parts.password:
        return False
    # No path, query or fragment: the value must be an origin, not a URL.
    if parts.path or parts.query or parts.fragment:
        return False
    host = parts.hostname
    if not host or not _HOST_RE.match(host):
        return False
    if port is not None and not 1 <= port <= 65535:
        return False
    # Plain http to a remote UI would send API keys in the clear.
    return parts.scheme != "http" or host in _LOCAL_HTTP_HOSTS


def parse_origins(environ: Mapping[str, str] | None = None) -> list[str]:
    """The configured CORS origins, in first-seen order, without duplicates.

    Returns ``[]`` when the variable is unset or blank. Raises ``ValueError``
    for any entry that is not an exact origin -- the message names the
    variable and never echoes the entry, so a mistyped value (which may be a
    token pasted by mistake) is not copied into logs.
    """
    env = os.environ if environ is None else environ
    raw = env.get(CORS_ENV, "")
    entries = [part.strip() for part in raw.split(",")]
    entries = [entry for entry in entries if entry]
    if len(entries) > _MAX_ORIGINS:
        raise ValueError(_INVALID)

    origins: list[str] = []
    for entry in entries:
        if not _is_valid_origin(entry):
            raise ValueError(_INVALID)
        if entry not in origins:
            origins.append(entry)
    return origins


def install(app, environ: Mapping[str, str] | None = None) -> None:
    """Install ``CORSMiddleware`` on *app* when origins are configured.

    Call this after every ``@app.middleware("http")`` definition: in Starlette
    the last middleware added is the outermost, and a credential-less preflight
    ``OPTIONS`` must be answered before the API-key gate can reject it.
    """
    origins = parse_origins(environ)
    if not origins:
        # Secure default: no middleware, no headers, no change to the stack.
        return
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=_ALLOW_METHODS,
        allow_headers=_ALLOW_HEADERS,
        expose_headers=_EXPOSE_HEADERS,
        max_age=_MAX_AGE,
    )
