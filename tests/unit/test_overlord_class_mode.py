"""Authority-matrix tiers gate the triage ruling path and the high-risk merge.

``pipeline/overlord_class_mode.py`` binds both unattended paths to the
authority matrix, one decision class at a time:

* ``class_mode`` folds the global ``triage_mode`` kill switch with the class's
  tier: ``execute`` only at ``auto``, ``notify`` at ``auto+notify``, and
  ``shadow`` otherwise.
* ``matrix_tier`` fails closed for ``high_risk_merge`` (always governed) and
  leaves unnamed park classes to ``triage_mode`` alone.

The matrix is a real org layer (and team layer) written to ``tmp_path`` and
pointed at by ``PIPELINE_AUTHORITY_MATRIX`` / ``PIPELINE_AUTHORITY_TEAM_LAYER``,
so the folding in ``pipeline.authority_matrix`` runs for real. Only the
overlord (``_invoke_overlord``) and the notification/decision-log seams are
stubbed.
"""

import json
from pathlib import Path

import pytest

import pipeline.server  # noqa: F401  (pre-existing import cycle; keep first)
from pipeline import authority_matrix as matrix_mod
from pipeline import config as config_mod
from pipeline import merge as merge_mod
from pipeline import overlord as overlord_mod
from pipeline import overlord_class_mode as mode_mod
from pipeline import persistence as persistence_mod
from pipeline import server as server_mod
from pipeline import triage as triage_mod

REPO_ROOT = Path(__file__).resolve().parents[2]

PROCEED_REPLY = (
    "RULING: proceed\nRATIONALE: checks are green and the change is contained"
)
HUMAN_REASON = "overlord ruled proceed (authority tier human: held for a human)"
SHADOW_REASON = "overlord ruled proceed (shadow: held for a human)"


def _rule(class_name, value, delegation="locked", loosest="auto+notify"):
    return {
        "id": f"{class_name}.tier",
        "class": class_name,
        "type": "ordered_enum",
        "values": ["human", "auto+notify", "auto"],
        "value": value,
        "delegation": delegation,
        "bounds": {"loosest": loosest},
    }


def _write_json(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _org(tmp_path, rules, name="org-matrix.json") -> Path:
    return _write_json(
        tmp_path / name,
        {"version": 1, "layer": "org", "rules": rules},
    )


@pytest.fixture(autouse=True)
def _clean_matrix_env(monkeypatch):
    """Each test chooses its own matrix; nothing leaks in from the environment."""
    monkeypatch.delenv("PIPELINE_AUTHORITY_MATRIX", raising=False)
    monkeypatch.delenv("PIPELINE_AUTHORITY_TEAM_LAYER", raising=False)
    monkeypatch.delenv("PIPELINE_TRIAGE_MODE", raising=False)


def _use_org(monkeypatch, path: Path) -> None:
    monkeypatch.setenv("PIPELINE_AUTHORITY_MATRIX", str(path))


# --- class_mode: the kill switch and the tier mapping -------------------------


@pytest.mark.parametrize("tier", ["human", "auto+notify", "auto"])
def test_class_mode_is_shadow_for_every_tier_when_triage_mode_is_shadow(
    monkeypatch, tier
):
    monkeypatch.setattr(mode_mod, "matrix_tier", lambda decision_class: tier)

    assert mode_mod.class_mode("transient_bookkeeping", "shadow") == "shadow"


def test_class_mode_executes_an_unnamed_park_class_under_execute(monkeypatch):
    monkeypatch.setattr(mode_mod, "matrix_tier", lambda decision_class: None)

    assert mode_mod.class_mode("rebase_conflict", "execute") == "execute"


def test_class_mode_is_shadow_for_a_human_tier_under_execute(monkeypatch):
    monkeypatch.setattr(mode_mod, "matrix_tier", lambda decision_class: "human")

    assert mode_mod.class_mode("transient_bookkeeping", "execute") == "shadow"


def test_class_mode_notifies_at_auto_notify_under_execute(monkeypatch):
    monkeypatch.setattr(
        mode_mod, "matrix_tier", lambda decision_class: "auto+notify"
    )

    assert mode_mod.class_mode("transient_bookkeeping", "execute") == "notify"


def test_class_mode_executes_at_auto_under_execute(monkeypatch):
    monkeypatch.setattr(mode_mod, "matrix_tier", lambda decision_class: "auto")

    assert mode_mod.class_mode("transient_bookkeeping", "execute") == "execute"


def test_class_mode_fails_closed_on_an_unknown_tier_string(monkeypatch):
    monkeypatch.setattr(mode_mod, "matrix_tier", lambda decision_class: "bogus")

    assert mode_mod.class_mode("transient_bookkeeping", "execute") == "shadow"


# --- matrix_tier: the fail-closed rules ---------------------------------------


def test_high_risk_merge_is_human_when_the_org_layer_is_unreadable(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(
        "PIPELINE_AUTHORITY_MATRIX", str(tmp_path / "missing-matrix.json")
    )

    assert mode_mod.matrix_tier("high_risk_merge") == "human"


def test_high_risk_merge_is_human_when_the_valid_matrix_omits_it(
    monkeypatch, tmp_path
):
    _use_org(monkeypatch, _org(tmp_path, [_rule("scope_widen", "human")]))

    assert mode_mod.matrix_tier("high_risk_merge") == "human"


def test_unnamed_park_class_is_not_governed_when_the_matrix_is_unreadable(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(
        "PIPELINE_AUTHORITY_MATRIX", str(tmp_path / "missing-matrix.json")
    )

    assert mode_mod.matrix_tier("rebase_conflict") is None


def test_a_named_class_returns_its_tier(monkeypatch, tmp_path):
    _use_org(monkeypatch, _org(tmp_path, [_rule("transient_bookkeeping", "auto")]))

    assert mode_mod.matrix_tier("transient_bookkeeping") == "auto"


def test_a_value_outside_the_tier_vocabulary_is_human(monkeypatch, tmp_path):
    class _FakeSource:
        def resolve(self):
            return matrix_mod.Resolved(
                values={"transient_bookkeeping.tier": "bogus"},
                violations=[],
                layer_shas=[],
            )

    monkeypatch.setattr(mode_mod, "FileLayerSource", lambda: _FakeSource())

    assert mode_mod.matrix_tier("transient_bookkeeping") == "human"


# --- team layer folds tighten-only inside the org envelope -------------------


def _team(tmp_path, value, name="team-layer.json") -> Path:
    return _write_json(
        tmp_path / name,
        {
            "version": 1,
            "layer": "team",
            "rules": [{"id": "high_risk_merge.tier", "value": value}],
        },
    )


def _org_high_risk_adjustable(tmp_path) -> Path:
    return _org(
        tmp_path,
        [
            _rule(
                "high_risk_merge",
                "human",
                delegation="adjustable",
                loosest="auto+notify",
            )
        ],
    )


def test_team_tier_inside_the_org_envelope_is_applied(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "PIPELINE_AUTHORITY_MATRIX", str(_org_high_risk_adjustable(tmp_path))
    )
    monkeypatch.setenv(
        "PIPELINE_AUTHORITY_TEAM_LAYER", str(_team(tmp_path, "auto+notify"))
    )

    assert mode_mod.class_mode("high_risk_merge", "execute") == "notify"


def test_team_tier_outside_the_org_envelope_is_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "PIPELINE_AUTHORITY_MATRIX", str(_org_high_risk_adjustable(tmp_path))
    )
    monkeypatch.setenv("PIPELINE_AUTHORITY_TEAM_LAYER", str(_team(tmp_path, "auto")))

    assert mode_mod.class_mode("high_risk_merge", "execute") == "shadow"


# --- merge path: _merge_decision reads the tier ------------------------------


def _story(**overrides):
    story = {
        "key": "TIER-STORY",
        "plan_name": "PLAN-1",
        "status": "pr_open",
        "parked_reason": None,
        "review_verdict": "APPROVE",
        "security_review_verdict": "APPROVE",
        "risk": "high",
        "summary": "Rewrite the auth token cache",
        "pr_checks": {"ci": "pass", "lint": "pass"},
    }
    story.update(overrides)
    return story


@pytest.fixture
def merge_harness(monkeypatch):
    """Stub the overlord and the notification/decision seams for a full merge."""
    monkeypatch.setattr(config_mod, "PIPELINE_AUTO_MERGE", True)
    monkeypatch.setattr(server_mod, "PIPELINE_AUTONOMY", "full", raising=False)
    monkeypatch.setattr(server_mod, "PIPELINE_RISK_THRESHOLD", "low", raising=False)
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "execute")

    state = {"notified": []}
    monkeypatch.setattr(overlord_mod, "_invoke_overlord", lambda *a, **k: PROCEED_REPLY)
    monkeypatch.setattr(
        persistence_mod,
        "_plan_role_config",
        lambda plan_name: {"role": "overlord", "model": "opus"},
    )
    monkeypatch.setattr(
        persistence_mod, "_append_decision", lambda plan_name, record: None
    )
    monkeypatch.setattr(
        persistence_mod,
        "_notify_user",
        lambda plan_name, message: state["notified"].append(message),
    )
    return state


def _decide():
    with merge_mod.merge_adjudication_plan("PLAN-1"):
        return merge_mod._merge_decision(_story(), "PLAN-1")


def test_merge_is_held_at_human_tier_under_execute(
    monkeypatch, tmp_path, merge_harness
):
    _use_org(monkeypatch, _org(tmp_path, [_rule("high_risk_merge", "human")]))

    decision = _decide()

    assert decision == {"action": "park", "reason": HUMAN_REASON}
    assert merge_harness["notified"] == []


def test_merge_proceeds_and_notifies_once_at_auto_notify_tier(
    monkeypatch, tmp_path, merge_harness
):
    _use_org(
        monkeypatch,
        _org(tmp_path, [_rule("high_risk_merge", "auto+notify")]),
    )

    decision = _decide()

    assert decision == {"action": "merge", "reason": "overlord ruled proceed"}
    assert len(merge_harness["notified"]) == 1
    assert "high-risk merge proceeding at tier auto+notify" in (
        merge_harness["notified"][0]
    )


def test_merge_proceeds_without_notify_at_auto_tier(
    monkeypatch, tmp_path, merge_harness
):
    _use_org(monkeypatch, _org(tmp_path, [_rule("high_risk_merge", "auto")]))

    decision = _decide()

    assert decision == {"action": "merge", "reason": "overlord ruled proceed"}
    assert merge_harness["notified"] == []


def test_merge_stays_shadow_held_when_triage_mode_is_shadow(
    monkeypatch, tmp_path, merge_harness
):
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "shadow")
    _use_org(monkeypatch, _org(tmp_path, [_rule("high_risk_merge", "auto")]))

    decision = _decide()

    assert decision == {"action": "park", "reason": SHADOW_REASON}


# --- triage path: a ruling is shadowed by its park class tier ----------------


@pytest.fixture
def triage_harness(monkeypatch):
    """Drive ``_apply_ruling_for_mode`` with the executor and seams stubbed."""
    monkeypatch.setattr(server_mod, "PIPELINE_AUTONOMY", "gated", raising=False)
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "execute")

    calls = {"executor": [], "records": [], "notified": []}

    def fake_executor(plan_name, story_key, story, ruling, manifest):
        calls["executor"].append(story_key)
        return "executed"

    monkeypatch.setitem(triage_mod.EXECUTORS, "retry_transient", fake_executor)
    monkeypatch.setattr(
        triage_mod,
        "_record_execution",
        lambda plan_name, story_key, action, result, mode, *rest, **extra: (
            calls["records"].append((action, result, mode))
        ),
    )
    monkeypatch.setattr(
        triage_mod,
        "_notify_user",
        lambda plan_name, message: calls["notified"].append(message),
    )
    return calls


def _ruling(park_class):
    return {
        "action": "retry_transient",
        "park_class": park_class,
        "rationale": "transient bookkeeping failure",
    }


def test_a_park_class_held_at_human_tier_shadows_and_calls_no_executor(
    monkeypatch, tmp_path, triage_harness
):
    _use_org(
        monkeypatch, _org(tmp_path, [_rule("transient_bookkeeping", "human")])
    )

    outcome = triage_mod._apply_ruling_for_mode(
        "PLAN-1", "TRIAGE-STORY", _story(), _ruling("transient_bookkeeping"), {}
    )

    assert outcome == "shadow"
    assert triage_harness["executor"] == []


def test_an_unnamed_park_class_executes_under_execute(
    monkeypatch, tmp_path, triage_harness
):
    _use_org(monkeypatch, _org(tmp_path, [_rule("high_risk_merge", "human")]))

    outcome = triage_mod._apply_ruling_for_mode(
        "PLAN-1", "TRIAGE-STORY", _story(), _ruling("transient_bookkeeping"), {}
    )

    assert outcome == "executed"
    assert triage_harness["executor"] == ["TRIAGE-STORY"]


# --- docs: the prose names the tier gate that the code now reads -------------


def _flat(path: Path) -> str:
    """Collapse hard-wrapping so a sentence split across lines still matches."""
    return " ".join(path.read_text(encoding="utf-8").split())


def test_overlord_policy_says_the_merge_gate_reads_the_matrix():
    assert "The merge gate reads the matrix" in _flat(REPO_ROOT / "overlord-policy.md")


@pytest.mark.parametrize("doc", ["README.md", "REFERENCE.md"])
def test_doc_names_the_high_risk_merge_tier_gate(doc):
    assert "`high_risk_merge` tier is `auto+notify` or `auto`" in _flat(
        REPO_ROOT / doc
    )
