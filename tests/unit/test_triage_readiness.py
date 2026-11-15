"""Tests for the per-class promotion readiness report (ADR 0014, decision 8).

A park class may leave shadow mode only after enough paired shadow decisions
with no false proceeds and a low false-park rate. ``readiness`` computes those
counts from the decisions stream; these tests pin the thresholds, the pairing
rule and the fail-closed edges. Every test uses synthetic records, so the
thresholds are not tied to whatever the live decisions log holds.
"""

from __future__ import annotations

import json

import pytest

from pipeline import persistence as ppers
from pipeline import triage_readiness as tr

_CLASS = "rebase_conflict"


def _ruling(
    key: str,
    *,
    action: str,
    park_class: str = _CLASS,
    decided_at: str | None = None,
) -> dict:
    return {
        "question": "failure triage",
        "story_key": key,
        "action": action,
        "park_class": park_class,
        "decided_at": decided_at or f"2026-11-01T00:00:{key}",
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


def _paired_park_agree(count: int, prefix: str = "P") -> list[dict]:
    """``count`` paired rulings where the machine parked and the human parked."""
    records: list[dict] = []
    for index in range(count):
        ruling = _ruling(f"{prefix}{index}", action=tr.PARK_ACTION)
        records += [ruling, _human(ruling, "park")]
    return records


def _cls(report: dict, name: str = _CLASS) -> dict:
    return report[name]


# --- eligibility thresholds -------------------------------------------------


def test_nineteen_paired_decisions_are_not_eligible() -> None:
    report = tr.readiness(_paired_park_agree(19))

    assert _cls(report)["paired_count"] == 19
    assert _cls(report)["eligible"] is False


def test_twenty_paired_decisions_with_no_false_outcomes_are_eligible() -> None:
    report = tr.readiness(_paired_park_agree(20))

    assert _cls(report)["paired_count"] == 20
    assert _cls(report)["eligible"] is True


def test_false_park_rate_of_exactly_ten_percent_is_eligible() -> None:
    # 20 paired decisions, 2 of them false parks: 10% exactly.
    records = _paired_park_agree(18)
    for index in range(2):
        ruling = _ruling(f"F{index}", action=tr.PARK_ACTION)
        records += [ruling, _human(ruling, "retry")]

    cls = _cls(tr.readiness(records))

    assert cls["false_park"] == 2
    assert cls["false_park_rate"] == pytest.approx(0.1)
    assert cls["eligible"] is True


def test_false_park_rate_above_ten_percent_is_not_eligible() -> None:
    # 20 paired decisions, 3 false parks: 15%.
    records = _paired_park_agree(17)
    for index in range(3):
        ruling = _ruling(f"F{index}", action=tr.PARK_ACTION)
        records += [ruling, _human(ruling, "rebrief")]

    cls = _cls(tr.readiness(records))

    assert cls["false_park"] == 3
    assert cls["eligible"] is False
    assert any("false-park rate" in reason for reason in cls["blockers"])


def test_one_false_proceed_makes_class_not_eligible() -> None:
    records = _paired_park_agree(19)
    ruling = _ruling("PROCEED", action="resolve_rebase")
    records += [ruling, _human(ruling, "park")]

    cls = _cls(tr.readiness(records))

    assert cls["false_proceed"] == 1
    assert cls["eligible"] is False
    assert any("false proceed" in reason for reason in cls["blockers"])


# --- classification of agreement ------------------------------------------


def test_proceed_ruling_with_non_park_human_action_is_agreement() -> None:
    ruling = _ruling("OK", action="resolve_rebase")
    cls = _cls(tr.readiness([ruling, _human(ruling, "resume_review")]))

    assert cls["agree"] == 1
    assert cls["false_proceed"] == 0
    assert cls["false_park"] == 0


def test_park_ruling_with_park_human_action_is_agreement() -> None:
    ruling = _ruling("OK", action=tr.PARK_ACTION)
    cls = _cls(tr.readiness([ruling, _human(ruling, "park")]))

    assert cls["agree"] == 1


def test_park_ruling_resolved_without_parking_is_false_park() -> None:
    ruling = _ruling("FP", action=tr.PARK_ACTION)
    cls = _cls(tr.readiness([ruling, _human(ruling, "amend_scope")]))

    assert cls["false_park"] == 1
    assert cls["agree"] == 0


# --- pairing ---------------------------------------------------------------


def test_unpaired_rulings_are_counted_but_excluded_from_rates() -> None:
    # Ten paired agreements plus ten rulings with no human resolution.
    records = _paired_park_agree(10)
    records += [_ruling(f"U{i}", action="resolve_rebase") for i in range(10)]

    cls = _cls(tr.readiness(records))

    assert cls["shadow_count"] == 20
    assert cls["paired_count"] == 10
    assert cls["false_proceed"] == 0
    assert cls["false_park_rate"] == 0.0
    assert cls["eligible"] is False


def test_resolution_pairs_only_with_its_own_ruling_reference() -> None:
    ruling = _ruling("S1", action="resolve_rebase", decided_at="2026-11-01T01:00:00")
    stale = _ruling("S1", action="resolve_rebase", decided_at="2026-10-01T01:00:00")
    # The resolution points at the older ruling, so the newer one stays unpaired.
    orphan = _human(stale, "park")

    cls = _cls(tr.readiness([stale, ruling, orphan]))

    assert cls["shadow_count"] == 2
    assert cls["paired_count"] == 1
    assert cls["false_proceed"] == 1


def test_execution_records_are_not_counted_as_rulings() -> None:
    execution = {"question": "failure triage execution", "story_key": "E1", "action": "resolve_rebase"}

    cls = _cls(tr.readiness([execution]))

    assert cls["shadow_count"] == 0


# --- empty and unknown input fail closed ----------------------------------


def test_empty_input_reports_no_class_eligible() -> None:
    report = tr.readiness([])

    assert report, "every known class is reported, even with no decisions"
    assert not any(cls["eligible"] for cls in report.values())
    assert all(cls["paired_count"] == 0 for cls in report.values())
    assert all(cls["false_park_rate"] is None for cls in report.values())


def test_class_with_no_paired_decisions_gives_no_rate_and_a_blocker() -> None:
    cls = _cls(tr.readiness([_ruling("N", action="resolve_rebase")]))

    assert cls["false_park_rate"] is None
    assert cls["eligible"] is False
    assert cls["blockers"], "a denial must name its reasons"


def test_ruling_without_park_class_is_bucketed_as_unknown() -> None:
    ruling = _ruling("L", action=tr.PARK_ACTION)
    del ruling["park_class"]

    report = tr.readiness([ruling, _human({**ruling, "park_class": None}, "park")])

    assert report["unknown"]["paired_count"] == 1


# --- CLI, read-only ---------------------------------------------------------


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    return d


def _write_decisions(plan_dir, plan: str, records: list[dict]) -> bytes:
    path = plan_dir / f"{plan}.decisions.json"
    path.write_text(json.dumps(records))
    return path.read_bytes()


def test_cli_json_output_for_named_plan_is_read_only(plan_dir, capsys) -> None:
    before = _write_decisions(plan_dir, "forge", _paired_park_agree(20))

    code = tr.main(["--plan", "forge", "--json"])

    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["forge"][_CLASS]["eligible"] is True
    assert (plan_dir / "forge.decisions.json").read_bytes() == before


def test_cli_all_reads_every_plan_and_writes_nothing(plan_dir, capsys) -> None:
    _write_decisions(plan_dir, "alpha", _paired_park_agree(3))
    _write_decisions(plan_dir, "beta", _paired_park_agree(2))
    before = sorted(p.name for p in plan_dir.iterdir())

    code = tr.main(["--all", "--json"])

    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert set(report) == {"alpha", "beta"}
    assert sorted(p.name for p in plan_dir.iterdir()) == before


def test_cli_table_output_names_each_class(plan_dir, capsys) -> None:
    _write_decisions(plan_dir, "forge", _paired_park_agree(1))

    code = tr.main(["--plan", "forge"])

    out = capsys.readouterr().out
    assert code == 0
    assert _CLASS in out
    assert "eligible" in out


def test_cli_refuses_when_neither_plan_nor_all_is_given(plan_dir, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        tr.main([])

    assert exc.value.code != 0
    assert "--all" in capsys.readouterr().err


def test_cli_missing_decisions_log_is_an_explicit_empty_report(plan_dir, capsys) -> None:
    code = tr.main(["--plan", "ghost", "--json"])

    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert not any(cls["eligible"] for cls in report["ghost"].values())


def test_cli_unreadable_decisions_log_is_refused_not_reported_empty(plan_dir, capsys) -> None:
    (plan_dir / "broken.decisions.json").write_text("{not json")

    code = tr.main(["--plan", "broken", "--json"])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "could not be read" in captured.err


def test_cli_refuses_a_plan_name_that_escapes_the_plan_directory(plan_dir, capsys) -> None:
    escaped = plan_dir.parent / "escape.decisions.json"
    escaped.write_text(json.dumps(_paired_park_agree(20)))

    code = tr.main(["--plan", "../escape", "--json"])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "invalid plan name" in captured.err
    assert "../escape" not in captured.err


def test_cli_refuses_a_decisions_log_holding_a_non_object_record(plan_dir, capsys) -> None:
    (plan_dir / "stray.decisions.json").write_text(json.dumps([1, "two"]))

    code = tr.main(["--plan", "stray", "--json"])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "could not be read" in captured.err
