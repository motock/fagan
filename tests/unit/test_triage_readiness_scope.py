"""Readiness scored across a scope: one plan, a team's namespace, or the deployment.

ADR 0014 decision 8 judges a park class on its paired decisions. A class is
promoted per decision class, not per plan, so a deployment must be able to pool
every plan's decisions and score each class once. These tests pin the pooling,
the namespace boundary of team scope, the unchanged per-plan CLI, and the rule
that one false proceed in any plan blocks the class at deployment scope.
Every test uses synthetic records.
"""

from __future__ import annotations

import json

import pytest

from pipeline import server
from pipeline import triage_readiness as tr

_CLASS = "rebase_conflict"
_PARK = tr.PARK_ACTION


def _ruling(plan: str, key: str, *, action: str, decided_at: str) -> dict:
    return {
        "question": "failure triage",
        "story_key": f"{plan}-{key}",
        "action": action,
        "park_class": _CLASS,
        "decided_at": decided_at,
    }


def _human(ruling: dict, human_action: str) -> dict:
    return {
        "question": "human resolution",
        "story_key": ruling["story_key"],
        "ruling_ref": ruling["decided_at"],
        "ruling_action": ruling["action"],
        "park_class": ruling["park_class"],
        "human_action": human_action,
    }


def _plan_decisions(plan: str, *, agreeing: int, false_proceeds: int = 0) -> list[dict]:
    """Paired decisions for one plan: ``agreeing`` park/park pairs, then false proceeds."""
    records: list[dict] = []
    for index in range(agreeing):
        ruling = _ruling(plan, f"a{index}", action=_PARK, decided_at=f"{plan}-a{index}")
        records += [ruling, _human(ruling, "park")]
    for index in range(false_proceeds):
        ruling = _ruling(plan, f"p{index}", action="rebrief", decided_at=f"{plan}-p{index}")
        records += [ruling, _human(ruling, "park")]
    return records


def _cls(report: dict, name: str = _CLASS) -> dict:
    return report[name]


# --- pooling across a deployment -------------------------------------------


def test_deployment_scope_pools_three_plans_to_reach_eligibility() -> None:
    plans = [
        ("default", "one", _plan_decisions("one", agreeing=7)),
        ("default", "two", _plan_decisions("two", agreeing=7)),
        ("default", "three", _plan_decisions("three", agreeing=6)),
    ]

    report = tr.scoped_readiness(plans, "deployment")

    assert set(report) == {"deployment"}
    assert _cls(report["deployment"])["paired_count"] == 20
    assert _cls(report["deployment"])["eligible"] is True


def test_plan_scope_does_not_reach_eligibility_for_the_same_plans() -> None:
    plans = [
        ("default", "one", _plan_decisions("one", agreeing=7)),
        ("default", "two", _plan_decisions("two", agreeing=7)),
        ("default", "three", _plan_decisions("three", agreeing=6)),
    ]

    report = tr.scoped_readiness(plans, "plan")

    assert set(report) == {"one", "two", "three"}
    assert [_cls(report[name])["paired_count"] for name in ("one", "two", "three")] == [7, 7, 6]
    assert all(_cls(report[name])["eligible"] is False for name in report)


def test_deployment_scope_blocks_a_class_for_one_false_proceed_in_any_plan() -> None:
    plans = [
        ("default", "one", _plan_decisions("one", agreeing=7)),
        ("default", "two", _plan_decisions("two", agreeing=7)),
        ("default", "three", _plan_decisions("three", agreeing=5, false_proceeds=1)),
    ]

    report = tr.scoped_readiness(plans, "deployment")

    verdict = _cls(report["deployment"])
    assert verdict["paired_count"] == 20
    assert verdict["false_proceed"] == 1
    assert verdict["eligible"] is False
    assert "1 false proceed(s), none allowed" in verdict["blockers"]


def test_deployment_scope_pairs_a_ruling_only_with_a_resolution_in_its_own_plan() -> None:
    # Both plans reuse the same ruling reference. A resolution in plan B must
    # not pair with plan A's ruling once the decisions are pooled.
    ruling = _ruling("alpha", "k", action=_PARK, decided_at="2026-11-01T00:00:00")
    other = _ruling("beta", "k", action=_PARK, decided_at="2026-11-01T00:00:00")
    ruling["story_key"] = other["story_key"] = "SAME-KEY"
    plans = [
        ("default", "alpha", [ruling]),
        ("default", "beta", [other, _human(other, "park")]),
    ]

    verdict = _cls(tr.scoped_readiness(plans, "deployment")["deployment"])

    assert verdict["shadow_count"] == 2
    assert verdict["paired_count"] == 1


# --- team scope ------------------------------------------------------------


def test_team_scope_keys_the_report_by_namespace_and_never_mixes_them() -> None:
    plans = [
        ("team-a", "a-one", _plan_decisions("a-one", agreeing=12)),
        ("team-a", "a-two", _plan_decisions("a-two", agreeing=8)),
        ("team-b", "b-one", _plan_decisions("b-one", agreeing=5, false_proceeds=1)),
    ]

    report = tr.scoped_readiness(plans, "team")

    assert set(report) == {"team-a", "team-b"}
    assert _cls(report["team-a"])["paired_count"] == 20
    assert _cls(report["team-a"])["eligible"] is True
    assert _cls(report["team-b"])["paired_count"] == 6
    assert _cls(report["team-b"])["false_proceed"] == 1
    assert _cls(report["team-b"])["eligible"] is False


# --- plan scope and invalid scope ------------------------------------------


def test_unknown_scope_is_refused() -> None:
    with pytest.raises(ValueError, match="scope"):
        tr.scoped_readiness([("default", "one", [])], "everything")


def test_plan_scope_with_no_plans_is_an_empty_report() -> None:
    assert tr.scoped_readiness([], "plan") == {}


# --- CLI -------------------------------------------------------------------


class _FakeRecords:
    def __init__(self, namespaces: dict[str, dict[str, list[dict]]]) -> None:
        self._namespaces = namespaces

    def list_namespaces(self) -> list[str]:
        return sorted(self._namespaces)

    def list_plans(self, namespace: str) -> list[str]:
        return sorted(self._namespaces.get(namespace, {}))

    def get_decisions(self, namespace: str, plan: str) -> list[dict]:
        return self._namespaces[namespace][plan]


class _FakeStore:
    """Namespaced plans with the default-namespace Store reads the CLI uses for --plan."""

    def __init__(self, namespaces: dict[str, dict[str, list[dict]]]) -> None:
        self.namespaces = namespaces
        self.reads: list[str] = []

    def records(self) -> _FakeRecords:
        return _FakeRecords(self.namespaces)

    def get_decisions(self, plan_name: str) -> list[dict]:
        self.reads.append(plan_name)
        return self.namespaces["default"].get(plan_name, [])


@pytest.fixture
def two_namespaces(monkeypatch) -> _FakeStore:
    store = _FakeStore(
        {
            "default": {
                "alpha": _plan_decisions("alpha", agreeing=7),
                "beta": _plan_decisions("beta", agreeing=7),
            },
            "team-b": {
                "gamma": _plan_decisions("gamma", agreeing=6),
            },
        }
    )
    monkeypatch.setattr(server, "_store", store)
    return store


def test_cli_all_defaults_to_deployment_scope_and_pools_every_namespace(two_namespaces, capsys) -> None:
    code = tr.main(["--all", "--json"])

    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert set(report) == {"deployment"}
    assert _cls(report["deployment"])["paired_count"] == 20
    assert _cls(report["deployment"])["eligible"] is True


def test_cli_all_with_team_scope_keys_the_report_by_namespace(two_namespaces, capsys) -> None:
    code = tr.main(["--all", "--scope", "team", "--json"])

    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert set(report) == {"default", "team-b"}
    assert _cls(report["default"])["paired_count"] == 14
    assert _cls(report["team-b"])["paired_count"] == 6


def test_cli_all_with_plan_scope_keeps_one_entry_per_plan(two_namespaces, capsys) -> None:
    code = tr.main(["--all", "--scope", "plan", "--json"])

    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert set(report) == {"alpha", "beta", "gamma"}


def test_cli_plan_is_unchanged_and_reads_through_the_store(two_namespaces, capsys) -> None:
    code = tr.main(["--plan", "alpha", "--json"])

    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert set(report) == {"alpha"}
    assert _cls(report["alpha"])["paired_count"] == 7
    assert _cls(report["alpha"])["eligible"] is False
    assert two_namespaces.reads == ["alpha"]


def test_cli_plan_refuses_a_deployment_or_team_scope(two_namespaces, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        tr.main(["--plan", "alpha", "--scope", "deployment", "--json"])

    assert exc.value.code == 2
    assert two_namespaces.reads == []


def test_cli_refuses_an_unknown_scope_value(two_namespaces, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        tr.main(["--all", "--scope", "org", "--json"])

    assert exc.value.code == 2


def test_plan_scope_refuses_a_plan_name_held_in_two_namespaces() -> None:
    plans = [
        ("team-a", "shared", _plan_decisions("shared", agreeing=3)),
        ("team-b", "shared", _plan_decisions("shared", agreeing=3)),
    ]

    with pytest.raises(ValueError, match="more than one namespace"):
        tr.scoped_readiness(plans, "plan")


def test_deployment_scope_pools_a_plan_name_held_in_two_namespaces() -> None:
    plans = [
        ("team-a", "shared", _plan_decisions("shared", agreeing=10)),
        ("team-b", "shared", _plan_decisions("shared", agreeing=10)),
    ]

    report = tr.scoped_readiness(plans, "deployment")

    assert _cls(report["deployment"])["paired_count"] == 20
