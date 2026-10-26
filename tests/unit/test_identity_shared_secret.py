"""Unit tests for the shared-secret identity provider (ADR 0009).

Covers the provider half of the tenancy seam in ``app.identity``: the
``IdentityProvider`` protocol, the ``LOCAL_OPERATOR`` principal that defines
local single-operator mode, and ``SharedSecretProvider``'s credential checks.

The security-relevant properties pinned here:

* a missing, empty, wrong, one-character-off or case-different credential is
  denied with ``IdentityError``;
* comparison happens on UTF-8 bytes via ``secrets.compare_digest`` so a
  non-ASCII credential is denied rather than raising ``TypeError`` (which
  would surface as a 500 instead of a denial);
* a callable expected secret is consulted on every call, never cached;
* neither the presented credential nor the expected secret leaks into the
  exception message.
"""

import pytest

from app.identity import (
    LOCAL_OPERATOR,
    ROLES,
    IdentityError,
    IdentityProvider,
    Principal,
    SharedSecretProvider,
)

SECRET = "s3cr3t"


# ---------------------------------------------------------------------------
# The protocol seam
# ---------------------------------------------------------------------------


def test_identity_provider_is_runtime_checkable():
    # runtime_checkable protocols support isinstance() checks.
    assert isinstance(SharedSecretProvider(SECRET), IdentityProvider)


def test_shared_secret_provider_satisfies_identity_provider():
    assert isinstance(SharedSecretProvider("x"), IdentityProvider) is True


def test_principal_is_not_an_identity_provider():
    assert isinstance(Principal("alice"), IdentityProvider) is False


# ---------------------------------------------------------------------------
# LOCAL_OPERATOR: today's single-operator identity
# ---------------------------------------------------------------------------


def test_local_operator_holds_the_full_role_set_and_wildcard_scope():
    assert LOCAL_OPERATOR.subject == "local-operator"
    assert LOCAL_OPERATOR.roles == ROLES
    assert LOCAL_OPERATOR.scopes == ("*",)
    assert LOCAL_OPERATOR.auth_method == "shared-secret"


def test_local_operator_is_a_principal():
    assert isinstance(LOCAL_OPERATOR, Principal)


# ---------------------------------------------------------------------------
# Matching credentials
# ---------------------------------------------------------------------------


def test_matching_secret_returns_local_operator():
    provider = SharedSecretProvider(SECRET)

    assert provider.authenticate(SECRET) == LOCAL_OPERATOR


def test_matching_secret_returns_the_shared_local_operator_instance():
    provider = SharedSecretProvider(SECRET)

    assert provider.authenticate(SECRET) is LOCAL_OPERATOR


# ---------------------------------------------------------------------------
# Denials
# ---------------------------------------------------------------------------


def test_missing_credential_is_rejected():
    provider = SharedSecretProvider(SECRET)

    with pytest.raises(IdentityError):
        provider.authenticate(None)


def test_empty_credential_is_rejected():
    provider = SharedSecretProvider(SECRET)

    with pytest.raises(IdentityError):
        provider.authenticate("")


def test_wrong_credential_is_rejected():
    provider = SharedSecretProvider(SECRET)

    with pytest.raises(IdentityError):
        provider.authenticate("wrong")


def test_one_character_off_credential_is_rejected():
    provider = SharedSecretProvider(SECRET)

    with pytest.raises(IdentityError):
        provider.authenticate("s3cr3t!")


def test_credential_differing_only_in_case_is_rejected():
    provider = SharedSecretProvider(SECRET)

    with pytest.raises(IdentityError):
        provider.authenticate("S3CR3T")


def test_prefix_of_the_expected_secret_is_rejected():
    provider = SharedSecretProvider(SECRET)

    with pytest.raises(IdentityError):
        provider.authenticate("s3cr")


# ---------------------------------------------------------------------------
# Security and boundary cases
# ---------------------------------------------------------------------------


def test_non_ascii_credential_is_denied_not_type_error():
    provider = SharedSecretProvider("key")

    # secrets.compare_digest raises TypeError on a non-ASCII str, so the
    # implementation must encode to bytes first; a TypeError here would be a
    # 500 rather than a denial.
    with pytest.raises(IdentityError):
        provider.authenticate("k\u00e9y")


def test_non_ascii_expected_secret_can_be_matched():
    provider = SharedSecretProvider("k\u00e9y")

    assert provider.authenticate("k\u00e9y") == LOCAL_OPERATOR


def test_4096_character_secret_with_matching_credential_is_accepted():
    long_secret = "a" * 4096
    provider = SharedSecretProvider(long_secret)

    assert provider.authenticate(long_secret) == LOCAL_OPERATOR


def test_4096_character_secret_with_one_character_off_is_rejected():
    provider = SharedSecretProvider("a" * 4096)

    with pytest.raises(IdentityError):
        provider.authenticate("a" * 4095 + "b")


def test_error_message_leaks_neither_credential_nor_expected_secret():
    provider = SharedSecretProvider(SECRET)

    with pytest.raises(IdentityError) as excinfo:
        provider.authenticate("s3cr3t!")

    message = str(excinfo.value)
    assert SECRET not in message
    assert "s3cr3t!" not in message


def test_error_message_for_missing_credential_leaks_the_expected_secret():
    provider = SharedSecretProvider(SECRET)

    with pytest.raises(IdentityError) as excinfo:
        provider.authenticate(None)

    assert SECRET not in str(excinfo.value)


# ---------------------------------------------------------------------------
# Callable expected secret: resolved per call, never cached
# ---------------------------------------------------------------------------


def test_callable_secret_is_consulted_on_every_authenticate():
    calls = []
    current = {"secret": "alpha"}

    def expected_secret() -> str:
        calls.append(current["secret"])
        return current["secret"]

    provider = SharedSecretProvider(expected_secret)
    assert calls == []  # never called at construction time

    assert provider.authenticate("alpha") == LOCAL_OPERATOR
    assert len(calls) == 1

    # The dashboard rotates its key lazily; the provider must honour the new
    # value on the next call rather than caching the first result.
    current["secret"] = "bravo"

    with pytest.raises(IdentityError):
        provider.authenticate("alpha")

    assert provider.authenticate("bravo") == LOCAL_OPERATOR
    assert len(calls) == 3


def test_callable_secret_returning_empty_string_denies_everything():
    provider = SharedSecretProvider(lambda: "")

    with pytest.raises(IdentityError):
        provider.authenticate("")
