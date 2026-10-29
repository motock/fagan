"""Tests for the dashboard's OIDC authorization-code + PKCE login flow.

The identity provider is mocked at the HTTP boundary: ``app.oidc_login``
performs every outbound request through ``_fetch_json`` (discovery) and
``_post_form`` (token endpoint), which these tests replace. The ID token is a
real RS256 JWT signed by a locally generated key, and ``app.oidc._fetch_jwks``
is replaced by that key's JWKS, so signature, issuer, audience and expiry are
checked for real. Nothing here touches the network.

Time is read through ``app.oidc_login._now`` only, so the expiry, single-use
and session-lifetime tests drive a fake clock instead of sleeping.
"""

import json
import logging
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import oidc, oidc_login

ISSUER = "https://issuer.example"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
AUTHORIZATION_ENDPOINT = f"{ISSUER}/authorize"
TOKEN_ENDPOINT = f"{ISSUER}/token"
JWKS_URI = f"{ISSUER}/jwks"
CLIENT_ID = "pipeline-dashboard"
REDIRECT_URI = "https://dashboard.example/auth/callback"
CLIENT_SECRET = "s3cr3t-client-secret-value"
KID = "k1"


class _Clock:
    """The single time seam the module exposes (``oidc_login._now``)."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _clean_state():
    """Module-level state outlives a test: clear it before and after each one."""
    oidc_login.reset_state()
    oidc._JWKS_CACHE.clear()
    yield
    oidc_login.reset_state()
    oidc._JWKS_CACHE.clear()


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(oidc_login, "_now", fake)
    return fake


@pytest.fixture(scope="module")
def private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def signer(private_key, monkeypatch):
    """Install a JWKS holding the test key, so verification stays offline."""
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": KID, "alg": "RS256", "use": "sig"})
    document = {"keys": [jwk]}
    oidc._JWKS_CACHE.clear()
    monkeypatch.setattr(oidc, "_fetch_jwks", lambda settings: document)
    return pem


class _FakeIdP:
    """The identity provider, mocked at the two HTTP seams the module uses."""

    def __init__(self, pem: bytes) -> None:
        self._pem = pem
        #: Claim ``nonce`` minted into the next ID token; None means "whatever
        #: the authorization request carried" (set by the test).
        self.nonce = ""
        #: When set, the token endpoint hands back this pre-minted ID token.
        self.custom_token: str | None = None
        self.fail = False
        self.error_payload: dict | None = None
        self.exchange_calls = 0
        self.forms: list[dict[str, str]] = []

    def fetch_json(self, url: str, *, allow_private: bool = False) -> dict:
        assert url == DISCOVERY_URL
        assert allow_private is False
        return {
            "issuer": ISSUER,
            "authorization_endpoint": AUTHORIZATION_ENDPOINT,
            "token_endpoint": TOKEN_ENDPOINT,
            "jwks_uri": JWKS_URI,
        }

    def post_form(
        self, url: str, form: dict[str, str], *, allow_private: bool = False
    ) -> dict:
        assert url == TOKEN_ENDPOINT
        self.exchange_calls += 1
        self.forms.append(dict(form))
        if self.fail:
            raise oidc_login.OidcLoginError("token endpoint returned 500")
        if self.error_payload is not None:
            return dict(self.error_payload)
        token = self.custom_token if self.custom_token is not None else self.id_token()
        return {"id_token": token, "token_type": "Bearer"}

    def id_token(self, **claim_overrides) -> str:
        issued = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": "user-1",
            "iat": issued,
            "exp": issued + 600,
            "nonce": self.nonce,
        }
        claims.update(claim_overrides)
        return jwt.encode(claims, self._pem, algorithm="RS256", headers={"kid": KID})


@pytest.fixture
def idp(signer, monkeypatch) -> _FakeIdP:
    fake = _FakeIdP(signer)
    monkeypatch.setattr(oidc_login, "_fetch_json", fake.fetch_json)
    monkeypatch.setattr(oidc_login, "_post_form", fake.post_form)
    return fake


def _env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    secret_file = tmp_path / "oidc_client_secret"
    secret_file.write_text(CLIENT_SECRET + "\n", encoding="utf-8")
    secret_file.chmod(0o600)
    env = {
        oidc_login.ENV_CLIENT_ID: CLIENT_ID,
        oidc_login.ENV_CLIENT_SECRET_FILE: str(secret_file),
        oidc_login.ENV_REDIRECT_URI: REDIRECT_URI,
        oidc_login.ENV_ISSUER: ISSUER,
    }
    env.update(overrides)
    return env


@pytest.fixture
def config(tmp_path):
    return oidc_login.load_config(_env(tmp_path))


def _browser(config) -> TestClient:
    """A client that behaves like a browser: an https origin and a cookie jar.

    The origin has to be https because the login flow's cookies are ``Secure``
    and ``__Host-`` prefixed, and a browser (like ``http.cookiejar``) stores but
    never returns a ``Secure`` cookie to a plain-http origin.
    """
    app = FastAPI()
    oidc_login.mount_auth_routes(app, config)
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
def client(config) -> TestClient:
    return _browser(config)


def _auth_log_text(caplog) -> str:
    """The log text our own module emitted.

    Third-party records (``httpx`` logging the request URL, which necessarily
    carries ``code`` and ``state``) are out of scope: what this module must
    never write is the assertion, and silencing a library's access log from
    here would be a change of someone else's behavior.
    """
    return "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == oidc_login.logger.name
    )


def _login_params(client: TestClient) -> dict[str, str]:
    response = client.get("/auth/login", follow_redirects=False)
    assert response.status_code == 302
    query = parse_qs(urlsplit(response.headers["location"]).query)
    return {name: values[0] for name, values in query.items()}


def _callback(client: TestClient, state: str, code: str = "auth-code-123"):
    return client.get(
        "/auth/callback",
        params={"state": state, "code": code},
        follow_redirects=False,
    )


def _session_cookie(response) -> str:
    header = response.headers["set-cookie"]
    prefix = f"{oidc_login.SESSION_COOKIE}="
    assert header.startswith(prefix)
    return header[len(prefix) :].split(";", 1)[0]


def _new_session(client: TestClient, idp: _FakeIdP, state: str) -> str:
    response = _callback(client, state)
    assert response.status_code == 302
    return _session_cookie(response)


# --------------------------------------------------------------------------
# GET /auth/login
# --------------------------------------------------------------------------


def test_login_redirects_to_authorization_endpoint(client, idp):
    response = client.get("/auth/login", follow_redirects=False)

    assert response.status_code == 302
    location = urlsplit(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == (
        AUTHORIZATION_ENDPOINT
    )
    params = {k: v[0] for k, v in parse_qs(location.query).items()}
    assert params["response_type"] == "code"
    assert params["client_id"] == CLIENT_ID
    assert params["redirect_uri"] == REDIRECT_URI
    assert "openid" in params["scope"].split()
    assert params["state"]
    assert params["nonce"]
    assert params["state"] != params["nonce"]
    assert params["code_challenge_method"] == "S256"
    assert len(params["code_challenge"]) == 43
    assert "=" not in params["code_challenge"]


def test_login_never_logs_state_nonce_or_challenge(client, idp, caplog):
    caplog.set_level(logging.DEBUG)

    params = _login_params(client)

    emitted = _auth_log_text(caplog)
    for value in (params["state"], params["nonce"], params["code_challenge"]):
        assert value not in emitted


def test_pkce_challenge_matches_rfc7636_example():
    # RFC 7636 Appendix B: the S256 transform of the documented verifier.
    challenge = oidc_login.pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")

    assert challenge == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_login_returns_generic_error_when_discovery_fails(client, monkeypatch):
    def failing(url: str, *, allow_private: bool = False) -> dict:
        raise oidc_login.OidcLoginError("discovery document unavailable")

    monkeypatch.setattr(oidc_login, "_fetch_json", failing)

    response = client.get("/auth/login", follow_redirects=False)

    assert response.status_code == 503
    assert response.json() == {"error": "authentication_unavailable"}


# --------------------------------------------------------------------------
# GET /auth/callback — happy path
# --------------------------------------------------------------------------


def test_callback_sets_secure_session_cookie(client, idp, clock):
    params = _login_params(client)
    idp.nonce = params["nonce"]

    response = _callback(client, params["state"])

    assert response.status_code == 302
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{oidc_login.SESSION_COOKIE}=")
    lowered = cookie.lower()
    assert "httponly" in lowered
    assert "secure" in lowered
    assert "samesite=strict" in lowered
    assert "path=/" in lowered
    assert "domain" not in lowered
    assert len(_session_cookie(response)) >= 43

    form = idp.forms[-1]
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "auth-code-123"
    assert form["redirect_uri"] == REDIRECT_URI
    assert form["client_id"] == CLIENT_ID
    assert form["client_secret"] == CLIENT_SECRET
    assert form["code_verifier"]
    assert CLIENT_SECRET not in response.text
    assert idp.id_token() not in response.text


# --------------------------------------------------------------------------
# GET /auth/callback — state handling
# --------------------------------------------------------------------------


def test_callback_rejects_unknown_state(client, idp):
    response = _callback(client, "state-never-issued")

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_request"}
    assert idp.exchange_calls == 0


def test_callback_rejects_state_at_its_expiry(client, idp, clock):
    params = _login_params(client)
    idp.nonce = params["nonce"]

    clock.now = oidc_login.STATE_TTL_S

    response = _callback(client, params["state"])

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_request"}
    assert idp.exchange_calls == 0


def test_callback_state_is_single_use(client, idp, clock):
    params = _login_params(client)
    idp.nonce = params["nonce"]

    clock.now = oidc_login.STATE_TTL_S - 1
    assert _callback(client, params["state"]).status_code == 302

    clock.now = oidc_login.STATE_TTL_S
    replay = _callback(client, params["state"])

    assert replay.status_code == 400
    assert replay.json() == {"error": "invalid_request"}
    assert idp.exchange_calls == 1


def test_callback_consumes_state_even_when_exchange_fails(client, idp, clock):
    params = _login_params(client)
    clock.now = 200
    idp.fail = True

    failed = _callback(client, params["state"])

    assert failed.status_code == 401
    assert failed.json() == {"error": "authentication_failed"}
    assert idp.exchange_calls == 1

    idp.fail = False
    clock.now = 201
    retry = _callback(client, params["state"])

    assert retry.status_code == 400
    assert retry.json() == {"error": "invalid_request"}
    assert idp.exchange_calls == 1


# --------------------------------------------------------------------------
# GET /auth/callback — token handling
# --------------------------------------------------------------------------


def test_callback_rejects_nonce_mismatch(client, idp, clock):
    params = _login_params(client)
    idp.nonce = "a-different-nonce"

    response = _callback(client, params["state"])

    assert response.status_code == 401
    assert response.json() == {"error": "authentication_failed"}
    # No session is minted; the only cookie written is the one-shot state
    # binding being cleared.
    assert not [
        header
        for header in response.headers.get_list("set-cookie")
        if header.startswith(f"{oidc_login.SESSION_COOKIE}=")
    ]


def test_callback_rejects_token_from_another_audience(client, idp, clock):
    params = _login_params(client)
    idp.nonce = params["nonce"]
    idp.custom_token = idp.id_token(aud="someone-else")

    response = _callback(client, params["state"])

    assert response.status_code == 401
    assert response.json() == {"error": "authentication_failed"}
    # No session is minted; the only cookie written is the one-shot state
    # binding being cleared.
    assert not [
        header
        for header in response.headers.get_list("set-cookie")
        if header.startswith(f"{oidc_login.SESSION_COOKIE}=")
    ]


def test_token_exchange_failure_is_generic(client, idp, caplog):
    caplog.set_level(logging.DEBUG)
    params = _login_params(client)
    idp.error_payload = {
        "error": "invalid_client",
        "error_description": f"secret {CLIENT_SECRET} abc123",
    }

    response = _callback(client, params["state"])

    assert response.status_code == 401
    assert response.json() == {"error": "authentication_failed"}
    for sensitive in (
        CLIENT_SECRET,
        "abc123",
        "invalid_client",
        "auth-code-123",
        params["state"],
    ):
        assert sensitive not in response.text
        assert sensitive not in _auth_log_text(caplog)


def test_failed_exchange_never_logs_code_verifier_or_token(client, idp, caplog):
    caplog.set_level(logging.DEBUG)
    params = _login_params(client)
    idp.error_payload = {"error": "server_error"}

    _callback(client, params["state"])

    verifier = idp.forms[-1]["code_verifier"]
    emitted = _auth_log_text(caplog)
    assert verifier not in emitted
    assert "code_verifier" not in emitted


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_config_with_no_variables_is_disabled():
    config = oidc_login.load_config({})

    assert config.enabled is False


def test_issuer_alone_does_not_enable_login():
    config = oidc_login.load_config({oidc_login.ENV_ISSUER: ISSUER})

    assert config.enabled is False


def test_config_with_all_variables_is_enabled(tmp_path):
    config = oidc_login.load_config(_env(tmp_path))

    assert config.enabled is True
    assert config.client_id == CLIENT_ID
    assert config.redirect_uri == REDIRECT_URI


@pytest.mark.parametrize(
    "missing",
    [
        oidc_login.ENV_CLIENT_ID,
        oidc_login.ENV_CLIENT_SECRET_FILE,
        oidc_login.ENV_REDIRECT_URI,
    ],
)
def test_partial_config_refuses_startup(tmp_path, missing):
    env = _env(tmp_path)
    env.pop(missing)

    with pytest.raises(oidc_login.OidcLoginConfigError) as excinfo:
        oidc_login.load_config(env)

    message = str(excinfo.value)
    assert missing in message
    assert CLIENT_SECRET not in message
    assert REDIRECT_URI not in message


def test_missing_secret_file_refuses_startup(tmp_path):
    env = _env(tmp_path)
    Path(env[oidc_login.ENV_CLIENT_SECRET_FILE]).unlink()

    with pytest.raises(oidc_login.OidcLoginConfigError) as excinfo:
        oidc_login.load_config(env)

    assert oidc_login.ENV_CLIENT_SECRET_FILE in str(excinfo.value)


def test_world_readable_secret_file_refuses_startup(tmp_path):
    env = _env(tmp_path)
    Path(env[oidc_login.ENV_CLIENT_SECRET_FILE]).chmod(0o644)

    with pytest.raises(oidc_login.OidcLoginConfigError) as excinfo:
        oidc_login.load_config(env)

    message = str(excinfo.value)
    assert oidc_login.ENV_CLIENT_SECRET_FILE in message
    assert CLIENT_SECRET not in message


def test_disabled_config_exposes_no_auth_routes():
    config = oidc_login.load_config({})
    app = FastAPI()
    oidc_login.mount_auth_routes(app, config)
    client = TestClient(app)

    assert client.get("/auth/login", follow_redirects=False).status_code == 404
    assert (
        client.get(
            "/auth/callback", params={"state": "s", "code": "c"}, follow_redirects=False
        ).status_code
        == 404
    )


# --------------------------------------------------------------------------
# Session lifetime
# --------------------------------------------------------------------------


def test_session_idle_timeout_ends_the_session(client, idp, clock):
    clock.now = 1000
    params = _login_params(client)
    idp.nonce = params["nonce"]
    session_id = _new_session(client, idp, params["state"])

    clock.now = 1000 + oidc_login.SESSION_IDLE_TIMEOUT_S - 1
    assert oidc_login.load_session(session_id) is not None

    clock.now = 1000 + 2 * oidc_login.SESSION_IDLE_TIMEOUT_S - 1
    assert oidc_login.load_session(session_id) is None


def test_session_survives_a_backwards_clock(client, idp, clock):
    clock.now = 1000
    params = _login_params(client)
    idp.nonce = params["nonce"]
    session_id = _new_session(client, idp, params["state"])

    clock.now = 2000
    session = oidc_login.load_session(session_id)
    assert session is not None
    assert session.last_seen == 2000

    clock.now = 3799
    session = oidc_login.load_session(session_id)
    assert session is not None
    assert session.last_seen == 3799

    # The clock steps backwards: the only legal write is max(last_seen, now).
    clock.now = 2500
    session = oidc_login.load_session(session_id)
    assert session is not None
    assert session.last_seen == 3799

    clock.now = 4300
    session = oidc_login.load_session(session_id)
    assert session is not None
    assert session.last_seen == 4300


def test_session_absolute_timeout_wins_over_a_recent_activity(client, idp, clock):
    clock.now = 1000
    params = _login_params(client)
    idp.nonce = params["nonce"]
    session_id = _new_session(client, idp, params["state"])

    # Kept alive by activity every 1700 s, so the idle window never closes:
    # only the hard deadline can end this session.
    deadline = 1000 + oidc_login.SESSION_ABSOLUTE_TIMEOUT_S
    now = 1000
    while now + 1700 < deadline:
        now += 1700
        clock.now = now
        assert oidc_login.load_session(session_id) is not None

    clock.now = deadline - 1
    session = oidc_login.load_session(session_id)
    assert session is not None
    assert clock.now - session.last_seen < oidc_login.SESSION_IDLE_TIMEOUT_S

    # Idle delta is 1 s, but the absolute deadline has arrived.
    clock.now = deadline
    assert oidc_login.load_session(session_id) is None


def test_unknown_session_id_returns_none():
    assert oidc_login.load_session("no-such-session") is None
    assert oidc_login.load_session(None) is None


# --------------------------------------------------------------------------
# GET /auth/callback — the state binding to the requesting browser
# --------------------------------------------------------------------------


def _state_cookie(response) -> str:
    """The ``__Host-`` binding cookie the browser must replay on the callback."""
    for header in response.headers.get_list("set-cookie"):
        prefix = f"{oidc_login.LOGIN_STATE_COOKIE}="
        if header.startswith(prefix):
            return header[len(prefix) :].split(";", 1)[0]
    raise AssertionError("no state binding cookie was set")


def test_login_binds_state_to_the_browser(client, idp):
    response = client.get("/auth/login", follow_redirects=False)

    params = {
        k: v[0]
        for k, v in parse_qs(urlsplit(response.headers["location"]).query).items()
    }
    cookie = [
        header
        for header in response.headers.get_list("set-cookie")
        if header.startswith(f"{oidc_login.LOGIN_STATE_COOKIE}=")
    ]
    assert cookie, "the login redirect must bind state to the requesting browser"
    assert _state_cookie(response) == params["state"]
    attributes = cookie[0].lower()
    assert "secure" in attributes
    assert "httponly" in attributes
    assert "samesite=lax" in attributes
    assert "path=/" in attributes
    assert "domain=" not in attributes


def test_callback_rejects_state_without_the_binding_cookie(config, client, idp, clock):
    """A callback URL lifted from another browser authenticates nobody.

    This is the login-CSRF case: the attacker completes the authorization in
    their own browser and hands the resulting callback URL to a victim. The
    victim's browser holds no binding cookie, so the attempt dies here.
    """
    params = _login_params(client)
    idp.nonce = params["nonce"]
    victim = _browser(config)

    response = _callback(victim, params["state"])

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_request"}
    assert oidc_login.SESSION_COOKIE not in "\n".join(
        response.headers.get_list("set-cookie")
    )
    assert idp.exchange_calls == 0
    # The state entry is untouched, so its legitimate owner can still finish.
    assert _callback(client, params["state"]).status_code == 302


def test_callback_rejects_a_foreign_binding_cookie(config, client, idp, clock):
    params = _login_params(client)
    idp.nonce = params["nonce"]
    attacker = _browser(config)
    attacker.cookies.set(oidc_login.LOGIN_STATE_COOKIE, "not-the-right-state")

    response = _callback(attacker, params["state"])

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_request"}
    assert oidc_login.SESSION_COOKIE not in "\n".join(
        response.headers.get_list("set-cookie")
    )
    assert idp.exchange_calls == 0


def test_callback_clears_the_binding_cookie_on_success(client, idp, clock):
    params = _login_params(client)
    idp.nonce = params["nonce"]

    response = _callback(client, params["state"])

    assert response.status_code == 302
    cleared = [
        header
        for header in response.headers.get_list("set-cookie")
        if header.startswith(f"{oidc_login.LOGIN_STATE_COOKIE}=")
    ]
    assert cleared, "the one-shot binding cookie must be cleared on success"
    value = cleared[0].split(";", 1)[0].split("=", 1)[1]
    assert value.strip('"') == "", "clearing must empty the binding cookie"


def test_callback_clears_the_binding_cookie_on_failure(client, idp, clock):
    params = _login_params(client)
    idp.nonce = "a-different-nonce"

    response = _callback(client, params["state"])

    assert response.status_code == 401
    cleared = [
        header
        for header in response.headers.get_list("set-cookie")
        if header.startswith(f"{oidc_login.LOGIN_STATE_COOKIE}=")
    ]
    assert cleared, "the binding cookie must be cleared even when login fails"
    value = cleared[0].split(";", 1)[0].split("=", 1)[1]
    assert value.strip('"') == "", "clearing must empty the binding cookie"


def test_callback_rejects_a_non_ascii_nonce_without_a_server_error(client, idp, clock):
    params = _login_params(client)
    idp.nonce = "nonce-with-a-\u00e9-character"

    response = _callback(client, params["state"])

    assert response.status_code == 401
    assert response.json() == {"error": "authentication_failed"}


# --------------------------------------------------------------------------
# GET /auth/logout
# --------------------------------------------------------------------------


def test_logout_ends_the_session_and_clears_the_cookie(client, idp, clock):
    params = _login_params(client)
    idp.nonce = params["nonce"]
    session_id = _new_session(client, idp, params["state"])
    assert oidc_login.load_session(session_id) is not None

    response = client.get("/auth/logout", follow_redirects=False)

    assert response.status_code == 302
    assert oidc_login.load_session(session_id) is None
    cleared = [
        header
        for header in response.headers.get_list("set-cookie")
        if header.startswith(f"{oidc_login.SESSION_COOKIE}=")
    ]
    assert cleared, "logout must clear the session cookie"
    value = cleared[0].split(";", 1)[0].split("=", 1)[1]
    assert value.strip('"') == "", "logout must empty the session cookie"


def test_logout_is_harmless_without_a_session(client):
    response = client.get("/auth/logout", follow_redirects=False)

    assert response.status_code == 302
    assert oidc_login.SESSION_COOKIE in "\n".join(
        response.headers.get_list("set-cookie")
    )


# --------------------------------------------------------------------------
# Bounds on the unauthenticated login routes
# --------------------------------------------------------------------------


def test_pending_logins_are_capped(client, idp, monkeypatch):
    monkeypatch.setattr(oidc_login, "MAX_PENDING_LOGINS", 3)

    codes = [
        client.get("/auth/login", follow_redirects=False).status_code for _ in range(5)
    ]

    assert codes.count(302) == 3
    assert codes[-1] == 429


# --------------------------------------------------------------------------
# Per-client rate limit on the unauthenticated routes
# --------------------------------------------------------------------------


def test_login_is_rate_limited_and_recovers_after_the_window(
    client, idp, clock, monkeypatch
):
    monkeypatch.setattr(oidc_login, "MAX_AUTH_REQUESTS", 2)
    clock.now = 0.0
    assert [
        client.get("/auth/login", follow_redirects=False).status_code for _ in range(2)
    ] == [302, 302]

    limited = client.get("/auth/login", follow_redirects=False)

    assert limited.status_code == 429
    assert limited.json() == {"error": "too_many_requests"}
    clock.now = oidc_login.AUTH_RATE_WINDOW_S - 0.1
    assert client.get("/auth/login", follow_redirects=False).status_code == 429
    # The window is half-open: a stamp from exactly one window ago is spent.
    clock.now = oidc_login.AUTH_RATE_WINDOW_S
    assert client.get("/auth/login", follow_redirects=False).status_code == 302


def test_a_rate_limited_request_is_not_counted(client, idp, clock, monkeypatch):
    monkeypatch.setattr(oidc_login, "MAX_AUTH_REQUESTS", 1)
    clock.now = 0.0
    assert client.get("/auth/login", follow_redirects=False).status_code == 302

    clock.now = 5.0
    assert client.get("/auth/login", follow_redirects=False).status_code == 429

    stamps = [stamp for bucket in oidc_login._RATE.values() for stamp in bucket]
    assert stamps == [0.0], "a refused request must not extend the window"


def test_callback_is_rate_limited_and_recovers_after_the_window(
    client, idp, clock, monkeypatch
):
    monkeypatch.setattr(oidc_login, "MAX_AUTH_REQUESTS", 1)
    clock.now = 0.0
    assert _callback(client, "unknown-state").status_code == 400

    limited = _callback(client, "unknown-state")

    assert limited.status_code == 429
    assert limited.json() == {"error": "too_many_requests"}
    assert idp.exchange_calls == 0, "a refused request must not reach the IdP"
    clock.now = oidc_login.AUTH_RATE_WINDOW_S
    assert _callback(client, "unknown-state").status_code == 400


# --------------------------------------------------------------------------
# Discovery-failure cooldown
# --------------------------------------------------------------------------


def _discovery_stub(monkeypatch, *, failures: int | None) -> list[str]:
    """Replace the discovery fetch: the first ``failures`` probes fail.

    ``None`` means every probe fails, which is what an identity provider that
    stays down does.
    """
    document = {
        "issuer": ISSUER,
        "authorization_endpoint": AUTHORIZATION_ENDPOINT,
        "token_endpoint": TOKEN_ENDPOINT,
        "jwks_uri": JWKS_URI,
    }
    probes: list[str] = []

    def fetch_json(url: str, *, allow_private: bool = False) -> dict:
        probes.append(url)
        if failures is None or len(probes) <= failures:
            raise oidc_login.OidcLoginError("discovery unreachable")
        return dict(document)

    monkeypatch.setattr(oidc_login, "_fetch_json", fetch_json)
    return probes


def test_discovery_failure_is_not_reprobed_inside_the_cooldown(
    config, clock, monkeypatch
):
    browser = _browser(config)
    probes = _discovery_stub(monkeypatch, failures=None)
    clock.now = 0.0

    first = browser.get("/auth/login", follow_redirects=False)

    assert first.status_code == 503
    assert first.json() == {"error": "authentication_unavailable"}
    assert probes == [DISCOVERY_URL]
    assert oidc_login._DISCOVERY_FAILURES[config.issuer] == (
        oidc_login.DISCOVERY_FAILURE_TTL_S
    )

    clock.now = oidc_login.DISCOVERY_FAILURE_TTL_S - 1
    second = browser.get("/auth/login", follow_redirects=False)

    assert second.status_code == 503
    assert probes == [DISCOVERY_URL], "the cooldown must skip the doomed probe"

    clock.now = oidc_login.DISCOVERY_FAILURE_TTL_S
    third = browser.get("/auth/login", follow_redirects=False)

    assert third.status_code == 503
    assert len(probes) == 2, "the cooldown must end at its deadline"


def test_discovery_success_clears_the_failure_marker(config, clock, monkeypatch):
    browser = _browser(config)
    probes = _discovery_stub(monkeypatch, failures=1)
    clock.now = 0.0
    assert browser.get("/auth/login", follow_redirects=False).status_code == 503
    assert config.issuer in oidc_login._DISCOVERY_FAILURES

    clock.now = oidc_login.DISCOVERY_FAILURE_TTL_S
    recovered = browser.get("/auth/login", follow_redirects=False)

    assert recovered.status_code == 302
    assert len(probes) == 2
    assert config.issuer not in oidc_login._DISCOVERY_FAILURES
    assert config.issuer in oidc_login._DISCOVERY


# --------------------------------------------------------------------------
# Secret file hardening
# --------------------------------------------------------------------------


def _secret_file_message(tmp_path: Path, path: Path) -> str:
    with pytest.raises(oidc_login.OidcLoginConfigError) as refused:
        oidc_login.load_config(
            _env(tmp_path, **{oidc_login.ENV_CLIENT_SECRET_FILE: str(path)})
        )
    message = str(refused.value)
    assert oidc_login.ENV_CLIENT_SECRET_FILE in message
    assert CLIENT_SECRET not in message, "the failure must not echo the secret"
    return message


def test_symlinked_secret_file_refuses_startup(tmp_path):
    target = tmp_path / "real_secret"
    target.write_text(CLIENT_SECRET, encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "linked_secret"
    link.symlink_to(target)

    message = _secret_file_message(tmp_path, link)

    assert "must not be a symbolic link" in message


def test_secret_file_owned_by_another_user_refuses_startup(tmp_path, monkeypatch):
    secret_file = tmp_path / "oidc_client_secret"
    secret_file.write_text(CLIENT_SECRET, encoding="utf-8")
    secret_file.chmod(0o600)
    monkeypatch.setattr(oidc_login.os, "geteuid", lambda: secret_file.stat().st_uid + 1)

    message = _secret_file_message(tmp_path, secret_file)

    assert "must be owned by the user running the dashboard" in message


# --------------------------------------------------------------------------
# GET /auth/callback — return to the validated ``next`` path
# --------------------------------------------------------------------------


def _start_login(client: TestClient, next_value: str) -> dict[str, str]:
    """Begin a login for ``next_value`` and return the minted auth-request
    parameters (``state``, ``nonce``, ...) from the redirect's Location."""
    response = client.get(
        "/auth/login", params={"next": next_value}, follow_redirects=False
    )
    assert response.status_code == 302
    query = parse_qs(urlsplit(response.headers["location"]).query)
    return {name: values[0] for name, values in query.items()}


def test_callback_redirects_to_the_validated_next(client, idp, clock):
    """The browser is sent back to the page it asked for, not to the root.

    ``validate_next_path`` is unit-tested on its own; this is the end-to-end
    proof that the value it validated is the one the callback redirects to.
    """
    clock.now = 1000.0
    params = _start_login(client, "/plans/x?tab=2")
    idp.nonce = params["nonce"]

    clock.now = 1001.0
    response = _callback(client, params["state"])

    assert response.status_code == 302
    assert response.headers["location"] == "/plans/x?tab=2"
    assert _session_cookie(response)


def test_callback_redirects_to_root_for_a_rejected_next(client, idp, clock):
    """A protocol-relative ``next`` is validated away before the redirect.

    ``//evil.example`` is what a browser would resolve to https://evil.example,
    so the whole flow must land on the dashboard root instead.
    """
    clock.now = 1002.0
    params = _start_login(client, "//evil.example")
    idp.nonce = params["nonce"]

    clock.now = 1003.0
    response = _callback(client, params["state"])

    assert response.status_code == 302
    assert response.headers["location"] == "/"
    assert _session_cookie(response)
