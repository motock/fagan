"""OIDC identity provider: JWKS-backed JWT verification (ADR 0009 phase 6).

Free-standing like ``app.identity``: it imports the identity types and nothing
else from ``app.*``, so the service layer can depend on it without a cycle.

Verification rules, each load-bearing:

* the accepted algorithms are a fixed allowlist (RS256, ES256). The token
  header never chooses its own algorithm, which is what makes ``alg: none``
  and the HS256-for-RS256 confusion attack impossible here;
* ``exp``, ``iss``, ``aud`` and ``sub`` are required, so a token that omits an
  expiry is rejected rather than treated as eternal;
* issuer and audience are compared against the configured values, never
  against values taken from the token;
* one bounded clock skew covers ``exp``/``nbf``;
* every failure raises ``IdentityError`` with a fixed reason token, never the
  token, the key material or a claims dump.
"""

from __future__ import annotations

import copy
import ipaddress
import logging
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

from .identity import ROLES, IdentityError, Principal

logger = logging.getLogger("pipeline.identity")

#: Algorithms accepted from the token. Anything else - including ``none`` and
#: every ``HS*`` - is refused before a key is even selected.
ALLOWED_ALGORITHMS = ("RS256", "ES256")

DEFAULT_CLOCK_SKEW_S = 60

#: Upper bound on the configured skew. A large skew un-expires tokens, so the
#: value is bounded rather than merely validated.
MAX_CLOCK_SKEW_S = 300

JWKS_TIMEOUT_S = 10.0
JWKS_CACHE_SECONDS = 300

#: Responses larger than this are refused unparsed: the JWKS document is a few
#: KiB in practice, and an unbounded body is a memory-exhaustion lever.
JWKS_MAX_BYTES = 256 * 1024

#: An unknown ``kid`` triggers at most one refetch in this window, so a stream
#: of tokens with bogus kids cannot turn into a stream of outbound requests.
JWKS_MIN_REFRESH_S = 10.0

#: Bound on the credential itself. A JWT is a few KiB; anything much larger is
#: not a token we should spend parsing effort on.
MAX_TOKEN_LENGTH = 16 * 1024

#: One cached JWKS document per URL, guarded because the dashboard is threaded
#: and an unguarded dict write during a refresh is a data race.
_JWKS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_JWKS_LOCK = threading.Lock()

#: Serializes refreshes so a burst of unauthenticated requests performs one
#: outbound fetch instead of one per worker thread.
_JWKS_FETCH_LOCK = threading.Lock()

#: Reason tokens for recent failed fetches, so a failing IdP is retried on a
#: short timer rather than once per caller. Without this a single unauthenticated
#: caller can pin every dashboard worker in a 10 s outbound timeout, and hammer
#: the IdP while it is already unhappy.
_JWKS_FAILURES: dict[str, tuple[float, str]] = {}
JWKS_FAILURE_TTL_S = 5.0


@dataclass(frozen=True)
class OidcSettings:
    """The token's expected provenance, from operator configuration."""

    issuer: str
    audience: str
    jwks_url: str
    clock_skew_s: int = DEFAULT_CLOCK_SKEW_S

    def __post_init__(self) -> None:
        for name in ("issuer", "audience", "jwks_url"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise IdentityError(f"invalid {name}: must be a non-empty string")
        if not self.jwks_url.startswith("https://"):
            raise IdentityError("invalid jwks_url: must use https")
        if _is_internal_target(self.jwks_url):
            raise IdentityError("invalid jwks_url: must not target an internal host")
        if (
            isinstance(self.clock_skew_s, bool)
            or not isinstance(self.clock_skew_s, int)
            or self.clock_skew_s < 0
        ):
            raise IdentityError("invalid clock_skew_s: must be a non-negative int")
        if self.clock_skew_s > MAX_CLOCK_SKEW_S:
            raise IdentityError(
                f"invalid clock_skew_s: must not exceed {MAX_CLOCK_SKEW_S}"
            )


#: Fixed reason token for a ``kid`` that is not published in the JWKS.
_UNKNOWN_KEY = "invalid token: unknown key"


def _is_internal_target(url: str) -> bool:
    """Return True if ``url`` points at a loopback/private/link-local target.

    ``jwks_url`` must be operator-controlled, but a misconfiguration (or a
    value sourced from the environment in OIDC-02) would otherwise turn the
    shared server into an SSRF probe against loopback or cloud metadata.
    """
    host = urllib.parse.urlsplit(url).hostname
    if not host:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost" or host.endswith(".localhost")
    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _fetch_jwks(settings: OidcSettings) -> dict[str, Any]:
    """Fetch and parse the JWKS document. Raises ``IdentityError`` on failure."""
    try:
        response = httpx.get(
            settings.jwks_url, timeout=JWKS_TIMEOUT_S, follow_redirects=False
        )
    except (httpx.HTTPError, httpx.InvalidURL, UnicodeError, ValueError):
        raise IdentityError("invalid token: keys unavailable") from None
    if response.status_code != 200:
        raise IdentityError("invalid token: keys unavailable")
    body = getattr(response, "content", b"")
    if isinstance(body, bytes) and len(body) > JWKS_MAX_BYTES:
        raise IdentityError("invalid token: keys unavailable")
    try:
        document = response.json()
    except ValueError:
        raise IdentityError("invalid token: keys unavailable") from None
    if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
        raise IdentityError("invalid token: keys unavailable")
    return document


def _jwks(settings: OidcSettings, force_refresh: bool = False) -> dict[str, Any]:
    """Return a cached JWKS document, refetching once it is older than the TTL.

    ``force_refresh`` is used on a ``kid`` miss so key rotation takes effect
    immediately, rate-limited so a stream of unknown kids cannot become a
    stream of outbound fetches. The refresh itself is serialized and the cache
    is re-checked inside the lock, so a burst of concurrent misses performs one
    fetch. A failed fetch writes nothing, so the next caller sees the original
    ``IdentityError`` rather than a half-populated entry.
    """
    now = time.monotonic()
    with _JWKS_LOCK:
        cached = _JWKS_CACHE.get(settings.jwks_url)
    if cached is not None:
        age = now - cached[0]
        if age < JWKS_CACHE_SECONDS and not (
            force_refresh and age >= JWKS_MIN_REFRESH_S
        ):
            return copy.deepcopy(cached[1])

    with _JWKS_FETCH_LOCK:
        now = time.monotonic()
        with _JWKS_LOCK:
            cached = _JWKS_CACHE.get(settings.jwks_url)
        if cached is not None:
            age = now - cached[0]
            if age < JWKS_CACHE_SECONDS and not (
                force_refresh and age >= JWKS_MIN_REFRESH_S
            ):
                return copy.deepcopy(cached[1])
        with _JWKS_LOCK:
            failure = _JWKS_FAILURES.get(settings.jwks_url)
        if failure is not None and now - failure[0] < JWKS_FAILURE_TTL_S:
            raise IdentityError(failure[1])
        try:
            document = _fetch_jwks(settings)
        except IdentityError as exc:
            with _JWKS_LOCK:
                _JWKS_FAILURES[settings.jwks_url] = (now, str(exc))
            raise
        with _JWKS_LOCK:
            _JWKS_CACHE[settings.jwks_url] = (now, document)
            _JWKS_FAILURES.pop(settings.jwks_url, None)
    return copy.deepcopy(document)


def _signing_key(settings: OidcSettings, kid: str | None, refresh: bool = False) -> Any:
    """Return the public key for ``kid``; an unknown kid is a rejection."""
    for jwk in _jwks(settings, force_refresh=refresh)["keys"]:
        if not isinstance(jwk, dict):
            continue
        # Bind to an explicit kid and to signature-use keys only: a JWK with no
        # kid, or one published for encryption, must never verify a token.
        if kid is None or jwk.get("kid") != kid:
            continue
        if jwk.get("use", "sig") != "sig":
            continue
        key_ops = jwk.get("key_ops")
        if isinstance(key_ops, list) and "verify" not in key_ops:
            continue
        try:
            return jwt.PyJWK(jwk).key
        except jwt.PyJWTError:
            raise IdentityError("invalid token: unusable key") from None
    raise IdentityError(_UNKNOWN_KEY)


def _roles_from_claims(claims: dict[str, Any]) -> frozenset[str]:
    """Intersect the token's group claim with the ADR 0009 role set."""
    raw = claims.get("groups") or claims.get("roles") or []
    if isinstance(raw, str):
        raw = raw.split()
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(role for role in raw if isinstance(role, str) and role in ROLES)


def _scopes_from_claims(claims: dict[str, Any]) -> tuple[str, ...]:
    """Return the token's scopes from a space-separated ``scope`` or ``scp`` list.

    Wildcards are server-resolved only (ADR 0009): a token that says ``*``
    means "no scope", not "every scope", because the token cannot carry the
    whole authorization decision.
    """
    raw = claims.get("scope")
    if isinstance(raw, str):
        return tuple(part for part in raw.split() if part and part != "*")
    fallback = claims.get("scp")
    if isinstance(fallback, list):
        return tuple(
            part for part in fallback if isinstance(part, str) and part and part != "*"
        )
    return ()


def _email_from_claims(claims: dict[str, Any]) -> str:
    """Return the token's email only when the issuer marks it verified.

    No consumer exists yet (OIDC-02 adds one), but ``email`` is a user-editable
    claim on several issuers; carrying an unverified address would invite
    spoofing the moment anything treats it as identification or a mail route.
    """
    email = claims.get("email")
    if not isinstance(email, str) or not email:
        return ""
    if claims.get("email_verified") is not True:
        return ""
    return email


def verify_token(token: str | None, settings: OidcSettings) -> Principal:
    """Verify ``token`` and return its ``Principal``, or raise ``IdentityError``.

    The header is read only to select a key by ``kid`` and to reject an
    algorithm outside the allowlist; every trust decision comes from the
    configured ``settings``, never from the token.
    """
    if not token:
        raise IdentityError("missing credential")
    if len(token) > MAX_TOKEN_LENGTH:
        raise IdentityError("invalid token: malformed")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise IdentityError("invalid token: malformed") from None
    algorithm = header.get("alg")
    if algorithm not in ALLOWED_ALGORITHMS:
        raise IdentityError("invalid token: unsupported algorithm")
    kid = header.get("kid")
    try:
        key = _signing_key(settings, kid)
    except IdentityError as exc:
        # A kid miss usually means the issuer rotated keys: refetch once
        # (rate-limited) before rejecting, so rotation is not a 300 s outage.
        if str(exc) != _UNKNOWN_KEY:
            raise
        try:
            key = _signing_key(settings, kid, refresh=True)
        except IdentityError as refresh_exc:
            _log_rejection(settings, kid, str(refresh_exc))
            raise IdentityError(str(refresh_exc)) from None
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=list(ALLOWED_ALGORITHMS),
            audience=settings.audience,
            issuer=settings.issuer,
            leeway=settings.clock_skew_s,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError:
        _log_rejection(settings, kid, "invalid token: verification failed")
        raise IdentityError("invalid token: verification failed") from None

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        _log_rejection(settings, kid, "invalid token: missing subject")
        raise IdentityError("invalid token: missing subject")
    # A ``sub`` is unique per issuer, never globally (RFC 9068 2.2), so it is
    # qualified by the issuer the token proved it came from. Without this, any
    # issuer whose users choose their own account name could mint a token whose
    # sub collides with this deployment's local operator and be treated as the
    # operator by LocalPolicy (CWE-290). The separator cannot appear in an
    # issuer or a subject under any of the allowlisted issuers, so no token can
    # produce the bare local-operator string.
    return Principal(
        subject=f"{claims['iss']}#{subject}",
        roles=_roles_from_claims(claims),
        scopes=_scopes_from_claims(claims),
        email=_email_from_claims(claims),
        auth_method="oidc",
    )


def _log_rejection(settings: OidcSettings, kid: object, reason: str) -> None:
    """Record a rejected verification: reason token, kid and issuer only.

    Never the token, the key material or a claims dump - an exception string
    and a log line both end up somewhere durable.
    """
    logger.warning(
        "oidc verification rejected: reason=%s kid=%s issuer=%s",
        reason,
        kid if isinstance(kid, str) else None,
        settings.issuer,
    )
