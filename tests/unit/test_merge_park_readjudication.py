"""MERGEPARK-2: a parked high-risk merge hold is re-adjudicated when the
evidence the ruling cited changes.

Pre-MERGEPARK-2, ``advance._adjudicate_merges`` skipped every story whose
status was not ``pr_open``, so a story parked by the merge gate - including one
parked by the overlord's own ruling in full autonomy - was never re-examined
even when the exact evidence the ruling cited later changed. Live case: plan
``chat-worktree-apply`` story ``WAP-1`` was parked 2026-09-15T01:26 with a
rationale citing missing PR checks; ``gh pr checks`` turned all-green minutes
later and no code path would ever revisit it.

The contract these tests pin down:

* The park branch of ``_adjudicate_merges`` records the evidence the ruling was
  made on: ``story["merge_park_evidence"] = {"pr_checks": story.get("pr_checks")}``.
* A story parked with the merge gate's exact hold reason, approved, carrying a
  ``pr_url`` and a ``merge_park_evidence`` snapshot is re-examined in
  ``PIPELINE_AUTONOMY == "full"``: the CURRENT single-poll CI state is gathered
  (``_ci_status_once`` lazy-imported from ``pipeline/ci.py`` - never the
  blocking ``_ci_status`` poller), the branch is resolved exactly like the
  merge path (``pipeline.pr._resolve_story_branch``, ``sha=""``), and the
  result is compared to the snapshot.
* Only a DIFFERING state re-invokes the gate (``_merge_decision``) under the
  same ``merge_adjudication_plan(plan_name)`` context the ``pr_open`` path
  uses. A ``merge`` ruling flips the story to ``pr_open`` and falls through
  into the SAME merge path (rebase -> push -> CI gate -> ``_merge_pr``); a
  ``park`` ruling stays parked and refreshes the snapshot, which is the flap
  guard: an unchanged state never re-invokes the overlord, so the cost is one
  overlord call per evidence TRANSITION, not per tick.
* Guard rails: no snapshot (legacy/human/triage parks), any other
  ``parked_reason``, a non-``APPROVE`` verdict, no ``pr_url``, and dry-run or
  gated autonomy are never re-adjudicated - and in the non-full modes the CI
  state is never even gathered.
* A gather failure never escapes the tick and leaves the old snapshot intact.

The heavy merge seams (rebase/push, CI gate, reverify, ``_merge_pr``) are
stubbed exactly the way the existing tick tests stub them, so "the merge path
was entered" is assertable without touching git or gh.
"""

# ruff: noqa: I001
# Import order below is deliberate, not disorganized: ``pipeline.server``
# transitively imports advance/ci/merge at module load, so importing it before
# ``pipeline.advance`` keeps that submodule import resolving against an
# already-initialized module (the ordering test_merge_overlord_adjudication.py
# documents). isort's alphabetical sort would put ``pipeline.advance`` first
# and reintroduce the circular import this ordering avoids.
import copy
import json
import re
from pathlib import Path

import pytest

from pipeline import server as server_mod
from pipeline import ci as ci_mod
from pipeline import merge as merge_mod
from pipeline import overlord as overlord_mod
from pipeline import persistence as persistence_mod
from pipeline import pr as pr_mod

# The module that owns the re-adjudication pass guarded by these tests.
from pipeline import advance as advance_mod

HOLD_REASON = "high risk held for human review"
PLAN = "PLAN-1"
KEY = "MERGEPARK-2-STORY"
BRANCH = "agent/MERGEPARK-2-STORY-2"

PROCEED_REPLY = "RULING: proceed\nRATIONALE: checks are green and the change is contained"
PARK_REPLY = "RULING: park\nRATIONALE: security review is too thin to merge unattended"

PENDING_CHECKS = {"ci": "pending", "lint": "pass"}
GREEN_CHECKS = {"ci": "pass", "lint": "pass"}

ADVANCE_PATH = Path(__file__).resolve().parents[2] / "pipeline" / "advance.py"


def _story(**overrides):
    """A production-shaped parked story: NO ``plan`` key, as manifests have."""
    story = {
        "key": KEY,
        "plan_name": PLAN,
        "status": "parked",
        "parked_reason": HOLD_REASON,
        "review_verdict": "APPROVE",
        "security_review_verdict": "APPROVE",
        "risk": "high",
        "summary": "Rewrite the auth token cache",
        "pr_url": "https://github.com/motock/pipeline/pull/1",
        "pr_checks": copy.deepcopy(PENDING_CHECKS),
        "merge_park_evidence": {"pr_checks": copy.deepcopy(PENDING_CHECKS)},
        "worktree": "",
        "dependencies": [],
    }
    story.update(overrides)
    return story


def _worktree(tmp_path):
    """A real directory, so branch resolution takes the ``_resolve_story_branch``
    path exactly like the merge path does (an empty/missing worktree hands the
    gate no branch at all and never probes the resolver)."""
    wt = tmp_path / "wt"
    wt.mkdir()
    return str(wt)


class _Harness:
    """Drives the REAL ``advance._adjudicate_merges`` with every heavy seam stubbed.

    ``_merge_decision`` is replaced by a counting stub that records the story it
    was handed and the ``merge_adjudication_plan`` context it ran under, so
    "the gate was re-invoked exactly once, under PLAN-1's context, with the
    refreshed pr_checks" is directly assertable. Pass ``decision=None`` to let
    the REAL gate run (with the overlord stubbed) instead.
    """

    def __init__(
        self,
        monkeypatch,
        plan_dir,
        autonomy="full",
        ruling=None,
        decision=None,
        ci_result=None,
        ci_exc=None,
        ci_gate_state="pass",
        threshold="low",
    ):
        self.plan_dir = plan_dir
        self.decisions = []  # _merge_decision invocations
        self.ci_calls = []  # pipeline.ci._ci_status_once invocations
        self.server_ci_calls = []  # pipeline.server._ci_status_once invocations
        self.branch_calls = []  # _resolve_story_branch invocations
        self.rebase_calls = []  # _rebase_and_push_for_merge invocations
        self.merge_calls = []  # _merge_pr invocations
        self.overlord_calls = []  # _invoke_overlord invocations
        self.ruling = ruling
        self.decision = decision
        self.ci_result = copy.deepcopy(
            GREEN_CHECKS if ci_result is None else ci_result
        )
        self.ci_exc = ci_exc
        self.ci_gate_state = ci_gate_state

        monkeypatch.setattr(server_mod, "PIPELINE_AUTONOMY", autonomy, raising=False)
        monkeypatch.setattr(
            server_mod, "PIPELINE_RISK_THRESHOLD", threshold, raising=False
        )

        # --- the overlord boundary (used only by the real gate) ---
        def fake_invoke(prompt, plan_role_config=None):
            self.overlord_calls.append(prompt)
            return self.ruling

        monkeypatch.setattr(overlord_mod, "_invoke_overlord", fake_invoke)
        monkeypatch.setattr(
            persistence_mod,
            "_plan_role_config",
            lambda plan_name: {"role": "overlord", "model": "opus"},
        )
        monkeypatch.setattr(
            persistence_mod,
            "_append_decision",
            lambda plan_name, record: None,
        )

        # --- the merge gate: counted, and records its adjudication context ---
        def fake_merge_decision(story):
            self.decisions.append(
                {
                    "story": copy.deepcopy(story),
                    "plan": merge_mod._adjudication_plan_name.get(),
                }
            )
            if self.decision is not None:
                return dict(self.decision)
            return merge_mod._merge_decision(story)

        monkeypatch.setattr(server_mod, "_merge_decision", fake_merge_decision)

        # --- the CI gather: the NON-polling variant, from pipeline/ci.py ---
        def fake_ci_status_once(branch, *, sha):
            self.ci_calls.append((branch, sha))
            if self.ci_exc is not None:
                raise self.ci_exc
            return copy.deepcopy(self.ci_result)

        monkeypatch.setattr(ci_mod, "_ci_status_once", fake_ci_status_once)
        # ``pipeline.server`` re-exports the same function, so a
        # ``from .server import _ci_status_once`` would silently bypass the stub
        # above. This second stub records into its own list (and mirrors the
        # failure mode) so the seam is assertable instead of shelling out to gh.
        def server_seam_ci_status_once(branch, *, sha):
            self.server_ci_calls.append((branch, sha))
            if self.ci_exc is not None:
                raise self.ci_exc
            return copy.deepcopy(self.ci_result)

        monkeypatch.setattr(
            server_mod, "_ci_status_once", server_seam_ci_status_once, raising=False
        )
        monkeypatch.setattr(
            advance_mod, "_ci_status_once", fake_ci_status_once, raising=False
        )

        # The blocking poller must never be used inside the tick.
        def blocking_ci(*a, **k):
            raise AssertionError(
                "the re-adjudication gather must use the NON-polling "
                "_ci_status_once, never the blocking _ci_status poller"
            )

        monkeypatch.setattr(ci_mod, "_ci_status", blocking_ci, raising=False)
        monkeypatch.setattr(server_mod, "_ci_status", blocking_ci, raising=False)

        # --- branch resolution, exactly like the merge path ---
        def fake_resolve(worktree, key):
            self.branch_calls.append((worktree, key))
            return BRANCH

        monkeypatch.setattr(pr_mod, "_resolve_story_branch", fake_resolve)

        # --- the merge path's heavy seams ---
        def fake_rebase(plan_name, key, branch, worktree):
            self.rebase_calls.append((plan_name, key, branch, worktree))
            return "", "sha-1"

        monkeypatch.setattr(
            server_mod, "_rebase_and_push_for_merge", fake_rebase, raising=False
        )
        monkeypatch.setattr(
            server_mod,
            "_merge_gate_ci_status",
            lambda branch, *, sha: {
                "state": self.ci_gate_state,
                "error": "" if self.ci_gate_state == "pass" else "still running",
            },
            raising=False,
        )
        monkeypatch.setattr(
            server_mod,
            "_reverify_acceptance",
            lambda story, wt, key: {"state": "pass"},
            raising=False,
        )
        monkeypatch.setattr(
            server_mod, "_reverify_build", lambda wt: {"state": "pass"}, raising=False
        )
        monkeypatch.setattr(
            server_mod, "_mcp_self_source_touched", lambda wt, base: "", raising=False
        )
        monkeypatch.setattr(
            server_mod, "_default_branch", lambda: "master", raising=False
        )
        monkeypatch.setattr(server_mod, "_ci_rerun", lambda sha: None, raising=False)
        monkeypatch.setattr(
            server_mod, "_ci_pending_expired", lambda since: False, raising=False
        )
        monkeypatch.setattr(
            server_mod, "_mark_plane_done", lambda *a, **k: None, raising=False
        )
        monkeypatch.setattr(
            server_mod, "_maybe_record_retro", lambda *a, **k: None, raising=False
        )
        monkeypatch.setattr(
            server_mod, "_notify_user", lambda *a, **k: None, raising=False
        )

        def fake_merge_pr(worktree, key):
            self.merge_calls.append((worktree, key))
            return "merged"

        monkeypatch.setattr(server_mod, "_merge_pr", fake_merge_pr, raising=False)
        monkeypatch.setattr(
            server_mod,
            "_atomic_write_json",
            lambda path, data: Path(path).write_text(json.dumps(data, indent=2)),
            raising=False,
        )

        try:
            from pipeline import plan_completion as plan_completion_mod
        except ImportError:  # pragma: no cover - defensive
            plan_completion_mod = None
        if plan_completion_mod is not None:
            monkeypatch.setattr(
                plan_completion_mod,
                "notify_if_plan_completed",
                lambda *a, **k: None,
                raising=False,
            )

    # -- driving ---------------------------------------------------------
    def write_manifest(self, story):
        (self.plan_dir / f"{PLAN}.manifest.json").write_text(
            json.dumps({"epics": {}, "stories": {story["key"]: story}}, indent=2)
        )

    def run(self, story=None):
        """Run one real ``_adjudicate_merges`` tick over the on-disk manifest."""
        if story is not None:
            self.write_manifest(story)
        summary = {
            "parked": [],
            "notify": [],
            "failed": [],
            "merged": [],
            "ci_pending": [],
        }
        advance_mod._adjudicate_merges(PLAN, summary)
        return summary

    def story(self, key=KEY):
        manifest = json.loads((self.plan_dir / f"{PLAN}.manifest.json").read_text())
        return manifest["stories"][key]

    def assert_gathered_from_ci_module(self):
        assert self.server_ci_calls == [], (
            "the re-adjudication gather must lazy-import _ci_status_once from "
            "pipeline/ci.py; it was resolved through pipeline.server instead"
        )


# --------------------------------------------------------------------------
# 1. the park branch records the evidence the ruling was made on
# --------------------------------------------------------------------------


def test_full_autonomy_park_records_the_pr_checks_evidence(monkeypatch, plan_dir):
    """A full-autonomy park decision snapshots the pr_checks seen at park time."""
    h = _Harness(monkeypatch, plan_dir, autonomy="full", ruling=PARK_REPLY)
    story = _story(status="pr_open", parked_reason=None)
    story.pop("merge_park_evidence")

    h.run(story)

    parked = h.story()
    assert parked["status"] == "parked"
    assert parked["parked_reason"] == HOLD_REASON
    assert parked["merge_park_evidence"] == {"pr_checks": PENDING_CHECKS}
    assert set(parked["merge_park_evidence"]) == {"pr_checks"}, (
        "the snapshot must carry exactly the pr_checks key"
    )


def test_park_records_a_none_snapshot_when_the_gather_failed(monkeypatch, plan_dir):
    """Boundary: with no pr_checks available at park time the snapshot is None.

    MERGEPARK-1's gather is fail-safe, so a failing gather leaves
    ``story["pr_checks"]`` unset and the snapshot records ``None`` - the key is
    still written, which is what makes the park re-adjudicable later.
    """
    h = _Harness(
        monkeypatch,
        plan_dir,
        autonomy="full",
        ruling=PARK_REPLY,
        ci_exc=RuntimeError("gh exploded"),
    )
    story = _story(status="pr_open", parked_reason=None)
    story.pop("merge_park_evidence")
    story.pop("pr_checks")

    h.run(story)

    parked = h.story()
    assert parked["status"] == "parked"
    assert parked["merge_park_evidence"] == {"pr_checks": None}


def test_gated_park_also_records_the_snapshot(monkeypatch, plan_dir):
    """The snapshot is written in every mode, not just full autonomy."""
    h = _Harness(monkeypatch, plan_dir, autonomy="gated", threshold="low")
    story = _story(
        status="pr_open",
        parked_reason=None,
        risk="medium",
        pr_checks=copy.deepcopy(GREEN_CHECKS),
    )
    story.pop("merge_park_evidence")

    h.run(story)

    parked = h.story()
    assert parked["status"] == "parked"
    assert parked["parked_reason"] == "risk above threshold low"
    assert parked["merge_park_evidence"] == {"pr_checks": GREEN_CHECKS}


# --------------------------------------------------------------------------
# 2. unchanged evidence: gather, compare, do nothing
# --------------------------------------------------------------------------


def test_unchanged_evidence_never_re_invokes_the_gate(monkeypatch, plan_dir, tmp_path):
    h = _Harness(
        monkeypatch,
        plan_dir,
        autonomy="full",
        decision={"action": "merge", "reason": "autonomy=full"},
        ci_result=PENDING_CHECKS,
    )
    story = _story(worktree=_worktree(tmp_path))
    before = copy.deepcopy(story)

    h.run(story)

    assert h.decisions == [], "an unchanged state must never re-invoke the gate"
    assert h.story() == before, "an unchanged state must not mutate the story"
    h.assert_gathered_from_ci_module()
    assert h.ci_calls == [(BRANCH, "")], (
        "the current single-poll CI state must be gathered once, branch-scoped "
        "with sha=''"
    )
    assert h.branch_calls[0] == (story["worktree"], KEY), (
        "the branch must be resolved with _resolve_story_branch exactly like "
        "the merge path"
    )


# --------------------------------------------------------------------------
# 3. changed evidence + park ruling: one re-invocation, snapshot refreshed
# --------------------------------------------------------------------------


def test_changed_evidence_re_invokes_the_gate_once_and_refreshes_the_snapshot(
    monkeypatch, plan_dir, tmp_path
):
    h = _Harness(
        monkeypatch,
        plan_dir,
        autonomy="full",
        decision={"action": "park", "reason": HOLD_REASON},
        ci_result=GREEN_CHECKS,
    )
    story = _story(worktree=_worktree(tmp_path))

    h.run(story)

    assert len(h.decisions) == 1, "a changed state must re-invoke the gate once"
    assert h.decisions[0]["plan"] == PLAN, (
        "the re-run must happen under the same merge_adjudication_plan(plan_name) "
        "context the pr_open path uses"
    )
    assert h.decisions[0]["story"]["pr_checks"] == GREEN_CHECKS, (
        "story['pr_checks'] must be refreshed to the new value BEFORE the gate "
        "is re-run"
    )
    h.assert_gathered_from_ci_module()
    assert h.ci_calls == [(BRANCH, "")]

    parked = h.story()
    assert parked["status"] == "parked"
    assert parked["parked_reason"] == HOLD_REASON
    assert parked["pr_checks"] == GREEN_CHECKS
    assert parked["merge_park_evidence"] == {"pr_checks": GREEN_CHECKS}, (
        "a park ruling must refresh the snapshot to the fresh value"
    )

    # Second tick: the snapshot now equals the live state -> no further call.
    h.run()
    assert len(h.decisions) == 1, (
        "the refreshed snapshot is the flap guard: an unchanged state must not "
        "re-invoke the gate on the next tick"
    )
    assert h.story()["merge_park_evidence"] == {"pr_checks": GREEN_CHECKS}
    assert len(h.ci_calls) == 2, "each tick still gathers the live state"


def test_live_wap1_case_parked_with_no_checks_then_green_is_re_adjudicated(
    monkeypatch, plan_dir, tmp_path
):
    """The live incident: parked citing missing checks, checks green minutes later.

    ``WAP-1`` was parked with ``PR CHECKS: (none)`` while its checks were merely
    still running. The snapshot therefore recorded ``None``; the live state is
    now green, which DIFFERS, so the gate must be re-invoked.
    """
    h = _Harness(
        monkeypatch,
        plan_dir,
        autonomy="full",
        decision={"action": "merge", "reason": "autonomy=full"},
        ci_result=GREEN_CHECKS,
    )
    story = _story(
        worktree=_worktree(tmp_path),
        pr_checks=None,
        merge_park_evidence={"pr_checks": None},
    )

    h.run(story)

    assert len(h.decisions) == 1, (
        "None -> green is an evidence transition and must re-invoke the gate"
    )
    assert h.decisions[0]["story"]["pr_checks"] == GREEN_CHECKS
    assert h.story()["status"] == "done"


def test_snapshot_comparison_is_dict_inequality_not_truthiness(
    monkeypatch, plan_dir, tmp_path
):
    """An extra key in the live dict is a difference, even though both are truthy."""
    h = _Harness(
        monkeypatch,
        plan_dir,
        autonomy="full",
        decision={"action": "park", "reason": HOLD_REASON},
        ci_result={"ci": "pending", "lint": "pass"},
    )
    story = _story(
        worktree=_worktree(tmp_path),
        pr_checks={"ci": "pending"},
        merge_park_evidence={"pr_checks": {"ci": "pending"}},
    )

    h.run(story)

    assert len(h.decisions) == 1, (
        "the comparison must be dict inequality, not truthiness"
    )
    assert h.story()["merge_park_evidence"] == {
        "pr_checks": {"ci": "pending", "lint": "pass"}
    }


def test_one_overlord_call_per_evidence_transition_not_per_tick(
    monkeypatch, plan_dir, tmp_path
):
    """End-to-end through the REAL gate: the overlord is asked once per transition."""
    h = _Harness(
        monkeypatch,
        plan_dir,
        autonomy="full",
        ruling=PARK_REPLY,
        ci_result=GREEN_CHECKS,
    )
    h.run(_story(worktree=_worktree(tmp_path)))
    assert len(h.overlord_calls) == 1, (
        "the changed evidence must reach the real overlord adjudication"
    )

    h.run()
    assert len(h.overlord_calls) == 1, (
        "the second tick must not re-ask the overlord: one call per evidence "
        "TRANSITION, not per tick"
    )


# --------------------------------------------------------------------------
# 4. changed evidence + proceed ruling: pr_open, then the SAME merge path
# --------------------------------------------------------------------------


def test_changed_evidence_with_proceed_ruling_enters_the_merge_path(
    monkeypatch, plan_dir, tmp_path
):
    wt = _worktree(tmp_path)
    h = _Harness(
        monkeypatch,
        plan_dir,
        autonomy="full",
        decision={"action": "merge", "reason": "autonomy=full"},
        ci_result=GREEN_CHECKS,
    )

    summary = h.run(_story(worktree=wt))

    assert len(h.decisions) == 1
    assert h.decisions[0]["plan"] == PLAN
    assert h.decisions[0]["story"]["pr_checks"] == GREEN_CHECKS
    h.assert_gathered_from_ci_module()

    # The SAME merge path a pr_open story takes: rebase/push then _merge_pr.
    assert len(h.rebase_calls) == 1, (
        "a merge ruling must fall through into the existing merge path, not a "
        "parallel implementation"
    )
    assert h.rebase_calls[0] == (PLAN, KEY, BRANCH, wt)
    assert h.merge_calls == [(wt, KEY)], "the existing _merge_pr seam must run"

    merged = h.story()
    assert merged["status"] == "done"
    assert "merge_park_evidence" not in merged, (
        "a merge ruling must clear merge_park_evidence"
    )
    assert summary["merged"] == [KEY]


def test_merge_ruling_sets_pr_open_before_the_merge_path_runs(
    monkeypatch, plan_dir, tmp_path
):
    """The status flip to pr_open happens FIRST; a blocked merge path leaves it there."""
    wt = _worktree(tmp_path)
    h = _Harness(
        monkeypatch,
        plan_dir,
        autonomy="full",
        decision={"action": "merge", "reason": "autonomy=full"},
        ci_result=GREEN_CHECKS,
        ci_gate_state="pending",
    )

    summary = h.run(_story(worktree=wt))

    assert len(h.decisions) == 1
    assert h.rebase_calls, "the merge path must have been entered"
    assert h.merge_calls == [], "a pending CI gate must not merge"
    story = h.story()
    assert story["status"] == "pr_open", (
        "the story must be flipped to pr_open before the merge path runs"
    )
    assert "merge_park_evidence" not in story
    assert summary["ci_pending"] == [KEY]


# --------------------------------------------------------------------------
# 5. guard rails: never re-adjudicated
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"parked_reason": "awaiting security review"},
        {"parked_reason": "not approved"},
        {"parked_reason": "risk above threshold low"},
        {"parked_reason": "dry-run"},
        {"parked_reason": None},
        {"review_verdict": "REQUEST_CHANGES"},
        {"review_verdict": None},
        {"pr_url": None},
        {"pr_url": ""},
    ],
)
def test_guard_rails_never_re_adjudicate(monkeypatch, plan_dir, tmp_path, overrides):
    h = _Harness(
        monkeypatch,
        plan_dir,
        autonomy="full",
        decision={"action": "merge", "reason": "autonomy=full"},
        ci_result=GREEN_CHECKS,
    )
    story = _story(worktree=_worktree(tmp_path), **overrides)
    before = copy.deepcopy(story)

    h.run(story)

    assert h.decisions == [], f"{overrides} must never re-adjudicate"
    assert h.ci_calls == [], f"{overrides} must never even gather CI state"
    assert h.story() == before, f"{overrides} must leave the story untouched"


@pytest.mark.parametrize("autonomy", ["gated", "dry-run"])
def test_non_full_autonomy_never_re_adjudicates_or_gathers(
    monkeypatch, plan_dir, tmp_path, autonomy
):
    h = _Harness(
        monkeypatch,
        plan_dir,
        autonomy=autonomy,
        decision={"action": "merge", "reason": "autonomy=full"},
        ci_result=GREEN_CHECKS,
    )
    story = _story(worktree=_worktree(tmp_path))
    before = copy.deepcopy(story)

    h.run(story)

    assert h.decisions == [], (
        f"autonomy={autonomy} never adjudicated in the first place"
    )
    assert h.ci_calls == [], (
        "PIPELINE_AUTONOMY must be checked BEFORE gathering the CI state"
    )
    assert h.story() == before


def test_legacy_park_without_a_snapshot_is_completely_untouched(
    monkeypatch, plan_dir, tmp_path
):
    """Parks created before this feature (human parks, triage parks) have no snapshot."""
    h = _Harness(
        monkeypatch,
        plan_dir,
        autonomy="full",
        decision={"action": "merge", "reason": "autonomy=full"},
        ci_result=GREEN_CHECKS,
    )
    story = _story(worktree=_worktree(tmp_path))
    story.pop("merge_park_evidence")
    before = copy.deepcopy(story)

    h.run(story)

    assert h.decisions == []
    assert h.ci_calls == []
    assert h.story() == before


@pytest.mark.parametrize("evidence", [None])
def test_null_snapshot_is_not_re_adjudicated(
    monkeypatch, plan_dir, tmp_path, evidence
):
    h = _Harness(
        monkeypatch,
        plan_dir,
        autonomy="full",
        decision={"action": "merge", "reason": "autonomy=full"},
        ci_result=GREEN_CHECKS,
    )
    story = _story(worktree=_worktree(tmp_path), merge_park_evidence=evidence)
    before = copy.deepcopy(story)

    h.run(story)

    assert h.decisions == []
    assert h.ci_calls == []
    assert h.story() == before


@pytest.mark.parametrize("status", ["done", "tests_passed", "in_progress", "failed"])
def test_non_parked_statuses_are_untouched(monkeypatch, plan_dir, tmp_path, status):
    h = _Harness(
        monkeypatch,
        plan_dir,
        autonomy="full",
        decision={"action": "merge", "reason": "autonomy=full"},
        ci_result=GREEN_CHECKS,
    )
    story = _story(status=status, worktree=_worktree(tmp_path))
    before = copy.deepcopy(story)

    h.run(story)

    assert h.decisions == []
    assert h.ci_calls == []
    assert h.story() == before


# --------------------------------------------------------------------------
# 6. a failing gather never escapes the tick
# --------------------------------------------------------------------------


def test_gather_failure_leaves_the_old_snapshot_intact(
    monkeypatch, plan_dir, tmp_path
):
    h = _Harness(
        monkeypatch,
        plan_dir,
        autonomy="full",
        decision={"action": "merge", "reason": "autonomy=full"},
        ci_exc=RuntimeError("gh exploded"),
    )
    story = _story(worktree=_worktree(tmp_path))
    before = copy.deepcopy(story)

    h.run(story)  # must not raise

    assert h.decisions == [], "no evidence to compare means no re-adjudication"
    assert h.story() == before, "the old snapshot must be left intact"


# --------------------------------------------------------------------------
# 7. source-level guards for the mechanically checkable requirements
# --------------------------------------------------------------------------


def test_advance_source_writes_the_snapshot_and_reads_it_back():
    src = ADVANCE_PATH.read_text()
    assert src.count("merge_park_evidence") >= 2, (
        "pipeline/advance.py must both write merge_park_evidence at park time "
        "and read it in the re-adjudication predicate"
    )
    assert re.search(r'merge_park_evidence"\]\s*=\s*\{[^}]*pr_checks', src), (
        'the park branch must record story["merge_park_evidence"] = '
        '{"pr_checks": story.get("pr_checks")}'
    )


def test_readjudication_uses_the_non_polling_gather_and_the_branch_resolver():
    src = ADVANCE_PATH.read_text()
    assert "_ci_status_once" in src, (
        "the re-adjudication pass must gather the current single-poll CI state "
        "with _ci_status_once"
    )
    assert "_resolve_story_branch" in src, (
        "the re-adjudication pass must resolve the branch exactly like the "
        "merge path does"
    )
    assert "merge_adjudication_plan" in src, (
        "the re-run must happen under the merge_adjudication_plan context"
    )
