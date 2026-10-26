"""Identity data types for the tenancy seam (ADR 0009).

This module is deliberately free-standing: it imports nothing from ``app.*``
or ``pipeline.*`` so that both the dashboard and the service layer can depend
on it without creating an import cycle. It holds data types only — no
provider, no policy, no wiring and no enforcement.
"""

from dataclasses import dataclass

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
