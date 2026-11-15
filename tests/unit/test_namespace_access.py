"""Verify the requested namespace for a principal, and audit every denial.

``verify_namespace`` answers one question before any namespace-scoped work
runs: may this principal reach the namespace it asked for? A denial returns an
explicit :class:`NamespaceDecision` with a reason token and appends exactly one
host audit record. A successful decision writes nothing. An audit failure never
turns a denial into an allow.
"""

from __future__ import annotations

import hashlib
import json

import pytest

import pipeline.namespace_access as access_module
import pipeline.namespace_resolver as resolver_module
from app.identity import ROLE_ADMIN, ROLE_AUTHOR, Principal
from app.plugin_loader import ALLOWLIST_ENV
from pipeline.namespace_access import (
    NAMESPACE_ERROR,
    NAMESPACE_MALFORMED,
    NAMESPACE_MISSING,
    NAMESPACE_NOT_PERMITTED,
    NamespaceDecision,
    verify_namespace,
)
from pipeline.namespace_resolver import (
    NAMESPACE_RESOLVER_PLUGIN_ENV,
    DefaultNamespaceResolver,
    NamespaceAccess,
)
from pipeline.store_records import DEFAULT_NAMESPACE

ALICE = Principal(
    subject="alice",
    roles=frozenset({ROLE_AUTHOR}),
    correlation_id="corr-1",
)
AGENT = Principal(
    subject="bot-1",
    roles=frozenset({ROLE_AUTHOR}),
    principal_type="agent",
    team_scope="acme/platform",
    correlation_id="corr-2",
)


class _Resolver:
    """Resolver double returning a fixed answer, or raising it, and counting calls."""

    def __init__(self, answer=None, error: Exception | None = None) -> None:
        self._answer = answer
        self._error = error
        self.calls = 0

    def accesses(self, principal):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._answer


def _accesses(*namespaces: str) -> tuple[NamespaceAccess, ...]:
    return tuple(
        NamespaceAccess(namespace, frozenset({ROLE_AUTHOR})) for namespace in namespaces
    )


@pytest.fixture(autouse=True)
def audit_recorder(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture every host audit append instead of writing the operator's chain."""
    monkeypatch.delenv(NAMESPACE_RESOLVER_PLUGIN_ENV, raising=False)
    monkeypatch.delenv(ALLOWLIST_ENV, raising=False)
    resolver_module._PLUGIN_RESOLVERS.clear()
    records: list[dict] = []

    def fake_append(**kwargs):
        records.append(kwargs)
        return kwargs

    monkeypatch.setattr(access_module, "append_host_audit", fake_append)
    yield records
    resolver_module._PLUGIN_RESOLVERS.clear()


# --- allowed decisions -------------------------------------------------------


def test_single_access_with_no_requested_namespace_is_allowed() -> None:
    decision = verify_namespace(ALICE, "", _Resolver(_accesses("acme")))
    assert decision == NamespaceDecision(True, "acme", frozenset({ROLE_AUTHOR}), "")


def test_single_access_with_no_requested_namespace_passes_none_too() -> None:
    decision = verify_namespace(ALICE, None, _Resolver(_accesses("acme")))
    assert decision.allowed is True
    assert decision.namespace == "acme"


def test_requested_namespace_among_several_accesses_is_allowed() -> None:
    resolver = _Resolver(_accesses("acme", "globex"))
    decision = verify_namespace(ALICE, "globex", resolver)
    assert decision.allowed is True
    assert decision.namespace == "globex"


def test_requested_two_segment_namespace_is_allowed() -> None:
    decision = verify_namespace(ALICE, "acme/platform", _Resolver(_accesses("acme/platform")))
    assert decision.allowed is True
    assert decision.namespace == "acme/platform"


def test_allowed_decision_carries_the_roles_of_that_access() -> None:
    resolver = _Resolver(
        (NamespaceAccess("acme", frozenset({ROLE_ADMIN})),)
    )
    decision = verify_namespace(ALICE, "acme", resolver)
    assert decision.roles == frozenset({ROLE_ADMIN})


def test_allowed_decision_writes_no_audit_record(audit_recorder) -> None:
    verify_namespace(ALICE, "", _Resolver(_accesses("acme")))
    assert audit_recorder == []


# --- denials: each reason token ----------------------------------------------


def test_several_accesses_with_no_requested_namespace_is_missing() -> None:
    decision = verify_namespace(ALICE, "", _Resolver(_accesses("acme", "globex")))
    assert decision.allowed is False
    assert decision.reason == NAMESPACE_MISSING


def test_requested_namespace_not_among_accesses_is_not_permitted() -> None:
    decision = verify_namespace(ALICE, "initech", _Resolver(_accesses("acme")))
    assert decision.allowed is False
    assert decision.reason == NAMESPACE_NOT_PERMITTED


@pytest.mark.parametrize(
    "requested",
    [
        "default\n",
        "\ndefault",
        "dëfault",
        "defａult",
        "Default",
        "a/b/c",
        "acme/",
        "../etc",
        " acme",
    ],
)
def test_malformed_requested_namespace_is_malformed(requested: str) -> None:
    decision = verify_namespace(ALICE, requested, _Resolver(_accesses("acme")))
    assert decision.allowed is False
    assert decision.reason == NAMESPACE_MALFORMED


def test_non_string_requested_namespace_is_malformed() -> None:
    decision = verify_namespace(ALICE, 42, _Resolver(_accesses("acme")))
    assert decision.allowed is False
    assert decision.reason == NAMESPACE_MALFORMED


def test_malformed_check_runs_before_the_resolver_is_consulted() -> None:
    resolver = _Resolver(error=RuntimeError("must not be reached"))
    decision = verify_namespace(ALICE, "acme\n", resolver)
    assert decision.reason == NAMESPACE_MALFORMED
    assert resolver.calls == 0


def test_resolver_raising_is_namespace_error() -> None:
    resolver = _Resolver(error=RuntimeError("backend down"))
    decision = verify_namespace(ALICE, "acme", resolver)
    assert decision.allowed is False
    assert decision.reason == NAMESPACE_ERROR


def test_resolver_raising_with_no_requested_namespace_is_namespace_error() -> None:
    resolver = _Resolver(error=RuntimeError("backend down"))
    decision = verify_namespace(ALICE, "", resolver)
    assert decision.allowed is False
    assert decision.reason == NAMESPACE_ERROR


def test_resolver_returning_no_accesses_is_namespace_error() -> None:
    decision = verify_namespace(ALICE, "acme", _Resolver(()))
    assert decision.allowed is False
    assert decision.reason == NAMESPACE_ERROR


def test_resolver_returning_a_list_instead_of_a_tuple_is_namespace_error() -> None:
    decision = verify_namespace(ALICE, "acme", _Resolver([NamespaceAccess("acme", frozenset())]))
    assert decision.allowed is False
    assert decision.reason == NAMESPACE_ERROR


def test_resolver_returning_a_non_access_entry_is_namespace_error() -> None:
    decision = verify_namespace(ALICE, "acme", _Resolver(("acme",)))
    assert decision.allowed is False
    assert decision.reason == NAMESPACE_ERROR


def test_denial_never_echoes_the_resolved_namespace() -> None:
    decision = verify_namespace(ALICE, "initech", _Resolver(_accesses("acme")))
    assert decision.namespace == ""
    assert decision.roles == frozenset()


# --- agent principals are confined to their own team_scope -------------------


def test_agent_on_its_own_team_scope_is_allowed() -> None:
    decision = verify_namespace(AGENT, "acme/platform", _Resolver(_accesses("acme/platform")))
    assert decision.allowed is True


def test_agent_requesting_another_team_scope_is_not_permitted() -> None:
    resolver = _Resolver(_accesses("acme/platform", "acme/billing"))
    decision = verify_namespace(AGENT, "acme/billing", resolver)
    assert decision.allowed is False
    assert decision.reason == NAMESPACE_NOT_PERMITTED


def test_agent_with_no_requested_namespace_gets_only_its_own_scope() -> None:
    decision = verify_namespace(AGENT, "", _Resolver(_accesses("acme/billing")))
    assert decision.allowed is False
    assert decision.reason == NAMESPACE_NOT_PERMITTED


def test_agent_on_the_default_namespace_is_not_permitted() -> None:
    # The default namespace is never an agent's team_scope, so an agent with
    # the default resolver is refused rather than handed the store default.
    decision = verify_namespace(AGENT, DEFAULT_NAMESPACE, DefaultNamespaceResolver())
    assert decision.allowed is False
    assert decision.reason == NAMESPACE_NOT_PERMITTED


# --- audit: one record per denial, none on success, never a raw malformed value


def test_each_denial_appends_exactly_one_host_record(audit_recorder) -> None:
    verify_namespace(ALICE, "initech", _Resolver(_accesses("acme")))
    assert len(audit_recorder) == 1


def test_denial_record_names_subject_reason_and_correlation_id(audit_recorder) -> None:
    verify_namespace(ALICE, "initech", _Resolver(_accesses("acme")))
    record = audit_recorder[0]
    assert record["action"] == "namespace:verify"
    assert record["outcome"] == "denied"
    assert record["detail"] == {
        "subject": "alice",
        "reason": NAMESPACE_NOT_PERMITTED,
        "requested": "initech",
        "correlation_id": "corr-1",
    }


def test_denial_record_names_the_missing_reason(audit_recorder) -> None:
    verify_namespace(ALICE, "", _Resolver(_accesses("acme", "globex")))
    assert audit_recorder[0]["detail"]["reason"] == NAMESPACE_MISSING


def test_malformed_denial_records_a_hash_prefix_not_the_raw_value(audit_recorder) -> None:
    raw = "acme\nsecret-token-value"
    verify_namespace(ALICE, raw, _Resolver(_accesses("acme")))
    record = audit_recorder[0]
    expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    assert record["detail"]["requested"] == expected
    assert record["detail"]["reason"] == NAMESPACE_MALFORMED
    assert "secret-token-value" not in json.dumps(record, default=str)


def test_malformed_non_string_records_no_raw_value(audit_recorder) -> None:
    verify_namespace(ALICE, {"token": "hunter2"}, _Resolver(_accesses("acme")))
    assert "hunter2" not in json.dumps(audit_recorder[0], default=str)


def test_audit_append_raising_still_denies(monkeypatch) -> None:
    def raiser(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(access_module, "append_host_audit", raiser)
    decision = verify_namespace(ALICE, "initech", _Resolver(_accesses("acme")))
    assert decision.allowed is False
    assert decision.reason == NAMESPACE_NOT_PERMITTED


def test_audit_append_raising_a_non_os_error_still_denies(monkeypatch) -> None:
    def raiser(**kwargs):
        raise RuntimeError("store broken")

    monkeypatch.setattr(access_module, "append_host_audit", raiser)
    decision = verify_namespace(ALICE, "", _Resolver(_accesses("acme", "globex")))
    assert decision.allowed is False
    assert decision.reason == NAMESPACE_MISSING


# --- default resolver path is unchanged ---------------------------------------


def test_with_no_plugin_configured_the_active_resolver_is_the_default() -> None:
    assert isinstance(resolver_module.active_namespace_resolver(), DefaultNamespaceResolver)


def test_default_path_allows_the_store_default_namespace_with_the_callers_roles(
    audit_recorder,
) -> None:
    decision = verify_namespace(ALICE, "")
    assert decision == NamespaceDecision(
        True, DEFAULT_NAMESPACE, frozenset({ROLE_AUTHOR}), ""
    )
    assert audit_recorder == []


def test_default_path_matches_the_default_resolver_called_directly() -> None:
    expected = DefaultNamespaceResolver().accesses(ALICE)[0]
    decision = verify_namespace(ALICE, DEFAULT_NAMESPACE)
    assert decision.namespace == expected.namespace
    assert decision.roles == expected.roles


def test_default_path_denies_any_other_namespace(audit_recorder) -> None:
    decision = verify_namespace(ALICE, "acme")
    assert decision.allowed is False
    assert decision.reason == NAMESPACE_NOT_PERMITTED
    assert audit_recorder[0]["detail"]["reason"] == NAMESPACE_NOT_PERMITTED
