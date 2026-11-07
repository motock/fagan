"""Identity types and the shared-secret provider for the tenancy seam (ADR 0009).

This module is deliberately free-standing: it imports nothing from ``app.*``
or ``pipeline.*`` so that both the dashboard and the service layer can depend
on it without creating an import cycle. It holds the identity data types, the
``IdentityProvider`` protocol and the local shared-secret provider — no
policy, no wiring and no enforcement.
"""

import re
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


#: The principal kinds ADR 0014 defines. ``human`` is the default so every
#: pre-existing construction keeps its meaning; ``agent`` is a machine caller
#: bound to one team; ``system`` is the deployment acting as itself.
PRINCIPAL_TYPES = frozenset({"human", "agent", "system"})

#: An agent's ``team_scope`` is an ``<org-id>/<team-id>`` pair. Both segments
#: start with a letter or digit and continue with letters, digits, ``.``, ``_``
#: or ``-``. ``fullmatch`` is load-bearing: ``re.match`` with a trailing ``$``
#: would accept ``"acme/team-1\n"``, because ``$`` also matches before a final
#: newline.
TEAM_SCOPE_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*")


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

    #: ``human`` | ``agent`` | ``system``. Defaulted so every pre-existing
    #: construction is unchanged and still means "a human caller".
    principal_type: str = "human"
    #: The OIDC client (``azp``) the caller authenticated as, for attribution.
    client_id: str = ""
    #: An agent's ``<org-id>/<team-id>`` binding. Empty for non-agents.
    team_scope: str = ""

    def __post_init__(self) -> None:
        if self.principal_type not in PRINCIPAL_TYPES:
            raise IdentityError("invalid principal type")

        # ADR 0014 decision 3: an agent is only ever scoped to one team, and
        # the scope comes from the signed token or operator config. Messages
        # never echo the scope value.
        if self.principal_type == "agent":
            if not isinstance(self.team_scope, str) or not TEAM_SCOPE_PATTERN.fullmatch(
                self.team_scope
            ):
                raise IdentityError(
                    "invalid team scope: must be an <org-id>/<team-id> pair"
                )
        elif self.team_scope:
            raise IdentityError("invalid team scope: only an agent may carry one")

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
    subject="system:scheduler", auth_method=SYSTEM_AUTH_METHOD, principal_type="system"
)

#: A dispatched worker's own identity: it patches its own story, and nothing
#: else (see ``app.policy`` for the grants).
SYSTEM_WORKER = Principal(
    subject="system:worker", auth_method=SYSTEM_AUTH_METHOD, principal_type="system"
)
