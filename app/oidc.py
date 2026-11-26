"""OIDC identity provider: JWKS-backed JWT verification (ADR 0009 phase 6).

Free-standing like ``app.identity``: besides the identity types it imports only
the plugin loader from ``app.*``, so the service layer can depend on it without
a cycle.

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

import contextlib
import copy
import json
import logging
import math
import os
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

from app.plugin_loader import (
    PluginError,
    load_plugin,
    require_allowlist,
    require_methods,
)

from . import outbound_guard
from .identity import (
    ROLES,
    SYSTEM_SUBJECT_PREFIX,
    IdentityError,
    IdentityProvider,
    Principal,
    SharedSecretProvider,
)

logger = logging.getLogger("pipeline.identity")


class _ReservedSubjectError(IdentityError, ValueError):
    """A token whose ``sub`` claims the reserved ``system:`` prefix.

    Subclasses both: the ``IdentityProvider`` contract requires every rejected
    credential to raise ``IdentityError``, and the prefix is a malformed value
    the caller may well catch as a ``ValueError``. The message names the failure
    kind only — never the token, its claims or the subject itself.
    """


#: Algorithms accepted from the token. Anything else - including ``none`` and
#: every ``HS*`` - is refused before a key is even selected.
ALLOWED_ALGORITHMS = ("RS256", "ES256")

DEFAULT_CLOCK_SKEW_S = 60

#: Upper bound on the configured skew. A large skew un-expires tokens, so the
#: value is bounded rather than merely validated.
MAX_CLOCK_SKEW_S = 300

JWKS_TIMEOUT_S = 10.0
JWKS_CACHE_SECONDS = 300

#: Operator configuration for the identity provider. Read once at build time
#: by :func:`build_identity_provider`, never per request.
IDENTITY_PROVIDER_ENV = "PIPELINE_IDENTITY_PROVIDER"
OIDC_ISSUER_ENV = "PIPELINE_OIDC_ISSUER"
OIDC_AUDIENCE_ENV = "PIPELINE_OIDC_AUDIENCE"
OIDC_JWKS_URL_ENV = "PIPELINE_OIDC_JWKS_URL"
OIDC_CLOCK_SKEW_ENV = "PIPELINE_OIDC_CLOCK_SKEW_S"

#: Opt-in for a JWKS hosted on a private network (a self-hosted Keycloak, Dex
#: or ADFS inside a VPC). Off by default; only ``1``/``true`` enable it.
OIDC_ALLOW_PRIVATE_JWKS_ENV = "PIPELINE_OIDC_ALLOW_PRIVATE_JWKS"

#: Comma-separated OIDC client ids (``azp``) allowed to authenticate as an
#: *agent* (ADR 0014). Empty - the default - means no client is an agent and
#: every token is a human principal. Read on every verification so a rotated
#: allowlist takes effect without a restart.
OIDC_AGENT_CLIENTS_ENV = "PIPELINE_OIDC_AGENT_CLIENTS"

#: The claim carrying an agent's ``<org-id>/<team-id>`` binding. Configurable
#: because IdPs differ on where a service account's scope lives.
OIDC_AGENT_SCOPE_CLAIM_ENV = "PIPELINE_OIDC_AGENT_SCOPE_CLAIM"
DEFAULT_AGENT_SCOPE_CLAIM = "team_scope"

#: Comma-separated token claim names to carry, uninterpreted, on the verified
#: principal. Empty (the default) carries nothing. Each name is an identifier.
OIDC_EXTRA_CLAIMS_ENV = "PIPELINE_OIDC_EXTRA_CLAIMS"
_CLAIM_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

#: Bounds on what a configured extra claim may add to a principal.
MAX_EXTRA_CLAIMS = 256
MAX_EXTRA_CLAIMS_BYTES = 16 * 1024

#: The prefix reserved for agent subjects, alongside ``system:``.
AGENT_SUBJECT_PREFIX = "agent:"

#: The accepted ``PIPELINE_IDENTITY_PROVIDER`` values. ``shared-secret`` is the
#: default and there is deliberately no "off": disabling authentication is not
#: a configuration option (ADR 0004).
_SHARED_SECRET = "shared-secret"
_OIDC = "oidc"

#: Bound on name resolution. ``socket.getaddrinfo`` has no timeout of its own and
#: runs while a fetch is in flight, so a slow or black-holed resolver would
#: otherwise stall every token verification behind one lookup. The value is the
#: shared guard's, so the JWKS fetch and a tenant-supplied endpoint cannot drift
#: apart; it stays a module attribute here because callers lower it.
JWKS_DNS_TIMEOUT_S = outbound_guard.DNS_TIMEOUT_S

#: Overall bound on one fetch. ``JWKS_TIMEOUT_S`` applies per socket operation,
#: so a trickling server could otherwise hold a worker (and the fetch lock) for
#: as long as it kept sending one byte at a time.
JWKS_TOTAL_TIMEOUT_S = 30.0

#: Responses larger than this are refused unparsed: the JWKS document is a few
#: KiB in practice, and an unbounded body is a memory-exhaustion lever.
JWKS_MAX_BYTES = 256 * 1024

#: An unknown ``kid`` triggers at most one refetch in this window, so a stream
#: of tokens with bogus kids cannot turn into a stream of outbound requests.
JWKS_MIN_REFRESH_S = 10.0

#: Bound on the credential itself. A JWT is a few KiB; anything much larger is
#: not a token we should spend parsing effort on.
MAX_TOKEN_LENGTH = 16 * 1024

#: Longest token lifetime accepted, measured as ``exp - iat``. The skew
#: allowance exists to absorb clock differences, not to admit credentials that
#: stay valid for years: once stolen, such a token is indistinguishable from a
#: permanent one, and no revocation path exists here.
MAX_TOKEN_LIFETIME_S = 3600

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
    #: Accept a JWKS hosted on a private network. Opt-in only: the default is
    #: what keeps a misconfigured ``jwks_url`` from probing loopback or a cloud
    #: metadata endpoint (see :func:`_is_internal_target`).
    allow_private_jwks: bool = False
    #: Token claim names to carry on the principal (see ``OIDC_EXTRA_CLAIMS_ENV``).
    extra_claims: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("issuer", "audience", "jwks_url"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise IdentityError(f"invalid {name}: must be a non-empty string")
        if not self.jwks_url.startswith("https://"):
            raise IdentityError("invalid jwks_url: must use https")
        if "#" in self.issuer:
            # ``#`` joins issuer and subject into one identifier; a value that
            # can carry it could be used to forge a different principal's id.
            raise IdentityError("invalid issuer: must not contain '#'")
        # Only this check is optional: the private-network opt-in exists for an
        # IdP inside a VPC, and must not relax the https, issuer or skew rules.
        if not self.allow_private_jwks and outbound_guard.is_internal_target(
            self.jwks_url
        ):
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

#: The JWK key type each accepted algorithm must be published under, so a key
#: of the wrong family is never handed to the verifier.
_KEY_TYPES = {"RS256": "RSA", "ES256": "EC"}


# --- the outbound guard, re-exported under its former private names ----------
#
# The address checks and the address-pinning transport now live in
# :mod:`app.outbound_guard`, because they are not OIDC-specific: a
# tenant-supplied model endpoint gets exactly the same treatment. These aliases
# keep this module's own surface compatible, so a caller that reached in for
# ``oidc._is_internal_address`` or ``oidc._PinnedTransport`` keeps working and
# cannot silently bypass the guard by finding the name gone.
_is_internal_address = outbound_guard.is_internal_address
_is_internal_target = outbound_guard.is_internal_target
_embedded_ipv4 = outbound_guard.embedded_ipv4
_PinnedTransport = outbound_guard.PinnedTransport

#: ``socket`` is re-exported for the same reason: name resolution is stubbed
#: through ``oidc.socket.getaddrinfo`` by callers that predate the move.
socket = outbound_guard.socket


def _resolve(url: str, *, allow_private: bool = False) -> str | None:
    """Return the public address ``url``'s host resolves to, or None.

    Thin wrapper over :func:`app.outbound_guard.resolve_public_address` that
    keeps the JWKS fetch bound to this module's ``JWKS_DNS_TIMEOUT_S``.
    """
    return outbound_guard.resolve_public_address(
        url, allow_private=allow_private, timeout=JWKS_DNS_TIMEOUT_S
    )


@contextlib.contextmanager
def _stream_response(url: str, address: str) -> Iterator[httpx.Response]:
    """Stream a ``GET`` for ``url`` against the already-validated ``address``."""
    with (
        outbound_guard.pinned_client(url, address, JWKS_TIMEOUT_S) as client,
        client.stream("GET", url) as response,
    ):
        yield response


def _fetch_jwks(settings: OidcSettings) -> dict[str, Any]:
    """Fetch and parse the JWKS document. Raises ``IdentityError`` on failure."""
    address = _resolve(
        settings.jwks_url, allow_private=settings.allow_private_jwks
    )
    if address is None:
        raise IdentityError("invalid token: keys unavailable")
    try:
        with _stream_response(settings.jwks_url, address) as response:
            if response.status_code != 200:
                raise IdentityError("invalid token: keys unavailable")
            # The cap is enforced while the body is read, not after: testing a
            # fully buffered response would let a compromised IdP stream
            # gigabytes into memory before the length check ever ran.
            body = bytearray()
            deadline = time.monotonic() + JWKS_TOTAL_TIMEOUT_S
            for chunk in response.iter_bytes():
                if time.monotonic() > deadline:
                    raise IdentityError("invalid token: keys unavailable")
                body.extend(chunk)
                if len(body) > JWKS_MAX_BYTES:
                    raise IdentityError("invalid token: keys unavailable")
    except (httpx.HTTPError, httpx.InvalidURL, UnicodeError, ValueError):
        raise IdentityError("invalid token: keys unavailable") from None
    try:
        document = json.loads(bytes(body))
    except (ValueError, RecursionError):
        # ``json.loads`` raises RecursionError, not ValueError, on a deeply
        # nested document: uncaught it would surface as a 500 from a rejection.
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


def _signing_key(
    settings: OidcSettings, kid: str | None, algorithm: str, refresh: bool = False
) -> Any:
    """Return the public key for ``kid``; an unknown kid is a rejection.

    ``algorithm`` is the header algorithm already vetted against the allowlist,
    and the JWK must agree with it: a key published for ES256 must not verify an
    RS256 token (and vice versa) even when both carry the same ``kid``, so key
    selection stays unambiguous when a JWKS publishes several keys under one id.
    """
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
        # An advertised algorithm or key type that disagrees with the header is
        # a different key, not a key to try anyway.
        if jwk.get("alg", algorithm) != algorithm:
            continue
        if jwk.get("kty", _KEY_TYPES[algorithm]) != _KEY_TYPES[algorithm]:
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


def _freeze(value: Any) -> Any:
    """Return ``value`` with every list and dict replaced by an immutable tuple."""
    if isinstance(value, dict):
        return tuple(sorted((key, _freeze(item)) for key, item in value.items()))
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _extra_claims_from_token(
    claims: dict[str, Any], names: tuple[str, ...]
) -> tuple[tuple[str, Any], ...]:
    """Copy the configured claims the token carries as immutable pairs.

    Refusal reports only the fixed reason; the claim values never reach the
    message. Absent names are skipped, so an empty ``names`` costs nothing.
    """
    present = [name for name in names if name in claims]
    if not present:
        return ()
    serialised = json.dumps(
        {name: claims[name] for name in present}, separators=(",", ":")
    )
    if (
        len(present) > MAX_EXTRA_CLAIMS
        or len(serialised.encode("utf-8")) > MAX_EXTRA_CLAIMS_BYTES
    ):
        raise IdentityError("claim too large")
    return tuple((name, _freeze(claims[name])) for name in present)


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


def _numeric_claim(claims: dict[str, Any], name: str) -> float:
    """Return the numeric claim ``name`` as a float, or raise ``IdentityError``.

    PyJWT validates ``exp``/``iat`` after ``int()``-coercing each value but leaves
    the decoded payload untouched, so a payload can carry ``"exp":
    "1700000000"`` (a string) or ``"iat": 1700000000.5`` (a float). Subtracting
    the first raises ``TypeError`` - turning a rejection into a 500 and breaking
    the "every failure is an ``IdentityError``" rule - while a non-``int`` ``iat``
    used to skip the lifetime bound entirely. Both claims are therefore
    normalized here, and anything that is not a finite real number (including a
    missing ``iat``, which would leave the bound unmeasurable) is refused with
    the fixed reason token.
    """
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise IdentityError("invalid token: verification failed")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        # An integer too large for a float (``10**400``) would otherwise raise
        # OverflowError here and turn a rejection into a 500.
        raise IdentityError("invalid token: verification failed") from None
    if not math.isfinite(number):
        raise IdentityError("invalid token: verification failed")
    return number


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
    if not isinstance(kid, str):
        kid = None
    try:
        key = _signing_key(settings, kid, algorithm)
    except IdentityError as exc:
        # A kid miss usually means the issuer rotated keys: refetch once
        # (rate-limited) before rejecting, so rotation is not a 300 s outage.
        if str(exc) != _UNKNOWN_KEY:
            raise
        try:
            key = _signing_key(settings, kid, algorithm, refresh=True)
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
    try:
        lifetime = _numeric_claim(claims, "exp") - _numeric_claim(claims, "iat")
    except IdentityError:
        _log_rejection(settings, kid, "invalid token: verification failed")
        raise
    if lifetime > MAX_TOKEN_LIFETIME_S:
        _log_rejection(settings, kid, "invalid token: verification failed")
        raise IdentityError("invalid token: verification failed")
    if "#" in subject:
        # ``#`` is the issuer/subject separator below. Refusing it here makes the
        # collision guarantee a checked invariant instead of an assumption about
        # what every configured issuer happens to mint.
        _log_rejection(settings, kid, "invalid token: unsupported subject")
        raise IdentityError("invalid token: unsupported subject")
    if subject.startswith(SYSTEM_SUBJECT_PREFIX):
        # ``system:`` is reserved for the principals the deployment itself acts
        # as (the scheduler daemon, a dispatched worker). No token may claim one,
        # so a system principal's authority is never reachable through the IdP.
        _log_rejection(settings, kid, "invalid token: reserved subject")
        raise _ReservedSubjectError("invalid token: reserved subject")
    if subject.startswith(AGENT_SUBJECT_PREFIX):
        # ``agent:`` is reserved too: an agent subject is *derived* below from
        # the token's scope claim, so a token that claims the prefix could
        # otherwise shadow or double-prefix another team's agent (ADR 0014).
        _log_rejection(settings, kid, "invalid token: reserved subject")
        raise _ReservedSubjectError("invalid token: reserved subject")
    # ADR 0014: an agent is a machine client the operator allowlisted. Its
    # ``azp`` is set by the IdP from the authenticated client, never by the
    # caller, which is why the allowlist check is trustworthy. Any other token
    # is a human principal and its scope claim is ignored outright.
    extra_claims = _extra_claims_from_token(claims, settings.extra_claims)
    azp = claims.get("azp")
    if isinstance(azp, str) and azp in _agent_clients():
        scope_claim = (
            str(
                os.environ.get(
                    OIDC_AGENT_SCOPE_CLAIM_ENV, DEFAULT_AGENT_SCOPE_CLAIM
                )
            ).strip()
            or DEFAULT_AGENT_SCOPE_CLAIM
        )
        team_scope = claims.get(scope_claim)
        if not isinstance(team_scope, str):
            _log_rejection(settings, kid, "invalid token: agent scope")
            raise IdentityError("invalid token: agent scope")
        try:
            return Principal(
                subject=f"{AGENT_SUBJECT_PREFIX}{team_scope}",
                roles=frozenset(),
                auth_method="oidc",
                principal_type="agent",
                client_id=azp,
                team_scope=team_scope,
                extra_claims=extra_claims,
            )
        except IdentityError:
            # ``Principal`` owns the team-scope grammar; the message stays a
            # fixed reason token and never echoes the claim value.
            _log_rejection(settings, kid, "invalid token: agent scope")
            raise IdentityError("invalid token: agent scope") from None
    # A ``sub`` is unique per issuer, never globally (RFC 9068 2.2), so it is
    # qualified by the issuer the token proved it came from. Without this, any
    # issuer whose users choose their own account name could mint a token whose
    # sub collides with this deployment's local operator and be treated as the
    # operator by LocalPolicy (CWE-290). ``#`` cannot appear in either half
    # (rejected above for the subject, refused in ``OidcSettings`` for the
    # issuer), so no token can produce the bare local-operator string.
    return Principal(
        subject=f"{claims['iss']}#{subject}",
        roles=_roles_from_claims(claims),
        scopes=_scopes_from_claims(claims),
        email=_email_from_claims(claims),
        auth_method="oidc",
        principal_type="human",
        client_id=azp if isinstance(azp, str) else "",
        extra_claims=extra_claims,
    )


def _agent_clients() -> frozenset[str]:
    """Return the allowlisted agent client ids, read from the env per call.

    Read on every verification rather than at import: the allowlist is
    operator configuration and a rotated value must take effect without a
    process restart. Empty entries are dropped, so ``"a, b,,"`` allows exactly
    ``a`` and ``b`` and an unset/empty variable allows no client at all.
    """
    raw = os.environ.get(OIDC_AGENT_CLIENTS_ENV, "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


class OidcProvider:
    """``IdentityProvider`` that verifies a bearer token against a JWKS."""

    # Declare ``network_grade = True`` only if ``authenticate()`` verifies a
    # bearer credential cryptographically (signature and expiry): the remote MCP
    # transport refuses every provider that does not declare it.
    network_grade = True

    def __init__(self, settings: OidcSettings) -> None:
        self._settings = settings

    def authenticate(self, credential: str | None) -> Principal:
        """Return the token's principal or raise ``IdentityError``."""
        return verify_token(credential, self._settings)


def _oidc_settings_from_env(env: Mapping[str, str]) -> OidcSettings:
    """Build ``OidcSettings`` from ``env``, rejecting missing or malformed values.

    Only names are echoed on failure, never values: ``jwks_url`` can carry
    credentials in its userinfo and the issuer is compared against the token,
    so neither belongs in an error message that may reach a log.

    ``PIPELINE_OIDC_ALLOW_PRIVATE_JWKS`` is the one loosening knob, and only
    ``1``/``true`` (case-insensitive) enable it: a typo such as ``yes`` or
    ``on`` leaves the restrictive default in place rather than opening the
    gate. Enabling it also pins the fetch to the resolved address, so the
    request cannot traverse an HTTP egress proxy - an operator behind one needs
    direct egress to the IdP.
    """
    missing = [
        name
        for name in (OIDC_ISSUER_ENV, OIDC_AUDIENCE_ENV, OIDC_JWKS_URL_ENV)
        if not str(env.get(name, "")).strip()
    ]
    if missing:
        raise IdentityError("invalid oidc configuration: " + ", ".join(missing))
    raw_skew = str(env.get(OIDC_CLOCK_SKEW_ENV, DEFAULT_CLOCK_SKEW_S)).strip()
    try:
        skew = int(raw_skew)
    except ValueError:
        raise IdentityError(
            "invalid oidc configuration: " + OIDC_CLOCK_SKEW_ENV
        ) from None
    if skew < 0:
        raise IdentityError("invalid oidc configuration: " + OIDC_CLOCK_SKEW_ENV)
    allow_private = str(env.get(OIDC_ALLOW_PRIVATE_JWKS_ENV, "")).strip().lower() in (
        "1",
        "true",
    )
    extra_claims = _extra_claim_names_from_env(env)
    return OidcSettings(
        issuer=str(env[OIDC_ISSUER_ENV]).strip(),
        audience=str(env[OIDC_AUDIENCE_ENV]).strip(),
        jwks_url=str(env[OIDC_JWKS_URL_ENV]).strip(),
        clock_skew_s=skew,
        allow_private_jwks=allow_private,
        extra_claims=extra_claims,
    )


def extra_claim_names(environ: dict[str, str] | None = None) -> tuple[str, ...]:
    """Return the configured extra claim names for a caller that builds its own settings.

    The browser login verifies the ID token against its own audience, so it
    builds an ``OidcSettings`` itself and must carry the same
    ``PIPELINE_OIDC_EXTRA_CLAIMS`` the API identity provider does. Raises
    IdentityError on a malformed list, exactly like the startup path.
    """
    source = dict(os.environ) if environ is None else dict(environ)
    return _extra_claim_names_from_env(source)


def _extra_claim_names_from_env(env: Mapping[str, str]) -> tuple[str, ...]:
    """Parse ``PIPELINE_OIDC_EXTRA_CLAIMS``; refuse the whole list on any bad name.

    Blank entries are dropped and duplicates collapse to their first position.
    The error names the variable only, never the offending entry.
    """
    raw = str(env.get(OIDC_EXTRA_CLAIMS_ENV, ""))
    names = [part.strip() for part in raw.split(",") if part.strip()]
    if any(not _CLAIM_NAME.match(name) for name in names):
        raise IdentityError("invalid oidc configuration: " + OIDC_EXTRA_CLAIMS_ENV)
    return tuple(dict.fromkeys(names))


def build_identity_provider(
    secret_reader: Callable[[], str],
    environ: Mapping[str, str] | None = None,
) -> IdentityProvider:
    """Return the configured provider, or raise on an unknown configuration.

    ``shared-secret`` (the default) resolves the operator's key lazily through
    ``secret_reader``, so rotation still takes effect per request. Any value
    other than ``shared-secret`` or ``oidc`` raises rather than falling back to
    the permissive local principal - an unknown setting must not silently
    downgrade authentication (ADR 0004). A plugin selected by the
    operator-controlled ``PIPELINE_IDENTITY_PLUGIN=module:attr`` variable wins
    over the built-in choice; a bad plugin spec raises rather than falling back.
    """
    env = os.environ if environ is None else environ
    # Scope, stated precisely: this selection makes a plugin the provider that
    # build_identity_provider returns, but it does NOT by itself put that
    # provider in front of any request. The remote MCP transport still refuses
    # to serve unless ``PIPELINE_IDENTITY_PROVIDER=oidc``
    # (pipeline/remote_transport.py::_settings_from_env), and that selector
    # conflicts with the plugin variable below, so wiring a plugin to the
    # remote transport needs a separate change to that gate. The dashboard
    # only consults an OidcProvider and stays on its API-key path otherwise,
    # and app/oidc_login.py's browser login is OIDC-specific and untouched.
    try:
        require_allowlist("identity", env)
        factory = load_plugin(
            "identity",
            env,
            selector=IDENTITY_PROVIDER_ENV,
            selector_default=_SHARED_SECRET,
        )
        if factory is not None:
            provider = factory(secret_reader, env)
            require_methods(provider, ("authenticate",), "identity")
            return provider
    except PluginError as exc:
        raise IdentityError(f"invalid identity plugin: {exc}") from None
    raw = str(env.get(IDENTITY_PROVIDER_ENV, _SHARED_SECRET)).strip()
    if raw in ("", _SHARED_SECRET):
        return SharedSecretProvider(secret_reader)
    if raw == _OIDC:
        return OidcProvider(_oidc_settings_from_env(env))
    raise IdentityError("invalid oidc configuration: unknown identity provider")


#: Bound on the ``kid`` echoed to the security log. The value is attacker-chosen
#: text, so it is truncated rather than allowed to bloat every rejection line.
_LOG_KID_MAX_CHARS = 64


def _log_safe_kid(kid: object) -> str:
    """Return a printable, bounded rendering of an unverified header ``kid``.

    ``kid`` comes from a token nobody has verified yet, so it is untrusted text.
    Raw CR/LF or ANSI escapes would let an unauthenticated caller forge a second
    audit line - a fake "rejected" record, or padding that hides the real attack
    sequence (CWE-117). Non-printables are dropped, not escaped, so the emitted
    message is always one line; the result is bounded and rendered with ``%r``
    so a value that had to be altered is visible as a quoted string.
    """
    if not isinstance(kid, str):
        return ""
    return "".join(ch for ch in kid if ch.isprintable())[:_LOG_KID_MAX_CHARS]


def _log_rejection(settings: OidcSettings, kid: object, reason: str) -> None:
    """Record a rejected verification: reason token, kid and issuer only.

    Never the token, the key material or a claims dump - an exception string
    and a log line both end up somewhere durable. ``kid`` is sanitized here, at
    the single point where an untrusted header value reaches a logger, so no
    future caller can reintroduce the injection by passing it raw.
    """
    logger.warning(
        "oidc verification rejected: reason=%r kid=%r issuer=%r",
        reason,
        _log_safe_kid(kid),
        settings.issuer,
    )
