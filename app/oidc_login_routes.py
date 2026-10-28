"""The ``/auth`` route handlers for the dashboard's OIDC login flow.

The flow's configuration, discovery, outbound HTTP, session store and the
cookie-authenticated request guard live in ``app.oidc_login``; this module holds
the request handlers that drive them and is imported lazily by
``app.oidc_login.mount_auth_routes``.

Every name kept in ``app.oidc_login`` is reached as ``core.<name>`` at call time
rather than imported directly, so tests (and operators) can retune a limit or a
clock by patching ``app.oidc_login`` and have the routes observe it.
"""

import hmac
import urllib.parse

import jwt
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from app import oidc
from app import oidc_login as core
from app.identity import IdentityError

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
    core.logger.warning("OIDC login refused", extra={"reason": reason})
    response = JSONResponse(status_code=status, content=payload)
    if clear_state:
        _clear_login_state_cookie(response)
    return response


def _set_login_state_cookie(response: Response, state: str) -> None:
    """Bind ``state`` to this browser, one shot, for the attempt's lifetime.

    A browser holds one binding at a time, so a second login in the same browser
    supersedes the first attempt's cookie - and therefore fails its binding
    check instead of consuming it. That is the safe direction: the superseded
    attempt is refused, never spent by whoever presents it.
    """
    response.set_cookie(
        core.LOGIN_STATE_COOKIE,
        state,
        max_age=core.STATE_TTL_S,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def _clear_login_state_cookie(response: Response) -> None:
    """Drop the binding cookie, on success and on every failure alike."""
    response.delete_cookie(
        core.LOGIN_STATE_COOKIE,
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
    bound = request.cookies.get(core.LOGIN_STATE_COOKIE) or ""
    if not bound or not bound.isascii() or not state.isascii():
        return False
    return hmac.compare_digest(bound, state)


def _begin_login(config: core.OidcLoginConfig, request: Request) -> Response:
    """Mint ``state``/``nonce``/PKCE verifier, store them, and redirect."""
    if core._rate_limited(core._client_key(request), core._now()):
        return _refuse(429, core.TOO_MANY_REQUESTS, "rate_limited")
    try:
        endpoints = core._discovery(config)
    except core.OidcLoginError:
        core.logger.warning("OIDC login unavailable", extra={"reason": "discovery_failed"})
        return JSONResponse(status_code=503, content=core.AUTHENTICATION_UNAVAILABLE)

    now = core._now()
    with core._LOCK:
        core._prune_pending(now)
        if len(core._PENDING) >= core.MAX_PENDING_LOGINS:
            at_capacity = True
        else:
            at_capacity = False
            state = core._new_token()
            nonce = core._new_token()
            verifier = core._new_code_verifier()
            core._PENDING[state] = core._PendingLogin(
                nonce=nonce, code_verifier=verifier, expires_at=now + core.STATE_TTL_S
            )
    if at_capacity:
        # Unauthenticated callers cannot make the pending map unbounded.
        return _refuse(
            429, core.TOO_MANY_REQUESTS, "pending_logins_at_capacity", clear_state=False
        )

    core.logger.info("OIDC login started")
    location = _with_query(
        endpoints["authorization_endpoint"],
        {
            "response_type": "code",
            "client_id": config.client_id,
            "redirect_uri": config.redirect_uri,
            "scope": core.LOGIN_SCOPE,
            "state": state,
            "nonce": nonce,
            "code_challenge": core.pkce_challenge(verifier),
            "code_challenge_method": "S256",
        },
    )
    response = RedirectResponse(location, status_code=302)
    _set_login_state_cookie(response, state)
    return response


def _finish_login(config: core.OidcLoginConfig, request: Request) -> Response:
    """Consume ``state``, exchange the code, verify the ID token, start a session."""
    if core._rate_limited(core._client_key(request), core._now()):
        return _refuse(429, core.TOO_MANY_REQUESTS, "rate_limited")
    params = request.query_params
    state = params.get("state") or ""
    code = params.get("code") or ""
    if not state:
        return _refuse(400, core.INVALID_REQUEST, "unknown_state")
    if not _state_is_from_this_browser(request, state):
        # Login CSRF: a callback URL minted in another browser must not be
        # spendable here, so the entry is left untouched for its real owner.
        return _refuse(400, core.INVALID_REQUEST, "state_not_bound_to_browser")
    with core._LOCK:
        now = core._now()
        # Single use, unconditionally: the entry is removed before the expiry
        # check and before the exchange, so a replay or a failed exchange can
        # never be retried, and a later callback cannot restart the window.
        pending = core._PENDING.pop(state, None) if state else None
    if pending is None:
        return _refuse(400, core.INVALID_REQUEST, "unknown_state")
    if now >= pending.expires_at:
        return _refuse(400, core.INVALID_REQUEST, "expired_state")
    if params.get("error"):
        # The provider's error code is deliberately not echoed or logged: it is
        # attacker-influenced text and belongs to the provider, not to us.
        return _refuse(401, core.AUTHENTICATION_FAILED, "provider_error")
    if not code:
        return _refuse(400, core.INVALID_REQUEST, "missing_code")

    try:
        endpoints = core._discovery(config)
    except core.OidcLoginError:
        core.logger.warning("OIDC login unavailable", extra={"reason": "discovery_failed"})
        return JSONResponse(status_code=503, content=core.AUTHENTICATION_UNAVAILABLE)

    try:
        secret = core._read_client_secret(config)
    except core.OidcLoginConfigError:
        core.logger.error("OIDC login unavailable", extra={"reason": "secret_unavailable"})
        return JSONResponse(status_code=503, content=core.AUTHENTICATION_UNAVAILABLE)

    try:
        tokens = core._post_form(
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
    except core.OidcLoginError:
        return _refuse(401, core.AUTHENTICATION_FAILED, "token_exchange_failed")

    id_token = tokens.get("id_token")
    if not isinstance(id_token, str) or not id_token:
        return _refuse(401, core.AUTHENTICATION_FAILED, "missing_id_token")

    try:
        settings = oidc.OidcSettings(
            issuer=config.issuer,
            audience=config.client_id,
            jwks_url=endpoints["jwks_uri"],
        )
        principal = oidc.verify_token(id_token, settings)
    except IdentityError:
        return _refuse(401, core.AUTHENTICATION_FAILED, "id_token_rejected")

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
        return _refuse(401, core.AUTHENTICATION_FAILED, "id_token_rejected")
    token_nonce = claims.get("nonce") if isinstance(claims, dict) else None
    # ``compare_digest`` raises on non-ASCII text, and a provider (or an
    # attacker-supplied token) can put any string in a claim: refuse instead of
    # turning a mismatch into a 500 with a traceback.
    if (
        not isinstance(token_nonce, str)
        or not token_nonce.isascii()
        or not hmac.compare_digest(token_nonce, pending.nonce)
    ):
        return _refuse(401, core.AUTHENTICATION_FAILED, "nonce_mismatch")

    session_id = core._create_session(principal)
    core.logger.info("OIDC login completed", extra={"principal_subject": principal.subject})
    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        core.SESSION_COOKIE,
        session_id,
        max_age=core.SESSION_ABSOLUTE_TIMEOUT_S,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    _clear_login_state_cookie(response)
    return response


def _end_session_url(config: core.OidcLoginConfig) -> str | None:
    """The provider's RP-initiated logout URL, or None when it has none.

    Discovery is best-effort: logout is local and must succeed even when the
    provider is unreachable.
    """
    try:
        return core._discovery(config).get("end_session_endpoint")
    except core.OidcLoginError:
        core.logger.warning(
            "OIDC logout metadata unavailable", extra={"reason": "discovery_failed"}
        )
        return None


def _end_session(
    config: core.OidcLoginConfig, request: Request, *, as_json: bool
) -> Response:
    """End this browser's session, clear its cookie, and answer the caller.

    Logout is local by design: the dashboard's sessions are its own, so ending
    one here is immediate, while revoking it at the identity provider only bites
    at the next authorization. The deletion is what makes the call effective:
    replaying the cookie afterwards resolves to no session. It sits outside the
    middleware's ``/api/`` same-origin check, but a cross-site request cannot
    reach it either - ``SameSite=Strict`` withholds the cookie. ``as_json`` adds
    the provider's end-session URL for the UI to follow.
    """
    session_id = request.cookies.get(core.SESSION_COOKIE) or ""
    with core._LOCK:
        session = core._SESSIONS.pop(session_id, None) if session_id else None
    core.logger.info(
        "OIDC logout completed",
        extra={"principal_subject": session.principal.subject if session else None},
    )
    response: Response = (
        JSONResponse({"end_session_url": _end_session_url(config)})
        if as_json
        else RedirectResponse("/", status_code=302)
    )
    response.delete_cookie(
        core.SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict"
    )
    return response


def mount_auth_routes(app: FastAPI, config: core.OidcLoginConfig) -> None:
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
        return _end_session(config, request, as_json=False)

    @app.post("/auth/logout")
    def logout_post(request: Request) -> Response:  # pragma: no cover - ditto
        return _end_session(config, request, as_json=True)
