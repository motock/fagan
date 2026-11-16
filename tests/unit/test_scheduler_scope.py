"""ss-01: the scheduler scope read from its two environment variables.

``PIPELINE_SCHEDULER_NAMESPACES_ONLY`` and ``PIPELINE_SCHEDULER_NAMESPACES_SKIP``
are each a comma-separated list. Unset or empty means no scope. A malformed
scope (both set, a blank entry, an invalid namespace) raises, so the tick can
refuse to advance anything.
"""

from __future__ import annotations

import pytest

from pipeline import scheduler_scope
from pipeline.scheduler_scope import SchedulerScopeError, load_scope

ONLY = "PIPELINE_SCHEDULER_NAMESPACES_ONLY"
SKIP = "PIPELINE_SCHEDULER_NAMESPACES_SKIP"


# ---------------------------------------------------------------------------
# unset / empty: no scope
# ---------------------------------------------------------------------------


def test_no_variables_set_means_no_scope() -> None:
    assert load_scope({}) is None


def test_empty_variables_mean_no_scope() -> None:
    assert load_scope({ONLY: "", SKIP: ""}) is None


# ---------------------------------------------------------------------------
# ONLY: advance just the listed namespaces
# ---------------------------------------------------------------------------


def test_only_admits_the_listed_namespace_and_nothing_else() -> None:
    scope = load_scope({ONLY: "alpha"})

    assert scope is not None
    assert scope.allows("alpha")
    assert not scope.allows("beta")
    assert not scope.allows("default")


def test_only_trims_whitespace_around_entries() -> None:
    scope = load_scope({ONLY: " alpha ,  beta/gamma "})

    assert scope is not None
    assert scope.allows("alpha")
    assert scope.allows("beta/gamma")
    assert not scope.allows("delta")


def test_only_admits_the_default_namespace_when_named() -> None:
    scope = load_scope({ONLY: "default,alpha"})

    assert scope is not None
    assert scope.allows("default")
    assert scope.allows("alpha")


def test_only_with_default_absent_does_not_admit_default() -> None:
    scope = load_scope({ONLY: "alpha"})

    assert scope is not None
    assert not scope.allows("default")


# ---------------------------------------------------------------------------
# SKIP: advance everything except the listed namespaces
# ---------------------------------------------------------------------------


def test_skip_excludes_the_listed_namespace_and_admits_the_rest() -> None:
    scope = load_scope({SKIP: "beta"})

    assert scope is not None
    assert not scope.allows("beta")
    assert scope.allows("alpha")
    assert scope.allows("default")


def test_skip_naming_default_excludes_the_default_namespace() -> None:
    scope = load_scope({SKIP: "default"})

    assert scope is not None
    assert not scope.allows("default")
    assert scope.allows("alpha")


# ---------------------------------------------------------------------------
# negative: malformed or conflicting scopes raise
# ---------------------------------------------------------------------------


def test_both_variables_set_is_refused() -> None:
    with pytest.raises(SchedulerScopeError):
        load_scope({ONLY: "alpha", SKIP: "beta"})


@pytest.mark.parametrize(
    "value",
    [
        "alpha,,beta",  # blank entry in the middle
        "alpha,",  # trailing comma leaves a blank entry
        ",alpha",  # leading comma leaves a blank entry
        "   ",  # whitespace-only entry
    ],
)
def test_blank_entry_is_refused(value: str) -> None:
    with pytest.raises(SchedulerScopeError):
        load_scope({ONLY: value})


@pytest.mark.parametrize(
    "value",
    [
        "Bad Name!",  # characters outside the namespace grammar
        "UPPER",  # namespaces are lower-case
        "a/b/c",  # three segments
        "alpha/",  # empty second segment
        "/beta",  # empty first segment
        "../escape",  # path traversal shape
    ],
)
def test_invalid_namespace_entry_is_refused(value: str) -> None:
    with pytest.raises(SchedulerScopeError):
        load_scope({SKIP: value})


def test_one_bad_entry_refuses_the_whole_list() -> None:
    with pytest.raises(SchedulerScopeError):
        load_scope({ONLY: "alpha,Bad Name!"})


def test_refusal_message_does_not_echo_the_offending_value() -> None:
    with pytest.raises(SchedulerScopeError) as excinfo:
        load_scope({SKIP: "secret-tenant/../x"})

    assert "secret-tenant" not in str(excinfo.value)


def test_scope_module_does_not_name_tenants_teams_or_organizations() -> None:
    source = scheduler_scope.__file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read().lower()

    for word in ("tenant", "team", "organization", "org "):
        assert word not in text
