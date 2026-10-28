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
"""

from __future__ import annotations

import base64
import hashlib
import hmac
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
import jwt
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _with_query(url: str, params: dict[str, str]) -> str:
    """Append ``params`` to ``url``, preserving any query it already carries."""
    parts = urllib.parse.urlsplit(url)
    query = "&".join(filter(None, [parts.query, urllib.parse.urlencode(params)]))
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, query, parts.fragment)
    )


def _refuse(
    status: int, payload: dict[str, str], reason: str, *, clear_state: bool = True
) -> JSONResponse:
    """A fixed error body plus a log line that names only the action and reason."""
    logger.warning("OIDC login refused", extra={"reason": reason})
    response = JSONResponse(status_code=status, content=payload)
    if clear_state:
        _clear_login_state_cookie(response)
    return response


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


def _set_login_state_cookie(response: Response, state: str) -> None:
    """Bind ``state`` to this browser, one shot, for the attempt's lifetime.

    A browser holds one binding at a time, so a second login in the same browser
    supersedes the first attempt's cookie - and therefore fails its binding
    check instead of consuming it. That is the safe direction: the superseded
    attempt is refused, never spent by whoever presents it.
    """
    response.set_cookie(
        LOGIN_STATE_COOKIE,
        state,
        max_age=STATE_TTL_S,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def _clear_login_state_cookie(response: Response) -> None:
    """Drop the binding cookie, on success and on every failure alike."""
    response.delete_cookie(
        LOGIN_STATE_COOKIE,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def _state_is_from_this_browser(request: Request, state: str) -> bool:
    """Whether ``state`` came back with the cookie that vouched for it.

    Both values are compared in constant time. A non-ASCII cookie or ``state``
    cannot equal the URL-safe value that was minted, and is refused rather than
    handed to ``hmac.compare_digest``, which raises on non-ASCII text.
    """
    bound = request.cookies.get(LOGIN_STATE_COOKIE) or ""
    if not bound or not bound.isascii() or not state.isascii():
        return False
    return hmac.compare_digest(bound, state)


def _begin_login(config: OidcLoginConfig, request: Request) -> Response:
    """Mint ``state``/``nonce``/PKCE verifier, store them, and redirect."""
    if _rate_limited(_client_key(request), _now()):
        return _refuse(429, TOO_MANY_REQUESTS, "rate_limited")
    try:
        endpoints = _discovery(config)
    except OidcLoginError:
        logger.warning("OIDC login unavailable", extra={"reason": "discovery_failed"})
        return JSONResponse(status_code=503, content=AUTHENTICATION_UNAVAILABLE)

    now = _now()
    with _LOCK:
        _prune_pending(now)
        if len(_PENDING) >= MAX_PENDING_LOGINS:
            at_capacity = True
        else:
            at_capacity = False
            state = _new_token()
            nonce = _new_token()
            verifier = _new_code_verifier()
            _PENDING[state] = _PendingLogin(
                nonce=nonce, code_verifier=verifier, expires_at=now + STATE_TTL_S
            )
    if at_capacity:
        # Unauthenticated callers cannot make the pending map unbounded.
        return _refuse(
            429, TOO_MANY_REQUESTS, "pending_logins_at_capacity", clear_state=False
        )

    logger.info("OIDC login started")
    location = _with_query(
        endpoints["authorization_endpoint"],
        {
            "response_type": "code",
            "client_id": config.client_id,
            "redirect_uri": config.redirect_uri,
            "scope": LOGIN_SCOPE,
            "state": state,
            "nonce": nonce,
            "code_challenge": pkce_challenge(verifier),
            "code_challenge_method": "S256",
        },
    )
    response = RedirectResponse(location, status_code=302)
    _set_login_state_cookie(response, state)
    return response


def _finish_login(config: OidcLoginConfig, request: Request) -> Response:
    """Consume ``state``, exchange the code, verify the ID token, start a session."""
    if _rate_limited(_client_key(request), _now()):
        return _refuse(429, TOO_MANY_REQUESTS, "rate_limited")
    params = request.query_params
    state = params.get("state") or ""
    code = params.get("code") or ""
    if not state:
        return _refuse(400, INVALID_REQUEST, "unknown_state")
    if not _state_is_from_this_browser(request, state):
        # Login CSRF: a callback URL minted in another browser must not be
        # spendable here, so the entry is left untouched for its real owner.
        return _refuse(400, INVALID_REQUEST, "state_not_bound_to_browser")
    with _LOCK:
        now = _now()
        # Single use, unconditionally: the entry is removed before the expiry
        # check and before the exchange, so a replay or a failed exchange can
        # never be retried, and a later callback cannot restart the window.
        pending = _PENDING.pop(state, None) if state else None
    if pending is None:
        return _refuse(400, INVALID_REQUEST, "unknown_state")
    if now >= pending.expires_at:
        return _refuse(400, INVALID_REQUEST, "expired_state")
    if params.get("error"):
        # The provider's error code is deliberately not echoed or logged: it is
        # attacker-influenced text and belongs to the provider, not to us.
        return _refuse(401, AUTHENTICATION_FAILED, "provider_error")
    if not code:
        return _refuse(400, INVALID_REQUEST, "missing_code")

    try:
        endpoints = _discovery(config)
    except OidcLoginError:
        logger.warning("OIDC login unavailable", extra={"reason": "discovery_failed"})
        return JSONResponse(status_code=503, content=AUTHENTICATION_UNAVAILABLE)

    try:
        secret = _read_client_secret(config)
    except OidcLoginConfigError:
        logger.error("OIDC login unavailable", extra={"reason": "secret_unavailable"})
        return JSONResponse(status_code=503, content=AUTHENTICATION_UNAVAILABLE)

    try:
        tokens = _post_form(
            endpoints["token_endpoint"],
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": config.redirect_uri,
                "client_id": config.client_id,
                "client_secret": secret,
                "code_verifier": pending.code_verifier,
            },
        )
    except OidcLoginError:
        return _refuse(401, AUTHENTICATION_FAILED, "token_exchange_failed")

    id_token = tokens.get("id_token")
    if not isinstance(id_token, str) or not id_token:
        return _refuse(401, AUTHENTICATION_FAILED, "missing_id_token")

    try:
        settings = oidc.OidcSettings(
            issuer=config.issuer,
            audience=config.client_id,
            jwks_url=endpoints["jwks_uri"],
        )
        principal = oidc.verify_token(id_token, settings)
    except IdentityError:
        return _refuse(401, AUTHENTICATION_FAILED, "id_token_rejected")

    try:
        # Signature, issuer, audience and expiry are already verified above;
        # only the nonce is read here, and only to compare it in constant time.
        claims = jwt.decode(
            id_token,
            options={
                "verify_signature": False,
                "verify_aud": False,
                "verify_exp": False,
                "verify_nbf": False,
                "verify_iat": False,
            },
        )
    except jwt.PyJWTError:
        return _refuse(401, AUTHENTICATION_FAILED, "id_token_rejected")
    token_nonce = claims.get("nonce") if isinstance(claims, dict) else None
    # ``compare_digest`` raises on non-ASCII text, and a provider (or an
    # attacker-supplied token) can put any string in a claim: refuse instead of
    # turning a mismatch into a 500 with a traceback.
    if (
        not isinstance(token_nonce, str)
        or not token_nonce.isascii()
        or not hmac.compare_digest(token_nonce, pending.nonce)
    ):
        return _refuse(401, AUTHENTICATION_FAILED, "nonce_mismatch")

    session_id = _create_session(principal)
    logger.info("OIDC login completed", extra={"principal_subject": principal.subject})
    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=SESSION_ABSOLUTE_TIMEOUT_S,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    _clear_login_state_cookie(response)
    return response


def _end_session(request: Request) -> Response:
    """Forget this browser's session and clear its cookie.

    Logout is local by design: the dashboard's sessions are its own, so ending
    one here is immediate, while revoking the session at the identity provider
    only bites at the next authorization (the session ends at its idle or
    absolute deadline in any case).
    """
    session_id = request.cookies.get(SESSION_COOKIE) or ""
    with _LOCK:
        session = _SESSIONS.pop(session_id, None) if session_id else None
    logger.info(
        "OIDC logout completed",
        extra={"principal_subject": session.principal.subject if session else None},
    )
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    return response


def mount_auth_routes(app: FastAPI, config: OidcLoginConfig) -> None:
    """Register the ``/auth`` routes when login is enabled.

    Disabled login registers nothing at all, so an unconfigured dashboard has
    no ``/auth`` surface to probe.
    """
    if not config.enabled:
        return

    @app.get("/auth/login")
    def login(request: Request) -> Response:  # pragma: no cover - via TestClient
        return _begin_login(config, request)

    @app.get("/auth/callback")
    def callback(request: Request) -> Response:  # pragma: no cover - ditto
        return _finish_login(config, request)

    @app.get("/auth/logout")
    def logout(request: Request) -> Response:  # pragma: no cover - ditto
        return _end_session(request)


__all__ = [
    "ENV_CLIENT_ID",
    "ENV_CLIENT_SECRET_FILE",
    "ENV_ISSUER",
    "ENV_REDIRECT_URI",
    "LOGIN_ENV",
    "LOGIN_SCOPE",
    "LOGIN_STATE_COOKIE",
    "MAX_PENDING_LOGINS",
    "SESSION_ABSOLUTE_TIMEOUT_S",
    "SESSION_COOKIE",
    "SESSION_IDLE_TIMEOUT_S",
    "STATE_TTL_S",
    "OidcLoginConfig",
    "OidcLoginConfigError",
    "OidcLoginError",
    "Session",
    "load_config",
    "load_session",
    "mount_auth_routes",
    "pkce_challenge",
    "reset_state",
]
