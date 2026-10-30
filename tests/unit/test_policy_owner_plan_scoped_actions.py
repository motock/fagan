"""ADR 0009 amendment 2026-10-30: a plan's owner and delegates may run the
plan-scoped actions (patch, interrupt, mark done, pause, plan config) too."""

from __future__ import annotations

import pytest

from app.identity import SYSTEM_SCHEDULER, SYSTEM_WORKER, Principal
from app.policy import Action, LocalPolicy, Resource
from pipeline.actor import ANONYMOUS

PLAN_SCOPED_ACTIONS = [
    Action.STORY_PATCH,
    Action.STORY_INTERRUPT,
    Action.STORY_MARK_DONE,
    Action.PLAN_PAUSE,
    Action.CONFIG_WRITE,
]

OWNED_PLAN = Resource(plan="p", owner="alice", delegates=("bob",))


@pytest.mark.parametrize("action", PLAN_SCOPED_ACTIONS)
def test_owner_is_allowed_plan_scoped_action(action: Action) -> None:
    alice = Principal(subject="alice", auth_method="oidc")

    assert LocalPolicy().allow(alice, action, OWNED_PLAN) is True


def test_delegate_is_allowed_story_patch() -> None:
    bob = Principal(subject="bob", auth_method="oidc")

    assert LocalPolicy().allow(bob, Action.STORY_PATCH, OWNED_PLAN) is True


@pytest.mark.parametrize("action", PLAN_SCOPED_ACTIONS)
def test_stranger_is_denied_plan_scoped_action(action: Action) -> None:
    mallory = Principal(subject="mallory", auth_method="oidc")

    assert LocalPolicy().allow(mallory, action, OWNED_PLAN) is False


def test_host_scoped_config_write_is_denied_to_oidc_principal() -> None:
    alice = Principal(subject="alice", auth_method="oidc")

    assert LocalPolicy().allow(alice, Action.CONFIG_WRITE, Resource(owner="")) is False


def test_host_scoped_config_write_is_denied_to_empty_subject() -> None:
    # Principal refuses an empty subject, so simulate one that bypassed it: the
    # empty-owner guard must hold on its own (defense in depth).
    nobody = Principal(subject="placeholder", auth_method="oidc")
    object.__setattr__(nobody, "subject", "")

    assert LocalPolicy().allow(nobody, Action.CONFIG_WRITE, Resource(owner="")) is False


def test_worker_is_still_denied_story_interrupt_on_owned_plan() -> None:
    resource = Resource(plan="p", owner=SYSTEM_WORKER.subject)

    assert LocalPolicy().allow(SYSTEM_WORKER, Action.STORY_INTERRUPT, resource) is False


def test_scheduler_is_still_denied_story_patch_on_owned_plan() -> None:
    resource = Resource(plan="p", owner=SYSTEM_SCHEDULER.subject)

    assert LocalPolicy().allow(SYSTEM_SCHEDULER, Action.STORY_PATCH, resource) is False


def test_anonymous_is_denied_story_patch_on_plan_it_does_not_own() -> None:
    assert LocalPolicy().allow(ANONYMOUS, Action.STORY_PATCH, OWNED_PLAN) is False
