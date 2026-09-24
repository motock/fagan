"""A transient overlord failure defers triage instead of parking the story.

PRH-2 (2026-09-24): the overlord call failed with a RuntimeError in the same
minute the reviewer logged a transport failure; triage failed open, spent an
attempt and parked the story for a human. A transport blip is not a ruling.
The sweep now skips such a story for the tick - no attempt recorded, no park -
at most TRIAGE_MAX_TRANSIENT_DEFERRALS times, then rules on it as before.

No real backend and no real git: the overlord, evidence and apply helpers are
monkeypatched on ``pipeline.triage``.
"""

import json

import pytest

import pipeline.server  # noqa: F401 - binds the server globals the sweep reads
from pipeline import triage

PLAN_NAME = "transient1"
EVIDENCE = "=== FAILURE EVIDENCE ===\nsomething broke\n"


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    import pipeline.server as p
    from pipeline import concurrency as pcon
    from pipeline import persistence as ppers

    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


@pytest.fixture
def manifest_path(plan_dir):
    path = plan_dir / f"{PLAN_NAME}.manifest.json"
    story = {
        "status": "parked",
        "parked_reason": "stuck",
        "worktree": "",
        "triage_attempts": 0,
        "triage_actions": [],
    }
    path.write_text(json.dumps({"stories": {"s1": story}}))
    return path


@pytest.fixture
def applied(monkeypatch):
    calls = []
    monkeypatch.setenv("PIPELINE_AUTO_TRIAGE", "1")
    monkeypatch.setattr(triage, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(triage, "classify_repo_health", lambda story, checkout, *a, **k: [])
    monkeypatch.setattr(triage, "collect_triage_evidence", lambda *a, **k: EVIDENCE)

    def _fake_apply(plan_name, story_key, story, ruling, manifest, manifest_path):
        calls.append(story_key)
        return ruling["action"]

    monkeypatch.setattr(triage, "_apply_ruling_for_mode", _fake_apply)
    return calls


@pytest.fixture(autouse=True)
def _stub_ruling_backends(monkeypatch):
    monkeypatch.setattr(triage, "_load_policy", lambda: "POLICY")
    monkeypatch.setattr(triage, "_plan_role_config", lambda plan_name: {})
    monkeypatch.setattr(triage, "_append_decision", lambda plan_name, record: None)


def _timeout_wrapped(prompt, plan_role_config=None):
    try:
        raise TimeoutError("read timed out")
    except TimeoutError as inner:
        raise RuntimeError("overlord backend failed") from inner


def _not_transient(prompt, plan_role_config=None):
    raise KeyError("missing field")


def _story(manifest_path):
    return json.loads(manifest_path.read_text())["stories"]["s1"]


def test_the_deferral_cap_is_three():
    assert triage.TRIAGE_MAX_TRANSIENT_DEFERRALS == 3


def test_a_transient_overlord_failure_marks_the_ruling_transient(monkeypatch):
    monkeypatch.setattr(triage, "_invoke_overlord", _timeout_wrapped)

    ruling = triage.rule_on_story("plan-x", "S-1", {}, EVIDENCE)

    assert ruling["transient"] is True


def test_a_non_transient_overlord_failure_is_not_marked_transient(monkeypatch):
    monkeypatch.setattr(triage, "_invoke_overlord", _not_transient)

    ruling = triage.rule_on_story("plan-x", "S-1", {}, EVIDENCE)

    assert ruling["transient"] is False


def test_a_successful_ruling_carries_no_transient_flag(monkeypatch):
    monkeypatch.setattr(
        triage,
        "_invoke_overlord",
        lambda prompt, plan_role_config=None: (
            "RULING: r\nTIER: t\nRISK: low\nRATIONALE: ok\nNOTIFY_USER: no\nACTION: park_for_human\n"
        ),
    )

    ruling = triage.rule_on_story("plan-x", "S-1", {}, EVIDENCE)

    assert "transient" not in ruling


def test_a_transient_failure_does_not_apply_a_ruling(monkeypatch, manifest_path, applied):
    monkeypatch.setattr(triage, "_invoke_overlord", _timeout_wrapped)

    result = triage.run_triage_sweep(PLAN_NAME)

    assert applied == []
    assert result["triaged"] == []


def test_a_transient_failure_spends_no_triage_attempt(monkeypatch, manifest_path, applied):
    monkeypatch.setattr(triage, "_invoke_overlord", _timeout_wrapped)

    triage.run_triage_sweep(PLAN_NAME)

    assert _story(manifest_path)["triage_attempts"] == 0
    assert _story(manifest_path)["parked_reason"] == "stuck"


def test_a_transient_failure_counts_one_deferral(monkeypatch, manifest_path, applied):
    monkeypatch.setattr(triage, "_invoke_overlord", _timeout_wrapped)

    triage.run_triage_sweep(PLAN_NAME)

    assert _story(manifest_path)["triage_transient_deferrals"] == 1


def test_the_fourth_transient_failure_is_ruled_on(monkeypatch, manifest_path, applied):
    monkeypatch.setattr(triage, "_invoke_overlord", _timeout_wrapped)

    triage.run_triage_sweep(PLAN_NAME)
    triage.run_triage_sweep(PLAN_NAME)
    triage.run_triage_sweep(PLAN_NAME)
    assert applied == []

    triage.run_triage_sweep(PLAN_NAME)

    assert applied == ["s1"]
    assert _story(manifest_path)["triage_attempts"] == 1


def test_a_non_transient_failure_is_ruled_on_at_once(monkeypatch, manifest_path, applied):
    monkeypatch.setattr(triage, "_invoke_overlord", _not_transient)

    triage.run_triage_sweep(PLAN_NAME)

    assert applied == ["s1"]
    assert _story(manifest_path)["triage_attempts"] == 1
    assert "triage_transient_deferrals" not in _story(manifest_path)
