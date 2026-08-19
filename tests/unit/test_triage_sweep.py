"""Tests for the triage sweep orchestrator in pipeline.triage.

These tests pin the new public surface added by the "triage sweep" story:

* ``TRIAGE_MAX_PER_TICK`` - a module-level literal constant bounding the
  number of candidates processed per scheduler tick.
* ``run_triage_sweep(plan_name)`` - the orchestrator. It must NEVER raise:
  every code path returns a well-formed dict. The flag-off short-circuit
  happens before any manifest read, manifest write, overlord call, or token
  spend.

The implementation does not exist yet, so this suite is RED on purpose; a
later dispatch implements against it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import pipeline.triage
from pipeline import triage as triage_mod

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def plan_name():
    return "cap1"


@pytest.fixture
def manifest_path(plan_dir, plan_name):
    return plan_dir / f"{plan_name}.manifest.json"


def _write_manifest(manifest_path: Path, manifest: dict) -> bytes:
    """Write a manifest dict to disk and return its raw bytes for later
    byte-for-byte comparison."""
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest_path.read_bytes()


@pytest.fixture
def parked_story():
    return {
        "status": "parked",
        "parked_reason": "stuck",
        "worktree": "",
        "triage_attempts": 0,
        "triage_actions": [],
    }


@pytest.fixture
def failed_story():
    return {
        "status": "failed",
        "parked_reason": "",
        "worktree": "",
        "triage_attempts": 0,
        "triage_actions": [],
    }


@pytest.fixture(autouse=True)
def _patch_notify(monkeypatch):
    """Replace pipeline.triage._notify_user with a no-op stub.

    Never make a real backend call from these tests.
    """
    def _fake_notify(*args, **kwargs):
        return None

    monkeypatch.setattr(triage_mod, "_notify_user", _fake_notify)


@pytest.fixture
def patch_rule(monkeypatch):
    """Helper fixture returning a recorder + setter for rule_on_story.

    Returns a dict with a ``calls`` list and a ``set`` callable that installs
    a new rule_on_story implementation.
    """
    state = {"calls": []}

    def _install(impl):
        def _wrapper(plan_name, story_key, story, evidence):
            state["calls"].append(
                {"plan_name": plan_name, "story_key": story_key, "story": story, "evidence": evidence}
            )
            return impl(plan_name, story_key, story, evidence)

        monkeypatch.setattr(triage_mod, "rule_on_story", _wrapper)

    state["set"] = _install
    return state


@pytest.fixture
def patch_classify(monkeypatch):
    """Stub classify_repo_health to return [] (no real subprocess probes)."""
    monkeypatch.setattr(triage_mod, "classify_repo_health", lambda story, checkout, *a, **k: [])


# ---------------------------------------------------------------------------
# Module-level constant
# ---------------------------------------------------------------------------

class TestTriageMaxPerTick:
    def test_constant_exists_and_is_literal_one(self):
        """TRIAGE_MAX_PER_TICK ships as the plain literal 1."""
        assert triage_mod.TRIAGE_MAX_PER_TICK == 1

    def test_constant_is_int(self):
        assert isinstance(triage_mod.TRIAGE_MAX_PER_TICK, int)

    def test_constant_not_read_from_environment(self, monkeypatch):
        """The cap is a module-level literal, not env-derived."""
        monkeypatch.setenv("TRIAGE_MAX_PER_TICK", "99")
        assert triage_mod.TRIAGE_MAX_PER_TICK == 1


# ---------------------------------------------------------------------------
# __all__ membership
# ---------------------------------------------------------------------------

class TestAllMembership:
    def test_run_triage_sweep_in_all(self):
        assert "run_triage_sweep" in triage_mod.__all__

    def test_triage_max_per_tick_in_all(self):
        assert "TRIAGE_MAX_PER_TICK" in triage_mod.__all__


# ---------------------------------------------------------------------------
# Flag-off short-circuit (must happen before any manifest read)
# ---------------------------------------------------------------------------

class TestFlagOff:
    def test_unset_flag_returns_disabled(self, plan_dir, plan_name, monkeypatch):
        """PIPELINE_AUTO_TRIAGE unset -> skipped 'disabled'."""
        monkeypatch.delenv("PIPELINE_AUTO_TRIAGE", raising=False)

        # A rule_on_story stub that fails if ever invoked.
        def _fail(*a, **k):
            pytest.fail("rule_on_story must not be called when triage is disabled")

        monkeypatch.setattr(triage_mod, "rule_on_story", _fail)

        result = pipeline.triage.run_triage_sweep(plan_name)
        assert result == {"ok": True, "skipped": "disabled"}

    def test_unset_flag_leaves_manifest_bytes_unchanged(
        self, plan_dir, plan_name, manifest_path, monkeypatch
    ):
        """When the flag is off, the manifest file's bytes must be unchanged
        afterwards - no manifest write at all."""
        monkeypatch.delenv("PIPELINE_AUTO_TRIAGE", raising=False)
        original = _write_manifest(manifest_path, {"paused": False, "stories": {"S1": {"status": "parked"}}})

        def _fail(*a, **k):
            pytest.fail("rule_on_story must not be called when triage is disabled")

        monkeypatch.setattr(triage_mod, "rule_on_story", _fail)

        pipeline.triage.run_triage_sweep(plan_name)
        assert manifest_path.read_bytes() == original

    def test_zero_flag_returns_disabled(self, plan_dir, plan_name, monkeypatch):
        monkeypatch.setenv("PIPELINE_AUTO_TRIAGE", "0")

        def _fail(*a, **k):
            pytest.fail("rule_on_story must not be called when triage is disabled")

        monkeypatch.setattr(triage_mod, "rule_on_story", _fail)

        result = pipeline.triage.run_triage_sweep(plan_name)
        assert result == {"ok": True, "skipped": "disabled"}

    def test_unrecognized_flag_fails_closed_to_disabled(self, plan_dir, plan_name, monkeypatch):
        """PIPELINE_AUTO_TRIAGE='banana' -> unrecognized fails closed to off."""
        monkeypatch.setenv("PIPELINE_AUTO_TRIAGE", "banana")

        def _fail(*a, **k):
            pytest.fail("rule_on_story must not be called when triage is disabled")

        monkeypatch.setattr(triage_mod, "rule_on_story", _fail)

        result = pipeline.triage.run_triage_sweep(plan_name)
        assert result == {"ok": True, "skipped": "disabled"}

    def test_flag_off_no_manifest_file_no_error(self, plan_dir, plan_name, monkeypatch):
        """Even with no manifest file present, flag-off must short-circuit
        before any read attempt."""
        monkeypatch.delenv("PIPELINE_AUTO_TRIAGE", raising=False)

        def _fail(*a, **k):
            pytest.fail("rule_on_story must not be called when triage is disabled")

        monkeypatch.setattr(triage_mod, "rule_on_story", _fail)

        # No manifest written.
        result = pipeline.triage.run_triage_sweep(plan_name)
        assert result == {"ok": True, "skipped": "disabled"}


# ---------------------------------------------------------------------------
# Paused manifest
# ---------------------------------------------------------------------------

class TestPausedManifest:
    def test_paused_manifest_returns_plan_paused(
        self, plan_dir, plan_name, manifest_path, monkeypatch, patch_classify
    ):
        monkeypatch.setenv("PIPELINE_AUTO_TRIAGE", "1")
        _write_manifest(manifest_path, {"paused": True, "stories": {"S1": {"status": "parked"}}})

        def _fail(*a, **k):
            pytest.fail("rule_on_story must not be called for a paused plan")

        monkeypatch.setattr(triage_mod, "rule_on_story", _fail)

        result = pipeline.triage.run_triage_sweep(plan_name)
        assert result == {"ok": True, "skipped": "plan_paused"}

    def test_paused_manifest_bytes_unchanged(
        self, plan_dir, plan_name, manifest_path, monkeypatch, patch_classify
    ):
        monkeypatch.setenv("PIPELINE_AUTO_TRIAGE", "1")
        original = _write_manifest(
            manifest_path, {"paused": True, "stories": {"S1": {"status": "parked"}}}
        )

        monkeypatch.setattr(triage_mod, "rule_on_story", lambda *a, **k: pytest.fail("no call"))

        pipeline.triage.run_triage_sweep(plan_name)
        assert manifest_path.read_bytes() == original


# ---------------------------------------------------------------------------
# No candidates
# ---------------------------------------------------------------------------

class TestNoCandidates:
    def test_only_interrupted_todo_no_candidates(
        self, plan_dir, plan_name, manifest_path, monkeypatch, patch_classify
    ):
        """interrupted/todo stories are NOT triage candidates."""
        monkeypatch.setenv("PIPELINE_AUTO_TRIAGE", "1")
        _write_manifest(
            manifest_path,
            {
                "paused": False,
                "stories": {
                    "S1": {"status": "interrupted"},
                    "S2": {"status": "todo"},
                },
            },
        )

        def _fail(*a, **k):
            pytest.fail("rule_on_story must not be called when there are no candidates")

        monkeypatch.setattr(triage_mod, "rule_on_story", _fail)

        result = pipeline.triage.run_triage_sweep(plan_name)
        assert result == {"ok": True, "triaged": []}

    def test_empty_stories_no_candidates(
        self, plan_dir, plan_name, manifest_path, monkeypatch, patch_classify
    ):
        monkeypatch.setenv("PIPELINE_AUTO_TRIAGE", "1")
        original = _write_manifest(manifest_path, {"paused": False, "stories": {}})

        monkeypatch.setattr(triage_mod, "rule_on_story", lambda *a, **k: pytest.fail("no call"))

        result = pipeline.triage.run_triage_sweep(plan_name)
        assert result == {"ok": True, "triaged": []}
        # No manifest write when nothing changed.
        assert manifest_path.read_bytes() == original


# ---------------------------------------------------------------------------
# Missing manifest
# ---------------------------------------------------------------------------

class TestMissingManifest:
    def test_missing_manifest_returns_no_manifest(
        self, plan_dir, plan_name, monkeypatch
    ):
        monkeypatch.setenv("PIPELINE_AUTO_TRIAGE", "1")

        def _fail(*a, **k):
            pytest.fail("rule_on_story must not be called when manifest is missing")

        monkeypatch.setattr(triage_mod, "rule_on_story", _fail)

        # No manifest file written.
        result = pipeline.triage.run_triage_sweep(plan_name)
        assert result == {"ok": True, "skipped": "no_manifest"}

    def test_missing_manifest_no_exception(self, plan_dir, plan_name, monkeypatch):
        monkeypatch.setenv("PIPELINE_AUTO_TRIAGE", "1")
        monkeypatch.setattr(triage_mod, "rule_on_story", lambda *a, **k: None)
        # Must not raise.
        result = pipeline.triage.run_triage_sweep(plan_name)
        assert result["ok"] is True
        assert result["skipped"] == "no_manifest"


# ---------------------------------------------------------------------------
# Per-story attempt cap (TRIAGE_MAX_ATTEMPTS)
# ---------------------------------------------------------------------------

class TestAttemptCap:
    def test_story_at_max_attempts_is_parked_without_ruling(
        self, plan_dir, plan_name, manifest_path, monkeypatch, patch_classify
    ):
        """A story already at TRIAGE_MAX_ATTEMPTS -> parked with a reason naming
        the cap, and rule_on_story is never called."""
        monkeypatch.setenv("PIPELINE_AUTO_TRIAGE", "1")
        manifest = {
            "paused": False,
            "stories": {
                "S1": {
                    "status": "parked",
                    "parked_reason": "stuck",
                    "worktree": "",
                    "triage_attempts": pipeline.triage.TRIAGE_MAX_ATTEMPTS,
                    "triage_actions": ["park_for_human"],
                },
            },
        }
        _write_manifest(manifest_path, manifest)

        def _fail(*a, **k):
            pytest.fail("rule_on_story must not be called for a capped story")

        monkeypatch.setattr(triage_mod, "rule_on_story", _fail)

        result = pipeline.triage.run_triage_sweep(plan_name)
        assert result["ok"] is True
        # The story was parked (not ruled).
        assert "S1" in result.get("triaged", []) or result.get("triaged") == []
        # The reason must name the cap.
        on_disk = json.loads(manifest_path.read_text())
        reason = on_disk["stories"]["S1"].get("parked_reason", "")
        assert str(pipeline.triage.TRIAGE_MAX_ATTEMPTS) in reason


# ---------------------------------------------------------------------------
# Action-already-tried override
# ---------------------------------------------------------------------------

class TestActionAlreadyTried:
    def test_repeated_action_overridden_to_park_for_human(
        self, plan_dir, plan_name, manifest_path, monkeypatch, patch_classify, patch_rule
    ):
        """flag '1' + a ruling whose action is already in the story's
        triage_actions -> the action passed to _apply_ruling_for_mode is
        'park_for_human'."""
        monkeypatch.setenv("PIPELINE_AUTO_TRIAGE", "1")
        manifest = {
            "paused": False,
            "stories": {
                "S1": {
                    "status": "parked",
                    "parked_reason": "stuck",
                    "worktree": "",
                    "triage_attempts": 0,
                    "triage_actions": ["split_story"],
                },
            },
        }
        _write_manifest(manifest_path, manifest)

        # The overlord rules "split_story" - which is already in triage_actions.
        patch_rule.set(lambda plan_name, key, story, evidence: {
            "ruling": "", "tier": "", "risk": "", "rationale": "try again",
            "notify_user": True, "action": "split_story",
        })

        applied = []

        def _capture_apply(pn, key, story, ruling, man, mpath):
            applied.append(ruling["action"])
            return "park_for_human"

        monkeypatch.setattr(triage_mod, "_apply_ruling_for_mode", _capture_apply)

        result = pipeline.triage.run_triage_sweep(plan_name)
        assert result["ok"] is True
        assert applied == ["park_for_human"], (
            f"expected action overridden to park_for_human, got {applied}"
        )

    def test_repeated_action_reason_names_the_action(
        self, plan_dir, plan_name, manifest_path, monkeypatch, patch_classify, patch_rule
    ):
        """The override reason must name the repeated action."""
        monkeypatch.setenv("PIPELINE_AUTO_TRIAGE", "1")
        manifest = {
            "paused": False,
            "stories": {
                "S1": {
                    "status": "parked",
                    "parked_reason": "",
                    "worktree": "",
                    "triage_attempts": 0,
                    "triage_actions": ["split_story"],
                },
            },
        }
        _write_manifest(manifest_path, manifest)

        patch_rule.set(lambda plan_name, key, story, evidence: {
            "ruling": "", "tier": "", "risk": "", "rationale": "x",
            "notify_user": True, "action": "split_story",
        })

        # Let the real _apply_ruling_for_mode run (it parks in dry-run/gated
        # via _park). We just need the parked_reason to record the override.
        pipeline.triage.run_triage_sweep(plan_name)
        on_disk = json.loads(manifest_path.read_text())
        reason = on_disk["stories"]["S1"].get("parked_reason", "")
        assert "split_story" in reason


# ---------------------------------------------------------------------------
# TRIAGE_MAX_PER_TICK rate limit
# ---------------------------------------------------------------------------

class TestMaxPerTick:
    def test_three_parked_stories_one_ruling(
        self, plan_dir, plan_name, manifest_path, monkeypatch, patch_classify, patch_rule
    ):
        """flag '1' + three parked stories -> exactly one rule_on_story call
        (TRIAGE_MAX_PER_TICK)."""
        monkeypatch.setenv("PIPELINE_AUTO_TRIAGE", "1")
        manifest = {
            "paused": False,
            "stories": {
                "S1": {"status": "parked", "parked_reason": "a", "worktree": "", "triage_attempts": 0, "triage_actions": []},
                "S2": {"status": "parked", "parked_reason": "b", "worktree": "", "triage_attempts": 0, "triage_actions": []},
                "S3": {"status": "parked", "parked_reason": "c", "worktree": "", "triage_attempts": 0, "triage_actions": []},
            },
        }
        _write_manifest(manifest_path, manifest)

        patch_rule.set(lambda plan_name, key, story, evidence: {
            "ruling": "", "tier": "", "risk": "", "rationale": "park",
            "notify_user": True, "action": "park_for_human",
        })

        # Stub _apply_ruling_for_mode so dry-run/autonomy is not re-checked.
        monkeypatch.setattr(
            triage_mod, "_apply_ruling_for_mode",
            lambda *a, **k: "park_for_human",
        )

        result = pipeline.triage.run_triage_sweep(plan_name)
        assert result["ok"] is True
        assert len(patch_rule["calls"]) == 1, (
            f"expected exactly 1 rule_on_story call, got {len(patch_rule['calls'])}"
        )
        # Exactly one key triaged.
        assert len(result["triaged"]) == 1
        # The actions dict maps that one key to its action.
        assert result["actions"] == {result["triaged"][0]: "park_for_human"}

    def test_max_per_tick_respects_attribute_monkeypatch(
        self, plan_dir, plan_name, manifest_path, monkeypatch, patch_classify, patch_rule
    ):
        """Monkeypatching TRIAGE_MAX_PER_TICK to 2 allows two rulings for two
        parked stories."""
        monkeypatch.setenv("PIPELINE_AUTO_TRIAGE", "1")
        monkeypatch.setattr(triage_mod, "TRIAGE_MAX_PER_TICK", 2)
        manifest = {
            "paused": False,
            "stories": {
                "S1": {"status": "parked", "parked_reason": "a", "worktree": "", "triage_attempts": 0, "triage_actions": []},
                "S2": {"status": "parked", "parked_reason": "b", "worktree": "", "triage_attempts": 0, "triage_actions": []},
            },
        }
        _write_manifest(manifest_path, manifest)

        patch_rule.set(lambda plan_name, key, story, evidence: {
            "ruling": "", "tier": "", "risk": "", "rationale": "park",
            "notify_user": True, "action": "park_for_human",
        })
        monkeypatch.setattr(
            triage_mod, "_apply_ruling_for_mode",
            lambda *a, **k: "park_for_human",
        )

        result = pipeline.triage.run_triage_sweep(plan_name)
        assert result["ok"] is True
        assert len(patch_rule["calls"]) == 2
        assert len(result["triaged"]) == 2


# ---------------------------------------------------------------------------
# Plan-level budget ceiling
# ---------------------------------------------------------------------------

class TestPlanBudget:
    def test_plan_budget_exhausted_parks_without_overlord(
        self, plan_dir, plan_name, manifest_path, monkeypatch, patch_classify, patch_rule
    ):
        """plan_triage_budget_exhausted(manifest) -> _park with a reason naming
        the plan ceiling; do NOT call the overlord."""
        monkeypatch.setenv("PIPELINE_AUTO_TRIAGE", "1")
        manifest = {
            "paused": False,
            "triage_created_stories": pipeline.triage.TRIAGE_MAX_CREATED_STORIES,
            "stories": {
                "S1": {
                    "status": "parked",
                    "parked_reason": "",
                    "worktree": "",
                    "triage_attempts": 0,
                    "triage_actions": [],
                },
            },
        }
        _write_manifest(manifest_path, manifest)

        def _fail(*a, **k):
            pytest.fail("rule_on_story must not be called when plan budget is exhausted")

        monkeypatch.setattr(triage_mod, "rule_on_story", _fail)

        result = pipeline.triage.run_triage_sweep(plan_name)
        assert result["ok"] is True
        on_disk = json.loads(manifest_path.read_text())
        reason = on_disk["stories"]["S1"].get("parked_reason", "")
        # Reason must name the plan ceiling (the created-stories cap).
        assert str(pipeline.triage.TRIAGE_MAX_CREATED_STORIES) in reason or "ceiling" in reason.lower() or "budget" in reason.lower()


# ---------------------------------------------------------------------------
# record_triage_attempt happens in every mode (no re-rule forever)
# ---------------------------------------------------------------------------

class TestRecordAttempt:
    def test_record_triage_attempt_called_in_dry_run(
        self, plan_dir, plan_name, manifest_path, monkeypatch, patch_classify, patch_rule
    ):
        """record_triage_attempt must run in EVERY mode including dry-run, so
        the sweep does not re-rule the same story every tick."""
        monkeypatch.setenv("PIPELINE_AUTO_TRIAGE", "1")
        # Force dry-run mode so _apply_ruling_for_mode withholds action.
        monkeypatch.setattr("pipeline.server.PIPELINE_AUTONOMY", "dry-run")
        manifest = {
            "paused": False,
            "stories": {
                "S1": {
                    "status": "parked",
                    "parked_reason": "",
                    "worktree": "",
                    "triage_attempts": 0,
                    "triage_actions": [],
                },
            },
        }
        _write_manifest(manifest_path, manifest)

        patch_rule.set(lambda plan_name, key, story, evidence: {
            "ruling": "", "tier": "", "risk": "", "rationale": "park",
            "notify_user": True, "action": "park_for_human",
        })

        result = pipeline.triage.run_triage_sweep(plan_name)
        assert result["ok"] is True
        on_disk = json.loads(manifest_path.read_text())
        # triage_attempts must have been incremented even in dry-run.
        assert on_disk["stories"]["S1"]["triage_attempts"] == 1


# ---------------------------------------------------------------------------
# classify_repo_health fallback
# ---------------------------------------------------------------------------

class TestClassifyRepoHealthFallback:
    def test_classify_repo_health_exception_falls_back_to_empty(
        self, plan_dir, plan_name, manifest_path, monkeypatch, patch_rule
    ):
        """classify_repo_health raising must fall back to [] (try/except),
        not propagate."""
        monkeypatch.setenv("PIPELINE_AUTO_TRIAGE", "1")
        manifest = {
            "paused": False,
            "stories": {
                "S1": {
                    "status": "parked",
                    "parked_reason": "",
                    "worktree": "",
                    "triage_attempts": 0,
                    "triage_actions": [],
                },
            },
        }
        _write_manifest(manifest_path, manifest)

        def _boom(*a, **k):
            raise RuntimeError("probe failed")

        monkeypatch.setattr(triage_mod, "classify_repo_health", _boom)

        patch_rule.set(lambda plan_name, key, story, evidence: {
            "ruling": "", "tier": "", "risk": "", "rationale": "park",
            "notify_user": True, "action": "park_for_human",
        })
        monkeypatch.setattr(
            triage_mod, "_apply_ruling_for_mode",
            lambda *a, **k: "park_for_human",
        )

        # Must not raise.
        result = pipeline.triage.run_triage_sweep(plan_name)
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# Never raises
# ---------------------------------------------------------------------------

class TestNeverRaises:
    def test_rule_on_story_runtime_error_returns_ok_false(
        self, plan_dir, plan_name, manifest_path, monkeypatch, patch_classify, patch_rule
    ):
        """rule_on_story raising RuntimeError -> run_triage_sweep returns
        {'ok': False, ...} and nothing propagates."""
        monkeypatch.setenv("PIPELINE_AUTO_TRIAGE", "1")
        _write_manifest(
            manifest_path,
            {
                "paused": False,
                "stories": {
                    "S1": {
                        "status": "parked",
                        "parked_reason": "",
                        "worktree": "",
                        "triage_attempts": 0,
                        "triage_actions": [],
                    }
                },
            },
        )

        def _boom(*a, **k):
            raise RuntimeError("overlord exploded")

        patch_rule.set(_boom)
        monkeypatch.setattr(
            triage_mod, "_apply_ruling_for_mode",
            lambda *a, **k: "park_for_human",
        )

        result = pipeline.triage.run_triage_sweep(plan_name)
        assert result["ok"] is False
        assert result["error"] == "RuntimeError"

    def test_rule_on_story_value_error_returns_ok_false(
        self, plan_dir, plan_name, manifest_path, monkeypatch, patch_classify, patch_rule
    ):
        monkeypatch.setenv("PIPELINE_AUTO_TRIAGE", "1")
        _write_manifest(
            manifest_path,
            {
                "paused": False,
                "stories": {
                    "S1": {
                        "status": "parked",
                        "parked_reason": "",
                        "worktree": "",
                        "triage_attempts": 0,
                        "triage_actions": [],
                    }
                },
            },
        )

        def _boom(*a, **k):
            raise ValueError("bad")

        patch_rule.set(_boom)
        monkeypatch.setattr(
            triage_mod, "_apply_ruling_for_mode",
            lambda *a, **k: "park_for_human",
        )

        result = pipeline.triage.run_triage_sweep(plan_name)
        assert result["ok"] is False
        assert result["error"] == "ValueError"

    def test_no_manifest_no_exception(self, plan_dir, plan_name, monkeypatch):
        """A missing manifest file -> skipped 'no_manifest', no exception."""
        monkeypatch.setenv("PIPELINE_AUTO_TRIAGE", "1")
        monkeypatch.setattr(triage_mod, "rule_on_story", lambda *a, **k: None)
        result = pipeline.triage.run_triage_sweep(plan_name)
        assert result == {"ok": True, "skipped": "no_manifest"}


# ---------------------------------------------------------------------------
# Return-shape contract
# ---------------------------------------------------------------------------

class TestReturnShape:
    def test_happy_path_return_shape(
        self, plan_dir, plan_name, manifest_path, monkeypatch, patch_classify, patch_rule
    ):
        """The happy-path return is {'ok': True, 'triaged': [<keys>],
        'actions': {<key>: <action>}}."""
        monkeypatch.setenv("PIPELINE_AUTO_TRIAGE", "1")
        _write_manifest(
            manifest_path,
            {
                "paused": False,
                "stories": {
                    "S1": {
                        "status": "parked",
                        "parked_reason": "",
                        "worktree": "",
                        "triage_attempts": 0,
                        "triage_actions": [],
                    }
                },
            },
        )

        patch_rule.set(lambda plan_name, key, story, evidence: {
            "ruling": "", "tier": "", "risk": "", "rationale": "park",
            "notify_user": True, "action": "park_for_human",
        })
        monkeypatch.setattr(
            triage_mod, "_apply_ruling_for_mode",
            lambda *a, **k: "park_for_human",
        )

        result = pipeline.triage.run_triage_sweep(plan_name)
        assert result["ok"] is True
        assert result["triaged"] == ["S1"]
        assert result["actions"] == {"S1": "park_for_human"}

    def test_manifest_persisted_after_change(
        self, plan_dir, plan_name, manifest_path, monkeypatch, patch_classify, patch_rule
    ):
        """The manifest is persisted with _atomic_write_json only when
        something changed."""
        monkeypatch.setenv("PIPELINE_AUTO_TRIAGE", "1")
        _write_manifest(
            manifest_path,
            {
                "paused": False,
                "stories": {
                    "S1": {
                        "status": "parked",
                        "parked_reason": "",
                        "worktree": "",
                        "triage_attempts": 0,
                        "triage_actions": [],
                    }
                },
            },
        )

        patch_rule.set(lambda plan_name, key, story, evidence: {
            "ruling": "", "tier": "", "risk": "", "rationale": "park",
            "notify_user": True, "action": "park_for_human",
        })
        monkeypatch.setattr(
            triage_mod, "_apply_ruling_for_mode",
            lambda *a, **k: "park_for_human",
        )

        pipeline.triage.run_triage_sweep(plan_name)
        on_disk = json.loads(manifest_path.read_text())
        # record_triage_attempt incremented the counter -> manifest changed.
        assert on_disk["stories"]["S1"]["triage_attempts"] == 1


# ---------------------------------------------------------------------------
# Lazy import / circular-import safety
# ---------------------------------------------------------------------------

class TestLazyImport:
    def test_triage_does_not_import_server_at_module_level(self):
        """pipeline.triage must NOT import pipeline.server at module level
        (circular-import rule). The server module must not appear in
        pipeline.triage's loaded modules as a direct attribute import."""
        import pipeline.triage as t
        # The module should not bind `server` as a top-level name.
        assert not hasattr(t, "server"), (
            "pipeline.triage must not bind `server` at module level"
        )
        # And must not have imported the server submodule as an attribute.
        assert "pipeline.server" not in getattr(t, "__dict__", {})