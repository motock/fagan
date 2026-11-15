"""enter_request_namespace verifies the requested namespace, then binds it.

The context manager is the single entry point a transport uses: a verified
namespace is bound for the life of the request and restored afterwards, and a
denial raises NamespaceDenied before the request body runs.
"""

from __future__ import annotations

import pytest

import pipeline.namespace_access as access_module
from app.identity import ROLE_AUTHOR, Principal
from pipeline.actor import bind_namespace, current_namespace
from pipeline.namespace_access import (
    NAMESPACE_MALFORMED,
    NAMESPACE_MISSING,
    NAMESPACE_NOT_PERMITTED,
    NamespaceDenied,
    enter_request_namespace,
)
from pipeline.namespace_resolver import NamespaceAccess
from pipeline.store_records import DEFAULT_NAMESPACE

ALICE = Principal(
    subject="alice",
    roles=frozenset({ROLE_AUTHOR}),
    correlation_id="corr-enter-1",
)


class _StubResolver:
    """Resolver double returning a fixed tuple of accesses."""

    def __init__(self, *accesses: NamespaceAccess) -> None:
        self._accesses = accesses

    def accesses(self, principal):
        return self._accesses


@pytest.fixture
def audit_calls(monkeypatch):
    """Capture host audit appends without touching the real chain."""
    calls: list[dict] = []
    monkeypatch.setattr(
        access_module, "append_host_audit", lambda **kwargs: calls.append(kwargs)
    )
    return calls


def _use_resolver(monkeypatch, resolver) -> None:
    monkeypatch.setattr(access_module, "active_namespace_resolver", lambda: resolver)


# Positive: the unchanged path with no plugin configured.


def test_absent_header_with_default_resolver_binds_the_default_namespace():
    with enter_request_namespace(ALICE, None):
        assert current_namespace() == DEFAULT_NAMESPACE


def test_default_resolver_namespace_is_restored_after_the_request():
    with bind_namespace("outer-ns"):
        with enter_request_namespace(ALICE, None):
            pass
        assert current_namespace() == "outer-ns"


def test_default_resolver_writes_no_audit_record_on_success(audit_calls):
    with enter_request_namespace(ALICE, None):
        pass
    assert audit_calls == []


def test_listed_namespace_is_bound_for_the_request(monkeypatch):
    _use_resolver(
        monkeypatch,
        _StubResolver(
            NamespaceAccess("alpha", frozenset({ROLE_AUTHOR})),
            NamespaceAccess("beta", frozenset({ROLE_AUTHOR})),
        ),
    )
    with enter_request_namespace(ALICE, "beta"):
        assert current_namespace() == "beta"


# Negative: each denial raises with its reason token and binds nothing.


def test_two_accesses_and_no_header_is_namespace_missing(monkeypatch, audit_calls):
    _use_resolver(
        monkeypatch,
        _StubResolver(
            NamespaceAccess("alpha", frozenset({ROLE_AUTHOR})),
            NamespaceAccess("beta", frozenset({ROLE_AUTHOR})),
        ),
    )
    with pytest.raises(NamespaceDenied) as raised, enter_request_namespace(ALICE, None):
        pytest.fail("body must not run after a denial")
    assert raised.value.reason == NAMESPACE_MISSING
    assert len(audit_calls) == 1


def test_unlisted_namespace_is_not_permitted_and_audited(monkeypatch, audit_calls):
    _use_resolver(
        monkeypatch,
        _StubResolver(NamespaceAccess("alpha", frozenset({ROLE_AUTHOR}))),
    )
    with pytest.raises(NamespaceDenied) as raised, enter_request_namespace(ALICE, "gamma"):
        pytest.fail("body must not run after a denial")
    assert raised.value.reason == NAMESPACE_NOT_PERMITTED
    assert len(audit_calls) == 1
    assert audit_calls[0]["detail"]["reason"] == NAMESPACE_NOT_PERMITTED


def test_malformed_namespace_is_refused_before_the_resolver(monkeypatch):
    def _unreachable():
        pytest.fail("the resolver must not be consulted for a malformed value")

    monkeypatch.setattr(access_module, "active_namespace_resolver", _unreachable)
    with pytest.raises(NamespaceDenied) as raised, enter_request_namespace(
        ALICE, "Not A Namespace!"
    ):
        pytest.fail("body must not run after a denial")
    assert raised.value.reason == NAMESPACE_MALFORMED


def test_denial_leaves_the_previously_bound_namespace_unchanged(monkeypatch):
    _use_resolver(
        monkeypatch,
        _StubResolver(NamespaceAccess("alpha", frozenset({ROLE_AUTHOR}))),
    )
    with bind_namespace("outer-ns"):
        with pytest.raises(NamespaceDenied), enter_request_namespace(ALICE, "gamma"):
            pass
        assert current_namespace() == "outer-ns"
