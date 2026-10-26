"""Tests for the service-layer authorization gate (ADR 0009).

``pipeline.authorization`` is the single place the service asks the policy for
a decision. These tests pin the switch semantics the operator chose --
enforcement is ON unless ``PIPELINE_POLICY_ENFORCE`` is set to an explicit
disabling value -- and the fail-closed behaviour of every unexpected path.
"""

from __future__ import annotations

import json

import pytest

# pipeline.server must load before pipeline.ingest: pipeline.ingest's module
# body imports pipeline.build_detect, which imports pipeline.server, which
# imports _ingest_plan_impl back from pipeline.ingest -- so importing
# pipeline.ingest first re-enters a partially-initialized pipeline.server.
import pipeline.server as p
from app.identity import LOCAL_OPERATOR
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline.actor import ANONYMOUS, bind_principal
from pipeline.authorization import (
    DEFAULT_TEAM,
    ENFORCE_ENV,
    authorize,
    enforcement_enabled,
    resource_for,
)

#: The credential-shaped sentinel ``tests/unit/test_policy_local.py`` uses; a
#: refusal message must never carry it.
SENTINEL_CREDENTIAL = "sk-sentinel-credential-do-not-log"


class _BrokenPolicy:
    """An authorizer whose ``allow`` blows up, to prove the gate fails closed."""

    def allow(self, principal, action, resource=None):
        raise RuntimeError(f"policy backend exploded with {SENTINEL_CREDENTIAL}")


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Point every PLAN_DIR binding at a throwaway directory.

    ``pipeline_persistence`` and ``pipeline_concurrency`` import ``PLAN_DIR``
    from ``pipeline_paths`` at module load and read it as a free variable, so
    the patch has to land on their own bindings too.
    """
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


@pytest.fixture
def anonymous_refusal(plan_dir, monkeypatch):
    """Deny one ``story:merge`` by an anonymous principal and return the result."""
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    with bind_principal(ANONYMOUS):
        return authorize("story:merge", plan_name="p", story_key="S", manifest={})


def test_enforcement_enabled_defaults_to_true() -> None:
    assert enforcement_enabled({}) is True


@pytest.mark.parametrize("value", ["", "1", "yes", "true", "garbage", "ENABLED"])
def test_enforcement_enabled_stays_on_for_non_disabling_value(value: str) -> None:
    assert enforcement_enabled({ENFORCE_ENV: value}) is True


@pytest.mark.parametrize(
    "value", ["0", "false", "no", "off", "disabled", " DISABLED "]
)
def test_enforcement_enabled_is_off_for_disabling_value(value: str) -> None:
    assert enforcement_enabled({ENFORCE_ENV: value}) is False


def test_local_operator_is_allowed_to_merge(plan_dir, monkeypatch) -> None:
    monkeypatch.delenv(ENFORCE_ENV, raising=False)

    with bind_principal(LOCAL_OPERATOR):
        result = authorize("story:merge", plan_name="p", story_key="S", manifest={})

    assert result is None


def test_unbound_principal_is_the_local_operator_and_is_allowed(
    plan_dir, monkeypatch
) -> None:
    monkeypatch.delenv(ENFORCE_ENV, raising=False)

    result = authorize("story:merge", plan_name="p", story_key="S", manifest={})

    assert result is None


def test_anonymous_principal_is_denied_with_a_refusal_dict(anonymous_refusal) -> None:
    assert isinstance(anonymous_refusal, dict)


def test_refusal_sets_ok_false(anonymous_refusal) -> None:
    assert anonymous_refusal["ok"] is False


def test_refusal_error_names_the_action(anonymous_refusal) -> None:
    assert "story:merge" in anonymous_refusal["error"]


def test_refusal_error_names_the_plan(anonymous_refusal) -> None:
    assert "plan 'p'" in anonymous_refusal["error"]


def test_refusal_error_has_no_credential_prefix(anonymous_refusal) -> None:
    assert "sk-" not in anonymous_refusal["error"]


def test_refusal_error_has_no_sentinel_credential(anonymous_refusal) -> None:
    assert SENTINEL_CREDENTIAL not in anonymous_refusal["error"]


def test_denial_is_recorded_once_as_a_warning(anonymous_refusal, plan_dir) -> None:
    path = plan_dir / "p.notifications.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]

    assert [(r["event"], r["severity"]) for r in records] == [
        ("authorization_denied", "warning")
    ]


def test_disabled_enforcement_allows_anonymous(plan_dir, monkeypatch) -> None:
    monkeypatch.setenv(ENFORCE_ENV, "0")

    with bind_principal(ANONYMOUS):
        result = authorize("story:merge", plan_name="p", story_key="S", manifest={})

    assert result is None


def test_disabled_enforcement_records_no_denial(plan_dir, monkeypatch) -> None:
    monkeypatch.setenv(ENFORCE_ENV, "0")

    with bind_principal(ANONYMOUS):
        authorize("story:merge", plan_name="p", story_key="S", manifest={})

    assert not (plan_dir / "p.notifications.jsonl").exists()


def test_raising_policy_fails_closed(plan_dir, monkeypatch) -> None:
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    monkeypatch.setattr(
        "pipeline.authorization.default_authorizer", lambda: _BrokenPolicy()
    )
    expected = (
        "not authorized: story:merge on plan 'p' "
        f"(principal {LOCAL_OPERATOR.subject!r})"
    )

    with bind_principal(LOCAL_OPERATOR):
        result = authorize("story:merge", plan_name="p", story_key="S", manifest={})

    assert result == {"ok": False, "error": expected}


def test_resource_for_builds_the_scope_chain() -> None:
    resource = resource_for({"team": "core", "repo_root": "/r"}, "p", "S")

    assert (
        resource.team,
        resource.repo,
        resource.plan,
        resource.story,
        resource.tenant,
    ) == ("core", "/r", "p", "S", "")


def test_resource_for_falls_back_to_default_team() -> None:
    resource = resource_for({}, "p", None)

    assert (resource.team, resource.story) == (DEFAULT_TEAM, "")


def test_missing_manifest_still_allows_the_local_operator(plan_dir, monkeypatch) -> None:
    monkeypatch.delenv(ENFORCE_ENV, raising=False)

    with bind_principal(LOCAL_OPERATOR):
        result = authorize("story:merge", plan_name="p", story_key="S")

    assert result is None


def test_missing_manifest_denies_anonymous(plan_dir, monkeypatch) -> None:
    monkeypatch.delenv(ENFORCE_ENV, raising=False)

    with bind_principal(ANONYMOUS):
        result = authorize("story:merge", plan_name="p", story_key="S")

    assert result["ok"] is False
