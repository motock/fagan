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

import logging
import threading
import time
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
JWKS_TIMEOUT_S = 10.0
JWKS_CACHE_SECONDS = 300

#: One cached JWKS document per URL, guarded because the dashboard is threaded
#: and an unguarded dict write during a refresh is a data race.
_JWKS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_JWKS_LOCK = threading.Lock()


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
        if not isinstance(self.clock_skew_s, int) or self.clock_skew_s < 0:
            raise IdentityError("invalid clock_skew_s: must be a non-negative int")


def _fetch_jwks(settings: OidcSettings) -> dict[str, Any]:
    """Fetch and parse the JWKS document. Raises ``IdentityError`` on failure."""
    try:
        response = httpx.get(
            settings.jwks_url, timeout=JWKS_TIMEOUT_S, follow_redirects=False
        )
    except httpx.HTTPError:
        raise IdentityError("invalid token: keys unavailable") from None
    if response.status_code != 200:
        raise IdentityError("invalid token: keys unavailable")
    try:
        document = response.json()
    except ValueError:
        raise IdentityError("invalid token: keys unavailable") from None
    if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
        raise IdentityError("invalid token: keys unavailable")
    return document


def _jwks(settings: OidcSettings) -> dict[str, Any]:
    """Return the cached JWKS document, refetching once it is older than the TTL."""
    now = time.monotonic()
    with _JWKS_LOCK:
        cached = _JWKS_CACHE.get(settings.jwks_url)
        if cached is not None and now - cached[0] < JWKS_CACHE_SECONDS:
            return cached[1]
    document = _fetch_jwks(settings)
    with _JWKS_LOCK:
        _JWKS_CACHE[settings.jwks_url] = (now, document)
    return document


def _signing_key(settings: OidcSettings, kid: str | None) -> Any:
    """Return the public key for ``kid``; an unknown kid is a rejection."""
    for jwk in _jwks(settings)["keys"]:
        if isinstance(jwk, dict) and jwk.get("kid") == kid:
            try:
                return jwt.PyJWK(jwk).key
            except jwt.PyJWTError:
                raise IdentityError("invalid token: unusable key") from None
    raise IdentityError("invalid token: unknown key")


def _roles_from_claims(claims: dict[str, Any]) -> frozenset[str]:
    """Intersect the token's group claim with the ADR 0009 role set."""
    raw = claims.get("groups") or claims.get("roles") or []
    if isinstance(raw, str):
        raw = raw.split()
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(role for role in raw if isinstance(role, str) and role in ROLES)


def _scopes_from_claims(claims: dict[str, Any]) -> tuple[str, ...]:
    """Return the token's scopes from a space-separated ``scope`` or ``scp`` list."""
    raw = claims.get("scope")
    if isinstance(raw, str):
        return tuple(part for part in raw.split() if part)
    fallback = claims.get("scp")
    if isinstance(fallback, list):
        return tuple(part for part in fallback if isinstance(part, str) and part)
    return ()


def verify_token(token: str | None, settings: OidcSettings) -> Principal:
    """Verify ``token`` and return its ``Principal``, or raise ``IdentityError``.

    The header is read only to select a key by ``kid`` and to reject an
    algorithm outside the allowlist; every trust decision comes from the
    configured ``settings``, never from the token.
    """
    if not token:
        raise IdentityError("missing credential")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise IdentityError("invalid token: malformed") from None
    algorithm = header.get("alg")
    if algorithm not in ALLOWED_ALGORITHMS:
        raise IdentityError("invalid token: unsupported algorithm")
    key = _signing_key(settings, header.get("kid"))
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
        raise IdentityError("invalid token: verification failed") from None

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise IdentityError("invalid token: missing subject")
    email = claims.get("email")
    return Principal(
        subject=subject,
        roles=_roles_from_claims(claims),
        scopes=_scopes_from_claims(claims),
        email=email if isinstance(email, str) else "",
        auth_method="oidc",
    )
