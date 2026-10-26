"""Unit tests for the tenancy identity seam's data types (ADR 0009).

Covers only the free-standing data types in ``app.identity``: the role
constants, the immutable ``Principal`` value object and its validation
contract. No provider, policy or wiring behaviour is exercised here.
"""

import dataclasses

import pytest

from app.identity import (
    ROLE_ADMIN,
    ROLE_APPROVER,
    ROLE_AUTHOR,
    ROLE_DELEGATE,
    ROLE_OWNER,
    ROLES,
    IdentityError,
    Principal,
)


def test_valid_principal_exposes_fields_and_is_hashable():
    principal = Principal(
        subject="alice",
        roles=frozenset({ROLE_AUTHOR, ROLE_APPROVER}),
        scopes=("read", "write"),
        email="alice@example.com",
        auth_method="oidc",
        correlation_id="corr-123",
    )

    assert principal.subject == "alice"
    assert principal.roles == frozenset({ROLE_AUTHOR, ROLE_APPROVER})
    assert principal.scopes == ("read", "write")
    assert principal.email == "alice@example.com"
    assert principal.auth_method == "oidc"
    assert principal.correlation_id == "corr-123"

    assert ROLES == frozenset(
        {ROLE_AUTHOR, ROLE_OWNER, ROLE_APPROVER, ROLE_DELEGATE, ROLE_ADMIN}
    )

    # Hashable: usable as a dict key and as a set member.
    assert {principal: "value"}[principal] == "value"
    assert principal in {principal}


def test_principal_is_immutable():
    principal = Principal("alice")

    with pytest.raises(dataclasses.FrozenInstanceError):
        principal.subject = "x"

    assert principal.subject == "alice"


def test_identity_error_for_empty_subject():
    with pytest.raises(IdentityError) as excinfo:
        Principal("")

    assert "subject" in str(excinfo.value)


def test_identity_error_for_whitespace_subject():
    with pytest.raises(IdentityError) as excinfo:
        Principal("   ")

    assert "subject" in str(excinfo.value)


def test_identity_error_for_none_subject():
    with pytest.raises(IdentityError) as excinfo:
        Principal(None)

    assert "subject" in str(excinfo.value)


def test_identity_error_for_unknown_role():
    with pytest.raises(IdentityError) as excinfo:
        Principal("alice", roles=frozenset({"superuser"}))

    message = str(excinfo.value)
    assert "role" in message
    assert "superuser" not in message


def test_identity_error_for_empty_scope():
    with pytest.raises(IdentityError) as excinfo:
        Principal("alice", scopes=("",))

    assert "scope" in str(excinfo.value)


def test_identity_error_for_non_string_scope():
    with pytest.raises(IdentityError) as excinfo:
        Principal("alice", scopes=(5,))

    message = str(excinfo.value)
    assert "scope" in message
    assert "5" not in message


def test_default_roles_and_scopes_are_independent_per_instance():
    a = Principal("alice")
    b = Principal("bob")

    # Deriving a new value from a's defaults must not mutate a's defaults...
    assert a.roles | frozenset({ROLE_ADMIN}) == frozenset({ROLE_ADMIN})
    assert a.roles == frozenset()
    assert b.roles == frozenset()

    assert a.scopes + ("read",) == ("read",)
    assert a.scopes == ()
    assert b.scopes == ()

    # ...and a later instance must still see pristine defaults.
    c = Principal("carol")
    assert c.roles == frozenset()
    assert c.scopes == ()


def test_subject_at_200_characters_is_accepted():
    subject = "a" * 200

    principal = Principal(subject)

    assert principal.subject == subject
    assert len(principal.subject) == 200
