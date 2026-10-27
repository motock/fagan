"""Tests for plan ownership in the local policy (OWN-02).

A plan manifest may name an ``owner`` and ``delegates``; those two fields now
reach :class:`app.policy.Resource`. Ownership is an ADDITIONAL authority for
exactly three actions -- ``plan:approve``, ``plan:reject`` and ``story:merge``
-- and never a subtraction for a principal that is allowed today. The local
single-operator policy still grants by subject, so an ownerless resource (every
plan written before OWN-01) must decide exactly as it did before.
"""

from __future__ import annotations

from app.identity import LOCAL_OPERATOR, ROLE_ADMIN, Principal
from app.policy import Action, LocalPolicy, Resource
from pipeline.authorization import resource_for

#: The only three actions a plan's owner and its delegates govern.
OWNER_GATED = (Action.PLAN_APPROVE, Action.PLAN_REJECT, Action.STORY_MERGE)


def _owned() -> Resource:
    """A resource owned by ``alice`` and delegated to ``bob``."""
    return Resource(plan="p1", owner="alice", delegates=("bob",))


def test_plan_owner_without_any_role_may_approve() -> None:
    assert (
        LocalPolicy().allow(Principal(subject="alice"), Action.PLAN_APPROVE, _owned())
        is True
    )


def test_delegate_without_any_role_may_reject() -> None:
    assert (
        LocalPolicy().allow(Principal(subject="bob"), Action.PLAN_REJECT, _owned())
        is True
    )


def test_stranger_without_any_role_is_denied_merge() -> None:
    assert (
        LocalPolicy().allow(Principal(subject="eve"), Action.STORY_MERGE, _owned())
        is False
    )


def test_delegate_on_an_ownerless_resource_is_denied_merge() -> None:
    # A manifest with delegates but no owner must grant nothing: otherwise a
    # stray ``delegates`` list outranks the missing owner and hands out
    # approve/reject/merge on a plan nobody owns.
    assert (
        LocalPolicy().allow(
            Principal(subject="bob"), Action.STORY_MERGE, Resource(delegates=("bob",))
        )
        is False
    )


def test_local_operator_is_still_allowed_every_action_on_a_plan_it_does_not_own() -> None:
    policy = LocalPolicy()

    assert [policy.allow(LOCAL_OPERATOR, action, _owned()) for action in Action] == [
        True
    ] * len(Action)


def test_admin_role_principal_keeps_todays_decision_on_someone_elses_plan() -> None:
    # The local policy grants by subject, so a principal that merely holds the
    # admin role has no permission today; ownership must neither subtract one
    # nor widen the policy by granting approval rights to every admin.
    policy = LocalPolicy()
    admin = Principal(subject="dana", roles=frozenset({ROLE_ADMIN}))

    assert policy.allow(admin, Action.PLAN_APPROVE, _owned()) == policy.allow(
        admin, Action.PLAN_APPROVE, Resource()
    )


def test_ownerless_resource_keeps_the_existing_local_decisions() -> None:
    # The allow/deny pairs tests/unit/test_policy_local.py pins for the local
    # cases: the operator may do everything, any other or missing principal may
    # do nothing, and an unknown action stays denied.
    ownerless = Resource()
    expected = (
        [(LOCAL_OPERATOR, action, True) for action in Action]
        + [(Principal(subject="system-agent"), action, False) for action in Action]
        + [
            (None, Action.PLAN_APPROVE, False),
            (LOCAL_OPERATOR, "plan:nuke", False),
        ]
    )

    assert [
        (principal, action, LocalPolicy().allow(principal, action, ownerless))
        for principal, action, _allowed in expected
    ] == expected


def test_delegate_is_not_granted_any_other_action() -> None:
    policy = LocalPolicy()
    bob = Principal(subject="bob")

    assert [
        policy.allow(bob, action, _owned())
        for action in (Action.PLAN_DISPATCH, Action.STORY_PATCH)
    ] == [False, False]


def test_resource_for_reads_owner_and_delegates_from_the_manifest() -> None:
    manifest = {"id": "p1", "owner": "alice", "delegates": ["bob", "carol"]}

    resource = resource_for(manifest, "p1", None)

    assert (resource.owner, resource.delegates) == ("alice", ("bob", "carol"))


def test_resource_for_defaults_owner_and_delegates_to_empty() -> None:
    resource = resource_for({"id": "p2"}, "p2", None)

    assert (resource.owner, resource.delegates) == ("", ())
