"""The instance scope refuses a namespace the instance does not serve.

``PIPELINE_SCHEDULER_NAMESPACES_ONLY`` and ``PIPELINE_SCHEDULER_NAMESPACES_SKIP``
bound which namespaces one instance serves, not only what its scheduler
advances. ``verify_namespace`` applies the scope to every request, read from
the environment on each call. A scope the environment does not name changes
nothing; a malformed scope denies every request, failing closed.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse

import pipeline.namespace_access as access_module
from app.identity import ROLE_AUTHOR, Principal
from app.plugin_loader import ALLOWLIST_ENV
from pipeline.namespace_access import (
    NAMESPACE_ERROR,
    serve_http_in_namespace,
    verify_namespace,
)
from pipeline.namespace_resolver import (
    NAMESPACE_RESOLVER_PLUGIN_ENV,
    NamespaceAccess,
)
from pipeline.scheduler_scope import ONLY_VAR, SKIP_VAR

#: The reason token for a request outside the instance scope. Pinned here as a
#: literal so the wire value is part of the contract, not an echo of the constant.
NAMESPACE_OUT_OF_SCOPE = "namespace_out_of_scope"

ALICE = Principal(
    subject="alice",
    roles=frozenset({ROLE_AUTHOR}),
    correlation_id="corr-scope-1",
)


class _Resolver:
    """Resolver double that grants every namespace it was built with."""

    def __init__(self, *namespaces: str) -> None:
        self._accesses = tuple(
            NamespaceAccess(namespace, frozenset({ROLE_AUTHOR}))
            for namespace in namespaces
        )

    def accesses(self, principal):
        return self._accesses


@pytest.fixture(autouse=True)
def audit_records(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture host audit appends instead of writing the operator's chain."""
    monkeypatch.delenv(NAMESPACE_RESOLVER_PLUGIN_ENV, raising=False)
    monkeypatch.delenv(ALLOWLIST_ENV, raising=False)
    monkeypatch.delenv(ONLY_VAR, raising=False)
    monkeypatch.delenv(SKIP_VAR, raising=False)
    records: list[dict] = []
    monkeypatch.setattr(
        access_module, "append_host_audit", lambda **kwargs: records.append(kwargs)
    )
    return records


def _grants_all() -> _Resolver:
    return _Resolver("alpha", "beta", "gamma")


# --- an unset scope changes nothing -------------------------------------------


def test_unset_scope_admits_a_granted_namespace() -> None:
    decision = verify_namespace(ALICE, "gamma", _grants_all())
    assert decision.allowed is True
    assert decision.namespace == "gamma"


def test_unset_scope_admits_a_single_access_with_no_header() -> None:
    decision = verify_namespace(ALICE, "", _Resolver("gamma"))
    assert decision.allowed is True
    assert decision.namespace == "gamma"


def test_empty_scope_variables_are_the_same_as_unset(
    monkeypatch, audit_records
) -> None:
    monkeypatch.setenv(ONLY_VAR, "")
    monkeypatch.setenv(SKIP_VAR, "")
    decision = verify_namespace(ALICE, "gamma", _grants_all())
    assert decision.allowed is True
    assert audit_records == []


# --- ONLY admits exactly its members -------------------------------------------


def test_only_admits_a_listed_namespace(monkeypatch) -> None:
    monkeypatch.setenv(ONLY_VAR, "alpha,beta")
    decision = verify_namespace(ALICE, "beta", _grants_all())
    assert decision.allowed is True
    assert decision.namespace == "beta"


def test_only_refuses_an_unlisted_namespace_even_when_the_resolver_grants_it(
    monkeypatch, audit_records
) -> None:
    monkeypatch.setenv(ONLY_VAR, "alpha,beta")
    decision = verify_namespace(ALICE, "gamma", _grants_all())
    assert decision.allowed is False
    assert decision.reason == NAMESPACE_OUT_OF_SCOPE
    assert audit_records[0]["detail"]["reason"] == NAMESPACE_OUT_OF_SCOPE


def test_only_refuses_a_single_out_of_scope_access_with_no_header(monkeypatch) -> None:
    monkeypatch.setenv(ONLY_VAR, "alpha")
    decision = verify_namespace(ALICE, "", _Resolver("gamma"))
    assert decision.allowed is False
    assert decision.reason == NAMESPACE_OUT_OF_SCOPE


def test_only_admits_a_single_in_scope_access_with_no_header(monkeypatch) -> None:
    monkeypatch.setenv(ONLY_VAR, "alpha")
    decision = verify_namespace(ALICE, "", _Resolver("alpha"))
    assert decision.allowed is True
    assert decision.namespace == "alpha"


def test_only_denial_names_no_namespace_in_the_decision(monkeypatch) -> None:
    monkeypatch.setenv(ONLY_VAR, "alpha")
    decision = verify_namespace(ALICE, "gamma", _grants_all())
    assert decision.namespace == ""
    assert decision.roles == frozenset()


# --- SKIP excludes its members -------------------------------------------------


def test_skip_refuses_a_listed_namespace(monkeypatch) -> None:
    monkeypatch.setenv(SKIP_VAR, "alpha,beta")
    decision = verify_namespace(ALICE, "alpha", _grants_all())
    assert decision.allowed is False
    assert decision.reason == NAMESPACE_OUT_OF_SCOPE


def test_skip_admits_an_unlisted_namespace(monkeypatch) -> None:
    monkeypatch.setenv(SKIP_VAR, "alpha,beta")
    decision = verify_namespace(ALICE, "gamma", _grants_all())
    assert decision.allowed is True
    assert decision.namespace == "gamma"


# --- a malformed or ambiguous scope denies every request -----------------------


@pytest.mark.parametrize("requested", ["alpha", "gamma", ""])
def test_both_scope_variables_set_denies_every_request(monkeypatch, requested) -> None:
    monkeypatch.setenv(ONLY_VAR, "alpha")
    monkeypatch.setenv(SKIP_VAR, "beta")
    decision = verify_namespace(ALICE, requested, _grants_all())
    assert decision.allowed is False
    assert decision.reason == NAMESPACE_ERROR


@pytest.mark.parametrize("value", ["alpha,,beta", "alpha,", "Not A Namespace", "a/b/c"])
def test_malformed_only_entry_denies_every_request(monkeypatch, value) -> None:
    monkeypatch.setenv(ONLY_VAR, value)
    decision = verify_namespace(ALICE, "alpha", _grants_all())
    assert decision.allowed is False
    assert decision.reason == NAMESPACE_ERROR


@pytest.mark.parametrize("value", ["alpha,,beta", "Not A Namespace"])
def test_malformed_skip_entry_denies_every_request(monkeypatch, value) -> None:
    monkeypatch.setenv(SKIP_VAR, value)
    decision = verify_namespace(ALICE, "gamma", _grants_all())
    assert decision.allowed is False
    assert decision.reason == NAMESPACE_ERROR


def test_malformed_scope_denies_a_request_with_no_header(monkeypatch) -> None:
    monkeypatch.setenv(ONLY_VAR, "alpha,,beta")
    decision = verify_namespace(ALICE, "", _Resolver("alpha"))
    assert decision.allowed is False
    assert decision.reason == NAMESPACE_ERROR


def test_malformed_scope_is_audited_with_the_reason_token_only(
    monkeypatch, audit_records
) -> None:
    monkeypatch.setenv(ONLY_VAR, "alpha,,beta")
    verify_namespace(ALICE, "alpha", _grants_all())
    assert len(audit_records) == 1
    assert audit_records[0]["detail"]["reason"] == NAMESPACE_ERROR


def test_malformed_scope_logs_only_the_reason_token(monkeypatch, caplog) -> None:
    secret_entry = "Secret Namespace Value"
    monkeypatch.setenv(ONLY_VAR, f"alpha,{secret_entry}")
    with caplog.at_level(logging.DEBUG):
        verify_namespace(ALICE, "alpha", _grants_all())
    assert NAMESPACE_ERROR in caplog.text
    assert secret_entry not in caplog.text
    assert "alpha" not in caplog.text


# --- the audit record and the log line never echo the namespace ----------------


def test_out_of_scope_denial_writes_one_audit_record(
    monkeypatch, audit_records
) -> None:
    monkeypatch.setenv(ONLY_VAR, "alpha")
    verify_namespace(ALICE, "gamma", _grants_all())
    assert len(audit_records) == 1
    record = audit_records[0]
    assert record["action"] == "namespace:verify"
    assert record["outcome"] == "denied"
    assert record["detail"] == {
        "subject": "alice",
        "reason": NAMESPACE_OUT_OF_SCOPE,
        "requested": "gamma",
        "correlation_id": "corr-scope-1",
    }


def test_out_of_scope_denial_logs_nothing_naming_the_namespace(
    monkeypatch, caplog
) -> None:
    monkeypatch.setenv(ONLY_VAR, "alpha")
    with caplog.at_level(logging.DEBUG):
        decision = verify_namespace(ALICE, "gamma", _grants_all())
    assert decision.reason == NAMESPACE_OUT_OF_SCOPE
    assert "gamma" not in caplog.text


# --- the scope is read on every call, so a change applies without a restart -----


def test_environment_change_between_calls_is_honoured(monkeypatch) -> None:
    resolver = _grants_all()
    assert verify_namespace(ALICE, "gamma", resolver).allowed is True

    monkeypatch.setenv(ONLY_VAR, "alpha")
    assert verify_namespace(ALICE, "gamma", resolver).reason == NAMESPACE_OUT_OF_SCOPE

    monkeypatch.delenv(ONLY_VAR)
    assert verify_namespace(ALICE, "gamma", resolver).allowed is True


def test_scope_change_to_malformed_takes_effect_on_the_next_call(monkeypatch) -> None:
    resolver = _grants_all()
    monkeypatch.setenv(SKIP_VAR, "beta")
    assert verify_namespace(ALICE, "gamma", resolver).allowed is True

    monkeypatch.setenv(SKIP_VAR, "beta,,")
    assert verify_namespace(ALICE, "gamma", resolver).reason == NAMESPACE_ERROR


# --- the 403 body for a scoped denial carries only the token and correlation id --


def _http_request(namespace: str | None) -> Request:
    headers = []
    if namespace is not None:
        headers.append((b"x-fagan-namespace", namespace.encode("utf-8")))
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


async def _call_next(request: Request) -> PlainTextResponse:
    return PlainTextResponse("served")


@pytest.fixture
def granted_resolver(monkeypatch):
    resolver = _grants_all()
    monkeypatch.setattr(access_module, "active_namespace_resolver", lambda: resolver)
    return resolver


def test_out_of_scope_http_request_is_refused_with_the_reason_and_correlation(
    monkeypatch, granted_resolver
) -> None:
    monkeypatch.setenv(ONLY_VAR, "alpha")
    response = asyncio.run(
        serve_http_in_namespace(
            ALICE, _http_request("gamma"), "corr-http-1", _call_next
        )
    )
    assert response.status_code == 403
    body = json.loads(response.body)
    assert body == {"detail": NAMESPACE_OUT_OF_SCOPE, "correlation_id": "corr-http-1"}
    assert b"gamma" not in response.body


def test_in_scope_http_request_is_served(monkeypatch, granted_resolver) -> None:
    monkeypatch.setenv(ONLY_VAR, "alpha")
    response = asyncio.run(
        serve_http_in_namespace(
            ALICE, _http_request("alpha"), "corr-http-2", _call_next
        )
    )
    assert response.status_code == 200
    assert response.body == b"served"


def test_malformed_scope_http_request_is_refused_with_namespace_error(
    monkeypatch, granted_resolver
) -> None:
    monkeypatch.setenv(ONLY_VAR, "alpha,,beta")
    response = asyncio.run(
        serve_http_in_namespace(
            ALICE, _http_request("alpha"), "corr-http-3", _call_next
        )
    )
    assert response.status_code == 403
    assert json.loads(response.body) == {
        "detail": NAMESPACE_ERROR,
        "correlation_id": "corr-http-3",
    }
