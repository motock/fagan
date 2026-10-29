"""Identity types and the shared-secret provider for the tenancy seam (ADR 0009).

This module is deliberately free-standing: it imports nothing from ``app.*``
or ``pipeline.*`` so that both the dashboard and the service layer can depend
on it without creating an import cycle. It holds the identity data types, the
``IdentityProvider`` protocol and the local shared-secret provider — no
policy, no wiring and no enforcement.
"""

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

ROLE_AUTHOR = "author"
ROLE_OWNER = "owner"
ROLE_APPROVER = "approver"
ROLE_DELEGATE = "delegate"
ROLE_ADMIN = "admin"

#: The roles defined by ADR 0009. Any other role is rejected.
ROLES = frozenset(
    {ROLE_AUTHOR, ROLE_OWNER, ROLE_APPROVER, ROLE_DELEGATE, ROLE_ADMIN}
)


class IdentityError(Exception):
    """Raised for every rejected principal or credential.

    Messages name the failure kind (``subject``/``role``/``scope``) and never
    echo the offending credential or scope value.
    """


@dataclass(frozen=True)
class Principal:
    """An authenticated caller: who they are, what they may do, how they proved it."""

    subject: str
    roles: frozenset[str] = frozenset()
    scopes: tuple[str, ...] = ()
    email: str = ""
    auth_method: str = ""
    correlation_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.subject, str) or not self.subject.strip():
            raise IdentityError("invalid subject: must be a non-empty string")

        for role in self.roles:
            if not isinstance(role, str) or role not in ROLES:
                raise IdentityError("invalid role: not a role defined by ADR 0009")

        for scope in self.scopes:
            if not isinstance(scope, str) or not scope:
                raise IdentityError("invalid scope: must be a non-empty string")


@runtime_checkable
class IdentityProvider(Protocol):
    """The authentication seam: turn a credential into a ``Principal``.

    Implementations raise ``IdentityError`` for every rejected credential and
    never leak the credential or the expected secret in the message.
    """

    def authenticate(self, credential: str | None) -> Principal:
        """Return the principal for ``credential`` or raise ``IdentityError``."""
        ...


#: Today's single-operator identity. ADR 0009 section 7 defines local mode as
#: this principal holding the full action set.
LOCAL_OPERATOR = Principal(
    subject="local-operator",
    roles=ROLES,
    scopes=("*",),
    auth_method="shared-secret",
)


class SharedSecretProvider:
    """Authenticate a shared secret against a fixed or lazily-resolved value.

    ``expected_secret`` may be a callable so the dashboard can pass
    ``app.auth.get_or_create_api_key``: the key is created or rotated lazily,
    so the callable is consulted on every ``authenticate`` and never cached.
    """

    def __init__(self, expected_secret: str | Callable[[], str]) -> None:
        self._expected_secret = expected_secret

    def authenticate(self, credential: str | None) -> Principal:
        """Return ``LOCAL_OPERATOR`` when the credential matches, else deny.

        Comparison is constant-time over UTF-8 bytes. Encoding both sides is
        load-bearing: ``secrets.compare_digest`` raises ``TypeError`` on a
        non-ASCII ``str``, and an HTTP header can carry latin-1 bytes, so a
        raw ``str`` comparison would surface as a 500 instead of a denial.
        """
        if not credential:
            raise IdentityError("missing credential")

        expected = self._expected_secret
        if callable(expected):
            expected = expected()

        if not isinstance(expected, str) or not expected:
            raise IdentityError("invalid credential")

        if not secrets.compare_digest(
            credential.encode("utf-8"), expected.encode("utf-8")
        ):
            raise IdentityError("invalid credential")

        return LOCAL_OPERATOR


#: The ``system:`` prefix is reserved for the non-human principals this module
#: defines. No identity provider may mint one: ``app/oidc.py`` refuses a token
#: whose ``sub`` claims the prefix, so a caller can never authenticate as the
#: scheduler or a worker (the subjects stay unforgeable by construction).
SYSTEM_SUBJECT_PREFIX = "system:"

#: The auth method of a principal the deployment itself acts as, rather than
#: one a caller proved. Paired with the reserved prefix above.
SYSTEM_AUTH_METHOD = "system"

#: The scheduler daemon's own identity: it dispatches, merges, marks done and
#: interrupts on the tick, and nothing else (see ``app.policy`` for the grants).
SYSTEM_SCHEDULER = Principal(
    subject="system:scheduler", auth_method=SYSTEM_AUTH_METHOD
)

#: A dispatched worker's own identity: it patches its own story, and nothing
#: else (see ``app.policy`` for the grants).
SYSTEM_WORKER = Principal(subject="system:worker", auth_method=SYSTEM_AUTH_METHOD)
