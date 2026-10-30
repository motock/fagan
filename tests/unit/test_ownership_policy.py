"""Ownership as an additional authority in the local policy (OWN-02).

A plan's ``owner`` and ``delegates`` reach :class:`app.policy.LocalPolicy`
through :class:`app.policy.Resource`. They grant exactly the five actions a
plan's owner governs -- ``plan:approve``, ``plan:reject``, ``plan:dispatch``,
``story:merge`` and ``decision:answer`` --
and never take anything away: every principal the role rules allowed before is
still allowed, and a plan with no owner (every plan written before OWN-01)
behaves exactly as it does today.
"""

from __future__ import annotations

import pytest

from app.identity import LOCAL_OPERATOR, ROLE_ADMIN, Principal
from app.policy import Resource, default_authorizer, is_allowed
from pipeline.authorization import resource_for

#: Every action ADR 0009 authorizes, spelled as the policy spells it.
ACTIONS = (
    "plan:ingest",
    "plan:approve",
    "plan:reject",
    "plan:dispatch",
    "plan:pause",
    "story:patch",
    "story:interrupt",
    "decision:answer",
    "story:merge",
    "story:mark_done",
    "config:write",
)

#: The five actions a plan's owner governs.
OWNER_GATED = (
    "plan:approve",
    "plan:reject",
    "plan:dispatch",
    "story:merge",
    "decision:answer",
)

#: A plan owned by alice, with one delegate.
OWNED = Resource(owner="alice", delegates=("bob",))

#: A principal holding the admin role but not the local operator's subject.
ADMIN = Principal(subject="carol", roles=frozenset({ROLE_ADMIN}))


def _decide(principal: Principal, action: str, resource: Resource) -> bool:
    """Ask the policy this deployment wires, through the fail-closed wrapper."""
    return is_allowed(default_authorizer(), principal, action, resource)


def test_owner_is_allowed_plan_approve_without_any_role() -> None:
    assert _decide(Principal(subject="alice", roles=()), "plan:approve", OWNED) is True


def test_delegate_is_allowed_plan_reject() -> None:
    assert _decide(Principal(subject="bob", roles=()), "plan:reject", OWNED) is True


def test_stranger_is_denied_story_merge() -> None:
    assert _decide(Principal(subject="mallory", roles=()), "story:merge", OWNED) is False


@pytest.mark.parametrize("action", ACTIONS)
def test_local_operator_is_allowed_every_action_on_a_plan_it_does_not_own(
    action: str,
) -> None:
    assert _decide(LOCAL_OPERATOR, action, OWNED) is True


@pytest.mark.parametrize("action", ACTIONS)
def test_admin_role_principal_is_never_subtracted_by_ownership(action: str) -> None:
    # LocalPolicy's role rules are the local operator subject only, so an
    # admin-role principal is denied everything today. Ownership must not
    # change that decision either way: it is an addition, never a subtraction.
    assert _decide(ADMIN, action, OWNED) == _decide(
        ADMIN, action, Resource()
    )


@pytest.mark.parametrize("action", OWNER_GATED)
def test_admin_role_principal_gains_nothing_on_a_plan_it_does_not_own(
    action: str,
) -> None:
    assert _decide(ADMIN, action, OWNED) is False


@pytest.mark.parametrize("action", ACTIONS)
def test_unowned_plan_allows_the_local_operator_for_every_action(action: str) -> None:
    assert _decide(LOCAL_OPERATOR, action, Resource(owner="", delegates=())) is True


@pytest.mark.parametrize("action", ACTIONS)
def test_unowned_plan_denies_a_roleless_principal_for_every_action(
    action: str,
) -> None:
    principal = Principal(subject="nobody", roles=())
    assert _decide(principal, action, Resource(owner="", delegates=())) is False


@pytest.mark.parametrize("action", ["plan:ingest"])
def test_delegate_is_not_allowed_beyond_the_owner_gated_actions(
    action: str,
) -> None:
    assert _decide(Principal(subject="bob", roles=()), action, OWNED) is False


def test_delegate_may_dispatch_the_plan_it_is_delegated() -> None:
    assert _decide(Principal(subject="bob", roles=()), "plan:dispatch", OWNED) is True


def test_resource_for_populates_the_plans_ownership_from_the_manifest() -> None:
    resource = resource_for(
        {"owner": "alice", "delegates": ["bob", "carol"]}, "p", None
    )

    assert (resource.owner, resource.delegates) == ("alice", ("bob", "carol"))


@pytest.mark.parametrize("manifest", [{}, {"owner": None, "delegates": None}])
def test_resource_for_defaults_to_empty_ownership(manifest: dict) -> None:
    resource = resource_for(manifest, "p", None)

    assert (resource.owner, resource.delegates) == ("", ())


def test_resource_for_leaves_the_manifests_ownership_untouched() -> None:
    manifest = {"owner": "alice"}

    first = resource_for(manifest, "p", None)
    second = resource_for(manifest, "p", None)

    assert (first.delegates, second.delegates, manifest) == ((), (), {"owner": "alice"})


def test_delegates_is_a_tuple_so_membership_is_not_substring_matching() -> None:
    resource = resource_for({"owner": "alice", "delegates": ["bob"]}, "p", None)

    assert "bo" not in resource.delegates


def test_policy_instance_caches_no_ownership_state_between_calls() -> None:
    policy = default_authorizer()

    decisions = (
        policy.allow(Principal(subject="nobody", roles=()), "plan:approve", Resource()),
        policy.allow(LOCAL_OPERATOR, "plan:approve", OWNED),
        policy.allow(Principal(subject="dave", roles=()), "plan:reject", OWNED),
    )

    assert decisions == (False, True, False)
