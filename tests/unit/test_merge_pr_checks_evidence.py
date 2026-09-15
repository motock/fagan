"""MERGEPARK-1: the high-risk merge adjudication must carry real PR-check evidence.

``_adjudicate_high_risk_merge`` builds the overlord prompt with
``f"PR CHECKS: {story.get('pr_checks') or '(none)'}\\n"``, but nothing in the
codebase ever *writes* ``pr_checks``. Every full-autonomy high-risk merge
adjudication therefore told the overlord ``PR CHECKS: (none)`` and the overlord
parked on missing evidence - live case: plan ``chat-worktree-apply`` story
``WAP-1`` was parked 2026-09-15T01:26 with a rationale explicitly citing "no PR
checks" while the PR's checks were merely still running and turned green
minutes later.

The contract these tests pin down:

* At the top of ``_adjudicate_high_risk_merge``, before the prompt is built, a
  falsy ``story["pr_checks"]`` is populated with the *current single-poll* CI
  state.
* The gather uses the NON-polling ``_ci_status_once`` (one query, returns
  immediately). The blocking ``_ci_status`` poller - which ``time.sleep(10)``
  loops up to the merge timeout - must never be used: this call runs inside the
  scheduler's plan-locked tick and must never block.
* Branch resolution mirrors ``_approve_merge_impl``: when ``story["worktree"]``
  is an existing directory the branch comes from
  ``pipeline.pr._resolve_story_branch``; otherwise the branch is ``""``. The
  query is branch-scoped (``sha=""``).
* The gather is fail-safe: any exception leaves ``pr_checks`` unset and the
  adjudication still runs and still fails closed to the standing high-risk
  hold. Nothing from the gather may propagate out of
  ``_adjudicate_high_risk_merge``.
* Population happens ONLY when ``pr_checks`` is missing. The OPSA-9 suite
  injects its own ``pr_checks`` and stubs only ``_invoke_overlord``; an
  unconditional overwrite would break those untouched tests.
* ``advance._adjudicate_merges`` persists the manifest at the end of its loop,
  so the populated ``pr_checks`` lands in the manifest with no extra write.

``pipeline/merge.py`` resolves ``_ci_status_once`` lazily inside the function
body through the module-documented monkeypatch seam (``from .server import
...`` - the same seam the sibling ``_merge_gate_ci_status`` uses), so the stub
is installed on the re-exported binding on ``pipeline.server``
(``pipeline.server._ci_status_once``) rather than on a ``pipeline.merge`` name.
"""

# ruff: noqa: I001
# Import order below is deliberate, not disorganized: ``pipeline.server``
# transitively imports advance/ci/merge at module load, so importing it before
# ``pipeline.advance`` keeps that submodule import resolving against an
# already-initialized module (the ordering test_merge_overlord_adjudication.py
# documents). isort's alphabetical sort would put ``pipeline.advance`` first
# and reintroduce the circular import this ordering avoids.
import json
import time
from pathlib import Path

import pytest

from pipeline import merge as merge_mod
from pipeline import overlord as overlord_mod
from pipeline import persistence as persistence_mod
from pipeline import server as server_mod
from pipeline import pr as pr_mod

# The module that owns the manifest persistence guarded by the end-to-end test
# at the bottom of this file.
from pipeline import advance as advance_mod

HOLD_REASON = "high risk held for human review"
PROCEED_REPLY = "RULING: proceed\nRATIONALE: checks are green and contained"
PARK_REPLY = "RULING: park\nRATIONALE: security review is too thin"

PLAN = "PLAN-1"
E2E_KEY = "MERGEPARK-1-E2E"

PASS_STATE = {"state": "pass", "error": ""}


def _boom_blocking(*args, **kwargs):
    """Stand-in for the blocking poller: using it is a hard failure."""
    raise AssertionError(
        "the blocking _ci_status poller must never be used by the merge "
        "adjudication - it time.sleep(10)-loops up to the merge timeout inside "
        "the scheduler's plan-locked tick"
    )


def _story(**overrides):
    """A production-shaped pr_open high-risk story.

    Deliberately carries NO ``pr_checks`` key: that is the bug scenario. Tests
    that need pre-existing evidence pass it explicitly.
    """
    story = {
        "key": "MERGEPARK-1-STORY",
        "plan_name": "PLAN-1",
        "status": "pr_open",
        "parked_reason": None,
        "review_verdict": "APPROVE",
        "security_review_verdict": "APPROVE",
        "risk": "high",
        "summary": "Rewrite the auth token cache",
        "worktree": "",
    }
    story.update(overrides)
    return story


class _Harness:
    """Patches the autonomy knobs, the overlord boundary and the CI gather.

    ``_ci_status_once`` is stubbed on ``pipeline.server`` only: that is the
    module-documented monkeypatch seam merge.py resolves the lazy import
    through (the same seam the sibling ``_merge_gate_ci_status`` uses). The
    blocking ``_ci_status`` is poisoned the same way so any use of it fails
    loudly instead of sleeping.
    """

    def __init__(
        self,
        monkeypatch,
        ruling=None,
        exc=None,
        gather=None,
        gather_exc=None,
        autonomy="full",
        threshold="low",
        resolved_branch="agent/mergepark-1",
    ):
        self.invocations = []
        self.decisions = []
        self.gather_calls = []
        self.blocking_calls = []
        self.branch_calls = []
        self.role_config = {"role": "overlord", "model": "opus"}
        self.ruling = ruling
        self.exc = exc
        self.resolved_branch = resolved_branch
        monkeypatch.setattr(server_mod, "PIPELINE_AUTONOMY", autonomy, raising=False)
        monkeypatch.setattr(
            server_mod, "PIPELINE_RISK_THRESHOLD", threshold, raising=False
        )

        def fake_invoke(prompt, plan_role_config=None):
            self.invocations.append(
                {"prompt": prompt, "plan_role_config": plan_role_config}
            )
            if self.exc is not None:
                raise self.exc
            return self.ruling

        monkeypatch.setattr(overlord_mod, "_invoke_overlord", fake_invoke)
        monkeypatch.setattr(
            persistence_mod, "_plan_role_config", lambda plan_name: self.role_config
        )
        monkeypatch.setattr(
            persistence_mod,
            "_append_decision",
            lambda plan_name, record: self.decisions.append((plan_name, record)),
        )

        def fake_once(branch, *, sha):
            self.gather_calls.append({"branch": branch, "sha": sha})
            if gather_exc is not None:
                raise gather_exc
            return dict(gather) if gather is not None else dict(PASS_STATE)

        def fake_blocking(*args, **kwargs):
            self.blocking_calls.append((args, kwargs))
            return _boom_blocking(*args, **kwargs)

        monkeypatch.setattr(server_mod, "_ci_status_once", fake_once, raising=False)
        monkeypatch.setattr(server_mod, "_ci_status", fake_blocking, raising=False)

        def fake_resolve(worktree, story_key):
            self.branch_calls.append((worktree, story_key))
            return self.resolved_branch

        monkeypatch.setattr(pr_mod, "_resolve_story_branch", fake_resolve)

    def decide(self, story):
        # Bind the production adjudication context exactly as
        # ``advance._adjudicate_merges`` does around its gate call.
        with merge_mod.merge_adjudication_plan("PLAN-1"):
            return merge_mod._merge_decision(story)

    @property
    def prompt(self):
        assert self.invocations, "the overlord was never invoked"
        return self.invocations[0]["prompt"]

    def pr_checks_line(self):
        lines = [
            line for line in self.prompt.splitlines() if line.startswith("PR CHECKS:")
        ]
        assert len(lines) == 1, f"expected exactly one PR CHECKS line, got {lines!r}"
        return lines[0]


# --------------------------------------------------------------------------
# the happy path: missing evidence is gathered and reaches the prompt
# --------------------------------------------------------------------------


def test_missing_pr_checks_gathers_evidence_into_the_prompt(monkeypatch):
    h = _Harness(monkeypatch, ruling=PROCEED_REPLY, gather=PASS_STATE)
    story = _story()
    assert "pr_checks" not in story

    decision = h.decide(story)

    assert decision["action"] == "merge"
    assert len(h.gather_calls) == 1, "exactly one single-poll query is expected"
    assert story["pr_checks"] == PASS_STATE, (
        "the gathered CI state must be stored on the story"
    )
    line = h.pr_checks_line()
    assert "(none)" not in line, (
        "the overlord must not be told PR CHECKS: (none) when the state was "
        "gathered successfully"
    )
    assert "'state': 'pass'" in line
    assert "PR CHECKS: (none)" not in h.prompt


def test_gathered_state_is_the_single_poll_result(monkeypatch):
    pending = {"state": "pending", "error": ""}
    h = _Harness(monkeypatch, ruling=PARK_REPLY, gather=pending)
    story = _story()

    h.decide(story)

    assert story["pr_checks"] == pending
    assert "'state': 'pending'" in h.pr_checks_line()


def test_gather_result_is_kept_when_the_overlord_fails(monkeypatch):
    h = _Harness(
        monkeypatch, exc=RuntimeError("overlord unavailable"), gather=PASS_STATE
    )
    story = _story()

    assert h.decide(story) == {"action": "park", "reason": HOLD_REASON}
    assert story["pr_checks"] == PASS_STATE


# --------------------------------------------------------------------------
# populate ONLY when missing (load-bearing for the untouched OPSA-9 suite)
# --------------------------------------------------------------------------


def test_existing_pr_checks_are_never_gathered_or_overwritten(monkeypatch):
    h = _Harness(
        monkeypatch, ruling=PROCEED_REPLY, gather={"state": "fail", "error": "boom"}
    )
    injected = {"ci": "pass", "lint": "fail"}
    story = _story(pr_checks=injected)

    h.decide(story)

    assert h.gather_calls == [], (
        "an already-populated pr_checks must not trigger a CI query"
    )
    assert story["pr_checks"] == injected
    assert "lint" in h.prompt and "fail" in h.prompt
    assert "boom" not in h.prompt


@pytest.mark.parametrize("existing", [None, {}, ""])
def test_falsy_pr_checks_is_treated_as_missing(monkeypatch, existing):
    h = _Harness(monkeypatch, ruling=PROCEED_REPLY, gather=PASS_STATE)
    story = _story(pr_checks=existing)

    h.decide(story)

    assert len(h.gather_calls) == 1
    assert story["pr_checks"] == PASS_STATE


# --------------------------------------------------------------------------
# fail-safe: the gather never propagates and the adjudication still fails closed
# --------------------------------------------------------------------------


def test_gather_failure_never_escapes_and_fails_closed(monkeypatch):
    h = _Harness(monkeypatch, ruling=None, gather_exc=RuntimeError("gh exploded"))
    story = _story()

    decision = h.decide(story)  # must not raise

    assert decision == {"action": "park", "reason": HOLD_REASON}
    assert not story.get("pr_checks"), (
        "a failed gather must leave pr_checks unset"
    )
    assert "PR CHECKS: (unreadable" in h.prompt
    assert "PR CHECKS: (none)" not in h.prompt
    assert len(h.invocations) == 1


def test_gather_failure_does_not_propagate_from_the_adjudicator(monkeypatch):
    _Harness(monkeypatch, ruling=PARK_REPLY, gather_exc=RuntimeError("boom"))
    story = _story()

    with merge_mod.merge_adjudication_plan("PLAN-1"):
        result = merge_mod._adjudicate_high_risk_merge(story, "PLAN-1")

    assert result == {"action": "park", "reason": HOLD_REASON}
    assert not story.get("pr_checks")


def test_gather_failure_with_no_worktree_never_raises(monkeypatch):
    h = _Harness(monkeypatch, ruling=None, gather_exc=RuntimeError("no gh"))
    story = _story(worktree="")

    assert h.decide(story) == {"action": "park", "reason": HOLD_REASON}
    assert not story.get("pr_checks")


def test_gather_failure_still_records_the_ruling(monkeypatch):
    h = _Harness(monkeypatch, ruling=PARK_REPLY, gather_exc=RuntimeError("boom"))
    story = _story()

    h.decide(story)

    assert len(h.decisions) == 1
    _, record = h.decisions[0]
    assert record["decided_by"] == "overlord"
    assert record["ruling"] == "park"


# --------------------------------------------------------------------------
# branch resolution mirrors _approve_merge_impl; the query is branch-scoped
# --------------------------------------------------------------------------


def test_no_worktree_gathers_with_convention_branch(monkeypatch):
    h = _Harness(monkeypatch, ruling=PROCEED_REPLY, gather=PASS_STATE)
    story = _story(worktree="")

    h.decide(story)

    assert len(h.gather_calls) == 1
    assert h.gather_calls[0]["branch"] == "agent/mergepark-1-story", (
        "with no worktree to probe the poll must degrade to the convention "
        "branch - polling an empty branch would resolve the CURRENT "
        "checkout's PR and import an unrelated CI verdict"
    )
    assert h.gather_calls[0]["sha"] == "", "the query must be branch-scoped"
    assert h.branch_calls == [], "no worktree means no branch probe"
    assert story["pr_checks"] == PASS_STATE


def test_missing_worktree_key_gathers_with_convention_branch(monkeypatch):
    h = _Harness(monkeypatch, ruling=PROCEED_REPLY, gather=PASS_STATE)
    story = _story()
    del story["worktree"]

    h.decide(story)

    assert h.gather_calls[0]["branch"] == "agent/mergepark-1-story"
    assert h.gather_calls[0]["sha"] == ""
    assert story["pr_checks"] == PASS_STATE


def test_existing_worktree_branch_is_resolved(monkeypatch, tmp_path):
    h = _Harness(
        monkeypatch,
        ruling=PROCEED_REPLY,
        gather=PASS_STATE,
        resolved_branch="agent/mergepark-1-alias",
    )
    story = _story(worktree=str(tmp_path))

    h.decide(story)

    assert h.branch_calls == [(str(tmp_path), "MERGEPARK-1-STORY")], (
        "an existing worktree dir must be probed via _resolve_story_branch"
    )
    assert h.gather_calls[0]["branch"] == "agent/mergepark-1-alias"
    assert h.gather_calls[0]["sha"] == ""


def test_nonexistent_worktree_dir_degrades_to_convention_branch(
    monkeypatch, tmp_path
):
    h = _Harness(monkeypatch, ruling=PROCEED_REPLY, gather=PASS_STATE)
    story = _story(worktree=str(tmp_path / "does-not-exist"))

    h.decide(story)

    assert h.branch_calls == [], (
        "a missing worktree dir must not pay a subprocess to resolve a branch"
    )
    assert h.gather_calls[0]["branch"] == "agent/mergepark-1-story", (
        "a missing worktree dir must degrade to the convention branch, never "
        "to an empty branch (which would poll the current checkout's PR)"
    )
    assert story["pr_checks"] == PASS_STATE


# --------------------------------------------------------------------------
# non-blocking proof: the sleeping _ci_status poller is never used
# --------------------------------------------------------------------------


def test_gather_is_non_blocking_and_single_poll(monkeypatch):
    def boom_sleep(*args, **kwargs):
        raise AssertionError(
            "the merge adjudication must never sleep-poll CI inside the "
            "scheduler's plan-locked tick"
        )

    monkeypatch.setattr(time, "sleep", boom_sleep)
    h = _Harness(monkeypatch, ruling=PROCEED_REPLY, gather=PASS_STATE)
    story = _story()

    decision = h.decide(story)

    assert decision["action"] == "merge"
    assert len(h.gather_calls) == 1
    assert h.blocking_calls == [], (
        "the blocking _ci_status poller must never be used"
    )
    assert story["pr_checks"] == PASS_STATE


def test_ci_status_once_is_imported_lazily(monkeypatch):
    assert not hasattr(merge_mod, "_ci_status_once"), (
        "merge.py must lazy-import _ci_status_once inside the function body, "
        "not bind it at import time"
    )


def test_gather_resolves_ci_status_once_from_pipeline_server(monkeypatch):
    """The single-poll helper must come from ``pipeline.server`` at call time.

    Only ``pipeline.server._ci_status_once`` is stubbed here (``pipeline.ci``
    and ``pipeline.merge`` are left alone), so this fails if the gather binds
    the helper from anywhere else - the module-documented seam is the
    re-exported binding on ``pipeline.server``.
    """
    monkeypatch.setattr(server_mod, "PIPELINE_AUTONOMY", "full", raising=False)
    monkeypatch.setattr(server_mod, "PIPELINE_RISK_THRESHOLD", "low", raising=False)
    monkeypatch.setattr(
        overlord_mod,
        "_invoke_overlord",
        lambda prompt, plan_role_config=None: PROCEED_REPLY,
    )
    monkeypatch.setattr(
        persistence_mod, "_plan_role_config", lambda plan_name: {"role": "overlord"}
    )
    monkeypatch.setattr(
        persistence_mod, "_append_decision", lambda plan_name, record: None
    )
    calls = []

    def fake_once(branch, *, sha):
        calls.append({"branch": branch, "sha": sha})
        return dict(PASS_STATE)

    monkeypatch.setattr(server_mod, "_ci_status_once", fake_once, raising=False)
    monkeypatch.setattr(server_mod, "_ci_status", _boom_blocking, raising=False)
    story = _story()

    with merge_mod.merge_adjudication_plan("PLAN-1"):
        decision = merge_mod._merge_decision(story)

    assert decision["action"] == "merge"
    assert len(calls) == 1, (
        "the gather must resolve _ci_status_once from pipeline.server"
    )
    assert story["pr_checks"] == PASS_STATE


# --------------------------------------------------------------------------
# the gather is scoped to the full-autonomy high-risk adjudication
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "autonomy,reason", [("dry-run", "dry-run"), ("gated", HOLD_REASON)]
)
def test_no_gather_outside_full_autonomy(monkeypatch, autonomy, reason):
    h = _Harness(
        monkeypatch, autonomy=autonomy, ruling=PROCEED_REPLY, gather=PASS_STATE
    )
    story = _story()

    assert h.decide(story) == {"action": "park", "reason": reason}
    assert h.gather_calls == []
    assert "pr_checks" not in story


def test_no_gather_when_the_story_is_not_approved(monkeypatch):
    h = _Harness(monkeypatch, ruling=PROCEED_REPLY, gather=PASS_STATE)
    story = _story(review_verdict="REQUEST_CHANGES")

    assert h.decide(story) == {"action": "park", "reason": "not approved"}
    assert h.gather_calls == []
    assert "pr_checks" not in story


def test_no_gather_for_low_risk_full_autonomy(monkeypatch):
    h = _Harness(monkeypatch, ruling=PROCEED_REPLY, gather=PASS_STATE)
    story = _story(risk="low")

    assert h.decide(story) == {"action": "merge", "reason": "autonomy=full"}
    assert h.gather_calls == []
    assert "pr_checks" not in story


# --------------------------------------------------------------------------
# end-to-end: the populated pr_checks lands in the persisted manifest
# --------------------------------------------------------------------------


def _e2e_story(**overrides):
    """A production-shaped pr_open story: NO ``plan`` key, as manifests have."""
    story = {
        "key": E2E_KEY,
        "status": "pr_open",
        "parked_reason": None,
        "review_verdict": "APPROVE",
        "security_review_verdict": "APPROVE",
        "risk": "high",
        "summary": "Rewrite the auth token cache",
        "worktree": "",
        "dependencies": [],
    }
    story.update(overrides)
    return story


def _summary():
    """A summary dict carrying every key ``_adjudicate_merges`` appends to."""
    return {"parked": [], "notify": [], "failed": [], "merged": [], "ci_pending": []}


def test_gathered_pr_checks_land_in_the_persisted_manifest(plan_dir, monkeypatch):
    """No separate write is needed: the loop's manifest write carries it.

    Drives the REAL ``advance._adjudicate_merges`` with a story carrying no
    ``pr_checks`` and no ``plan`` key (the production manifest shape). The
    gather populates the story dict, and the loop's own
    ``_atomic_write_json(manifest_path, manifest)`` must persist it.
    """
    monkeypatch.setattr(server_mod, "PIPELINE_AUTONOMY", "full", raising=False)
    monkeypatch.setattr(server_mod, "PIPELINE_RISK_THRESHOLD", "low", raising=False)
    monkeypatch.setattr(advance_mod, "_notify_user", lambda *a, **k: None)
    writes = []

    def fake_write(path, data):
        writes.append(path)
        Path(path).write_text(json.dumps(data, indent=2))

    monkeypatch.setattr(advance_mod, "_atomic_write_json", fake_write)
    prompts = []

    def fake_invoke(prompt, plan_role_config=None):
        prompts.append(prompt)
        return PARK_REPLY

    monkeypatch.setattr(overlord_mod, "_invoke_overlord", fake_invoke)

    def fake_once(branch, *, sha):
        return dict(PASS_STATE)

    monkeypatch.setattr(server_mod, "_ci_status_once", fake_once, raising=False)
    monkeypatch.setattr(server_mod, "_ci_status", _boom_blocking, raising=False)
    (plan_dir / f"{PLAN}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": {E2E_KEY: _e2e_story()}}, indent=2)
    )

    summary = _summary()
    advance_mod._adjudicate_merges(PLAN, summary)

    assert summary["parked"] == [E2E_KEY]
    manifest = json.loads((plan_dir / f"{PLAN}.manifest.json").read_text())
    assert manifest["stories"][E2E_KEY]["pr_checks"] == PASS_STATE, (
        "the gathered pr_checks must land in the manifest via the loop's own "
        "manifest write"
    )
    assert len(writes) == 1, (
        "the populated pr_checks must ride the loop's single manifest write - "
        "no separate write may be added"
    )
    assert len(prompts) == 1
    assert "PR CHECKS: (none)" not in prompts[0]
    assert "'state': 'pass'" in prompts[0]
