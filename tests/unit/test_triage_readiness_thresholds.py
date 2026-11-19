"""Tests for organization-tightened promotion thresholds (ADR 0014, decision 8).

The shipped defaults (20 paired decisions, 10% false-park rate) are the loosest
values allowed. An organization tightens them with the authority-matrix rules
``readiness.min_paired`` and ``readiness.max_false_park_percent``; a team layer
may only tighten them further. These tests build org and team layers in
``tmp_path`` from the shipped matrix, changing only the readiness rules, and
point the matrix environment variables at them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import pipeline.server  # noqa: F401  (pre-existing import cycle; see test_triage_cap_silent_skip.py)
from pipeline import triage_readiness as tr
from pipeline.authority_matrix import FileLayerSource

SHIPPED_MATRIX = Path(__file__).resolve().parents[2] / "overlord-authority.json"
_MIN_PAIRED_RULE = "readiness.min_paired"
_RATE_RULE = "readiness.max_false_park_percent"
_CLASS = "rebase_conflict"
_DEFAULTS = {"min_paired": 20, "max_false_park_percent": 10}


@pytest.fixture(autouse=True)
def _no_team_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PIPELINE_AUTHORITY_TEAM_LAYER", raising=False)


def _shipped_payload() -> dict:
    return json.loads(SHIPPED_MATRIX.read_text(encoding="utf-8"))


def _rule(payload: dict, rule_id: str) -> dict:
    return next(rule for rule in payload["rules"] if rule["id"] == rule_id)


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _org_layer(tmp_path: Path, mutate) -> Path:
    payload = _shipped_payload()
    mutate(payload)
    return _write_json(tmp_path / "org.json", payload)


def _team_layer(tmp_path: Path, values: dict) -> Path:
    payload = {
        "version": 1,
        "layer": "team",
        "rules": [{"id": rule_id, "value": value} for rule_id, value in values.items()],
    }
    return _write_json(tmp_path / "team.json", payload)


def _set_org(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv("PIPELINE_AUTHORITY_MATRIX", str(path))


def _paired_agree(count: int) -> list[dict]:
    """``count`` paired rulings where the machine parked and the human parked."""
    records: list[dict] = []
    for index in range(count):
        ruling = {
            "question": "failure triage",
            "story_key": f"S{index}",
            "action": tr.PARK_ACTION,
            "park_class": _CLASS,
            "decided_at": f"2026-11-01T00:00:{index:02d}",
        }
        human = {
            "question": "human resolution",
            "story_key": f"S{index}",
            "ruling_ref": ruling["decided_at"],
            "ruling_action": ruling["action"],
            "park_class": _CLASS,
            "human_action": "park",
        }
        records += [ruling, human]
    return records


def _paired_false_parks(count: int) -> list[dict]:
    """``count`` paired rulings the machine parked and the human resolved without parking."""
    records: list[dict] = []
    for index in range(count):
        ruling = {
            "question": "failure triage",
            "story_key": f"F{index}",
            "action": tr.PARK_ACTION,
            "park_class": _CLASS,
            "decided_at": f"2026-11-02T00:00:{index:02d}",
        }
        human = {
            "question": "human resolution",
            "story_key": f"F{index}",
            "ruling_ref": ruling["decided_at"],
            "ruling_action": ruling["action"],
            "park_class": _CLASS,
            "human_action": "resolve",
        }
        records += [ruling, human]
    return records


# --- thresholds() resolution -------------------------------------------------


def test_shipped_matrix_yields_default_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_org(monkeypatch, SHIPPED_MATRIX)

    assert tr.thresholds() == _DEFAULTS


def test_org_layer_tightening_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def tighten(payload: dict) -> None:
        _rule(payload, _MIN_PAIRED_RULE)["value"] = 40
        _rule(payload, _RATE_RULE)["value"] = 5

    _set_org(monkeypatch, _org_layer(tmp_path, tighten))

    assert tr.thresholds() == {"min_paired": 40, "max_false_park_percent": 5}


def test_team_layer_tightening_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_org(monkeypatch, SHIPPED_MATRIX)
    monkeypatch.setenv(
        "PIPELINE_AUTHORITY_TEAM_LAYER", str(_team_layer(tmp_path, {_MIN_PAIRED_RULE: 25}))
    )

    assert tr.thresholds()["min_paired"] == 25


def test_team_layer_loosening_is_refused_and_default_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_org(monkeypatch, SHIPPED_MATRIX)
    monkeypatch.setenv(
        "PIPELINE_AUTHORITY_TEAM_LAYER", str(_team_layer(tmp_path, {_MIN_PAIRED_RULE: 10}))
    )

    assert tr.thresholds()["min_paired"] == 20


def test_org_value_below_bounds_makes_matrix_unusable_and_defaults_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def loosen_below_bounds(payload: dict) -> None:
        _rule(payload, _MIN_PAIRED_RULE)["value"] = 10

    _set_org(monkeypatch, _org_layer(tmp_path, loosen_below_bounds))

    assert tr.thresholds() == _DEFAULTS


def test_org_min_paired_bounds_edited_down_are_clamped_in_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def edit_bounds(payload: dict) -> None:
        rule = _rule(payload, _MIN_PAIRED_RULE)
        rule["bounds"] = {"min": 5, "max": 100000}
        rule["value"] = 5

    _set_org(monkeypatch, _org_layer(tmp_path, edit_bounds))

    assert tr.thresholds()["min_paired"] == 20


def test_org_rate_bounds_edited_up_are_clamped_in_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def edit_bounds(payload: dict) -> None:
        rule = _rule(payload, _RATE_RULE)
        rule["bounds"] = {"min": 0, "max": 50}
        rule["value"] = 50

    _set_org(monkeypatch, _org_layer(tmp_path, edit_bounds))

    assert tr.thresholds()["max_false_park_percent"] == 10


def test_unreadable_matrix_yields_defaults_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_org(monkeypatch, tmp_path / "missing.json")

    assert tr.thresholds() == _DEFAULTS


def test_org_layer_without_readiness_rules_yields_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def drop_readiness(payload: dict) -> None:
        payload["rules"] = [r for r in payload["rules"] if r["class"] != "readiness"]

    _set_org(monkeypatch, _org_layer(tmp_path, drop_readiness))

    assert tr.thresholds() == _DEFAULTS


# --- readiness() uses the thresholds in force --------------------------------


def test_tightened_min_paired_blocks_class_with_too_few_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def tighten(payload: dict) -> None:
        _rule(payload, _MIN_PAIRED_RULE)["value"] = 40

    _set_org(monkeypatch, _org_layer(tmp_path, tighten))

    verdict = tr.readiness(_paired_agree(30))[_CLASS]

    assert verdict["eligible"] is False
    assert "30 paired decisions, 40 required" in verdict["blockers"]


def test_default_min_paired_admits_the_same_class(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_org(monkeypatch, SHIPPED_MATRIX)

    verdict = tr.readiness(_paired_agree(30))[_CLASS]

    assert "30 paired decisions, 20 required" not in verdict["blockers"]
    assert verdict["eligible"] is True


def test_tightened_rate_blocks_a_rate_the_default_allows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2 false parks in 30 paired decisions is 6.7%: above 5%, below 10%.
    records = _paired_agree(28) + _paired_false_parks(2)

    _set_org(monkeypatch, SHIPPED_MATRIX)
    assert tr.readiness(records)[_CLASS]["eligible"] is True

    def tighten(payload: dict) -> None:
        _rule(payload, _RATE_RULE)["value"] = 5

    _set_org(monkeypatch, _org_layer(tmp_path, tighten))
    verdict = tr.readiness(records)[_CLASS]

    assert verdict["eligible"] is False
    assert "false-park rate above 5%" in verdict["blockers"]


def test_explicit_limits_override_the_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_org(monkeypatch, SHIPPED_MATRIX)
    limits = {"min_paired": 5, "max_false_park_percent": 10}

    verdict = tr.readiness(_paired_agree(6), limits=limits)[_CLASS]

    assert verdict["eligible"] is True
    assert verdict["thresholds"] == limits


# --- every verdict carries thresholds and tier -------------------------------


def test_every_verdict_carries_thresholds_and_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_org(monkeypatch, SHIPPED_MATRIX)

    report = tr.readiness([])

    assert report
    for verdict in report.values():
        assert verdict["thresholds"] == _DEFAULTS
        assert "tier" in verdict


def test_high_risk_merge_tier_is_human_under_shipped_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_org(monkeypatch, SHIPPED_MATRIX)

    assert tr.readiness([])["high_risk_merge"]["tier"] == "human"


def test_park_class_absent_from_matrix_reports_not_governed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_org(monkeypatch, SHIPPED_MATRIX)

    assert tr.readiness([])[_CLASS]["tier"] == "not governed"


def test_table_has_tier_column_after_eligible(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_org(monkeypatch, SHIPPED_MATRIX)

    tr._print_table({"plan-a": tr.readiness([])})

    lines = capsys.readouterr().out.splitlines()
    header = next(line for line in lines if line.startswith("plan"))
    assert header.rstrip().endswith("eligible  tier")


def test_shipped_matrix_loads_with_no_violations() -> None:
    resolved = FileLayerSource(SHIPPED_MATRIX, team_path=None).resolve()

    assert resolved is not None
    assert resolved.violations == []
    assert resolved.values[_MIN_PAIRED_RULE] == 20
