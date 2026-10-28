"""OIDC authorization-code + PKCE login for the dashboard.

The dashboard can authenticate a browser instead of relying only on the
shared API key. The flow is the standard authorization code exchange with
PKCE, and each part below carries one invariant:

* configuration is all-or-nothing - the three ``PIPELINE_OIDC_*`` variables
  either enable the flow together or leave it disabled, and a partial set
  refuses to start rather than silently falling back to the API-only mode.
  The client secret is read from a file, never from the environment, so it
  cannot leak through ``ps`` or a crash dump;
* the login leg keeps ``state``, ``nonce`` and the PKCE ``code_verifier``
  server-side, keyed by ``state`` with a short TTL, so nothing that proves the
  flow is in flight ever reaches the browser;
* ``state`` is bound to the browser that started the attempt: ``/auth/login``
  also sets a one-shot ``__Host-`` cookie carrying the same value, and the
  callback requires it to match before it will spend the entry. Without it a
  callback URL lifted from the attacker's own browser would authenticate the
  victim's browser as the attacker's principal (login CSRF: every later action
  attributed to, and audited under, the wrong identity);
* the callback consumes a ``state`` entry exactly once, atomically and before
  the code exchange, so neither a replay nor a failed exchange can be retried
  into a second session;
* the ID token is verified by the existing ``app.oidc`` rules (fixed algorithm
  allowlist, required ``exp``/``iss``/``aud``/``sub``, configured issuer and
  audience) plus the ``nonce`` bound to this attempt, compared in constant
  time;
* a session holds only the principal and its deadlines; the cookie carries a
  256-bit random identifier and is ``__Host-`` scoped, so it is unreadable to
  script, unusable cross-site, and not shareable with a sibling host or a
  plain-text scheme. A session ends at ``SESSION_IDLE_TIMEOUT_S`` of inactivity,
  at ``SESSION_ABSOLUTE_TIMEOUT_S`` regardless of activity, or at
  ``/auth/logout``; revoking it at the identity provider does not reach a live
  session, so the local deadlines (and re-authentication) are what bound it;
* the session cookie is a credential the dashboard accepts in place of a bearer
  token, which makes it *ambient*: the browser sends it on a request any page
  can trigger. So a state-changing request authenticated by the cookie must also
  prove it came from this site (``Sec-Fetch-Site: same-origin``, or an
  ``Origin`` equal to the configured redirect URI's origin), and one that cannot
  is refused before it reaches a route. A bearer token is exempt from that proof;
* no code, verifier, token or secret is logged or echoed in an error body -
  every failure returns a fixed payload, and every log line names the action
  and (at most) the principal subject;
* the unauthenticated routes are bounded rather than merely cheap: pending
  attempts and tracked clients are capped (``MAX_PENDING_LOGINS``,
  ``MAX_TRACKED_CLIENTS``), a client's request rate is limited
  (``MAX_AUTH_REQUESTS`` per ``AUTH_RATE_WINDOW_S``), and an identity provider
  that has just failed discovery is not re-probed until
  ``DISCOVERY_FAILURE_TTL_S`` has passed, so an unauthenticated caller cannot
  turn the login page into an amplifier against the provider or the threadpool.

The request handlers for ``/auth/login``, ``/auth/callback`` and ``/auth/logout``
live in ``app.oidc_login_routes``, which reads the configuration, discovery,
outbound HTTP, pending-login and session machinery kept here. That module imports
this one, so ``mount_auth_routes`` below is a shim that imports it lazily.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import stat
import threading
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import FastAPI, Request

from . import oidc
from .identity import IdentityError, Principal

logger = logging.getLogger("pipeline.auth")

ENV_CLIENT_ID = "PIPELINE_OIDC_CLIENT_ID"
ENV_CLIENT_SECRET_FILE = "PIPELINE_OIDC_CLIENT_SECRET_FILE"
ENV_REDIRECT_URI = "PIPELINE_OIDC_REDIRECT_URI"
ENV_ISSUER = "PIPELINE_OIDC_ISSUER"

#: The three variables that switch the flow on: all of them or none.
LOGIN_ENV = (ENV_CLIENT_ID, ENV_CLIENT_SECRET_FILE, ENV_REDIRECT_URI)

#: ``__Host-`` is not decoration: the prefix makes the browser refuse a cookie
#: with a ``Domain`` attribute and require ``Path=/`` over HTTPS, so a sibling
#: subdomain cannot overwrite the session of this one.
SESSION_COOKIE = "__Host-pipeline_session"

#: Methods with nothing to forge, so the same-origin proof is not asked of them.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: One-shot cookie binding ``state`` to the browser that asked for it. Same
#: ``__Host-`` rules, but ``SameSite=Lax``: the identity provider sends the
#: browser back with a cross-site top-level GET, which a ``Strict`` cookie would
#: not survive. It carries the ``state`` value, which already travels in the URL
#: - never the ``nonce`` or the PKCE verifier, which stay server-side.
LOGIN_STATE_COOKIE = "__Host-pipeline_login_state"

#: Lifetime of one authorization attempt: how long the browser has to come
#: back from the identity provider before ``state`` stops being accepted.
STATE_TTL_S = 600

#: Re-authentication thresholds: an idle session, and a hard deadline that no
#: amount of activity extends.
SESSION_IDLE_TIMEOUT_S = 1800
SESSION_ABSOLUTE_TIMEOUT_S = 28800

#: Scope requested from the identity provider; ``openid`` is what makes the
#: response an ID token rather than a bare access token.
LOGIN_SCOPE = "openid profile email"

DISCOVERY_SUFFIX = "/.well-known/openid-configuration"
DISCOVERY_TTL_S = 300

#: A failed discovery is remembered this long, so an unreachable identity
#: provider is not re-probed by every unauthenticated ``/auth/login`` (each
#: probe costs a DNS resolve and up to ``HTTP_TIMEOUT_S`` of a threadpool
#: worker, which is the whole dashboard's capacity).
DISCOVERY_FAILURE_TTL_S = 30

#: In-flight login attempts held at once. ``/auth/login`` is unauthenticated, so
#: the pending map is bounded: past the bound the flow answers 429 rather than
#: growing an attacker-controlled table.
MAX_PENDING_LOGINS = 1024

#: Requests one client may make to the unauthenticated ``/auth`` routes inside
#: ``AUTH_RATE_WINDOW_S``. Both routes are cheap on a hit; the limit keeps them
#: cheap on a miss too.
AUTH_RATE_WINDOW_S = 60.0
MAX_AUTH_REQUESTS = 60

#: Client keys tracked by the rate limiter at once, so the limiter's own table
#: is bounded independently of ``MAX_PENDING_LOGINS``: the key is the peer
#: address (see ``_client_key``), and stale keys are pruned once this bound is
#: reached.
MAX_TRACKED_CLIENTS = 1024

HTTP_TIMEOUT_S = 10.0

#: Upper bound on a discovery or token response body, enforced while reading.
MAX_DOCUMENT_BYTES = 256 * 1024

#: Fixed error payloads. Never assembled from an exception, a provider response
#: or a request parameter: a body that varies with the failure is an oracle.
AUTHENTICATION_FAILED = {"error": "authentication_failed"}
INVALID_REQUEST = {"error": "invalid_request"}
AUTHENTICATION_UNAVAILABLE = {"error": "authentication_unavailable"}
TOO_MANY_REQUESTS = {"error": "too_many_requests"}


def _now() -> float:
    """The module's only clock read: injected by tests to drive expiry."""
    return time.time()


class OidcLoginError(RuntimeError):
    """An identity-provider interaction failed. Never carries a value."""


class OidcLoginConfigError(RuntimeError):
    """Login is misconfigured. Names the offending variable, never its value."""


def _new_token() -> str:
    """A 256-bit URL-safe random handle for ``state``, ``nonce`` or a session."""
    return secrets.token_urlsafe(32)


def _new_code_verifier() -> str:
    """A PKCE ``code_verifier``: 43 characters from 256 bits of randomness."""
    return secrets.token_urlsafe(32)


def pkce_challenge(verifier: str) -> str:
    """S256 transform of ``verifier``: base64url(sha256(verifier)), unpadded.

    The verifier is hashed, not the challenge, and the padding is stripped:
    both are part of the RFC 7636 definition, and an identity provider that
    computes the value itself will reject anything else.
    """
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class OidcLoginConfig:
    """The resolved login configuration. Disabled unless all three are set."""

    enabled: bool = False
    client_id: str = ""
    client_secret_file: str = field(default="", repr=False)
    redirect_uri: str = ""
    issuer: str = ""


@dataclass
class _PendingLogin:
    """One in-flight authorization attempt, keyed server-side by ``state``."""

    nonce: str = field(repr=False)
    code_verifier: str = field(repr=False)
    expires_at: float = 0.0
    #: The validated same-origin path to return to after login. Kept last so
    #: existing positional construction of the record is unaffected.
    next: str = "/"


@dataclass
class Session:
    """An authenticated browser session: its principal and its deadlines."""

    session_id: str
    principal: Principal
    created_at: float
    last_seen: float


_PENDING: dict[str, _PendingLogin] = {}
_SESSIONS: dict[str, Session] = {}
_DISCOVERY: dict[str, tuple[float, dict[str, str]]] = {}
#: Issuer -> monotonic instant before which discovery is not retried.
_DISCOVERY_FAILURES: dict[str, float] = {}
#: Client key -> instants of the recent unauthenticated requests.
_RATE: dict[str, list[float]] = {}
_LOCK = threading.Lock()


def reset_state() -> None:
    """Drop every pending login, session and cached document.

    Module-level state outlives a single test, and a process that reloads its
    configuration must not keep a session minted under the old one.
    """
    with _LOCK:
        _PENDING.clear()
        _SESSIONS.clear()
        _DISCOVERY.clear()
        _DISCOVERY_FAILURES.clear()
        _RATE.clear()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _require_https(value: str, name: str) -> str:
    """Return ``value`` when it is an absolute https URL, else refuse to start."""
    parts = urllib.parse.urlsplit(value)
    if parts.scheme != "https" or not parts.netloc or parts.fragment:
        raise OidcLoginConfigError(
            f"OIDC login misconfigured: {name} must be an absolute https URL"
        )
    return value


def _secret_file_problem(path: str) -> str | None:
    """Why ``path`` is unusable as a secret file, or None when it is fine.

    ``lstat`` rather than ``stat``: a symlink is refused outright instead of
    being followed to whatever it points at, and the file must belong to the
    process that reads it, so a secret planted by another account is not
    picked up.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return "is not readable"
    if stat.S_ISLNK(info.st_mode):
        return "must not be a symbolic link"
    if not stat.S_ISREG(info.st_mode):
        return "is not a regular file"
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        return "must be owned by the user running the dashboard"
    if info.st_mode & 0o077:
        return "must not be readable by group or others (chmod 600)"
    if info.st_size == 0:
        return "is empty"
    return None


def load_config(env: Mapping[str, str] | None = None) -> OidcLoginConfig:
    """Resolve the login configuration from ``env`` (default ``os.environ``).

    Three variables switch the flow on. None of them leaves the dashboard in
    today's API-only mode. A partial set is refused here, at start-up, naming
    only the variables - never a value - so a typo cannot silently disable
    login, nor half-configure it.
    """
    source = os.environ if env is None else env
    values = {name: (source.get(name) or "").strip() for name in LOGIN_ENV}
    if not any(values.values()):
        return OidcLoginConfig()

    missing = [name for name in LOGIN_ENV if not values[name]]
    if missing:
        raise OidcLoginConfigError(
            "OIDC login misconfigured: set all of "
            + ", ".join(LOGIN_ENV)
            + "; missing "
            + ", ".join(missing)
        )

    issuer = (source.get(ENV_ISSUER) or "").strip()
    if not issuer:
        raise OidcLoginConfigError(
            f"OIDC login misconfigured: {ENV_ISSUER} is required for discovery"
        )
    _require_https(issuer, ENV_ISSUER)
    _require_https(values[ENV_REDIRECT_URI], ENV_REDIRECT_URI)

    problem = _secret_file_problem(values[ENV_CLIENT_SECRET_FILE])
    if problem is not None:
        raise OidcLoginConfigError(
            f"OIDC login misconfigured: {ENV_CLIENT_SECRET_FILE} {problem}"
        )

    return OidcLoginConfig(
        enabled=True,
        client_id=values[ENV_CLIENT_ID],
        client_secret_file=values[ENV_CLIENT_SECRET_FILE],
        redirect_uri=values[ENV_REDIRECT_URI],
        issuer=issuer.rstrip("/"),
    )


def _read_client_secret(config: OidcLoginConfig) -> str:
    """Read the client secret from its file. The value never leaves this call."""
    try:
        with open(config.client_secret_file, encoding="utf-8") as handle:
            secret = handle.read().strip()
    except OSError:
        raise OidcLoginConfigError(
            f"OIDC login misconfigured: {ENV_CLIENT_SECRET_FILE} is not readable"
        ) from None
    if not secret:
        raise OidcLoginConfigError(
            f"OIDC login misconfigured: {ENV_CLIENT_SECRET_FILE} is empty"
        )
    return secret


# ---------------------------------------------------------------------------
# Outbound HTTP: address-checked, pinned, size- and time-bounded
# ---------------------------------------------------------------------------


def _read_document(response: httpx.Response) -> Any:
    """Read a JSON document from a streaming response, capped while reading."""
    body = bytearray()
    deadline = time.monotonic() + HTTP_TIMEOUT_S
    for chunk in response.iter_bytes():
        if time.monotonic() > deadline:
            raise OidcLoginError("identity provider response timed out")
        body.extend(chunk)
        if len(body) > MAX_DOCUMENT_BYTES:
            raise OidcLoginError("identity provider response too large")
    try:
        return json.loads(bytes(body))
    except (ValueError, RecursionError):
        # ``json.loads`` raises RecursionError, not ValueError, on a deeply
        # nested document; uncaught it would surface as a 500.
        raise OidcLoginError("identity provider response malformed") from None


def _client_for(url: str, address: str) -> httpx.Client:
    """A client that connects to the already-validated ``address``."""
    hostname = urllib.parse.urlsplit(url).hostname or ""
    return httpx.Client(
        transport=oidc._PinnedTransport(address, hostname),
        timeout=httpx.Timeout(HTTP_TIMEOUT_S),
        follow_redirects=False,
    )


def _fetch_json(url: str, *, allow_private: bool = False) -> dict[str, Any]:
    """GET and parse one JSON document over the address-checked transport.

    The host is resolved and judged by the same rule as the JWKS fetch, then
    pinned for the connection, so a discovery document or token endpoint on a
    private address is refused instead of probed.
    """
    address = oidc._resolve(url, allow_private=allow_private)
    if address is None:
        raise OidcLoginError("identity provider address refused")
    try:
        with (
            _client_for(url, address) as client,
            client.stream(
                "GET", url, headers={"accept": "application/json"}
            ) as response,
        ):
            if response.status_code != 200:
                raise OidcLoginError("identity provider document unavailable")
            document = _read_document(response)
    except (httpx.HTTPError, httpx.InvalidURL, UnicodeError):
        raise OidcLoginError("identity provider request failed") from None
    if not isinstance(document, dict):
        raise OidcLoginError("identity provider document malformed")
    return document


def _post_form(
    url: str, form: dict[str, str], *, allow_private: bool = False
) -> dict[str, Any]:
    """POST ``form`` to the token endpoint over the address-checked transport."""
    address = oidc._resolve(url, allow_private=allow_private)
    if address is None:
        raise OidcLoginError("identity provider address refused")
    try:
        with _client_for(url, address) as client:
            response = client.post(
                url,
                data=form,
                headers={
                    "accept": "application/json",
                    "content-type": "application/x-www-form-urlencoded",
                },
            )
            if response.status_code != 200:
                raise OidcLoginError("identity provider token endpoint failed")
            document = _read_document(response)
    except (httpx.HTTPError, httpx.InvalidURL, UnicodeError):
        raise OidcLoginError("identity provider request failed") from None
    if not isinstance(document, dict):
        raise OidcLoginError("identity provider token response malformed")
    return document


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _discovery(config: OidcLoginConfig) -> dict[str, str]:
    """Return the issuer's endpoints, cached for ``DISCOVERY_TTL_S``.

    The document is trusted only when it names the configured issuer, so a
    misdirected or hijacked document cannot point the flow at another party's
    authorization or token endpoint.
    """
    url = config.issuer + DISCOVERY_SUFFIX
    now = _now()
    with _LOCK:
        cached = _DISCOVERY.get(config.issuer)
        cooling = _DISCOVERY_FAILURES.get(config.issuer)
    if cached is not None and now - cached[0] < DISCOVERY_TTL_S:
        return cached[1]
    if cooling is not None and now < cooling:
        # Recently unreachable: refuse without spending a DNS resolve and a
        # threadpool worker on the same doomed request again.
        raise OidcLoginError("discovery recently failed")

    try:
        document = _fetch_json(url)
        if document.get("issuer") != config.issuer:
            raise OidcLoginError("discovery issuer mismatch")
        endpoints: dict[str, str] = {}
        for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            value = document.get(name)
            if not isinstance(value, str) or not value.startswith("https://"):
                raise OidcLoginError("discovery document incomplete")
            endpoints[name] = value
        # Optional: not every provider supports RP-initiated logout, and a value
        # that is not an https URL is ignored rather than followed.
        end_session = document.get("end_session_endpoint")
        if isinstance(end_session, str) and end_session.startswith("https://"):
            endpoints["end_session_endpoint"] = end_session
        elif end_session is not None:
            logger.warning(
                "OIDC discovery end_session_endpoint ignored", extra={"reason": "not_https"}
            )
    except OidcLoginError:
        with _LOCK:
            # Stamp the cooldown when the failure is known, not when the probe
            # started: a probe that burned ``HTTP_TIMEOUT_S`` must still leave
            # a full cooldown behind it.
            _DISCOVERY_FAILURES[config.issuer] = _now() + DISCOVERY_FAILURE_TTL_S
        raise
    with _LOCK:
        _DISCOVERY[config.issuer] = (now, endpoints)
        _DISCOVERY_FAILURES.pop(config.issuer, None)
    return endpoints


# ---------------------------------------------------------------------------
# Pending authorizations and sessions
# ---------------------------------------------------------------------------


def _prune_pending(now: float) -> None:
    """Drop attempts whose window has closed. Caller holds ``_LOCK``."""
    for stale in [key for key, item in _PENDING.items() if now >= item.expires_at]:
        _PENDING.pop(stale, None)


def _prune_sessions(now: float) -> None:
    """Drop sessions past either deadline. Caller holds ``_LOCK``."""
    for stale in [
        key
        for key, item in _SESSIONS.items()
        if now - item.created_at >= SESSION_ABSOLUTE_TIMEOUT_S
        or now - item.last_seen >= SESSION_IDLE_TIMEOUT_S
    ]:
        _SESSIONS.pop(stale, None)


def _create_session(principal: Principal) -> str:
    """Store a session for ``principal`` and return its identifier."""
    now = _now()
    session_id = _new_token()
    with _LOCK:
        _prune_sessions(now)
        _SESSIONS[session_id] = Session(
            session_id=session_id,
            principal=principal,
            created_at=now,
            last_seen=now,
        )
    return session_id


def load_session(session_id: str | None) -> Session | None:
    """Return the live session for ``session_id``, or None.

    Both deadlines are checked against the stored timestamps before anything is
    written, and the only write is ``last_seen = max(last_seen, now)``. Writing
    ``now`` unconditionally would corrupt the idle window after a backwards
    clock step, and writing before checking would reset the deadline that is
    about to expire.

    This is the only way a session becomes an identity: a caller turns the
    returned principal into ``request.state.principal`` and still goes through
    whatever gate the route already has. A session must never be accepted in
    place of the API-key check, so the two remain separate decisions.
    """
    if not isinstance(session_id, str) or not session_id:
        return None
    with _LOCK:
        session = _SESSIONS.get(session_id)
        if session is None:
            return None
        now = _now()
        if now - session.created_at >= SESSION_ABSOLUTE_TIMEOUT_S:
            _SESSIONS.pop(session_id, None)
            return None
        if now - session.last_seen >= SESSION_IDLE_TIMEOUT_S:
            _SESSIONS.pop(session_id, None)
            return None
        session.last_seen = max(session.last_seen, now)
        return session


def session_principal(request: Request) -> Principal | None:
    """Return the principal of this request's session cookie, or None.

    The cookie is only a *lookup key*: the deadlines and the principal behind it
    stay server-side, so an expired or ended session resolves to None rather
    than to a stale identity. This never authenticates a request on its own.
    """
    session = load_session(request.cookies.get(SESSION_COOKIE))
    return session.principal if session is not None else None


def allows_cookie_write(request: Request, config: OidcLoginConfig) -> bool:
    """Whether a state-changing request carrying the session cookie may proceed.

    A cookie is ambient: the browser attaches it to a request any page can
    trigger, so authentication by cookie alone says nothing about who caused the
    request. It must also prove same-origin, via ``Sec-Fetch-Site`` or an
    ``Origin`` naming this site; ``same-site`` is refused, and read-only methods
    are not asked. Deny by default: no proof is False.
    """
    if request.method in SAFE_METHODS:
        return True
    if not config.enabled:
        return False
    if request.headers.get("sec-fetch-site", "").strip().lower() == "same-origin":
        return True
    expected = urllib.parse.urlsplit(config.redirect_uri)
    origin = urllib.parse.urlsplit(request.headers.get("origin", ""))
    if not expected.netloc or not origin.netloc:
        return False
    return (origin.scheme.lower(), origin.netloc.lower()) == (
        expected.scheme.lower(),
        expected.netloc.lower(),
    )


class SessionRefused(Exception):
    """An ``/api/`` request whose credential did not authenticate.

    Carries the refusal the middleware returns; ``detail`` is fixed text.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def authenticate_api_request(
    request: Request, identity: oidc.OidcProvider, config: OidcLoginConfig
) -> Principal:
    """Return the principal for one ``/api/`` request, or raise ``SessionRefused``.

    Order matters. An explicitly presented bearer token is authoritative: when
    it is rejected the request is refused, never silently downgraded to the
    browser's ambient session cookie, which would attribute the action to the
    wrong principal. Only with no bearer token does the session cookie
    authenticate, and then the request must also pass the same-origin proof a
    cookie-driven write needs. A session counts only while login is configured:
    otherwise there is no origin to compare against and no flow that could have
    minted the cookie, so an unverifiable cookie is refused.
    """
    authorization = request.headers.get("authorization") or ""
    scheme, _, token = authorization.partition(" ")
    credential = token.strip() if scheme.lower() == "bearer" else None
    if credential:
        try:
            return identity.authenticate(credential)
        except IdentityError:
            raise SessionRefused(401, "Unauthorized") from None
    principal = session_principal(request) if config.enabled else None
    if principal is None:
        raise SessionRefused(401, "Unauthorized")
    if not allows_cookie_write(request, config):
        logger.warning(
            "refused cross-site write",
            extra={"principal_subject": principal.subject},
        )
        raise SessionRefused(403, "Forbidden")
    return principal


def _client_key(request: Request) -> str:
    """Who to count a request against: the peer address, nothing else.

    The peer address is the proxy in front of the dashboard, not the end user,
    unless the deployment preserves client addresses - so ``MAX_AUTH_REQUESTS``
    can behave as one global cap rather than a per-user one. That is the safe
    direction (it throttles rather than over-admits), and the pending map stays
    bounded either way: an attacker who does fill ``MAX_PENDING_LOGINS``
    entries from one or two addresses only makes ``/auth/login`` answer 429 for
    a while, never unboundedly large.
    """
    return request.client.host if request.client is not None else "unknown"


def _rate_limited(key: str, now: float) -> bool:
    """True when ``key`` has already spent its allowance for this window."""
    with _LOCK:
        stamps = [
            stamp for stamp in _RATE.get(key, ()) if now - stamp < AUTH_RATE_WINDOW_S
        ]
        limited = len(stamps) >= MAX_AUTH_REQUESTS
        if not limited:
            stamps.append(now)
        _RATE[key] = stamps
        for stale in [
            other
            for other, items in _RATE.items()
            if not any(now - stamp < AUTH_RATE_WINDOW_S for stamp in items)
        ]:
            _RATE.pop(stale, None)
        while len(_RATE) > MAX_TRACKED_CLIENTS:
            _RATE.pop(next(iter(_RATE)))
    return limited


def mount_auth_routes(app: FastAPI, config: OidcLoginConfig) -> None:
    """Mount ``/auth/login``, ``/auth/callback`` and ``/auth/logout``; see ``app.oidc_login_routes``."""
    from app import oidc_login_routes  # deferred: that module imports this one

    oidc_login_routes.mount_auth_routes(app, config)


__all__ = [
    "ENV_CLIENT_ID",
    "ENV_CLIENT_SECRET_FILE",
    "ENV_ISSUER",
    "ENV_REDIRECT_URI",
    "LOGIN_ENV",
    "LOGIN_SCOPE",
    "LOGIN_STATE_COOKIE",
    "MAX_PENDING_LOGINS",
    "SAFE_METHODS",
    "SESSION_ABSOLUTE_TIMEOUT_S",
    "SESSION_COOKIE",
    "SESSION_IDLE_TIMEOUT_S",
    "STATE_TTL_S",
    "OidcLoginConfig",
    "OidcLoginConfigError",
    "OidcLoginError",
    "Session",
    "SessionRefused",
    "allows_cookie_write",
    "authenticate_api_request",
    "load_config",
    "load_session",
    "mount_auth_routes",
    "pkce_challenge",
    "reset_state",
    "session_principal",
]
