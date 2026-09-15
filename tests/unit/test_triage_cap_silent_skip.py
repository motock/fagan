"""TDD tests for the capped-candidate silent-skip behavior of ``run_triage_sweep``.

Three live defects are pinned here (observed on plan ``chat-worktree-apply``
since 2026-09-14T01:54, stories WAP-1 / WAP-4):

1. NOTIFICATION SPAM -- a candidate that fails ``triage_allowed`` (at the
   attempt cap) is re-parked every ~60s tick forever, so a ``story_parked``
   notification + manifest write fires on every tick.
2. PARKED_REASON CLOBBERING -- ``_park`` overwrites the meaningful reason the
   story was originally parked with the generic cap message.
3. BUDGET STARVATION -- capped candidates still consume the
   ``[:TRIAGE_MAX_PER_TICK]`` slice, so a capped story ahead in sort order
   starves actionable candidates every tick.

The fix (in ``pipeline/triage.py`` only) partitions candidates BEFORE the
per-tick slice: capped + already-``parked`` stories are skipped silently (no
``_park``, no notification, no ``parked_reason`` change, no manifest write) and
recorded in the sweep's return payload under ``skipped_cap``; capped stories
that are NOT yet parked are parked exactly once; the ``TRIAGE_MAX_PER_TICK``
slice is applied to the ACTIONABLE list so capped stories never starve
actionable ones.

These tests drive the REAL ``run_triage_sweep`` with the same hermetic seams
the existing triage suites use: ``_notify_user`` (notification seam),
``_atomic_write_json`` (manifest write seam), ``classify_repo_health``,
``rule_on_story`` and ``_apply_ruling_for_mode`` (the overlord/ruling
boundary). They are RED until the implementation lands.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest

# Import pipeline.server FIRST: pipeline.triage -> build_detect -> server ->
# triage is a circular import, so importing pipeline.triage standalone raises
# ImportError. Importing the server module first breaks the cycle.
import pipeline.server
import pipeline.triage
from pipeline import triage as triage_mod

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_name():
    return "capskip"


@pytest.fixture
def manifest_path(plan_dir, plan_name):
    return plan_dir / f"{plan_name}.manifest.json"


def _write_manifest(manifest_path: Path, manifest: dict) -> bytes:
    """Write a manifest dict to disk and return its raw bytes."""
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest_path.read_bytes()


@pytest.fixture
def enable_triage(monkeypatch):
    """Opt in to the sweep (the suite clears every PIPELINE_* var at import)."""
    monkeypatch.setenv("PIPELINE_AUTO_TRIAGE", "1")


@pytest.fixture
def notify_calls(monkeypatch):
    """Capture every ``_notify_user`` call made by the module under test."""
    calls = []

    def fake_notify(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(triage_mod, "_notify_user", fake_notify)
    return calls


@pytest.fixture
def park_calls(monkeypatch):
    """Record ``_park`` invocations while still running the real ``_park``."""
    calls = []
    real = triage_mod._park

    def fake_park(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return real(*args, **kwargs)

    monkeypatch.setattr(triage_mod, "_park", fake_park)
    return calls


@pytest.fixture
def manifest_writes(monkeypatch):
    """Record manifest writes while still performing the real write."""
    calls = []
    real = triage_mod._atomic_write_json

    def fake_write(path, data, *args, **kwargs):
        calls.append({"path": path, "data": data})
        return real(path, data, *args, **kwargs)

    monkeypatch.setattr(triage_mod, "_atomic_write_json", fake_write)
    return calls


@pytest.fixture
def patch_classify(monkeypatch):
    """Stub classify_repo_health so no real subprocess probes run."""
    monkeypatch.setattr(
        triage_mod, "classify_repo_health", lambda story, checkout, *a, **k: []
    )


@pytest.fixture
def rule_calls(monkeypatch):
    """Stub the overlord ruling boundary and record each invocation."""
    calls = []

    def fake_rule(plan_name, story_key, story, evidence):
        calls.append(
            {
                "plan_name": plan_name,
                "story_key": story_key,
                "story": story,
                "evidence": evidence,
            }
        )
        return {
            "ruling": "",
            "tier": "",
            "risk": "",
            "rationale": "r",
            "notify_user": True,
            "action": "park_for_human",
        }

    monkeypatch.setattr(triage_mod, "rule_on_story", fake_rule)
    return calls


@pytest.fixture
def apply_calls(monkeypatch):
    """Stub the ruling executor boundary and record each invocation."""
    calls = []

    def fake_apply(plan_name, story_key, story, ruling, manifest, manifest_path):
        calls.append(
            {
                "plan_name": plan_name,
                "story_key": story_key,
                "story": story,
                "ruling": ruling,
            }
        )
        return ruling.get("action", "park_for_human")

    monkeypatch.setattr(triage_mod, "_apply_ruling_for_mode", fake_apply)
    return calls


@pytest.fixture
def forbid_ruling(monkeypatch):
    """Fail loudly if the ruling path runs at all."""

    def _fail(*a, **k):
        pytest.fail("the ruling path must not run for a capped, already-parked story")

    monkeypatch.setattr(triage_mod, "rule_on_story", _fail)
    monkeypatch.setattr(triage_mod, "_apply_ruling_for_mode", _fail)


# The diagnostic reason WAP-4 originally carried; the cap message must never
# clobber it.
ORIGINAL_REASON = (
    "no new commit after 2 rework redispatches - agent keeps parking/crashing "
    "without writing code."
)


def _capped_parked_story(reason: str = ORIGINAL_REASON) -> dict:
    """A story that is parked AND at/above the triage attempt cap."""
    return {
        "status": "parked",
        "parked_reason": reason,
        "worktree": "",
        "triage_attempts": triage_mod.TRIAGE_MAX_ATTEMPTS,
        "triage_actions": ["park_for_human"],
    }


def _capped_not_parked_story(status: str) -> dict:
    """A story at/above the attempt cap that is NOT yet parked."""
    story = {
        "status": status,
        "parked_reason": "",
        "worktree": "",
        "triage_attempts": triage_mod.TRIAGE_MAX_ATTEMPTS,
        "triage_actions": [],
    }
    if status not in ("parked", "failed"):
        # Make it a candidate via the step-cap streak trigger.
        story["step_cap_streak"] = triage_mod.STEP_CAP_FALLBACK_THRESHOLD
    return story


# ---------------------------------------------------------------------------
# Capped + already parked -> silent skip
# ---------------------------------------------------------------------------


class TestCappedAlreadyParkedIsSilent:
    def test_sweep_ok_and_records_the_skip(
        self,
        plan_dir,
        plan_name,
        manifest_path,
        enable_triage,
        patch_classify,
        forbid_ruling,
        notify_calls,
        park_calls,
        manifest_writes,
    ):
        _write_manifest(
            manifest_path,
            {"paused": False, "stories": {"S1": _capped_parked_story()}},
        )

        result = pipeline.triage.run_triage_sweep(plan_name)

        assert result["ok"] is True
        assert result["triaged"] == []
        assert "skipped_cap" in result, (
            "the sweep must record capped skips in its return payload"
        )
        assert "S1" in result["skipped_cap"]

    def test_no_park_call_and_no_notification(
        self,
        plan_dir,
        plan_name,
        manifest_path,
        enable_triage,
        patch_classify,
        forbid_ruling,
        notify_calls,
        park_calls,
    ):
        _write_manifest(
            manifest_path,
            {"paused": False, "stories": {"S1": _capped_parked_story()}},
        )

        pipeline.triage.run_triage_sweep(plan_name)

        assert park_calls == [], "a capped, already-parked story must not be re-parked"
        assert notify_calls == [], (
            f"no notification may fire for a capped, already-parked story; got {notify_calls!r}"
        )

    def test_parked_reason_is_unchanged_on_disk(
        self,
        plan_dir,
        plan_name,
        manifest_path,
        enable_triage,
        patch_classify,
        forbid_ruling,
        notify_calls,
        park_calls,
    ):
        original = _write_manifest(
            manifest_path,
            {"paused": False, "stories": {"S1": _capped_parked_story()}},
        )

        pipeline.triage.run_triage_sweep(plan_name)

        on_disk = json.loads(manifest_path.read_text())
        assert on_disk["stories"]["S1"]["parked_reason"] == ORIGINAL_REASON
        assert manifest_path.read_bytes() == original, (
            "the manifest must not be mutated for a capped, already-parked story"
        )

    def test_no_manifest_write_occurs(
        self,
        plan_dir,
        plan_name,
        manifest_path,
        enable_triage,
        patch_classify,
        forbid_ruling,
        notify_calls,
        park_calls,
        manifest_writes,
    ):
        _write_manifest(
            manifest_path,
            {"paused": False, "stories": {"S1": _capped_parked_story()}},
        )

        pipeline.triage.run_triage_sweep(plan_name)

        assert manifest_writes == [], (
            f"no manifest write may occur for a capped, already-parked story; got {manifest_writes!r}"
        )

    def test_second_sweep_is_identical_and_still_silent(
        self,
        plan_dir,
        plan_name,
        manifest_path,
        enable_triage,
        patch_classify,
        forbid_ruling,
        notify_calls,
        park_calls,
        manifest_writes,
    ):
        original = _write_manifest(
            manifest_path,
            {"paused": False, "stories": {"S1": _capped_parked_story()}},
        )

        first = pipeline.triage.run_triage_sweep(plan_name)
        second = pipeline.triage.run_triage_sweep(plan_name)

        assert first["ok"] is True and second["ok"] is True
        assert "skipped_cap" in first and "S1" in first["skipped_cap"]
        assert "skipped_cap" in second and "S1" in second["skipped_cap"]
        assert park_calls == []
        assert notify_calls == []
        assert manifest_writes == []
        assert manifest_path.read_bytes() == original


# ---------------------------------------------------------------------------
# Capped + NOT parked -> parked exactly once
# ---------------------------------------------------------------------------


class TestCappedNotParkedIsParkedOnce:
    @pytest.mark.parametrize("status", ["failed", "in_progress"])
    def test_parks_once_with_the_cap_reason(
        self,
        status,
        plan_dir,
        plan_name,
        manifest_path,
        enable_triage,
        patch_classify,
        forbid_ruling,
        notify_calls,
        park_calls,
    ):
        story = _capped_not_parked_story(status)
        expected_reason = triage_mod.triage_allowed(dict(story))[1]
        assert expected_reason, "the cap reason must be non-empty"
        _write_manifest(manifest_path, {"paused": False, "stories": {"S1": story}})

        pipeline.triage.run_triage_sweep(plan_name)

        assert len(park_calls) == 1, (
            f"a capped, not-yet-parked story must be parked exactly once; got {park_calls!r}"
        )
        on_disk = json.loads(manifest_path.read_text())
        assert on_disk["stories"]["S1"]["status"] == "parked"
        assert on_disk["stories"]["S1"]["parked_reason"] == expected_reason
        assert "at or above cap" in on_disk["stories"]["S1"]["parked_reason"]

    @pytest.mark.parametrize("status", ["failed", "in_progress"])
    def test_one_story_parked_notification(
        self,
        status,
        plan_dir,
        plan_name,
        manifest_path,
        enable_triage,
        patch_classify,
        forbid_ruling,
        notify_calls,
        park_calls,
    ):
        _write_manifest(
            manifest_path,
            {"paused": False, "stories": {"S1": _capped_not_parked_story(status)}},
        )

        pipeline.triage.run_triage_sweep(plan_name)

        assert len(notify_calls) == 1, (
            f"expected exactly one notification, got {notify_calls!r}"
        )
        assert notify_calls[0]["kwargs"].get("event") == "story_parked"

    @pytest.mark.parametrize("status", ["failed", "in_progress"])
    def test_second_sweep_makes_no_further_change_or_notification(
        self,
        status,
        plan_dir,
        plan_name,
        manifest_path,
        enable_triage,
        patch_classify,
        forbid_ruling,
        notify_calls,
        park_calls,
        manifest_writes,
    ):
        _write_manifest(
            manifest_path,
            {"paused": False, "stories": {"S1": _capped_not_parked_story(status)}},
        )

        pipeline.triage.run_triage_sweep(plan_name)
        after_first = manifest_path.read_bytes()
        assert len(park_calls) == 1
        assert len(notify_calls) == 1

        second = pipeline.triage.run_triage_sweep(plan_name)

        assert second["ok"] is True
        assert "skipped_cap" in second and "S1" in second["skipped_cap"]
        assert len(park_calls) == 1, "the second sweep must not re-park the story"
        assert len(notify_calls) == 1, "the second sweep must not notify again"
        assert manifest_path.read_bytes() == after_first


# ---------------------------------------------------------------------------
# Starvation proof
# ---------------------------------------------------------------------------


class TestCappedStoryDoesNotStarveActionable:
    def test_actionable_story_is_still_processed_this_tick(
        self,
        plan_dir,
        plan_name,
        manifest_path,
        enable_triage,
        patch_classify,
        rule_calls,
        apply_calls,
        monkeypatch,
    ):
        # The cap is 1; the capped story sorts first, so a raw
        # candidates[:1] slice would starve the actionable story.
        monkeypatch.setattr(triage_mod, "TRIAGE_MAX_PER_TICK", 1)
        assert triage_mod.TRIAGE_MAX_PER_TICK == 1

        manifest = {
            "paused": False,
            "stories": {
                "A-capped": _capped_parked_story(),
                "B-actionable": {
                    "status": "parked",
                    "parked_reason": "needs a look",
                    "worktree": "",
                    "triage_attempts": 0,
                    "triage_actions": [],
                },
            },
        }
        _write_manifest(manifest_path, manifest)
        assert triage_mod.triage_candidates(manifest["stories"]) == [
            "A-capped",
            "B-actionable",
        ]

        result = pipeline.triage.run_triage_sweep(plan_name)

        assert result["ok"] is True
        assert result["triaged"] == ["B-actionable"], (
            "the actionable story must be processed even though a capped story "
            "sorts first and TRIAGE_MAX_PER_TICK is 1"
        )
        assert "skipped_cap" in result and "A-capped" in result["skipped_cap"]
        assert [c["story_key"] for c in rule_calls] == ["B-actionable"]
        assert [c["story_key"] for c in apply_calls] == ["B-actionable"]


# ---------------------------------------------------------------------------
# Allowed (under-cap) candidate: behavioral no-regression
# ---------------------------------------------------------------------------


class TestAllowedCandidateNoRegression:
    def test_allowed_candidate_flows_through_the_ruling_path(
        self,
        plan_dir,
        plan_name,
        manifest_path,
        enable_triage,
        patch_classify,
        rule_calls,
        apply_calls,
    ):
        _write_manifest(
            manifest_path,
            {
                "paused": False,
                "stories": {
                    "S1": {
                        "status": "parked",
                        "parked_reason": "needs a look",
                        "worktree": "",
                        "triage_attempts": 0,
                        "triage_actions": [],
                    }
                },
            },
        )

        result = pipeline.triage.run_triage_sweep(plan_name)

        assert result["ok"] is True
        assert result["triaged"] == ["S1"]
        assert result["actions"] == {"S1": "park_for_human"}
        assert [c["story_key"] for c in rule_calls] == ["S1"]
        assert [c["story_key"] for c in apply_calls] == ["S1"]

    def test_allowed_candidate_is_not_recorded_as_skipped(
        self,
        plan_dir,
        plan_name,
        manifest_path,
        enable_triage,
        patch_classify,
        rule_calls,
        apply_calls,
    ):
        _write_manifest(
            manifest_path,
            {
                "paused": False,
                "stories": {
                    "S1": {
                        "status": "parked",
                        "parked_reason": "needs a look",
                        "worktree": "",
                        "triage_attempts": 0,
                        "triage_actions": [],
                    }
                },
            },
        )

        result = pipeline.triage.run_triage_sweep(plan_name)

        assert result.get("skipped_cap", []) == []


# ---------------------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------------------


class TestBoundaries:
    def test_empty_stories_returns_empty_triaged(
        self,
        plan_dir,
        plan_name,
        manifest_path,
        enable_triage,
        notify_calls,
        park_calls,
        manifest_writes,
    ):
        _write_manifest(manifest_path, {"paused": False, "stories": {}})

        result = pipeline.triage.run_triage_sweep(plan_name)

        assert result["ok"] is True
        assert result["triaged"] == []
        assert park_calls == []
        assert notify_calls == []
        assert manifest_writes == []

    def test_non_candidate_capped_story_is_not_skipped(
        self,
        plan_dir,
        plan_name,
        manifest_path,
        enable_triage,
        patch_classify,
        forbid_ruling,
        notify_calls,
        park_calls,
    ):
        """A capped story that is not a candidate at all is invisible to the
        sweep: it must not appear in skipped_cap nor be parked."""
        _write_manifest(
            manifest_path,
            {
                "paused": False,
                "stories": {
                    "S1": {
                        "status": "done",
                        "parked_reason": "",
                        "worktree": "",
                        "triage_attempts": triage_mod.TRIAGE_MAX_ATTEMPTS + 3,
                        "triage_actions": [],
                    }
                },
            },
        )

        result = pipeline.triage.run_triage_sweep(plan_name)

        assert result["ok"] is True
        assert result["triaged"] == []
        assert result.get("skipped_cap", []) == []
        assert park_calls == []
        assert notify_calls == []


# ---------------------------------------------------------------------------
# Unchanged helpers: _park / triage_allowed / triage_candidates
# ---------------------------------------------------------------------------


class TestUnchangedHelpers:
    """The fix must not change these three helpers' behavior."""

    def test_triage_allowed_below_cap(self):
        assert triage_mod.triage_allowed({"triage_attempts": 0}) == (True, "")

    def test_triage_allowed_at_and_above_cap(self):
        allowed, reason = triage_mod.triage_allowed(
            {"triage_attempts": triage_mod.TRIAGE_MAX_ATTEMPTS}
        )
        assert allowed is False
        assert reason == (
            f"triage attempts ({triage_mod.TRIAGE_MAX_ATTEMPTS}) at or above cap "
            f"({triage_mod.TRIAGE_MAX_ATTEMPTS})"
        )

    def test_triage_candidates_sorted_and_streak_triggered(self):
        stories = {
            "Z-parked": {"status": "parked"},
            "A-failed": {"status": "failed"},
            "M-streak": {
                "status": "in_progress",
                "step_cap_streak": triage_mod.STEP_CAP_FALLBACK_THRESHOLD,
            },
            "N-done": {"status": "done"},
        }
        assert triage_mod.triage_candidates(stories) == [
            "A-failed",
            "M-streak",
            "Z-parked",
        ]

    def test_park_sets_status_and_reason_and_notifies_once(self, notify_calls):
        story = {"status": "in_progress", "parked_reason": ""}

        returned = triage_mod._park("plan-x", "S1", story, "the cap reason")

        assert returned == "park_for_human"
        assert story["status"] == "parked"
        assert story["parked_reason"] == "the cap reason"
        assert len(notify_calls) == 1
        assert notify_calls[0]["kwargs"].get("event") == "story_parked"


# ---------------------------------------------------------------------------
# Mechanically-checkable source requirements
# ---------------------------------------------------------------------------


class TestSourcePartition:
    def test_partition_precedes_the_per_tick_slice(self):
        """``triage_allowed`` must be computed before the TRIAGE_MAX_PER_TICK
        slice inside run_triage_sweep."""
        src = inspect.getsource(triage_mod.run_triage_sweep)
        normalized = re.sub(r"\s+", "", src)
        assert "triage_allowed" in normalized
        assert "TRIAGE_MAX_PER_TICK" in normalized
        assert normalized.index("triage_allowed") < normalized.index(
            "TRIAGE_MAX_PER_TICK"
        ), "the allowed/capped partition must run before the per-tick slice"

    def test_skip_is_recorded_under_skipped_cap(self):
        src = inspect.getsource(triage_mod.run_triage_sweep)
        assert "skipped_cap" in src, (
            "run_triage_sweep must record capped skips under 'skipped_cap'"
        )

    def test_slice_is_applied_to_a_partitioned_list(self):
        """The per-tick slice must be applied to a partitioned list, not the
        raw candidate list.

        The behavioral proof lives in
        ``TestCappedStoryDoesNotStarveActionable``; this is the source-level
        companion: the slice target must not be the raw ``candidates`` name.
        """
        src = inspect.getsource(triage_mod.run_triage_sweep)
        match = re.search(
            r"(\w+)\s*\[\s*:\s*TRIAGE_MAX_PER_TICK\s*\]", src
        )
        assert match is not None, (
            "run_triage_sweep must still slice with TRIAGE_MAX_PER_TICK"
        )
        assert match.group(1) != "candidates", (
            "the per-tick slice must be applied to the actionable list, not the "
            "raw candidates list"
        )
