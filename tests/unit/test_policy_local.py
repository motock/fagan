"""Tests for the deny-by-default local authorization policy (ADR 0009).

The policy is inert in this plan — nothing calls it yet — so these tests are
the contract the enforcement plan will trust: strictly deny-by-default and
fail closed.
"""

from __future__ import annotations

import logging
from dataclasses import FrozenInstanceError

import pytest

from app.identity import Principal
from app.policy import (
    LOCAL_OPERATOR,
    Action,
    LocalPolicy,
    Resource,
    default_authorizer,
    is_allowed,
)

#: A credential-shaped sentinel that must never reach the log.
SENTINEL_CREDENTIAL = "sk-sentinel-credential-do-not-log"

#: The eleven actions ADR 0009 defines, with their wire values.
ADR_0009_ACTIONS = [
    (Action.PLAN_INGEST, "plan:ingest"),
    (Action.PLAN_APPROVE, "plan:approve"),
    (Action.PLAN_REJECT, "plan:reject"),
    (Action.PLAN_DISPATCH, "plan:dispatch"),
    (Action.PLAN_PAUSE, "plan:pause"),
    (Action.STORY_PATCH, "story:patch"),
    (Action.STORY_INTERRUPT, "story:interrupt"),
    (Action.DECISION_ANSWER, "decision:answer"),
    (Action.STORY_MERGE, "story:merge"),
    (Action.STORY_MARK_DONE, "story:mark_done"),
    (Action.CONFIG_WRITE, "config:write"),
]


class RaisingAuthorizer(LocalPolicy):
    """An authorizer whose ``allow`` blows up, to prove ``is_allowed`` fails closed."""

    def allow(
        self,
        principal: Principal | None,
        action: Action | str,
        resource: Resource | None = None,
    ) -> bool:
        raise RuntimeError(f"policy backend exploded with {SENTINEL_CREDENTIAL}")


@pytest.mark.parametrize(("member", "value"), ADR_0009_ACTIONS)
def test_action_member_has_adr_0009_value(member: Action, value: str) -> None:
    assert member.value == value


@pytest.mark.parametrize(("member", "value"), ADR_0009_ACTIONS)
def test_action_member_is_a_string(member: Action, value: str) -> None:
    assert isinstance(member, str)
    assert member == value


def test_resource_defaults_to_empty_scope_chain() -> None:
    resource = Resource()

    assert resource.tenant == ""
    assert resource.team == ""
    assert resource.repo == ""
    assert resource.plan == ""
    assert resource.story == ""


def test_resource_is_frozen() -> None:
    resource = Resource(tenant="acme", team="core", repo="pipeline")

    with pytest.raises(FrozenInstanceError):
        resource.tenant = "other"  # type: ignore[misc]


def test_default_authorizer_returns_local_policy() -> None:
    assert isinstance(default_authorizer(), LocalPolicy)


def test_none_principal_is_denied() -> None:
    assert LocalPolicy().allow(None, Action.PLAN_APPROVE) is False


def test_invalid_raw_string_action_is_denied() -> None:
    assert LocalPolicy().allow(LOCAL_OPERATOR, "plan:nuke") is False


def test_valid_raw_string_action_is_allowed_for_local_operator() -> None:
    assert LocalPolicy().allow(LOCAL_OPERATOR, "plan:approve") is True


@pytest.mark.parametrize("action", [None, 42, object()])
def test_unexpected_action_type_is_denied_without_raising(action: object) -> None:
    assert LocalPolicy().allow(LOCAL_OPERATOR, action) is False  # type: ignore[arg-type]


def test_non_local_principal_is_denied_for_every_action() -> None:
    system_principal = Principal(subject="system-agent")
    policy = LocalPolicy()

    allowed = {action for action in Action if policy.allow(system_principal, action)}

    assert allowed == set()


def test_non_local_principal_is_denied_plan_approve() -> None:
    # ADR 0009: a system principal must never approve its own plan.
    assert (
        LocalPolicy().allow(Principal(subject="system-agent"), Action.PLAN_APPROVE)
        is False
    )


def test_local_operator_is_allowed_every_action() -> None:
    policy = LocalPolicy()

    allowed = {action for action in Action if policy.allow(LOCAL_OPERATOR, action)}

    assert allowed == set(Action)


def test_raising_authorizer_fails_closed_and_logs_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    principal = Principal(subject="local-operator", email=SENTINEL_CREDENTIAL)

    with caplog.at_level(logging.WARNING, logger="app.policy"):
        result = is_allowed(RaisingAuthorizer(), principal, Action.PLAN_APPROVE)

    assert result is False
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    assert "plan:approve" in record.getMessage()
    assert "local-operator" in record.getMessage()
    assert SENTINEL_CREDENTIAL not in record.getMessage()
    assert SENTINEL_CREDENTIAL not in caplog.text


def test_failed_decision_is_not_cached() -> None:
    assert is_allowed(RaisingAuthorizer(), LOCAL_OPERATOR, Action.PLAN_APPROVE) is False
    assert is_allowed(default_authorizer(), LOCAL_OPERATOR, Action.PLAN_APPROVE) is True


def test_empty_resource_does_not_change_any_decision() -> None:
    policy = LocalPolicy()
    empty = Resource()
    system_principal = Principal(subject="system-agent")

    for action in Action:
        assert policy.allow(LOCAL_OPERATOR, action, empty) is True
        assert policy.allow(None, action, empty) is False
        assert policy.allow(system_principal, action, empty) is False

    assert policy.allow(LOCAL_OPERATOR, "plan:nuke", empty) is False
    assert policy.allow(LOCAL_OPERATOR, None, empty) is False


# --- DK-05: a plan's authenticated caller may ingest (and re-ingest) it -----
#
# ADR 0006 makes the authenticated caller the plan's owner at ingest, so a
# first ingest carries no owner yet (or a pre-OWN-01 ownerless plan) and must
# be allowed for any authenticated principal. Re-ingest is allowed only for the
# owner. Every other action keeps the rules it had: ownership buys exactly the
# four governing actions, and a reserved system principal keeps exactly its row.

#: A remote (OIDC) caller: authenticated, not the local operator, not reserved.
OIDC_ALICE = Principal(subject="oidc:alice", auth_method="oidc")
OIDC_BOB_SUBJECT = "oidc:bob"

#: The scheduler's own identity, spelled out so this module needs no new import.
SYSTEM_SCHEDULER_PRINCIPAL = Principal(subject="system:scheduler", auth_method="system")


def test_first_ingest_is_allowed_for_non_operator_principal() -> None:
    # No manifest yet: the resource has no owner, so the caller becomes owner.
    assert (
        LocalPolicy().allow(OIDC_ALICE, Action.PLAN_INGEST, Resource(owner="")) is True
    )


def test_reingest_of_owned_plan_is_allowed_for_owner() -> None:
    resource = Resource(plan="p1", owner=OIDC_ALICE.subject)

    assert LocalPolicy().allow(OIDC_ALICE, Action.PLAN_INGEST, resource) is True


def test_ingest_of_plan_owned_by_someone_else_is_denied() -> None:
    resource = Resource(plan="p1", owner=OIDC_BOB_SUBJECT)

    assert LocalPolicy().allow(OIDC_ALICE, Action.PLAN_INGEST, resource) is False


def test_unauthenticated_principal_cannot_ingest() -> None:
    assert LocalPolicy().allow(None, Action.PLAN_INGEST, Resource(owner="")) is False
    assert LocalPolicy().allow(None, Action.PLAN_INGEST, None) is False


def test_anonymous_principal_cannot_ingest() -> None:
    # `ANONYMOUS` is a Principal too; "none" is not an identity to own a plan.
    anonymous = Principal(subject="anonymous", auth_method="none")

    assert LocalPolicy().allow(anonymous, Action.PLAN_INGEST, Resource(owner="")) is False


def test_principal_without_an_auth_method_cannot_ingest() -> None:
    unauthenticated = Principal(subject="system-agent")

    assert (
        LocalPolicy().allow(unauthenticated, Action.PLAN_INGEST, Resource(owner=""))
        is False
    )


def test_system_scheduler_cannot_ingest() -> None:
    # The reserved-system branch is decided first: ownership never widens it.
    assert (
        LocalPolicy().allow(
            SYSTEM_SCHEDULER_PRINCIPAL, Action.PLAN_INGEST, Resource(owner="")
        )
        is False
    )
    assert LocalPolicy().allow(SYSTEM_SCHEDULER_PRINCIPAL, Action.PLAN_INGEST) is False


def test_ingest_branch_does_not_change_other_actions() -> None:
    policy = LocalPolicy()

    # Approving is still refused on a plan with no owner ...
    assert policy.allow(OIDC_ALICE, Action.PLAN_APPROVE, Resource(owner="")) is False
    # ... while the four governing actions still follow ownership.
    owned = Resource(plan="p1", owner=OIDC_ALICE.subject)
    assert policy.allow(OIDC_ALICE, Action.STORY_MERGE, owned) is True
    assert policy.allow(OIDC_ALICE, Action.DECISION_ANSWER, owned) is True
