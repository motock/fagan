"""OA2-05: widen merge-park re-adjudication to the risk-threshold park reasons.

The merge gate (``pipeline/merge.py``) emits parks whose reason is
``f"risk above threshold {PIPELINE_RISK_THRESHOLD}"``. Before OA2-05 the
re-adjudication pass in ``pipeline/advance.py`` only accepted the exact
high-risk-hold string, so a risk-threshold park was unreachable in full
autonomy. This module pins the widened predicate:

* ``"high risk held for human review"`` still re-adjudicates;
* any reason STARTING WITH ``"risk above threshold"`` re-adjudicates
  (bare, ``low``, ``medium``, ``high``);
* the guard rails are unchanged: ``"not approved"``, ``"dry-run"``,
  ``"awaiting security review"``, an unrelated reason and a missing
  (``None``) reason are never re-adjudicated;
* non-``full`` autonomy never re-adjudicates.

The harness mirrors ``tests/unit/test_merge_park_readjudication.py``: the
REAL ``advance._adjudicate_merges`` tick runs over an on-disk manifest with
every heavy seam stubbed, and ``_merge_decision`` is a counting stub so
"the gate was re-invoked" is directly assertable.
"""

# ruff: noqa: I001
# Import order is deliberate: ``pipeline.server`` transitively imports
# advance/ci/merge at module load, so importing it before ``pipeline.advance``
# keeps that submodule import resolving against an already-initialized module.
import copy
import json

import pytest

from pipeline import server as server_mod
from pipeline import ci as ci_mod
from pipeline import merge as merge_mod
from pipeline import overlord as overlord_mod
from pipeline import persistence as persistence_mod
from pipeline import pr as pr_mod

from pipeline import advance as advance_mod

HOLD_REASON = "high risk held for human review"
RISK_LOW = "risk above threshold low"
PLAN = "PLAN-1"
KEY = "OA2-05-STORY"
BRANCH = "agent/OA2-05-STORY-2"

PENDING_CHECKS = {"ci": "pending", "lint": "pass"}
GREEN_CHECKS = {"ci": "pass", "lint": "pass"}


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
    """A real directory, so branch resolution takes the resolver path."""
    wt = tmp_path / "wt"
    wt.mkdir()
    return str(wt)


class _Harness:
    """Drives the REAL ``advance._adjudicate_merges`` with heavy seams stubbed."""

    def __init__(self, monkeypatch, plan_dir, autonomy="full", decision=None):
        self.plan_dir = plan_dir
        self.decisions = []
        self.ci_calls = []
        self.decision = decision

        monkeypatch.setattr(server_mod, "PIPELINE_AUTONOMY", autonomy, raising=False)
        monkeypatch.setattr(
            server_mod, "PIPELINE_RISK_THRESHOLD", "low", raising=False
        )

        monkeypatch.setattr(
            overlord_mod, "_invoke_overlord", lambda prompt, plan_role_config=None: None
        )
        monkeypatch.setattr(
            persistence_mod,
            "_plan_role_config",
            lambda plan_name: {"role": "overlord", "model": "opus"},
        )
        monkeypatch.setattr(
            persistence_mod, "_append_decision", lambda plan_name, record: None
        )

        def fake_merge_decision(story):
            self.decisions.append(copy.deepcopy(story))
            if self.decision is not None:
                return dict(self.decision)
            return merge_mod._merge_decision(story)

        monkeypatch.setattr(server_mod, "_merge_decision", fake_merge_decision)

        def fake_ci_status_once(branch, *, sha):
            self.ci_calls.append((branch, sha))
            return copy.deepcopy(GREEN_CHECKS)

        monkeypatch.setattr(ci_mod, "_ci_status_once", fake_ci_status_once)
        monkeypatch.setattr(
            server_mod, "_ci_status_once", fake_ci_status_once, raising=False
        )
        monkeypatch.setattr(
            advance_mod, "_ci_status_once", fake_ci_status_once, raising=False
        )

        def blocking_ci(*a, **k):
            raise AssertionError("the re-adjudication gather must be non-polling")

        monkeypatch.setattr(ci_mod, "_ci_status", blocking_ci, raising=False)
        monkeypatch.setattr(server_mod, "_ci_status", blocking_ci, raising=False)

        monkeypatch.setattr(
            pr_mod, "_resolve_story_branch", lambda worktree, key: BRANCH
        )

        monkeypatch.setattr(
            server_mod,
            "_rebase_and_push_for_merge",
            lambda plan_name, key, branch, worktree: ("", "sha-1"),
            raising=False,
        )
        monkeypatch.setattr(
            server_mod,
            "_merge_gate_ci_status",
            lambda branch, *, sha: {"state": "pass", "error": ""},
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
            server_mod, "_ci_pending_expired", lambda *a, **k: False, raising=False
        )
        monkeypatch.setattr(
            server_mod, "_merge_pr", lambda *a, **k: None, raising=False
        )
        monkeypatch.setattr(
            server_mod, "_notify", lambda *a, **k: None, raising=False
        )
        monkeypatch.setattr(
            server_mod,
            "notify_if_plan_completed",
            lambda *a, **k: None,
            raising=False,
        )

    def write_manifest(self, story):
        (self.plan_dir / f"{PLAN}.manifest.json").write_text(
            json.dumps({"epics": {}, "stories": {story["key"]: story}}, indent=2)
        )

    def run(self, story=None):
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


@pytest.fixture
def plan_dir(tmp_path):
    d = tmp_path / "plans"
    d.mkdir()
    return d


def _run(monkeypatch, plan_dir, tmp_path, reason, autonomy="full"):
    h = _Harness(
        monkeypatch,
        plan_dir,
        autonomy=autonomy,
        decision={"action": "merge", "reason": "autonomy=full"},
    )
    story = _story(worktree=_worktree(tmp_path), parked_reason=reason)
    before = copy.deepcopy(story)
    h.run(story)
    return h, before


def test_risk_above_threshold_low_re_adjudicated(monkeypatch, plan_dir, tmp_path):
    h, _ = _run(monkeypatch, plan_dir, tmp_path, RISK_LOW)
    assert h.decisions, "'risk above threshold low' must re-adjudicate in full autonomy"


def test_high_risk_hold_still_re_adjudicated(monkeypatch, plan_dir, tmp_path):
    h, _ = _run(monkeypatch, plan_dir, tmp_path, HOLD_REASON)
    assert h.decisions, "the high-risk hold must still re-adjudicate"


def test_not_approved_never_re_adjudicated(monkeypatch, plan_dir, tmp_path):
    h, before = _run(monkeypatch, plan_dir, tmp_path, "not approved")
    assert h.decisions == [], "'not approved' is a decision, never re-adjudicated"
    assert h.ci_calls == [], "'not approved' must never even gather CI state"
    assert h.story() == before, "'not approved' must leave the story untouched"


@pytest.mark.parametrize("reason", [RISK_LOW, HOLD_REASON])
def test_gated_never_re_adjudicated(monkeypatch, plan_dir, tmp_path, reason):
    h, before = _run(monkeypatch, plan_dir, tmp_path, reason, autonomy="gated")
    assert h.decisions == [], "gated autonomy must never re-adjudicate"
    assert h.ci_calls == [], "gated autonomy must never gather CI state"
    assert h.story() == before


@pytest.mark.parametrize(
    "reason",
    [
        "risk above threshold low",
        "risk above threshold medium",
        "risk above threshold high",
        "risk above threshold",
    ],
)
def test_risk_above_threshold_suffixes(monkeypatch, plan_dir, tmp_path, reason):
    h, _ = _run(monkeypatch, plan_dir, tmp_path, reason)
    assert h.decisions, f"{reason!r} must re-adjudicate"


@pytest.mark.parametrize(
    "reason",
    ["dry-run", "awaiting security review", "something else"],
)
def test_unrelated_reasons_not_re_adjudicated(monkeypatch, plan_dir, tmp_path, reason):
    h, before = _run(monkeypatch, plan_dir, tmp_path, reason)
    assert h.decisions == [], f"{reason!r} must never re-adjudicate"
    assert h.story() == before


def test_missing_reason_not_re_adjudicated(monkeypatch, plan_dir, tmp_path):
    h, before = _run(monkeypatch, plan_dir, tmp_path, None)
    assert h.decisions == [], "a missing reason must never re-adjudicate"
    assert h.story() == before
