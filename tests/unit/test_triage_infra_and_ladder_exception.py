"""Two independent triage fixes, pinned here.

FIX A - ``run_triage_sweep`` must not let the LLM rule on a story that is
mid infra-transport failure. ``pipeline/story_status.py`` already has a
dedicated classifier for a dispatch that died on an LLM/Ollama transport
error (``INFRA_FAILURE_LOG_SUBSTRING`` in ``last_log_line``) and marks the
story ``interrupted`` for a clean resume, deliberately without touching
triage or the rework/attempt counters. Triage can race ahead of that
classifier (observed live 2026-09-17: a step-0 Ollama transport error with 0
new commits reached the sweep first, the LLM ruled ``repo_issue`` - a
DEFERRED_ACTIONS ruling - and the story burned both triage attempts parked
"for a human" before ``check_story_status`` finally resumed it ~25 minutes
later). So: when the evidence shows BOTH an infra-transport failure AND zero
new commits, skip the story for this tick - no ``rule_on_story``, no
``record_triage_attempt``, no ``_park``, no ``triaged_keys`` entry.

FIX B - ``execute_ruling``'s ``escalate_model`` branch collapsed the real
exception to just its class name, leaving an operator no way to diagnose
what broke. The parked reason must carry the message too, truncated to a
bounded length.

No real git, no real backend: ``classify_repo_health``,
``collect_triage_evidence``, ``rule_on_story``, ``_apply_ruling_for_mode``,
the escalation helpers and ``_notify_user`` are all monkeypatched on
``pipeline.triage``.
"""

from __future__ import annotations

import json

import pytest

# Import pipeline.server first: pipeline.triage -> build_detect -> server ->
# triage is a pre-existing import cycle that only bites when triage is the
# first pipeline module imported (tests/unit/test_triage_sweep.py has the same
# standalone-collection failure). Importing server first initializes triage
# fully, so this module is runnable on its own as well as in the full suite.
import pipeline.server
import pipeline.triage
from pipeline import triage as triage_mod
from pipeline.config import INFRA_FAILURE_LOG_SUBSTRING

PLAN_NAME = "infra1"

# The exact phrasing ``_current_git_state_impl`` emits for a branch with no
# commits beyond base: "BRANCH HAS NO NEW COMMITS vs <base> (0 new commits
# beyond base)".
_NO_NEW_COMMITS_LINE = "BRANCH HAS NO NEW COMMITS vs master (0 new commits beyond base)"
_NEW_COMMITS_LINE = "BRANCH HAS NEW COMMITS vs master: yes"

_INFRA_LINE = f"{INFRA_FAILURE_LOG_SUBSTRING}: connection refused"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Patch every PLAN_DIR binding the sweep could read through."""
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
    return plan_dir / f"{PLAN_NAME}.manifest.json"


@pytest.fixture
def story():
    return {
        "status": "parked",
        "parked_reason": "stuck",
        "worktree": "",
        "triage_attempts": 0,
        "triage_actions": [],
    }


@pytest.fixture
def manifest(story):
    return {"stories": {"s1": story}}


@pytest.fixture
def write_manifest(manifest_path):
    def _write(manifest):
        manifest_path.write_text(json.dumps(manifest, indent=2))
        return manifest_path

    return _write


@pytest.fixture(autouse=True)
def _patch_notify(monkeypatch):
    monkeypatch.setattr(triage_mod, "_notify_user", lambda *a, **k: None)


@pytest.fixture
def sweep(monkeypatch):
    """Wire the sweep for a synthetic evidence string and record the ruling.

    Returns a state dict with the recorded ``rule_on_story`` calls and a
    ``set_evidence`` hook so a test can change the evidence between ticks.
    """
    state = {"rule_calls": [], "evidence": "", "applied": []}

    monkeypatch.setenv("PIPELINE_AUTO_TRIAGE", "1")
    monkeypatch.setattr(
        triage_mod, "classify_repo_health", lambda story, checkout, *a, **k: []
    )
    monkeypatch.setattr(
        triage_mod, "collect_triage_evidence", lambda *a, **k: state["evidence"]
    )

    def _fake_rule(plan_name, story_key, story, evidence):
        state["rule_calls"].append(
            {"plan_name": plan_name, "story_key": story_key, "evidence": evidence}
        )
        return {"action": "park_for_human", "rationale": "synthetic"}

    monkeypatch.setattr(triage_mod, "rule_on_story", _fake_rule)

    def _fake_apply(plan_name, story_key, story, ruling, manifest, manifest_path):
        state["applied"].append(story_key)
        return ruling["action"]

    monkeypatch.setattr(triage_mod, "_apply_ruling_for_mode", _fake_apply)
    return state


def _rule_keys(state):
    return [call["story_key"] for call in state["rule_calls"]]


def _persisted_story(manifest_path, key="s1"):
    """The story as the sweep actually persisted it (the sweep re-reads the
    manifest from disk each tick, so the fixture dict is not the live one)."""
    return json.loads(manifest_path.read_text())["stories"][key]


# ---------------------------------------------------------------------------
# FIX A - skip a story that is mid infra-transport failure with no progress
# ---------------------------------------------------------------------------

class TestInfraFailureShortCircuit:
    def test_skip_infra_and_no_new_commits(
        self, sweep, manifest, write_manifest, manifest_path
    ):
        """Infra substring + zero new commits -> no ruling, no park, no attempt."""
        sweep["evidence"] = f"{_INFRA_LINE}\n{_NO_NEW_COMMITS_LINE}\n"
        write_manifest(manifest)

        result = pipeline.triage.run_triage_sweep(PLAN_NAME)

        assert _rule_keys(sweep) == [], "rule_on_story must not be called"
        assert result["triaged"] == []
        assert "s1" not in result["triaged"]
        persisted = _persisted_story(manifest_path)
        assert persisted["status"] == "parked"
        assert persisted["parked_reason"] == "stuck", "story must not be re-parked"
        assert persisted["triage_attempts"] == 0
        assert persisted["triage_actions"] == []

    def test_no_skip_infra_but_progress(self, sweep, manifest, write_manifest):
        """Infra substring but the agent DID commit -> normal ruling path."""
        sweep["evidence"] = f"{_INFRA_LINE}\n{_NEW_COMMITS_LINE}\n"
        write_manifest(manifest)

        pipeline.triage.run_triage_sweep(PLAN_NAME)

        assert _rule_keys(sweep) == ["s1"]

    def test_no_skip_no_commits_but_no_infra(self, sweep, manifest, write_manifest):
        """Zero new commits with no infra substring -> normal ruling path."""
        sweep["evidence"] = f"{_NO_NEW_COMMITS_LINE}\n"
        write_manifest(manifest)

        pipeline.triage.run_triage_sweep(PLAN_NAME)

        assert _rule_keys(sweep) == ["s1"]

    def test_skip_does_not_burn_attempts_across_ticks(
        self, sweep, manifest, write_manifest, manifest_path
    ):
        """The subtle part: two infra ticks must leave the attempt counter at 0.

        A careless implementation that still calls ``record_triage_attempt``
        (or ``_park``) before ``continue`` looks correct on tick 1 but has
        burned both attempts by tick 3, so the story parks instead of ruling.
        """
        sweep["evidence"] = f"{_INFRA_LINE}\n{_NO_NEW_COMMITS_LINE}\n"
        write_manifest(manifest)

        # Tick 1
        result1 = pipeline.triage.run_triage_sweep(PLAN_NAME)
        assert _rule_keys(sweep) == []
        assert result1["triaged"] == []
        assert _persisted_story(manifest_path)["triage_attempts"] == 0
        assert _persisted_story(manifest_path)["status"] == "parked"
        assert _persisted_story(manifest_path)["parked_reason"] == "stuck"

        # Tick 2 - same evidence, same outcome, counter still untouched.
        result2 = pipeline.triage.run_triage_sweep(PLAN_NAME)
        assert _rule_keys(sweep) == []
        assert result2["triaged"] == []
        assert _persisted_story(manifest_path)["triage_attempts"] == 0
        assert _persisted_story(manifest_path)["status"] == "parked"
        assert _persisted_story(manifest_path)["parked_reason"] == "stuck"

        # Tick 3 - the infra failure cleared and the agent made progress, so
        # triage rules normally and spends exactly one attempt.
        sweep["evidence"] = f"{_NEW_COMMITS_LINE}\n"
        result3 = pipeline.triage.run_triage_sweep(PLAN_NAME)
        assert _rule_keys(sweep) == ["s1"]
        assert result3["triaged"] == ["s1"]
        assert _persisted_story(manifest_path)["triage_attempts"] == 1


# ---------------------------------------------------------------------------
# FIX B - keep the real exception when the escalation ladder is exhausted
# ---------------------------------------------------------------------------

@pytest.fixture
def escalate_ruling():
    return {
        "action": "escalate_model",
        "rationale": "local model failed; try the next rung",
        "ruling": "escalate",
        "tier": "tier1",
        "risk": "low",
        "notify_user": True,
    }


@pytest.fixture
def escalate_story():
    return {
        "status": "failed",
        "model": "qwen2.5-coder",
        "backend": "local",
    }


def _drive_escalate(monkeypatch, story, ruling, exc):
    """Run execute_ruling's escalate_model branch with a raising escalation."""
    monkeypatch.setattr(triage_mod, "_auto_escalation_enabled", lambda: True)
    monkeypatch.setattr(
        triage_mod, "_escalate_to_local_fallback_model", lambda *a, **k: None
    )

    def _boom(*a, **k):
        raise exc

    monkeypatch.setattr(triage_mod, "_escalate_to_claude", _boom)
    return pipeline.triage.execute_ruling(
        PLAN_NAME, "s1", story, ruling, {"stories": {"s1": story}}, None
    )


class TestEscalateLadderException:
    def test_escalate_exception_message_included(
        self, monkeypatch, escalate_story, escalate_ruling
    ):
        """The parked reason carries the exception type AND its message."""
        _drive_escalate(
            monkeypatch,
            escalate_story,
            escalate_ruling,
            FileNotFoundError("no such file: /foo/bar"),
        )

        reason = escalate_story["parked_reason"]
        assert "FileNotFoundError" in reason
        assert "no such file: /foo/bar" in reason
        assert reason == (
            "escalate_model ruled but ladder exhausted: "
            "FileNotFoundError: no such file: /foo/bar"
        )

    def test_escalate_exception_message_truncated(
        self, monkeypatch, escalate_story, escalate_ruling
    ):
        """A pathological message is bounded to 300 chars, prefix intact."""
        _drive_escalate(
            monkeypatch, escalate_story, escalate_ruling, RuntimeError("z" * 400)
        )

        reason = escalate_story["parked_reason"]
        assert "z" * 300 in reason
        assert "z" * 301 not in reason
        assert reason.startswith("escalate_model ruled but ladder exhausted: RuntimeError: ")
