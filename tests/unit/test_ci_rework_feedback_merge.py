"""Mode 50, root cause 3: a CI-fail rework must not discard review feedback.

``pipeline/advance_merge.py``'s merge-gate CI-fail branch used to do::

    story["review_feedback"] = _ci_rework_feedback(gate_error, attempts)

which REPLACES any earlier review feedback. When a story had a REQUEST_CHANGES
round (whose findings live in ``review_feedback``) and the reviewer later
APPROVEd but CI failed on something else, the next dispatch saw only the CI
error - so the implementer fixed the CI error and could silently re-break what
the reviewer had asked for.

The fix keeps the prior text in front of the CI failure via
``_merge_ci_rework_feedback``, unless the prior text is itself only a previous
CI block (stale - its gate error is superseded by this one) or empty.

Mocking policy: the integration test drives the REAL merge-adjudication tick
and stubs only the external seams (CI status, the CI-feedback formatter, the
PR merge), following
``tests/unit/test_advance_merge_adjudication_runs_first.py::_stub_tick``.
"""

import json
from pathlib import Path

import pipeline.advance as adv
import pipeline.advance_merge as adv_merge
from pipeline import server as p
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _read_manifest,
    _write_manifest,
    plan_dir,
)

PRIOR = "REQUEST_CHANGES: the README table must keep its four columns."
CI_TEXT = "CI TEXT"
MARKER = adv_merge._CI_FEEDBACK_MARKER
HEADER = adv_merge._PRIOR_FEEDBACK_HEADER


# ---------- unit tests: the pure string helper ----------


def test_prior_review_feedback_is_preserved_before_the_ci_failure():
    """The reviewer's findings must survive, ahead of the CI failure."""
    merged = adv_merge._merge_ci_rework_feedback(PRIOR, CI_TEXT)

    assert PRIOR in merged
    assert CI_TEXT in merged
    assert merged.index(PRIOR) < merged.index(CI_TEXT), (
        "the earlier review feedback must come BEFORE the CI failure; "
        f"got {merged!r}"
    )


def test_no_prior_feedback_returns_ci_feedback_unchanged():
    """No prior round -> the CI text is returned byte-for-byte."""
    assert adv_merge._merge_ci_rework_feedback(None, CI_TEXT) == CI_TEXT


def test_whitespace_only_prior_returns_ci_feedback_unchanged():
    """A blank prior round is not feedback -> CI text unchanged."""
    assert adv_merge._merge_ci_rework_feedback("   \n", CI_TEXT) == CI_TEXT


def test_second_ci_round_does_not_stack_the_first_rounds_gate_error():
    """Repeat CI rounds must not accumulate stale gate errors."""
    first = adv_merge._merge_ci_rework_feedback(
        PRIOR, MARKER + "\nGate error: ROUND ONE"
    )
    second = adv_merge._merge_ci_rework_feedback(
        first, MARKER + "\nGate error: ROUND TWO"
    )

    assert second.count(PRIOR) == 1, (
        f"the prior feedback must appear exactly once; got {second!r}"
    )
    assert "ROUND ONE" not in second, (
        f"the superseded gate error must be dropped; got {second!r}"
    )
    assert "ROUND TWO" in second


def test_prior_feedback_that_is_only_a_previous_ci_block_is_dropped():
    """A prior that is only an old CI block is stale, not review feedback."""
    prior = MARKER + "\nGate error: old gate error"

    assert adv_merge._merge_ci_rework_feedback(prior, CI_TEXT) == CI_TEXT


class _FakeBackend:
    """Stand-in for ``app.backend`` as advance.py sees it via its _ServerRef."""

    def get_backend(self, role, name=None):
        return self

    def resource_status(self, model_tag=None):
        return {"ok": True, "reason": ""}


def _stub_tick(monkeypatch):
    """Stub every model/subprocess seam around the tick.

    Only the external seams are replaced: the merge-gate CI status, the
    CI-feedback formatter and the PR merge. The real tick and the real
    merge-gate branch run end to end.
    """
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setenv("PIPELINE_REWORK_ON_CI_FAIL", "1")
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "MAX_CONCURRENT_AGENTS", 4)
    monkeypatch.setattr(
        p, "_role_resource_ok",
        lambda role, plan_role_config=None: (True, ""),
    )
    monkeypatch.setattr(p, "backend", _FakeBackend())
    monkeypatch.setattr(
        p, "check_story_status", lambda plan, key: {"status": "running"},
    )
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg, **kw: None)
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: None)
    monkeypatch.setattr(p, "review_story", lambda plan, key: None)
    monkeypatch.setattr(
        p, "interrupt_story", lambda plan, key: {"ok": True},
    )
    # Merge helpers (all _ServerRef names -> patch on pipeline.server).
    monkeypatch.setattr(
        p, "_merge_decision", lambda story: {"action": "merge", "reason": ""},
    )
    monkeypatch.setattr(
        p, "_rebase_and_push_for_merge",
        lambda plan, key, branch, worktree: ("", "abc123"),
    )
    monkeypatch.setattr(
        p, "_merge_gate_ci_status",
        lambda branch, *, sha: {
            "state": "fail",
            "error": "ci fail: Lint (ruff): failure",
        },
    )
    monkeypatch.setattr(p, "_ci_rerun", lambda sha: None)
    monkeypatch.setattr(p, "_ci_pending_expired", lambda since: False)
    monkeypatch.setattr(
        p, "_ci_rework_feedback", lambda gate_error, attempts: CI_TEXT,
    )
    monkeypatch.setattr(p, "_merge_pr", lambda worktree, key: None)
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan: None)
    monkeypatch.setattr(p, "_maybe_record_retro", lambda plan, manifest: None)
    monkeypatch.setattr(p, "_mcp_restart_notice", lambda touched: "restart")
    monkeypatch.setattr(
        p, "_atomic_write_json",
        lambda path, data: Path(path).write_text(json.dumps(data, indent=2)),
    )
    monkeypatch.setattr(
        adv, "_count_on_device_in_progress_agents", lambda: 0,
    )


def test_merge_gate_ci_failure_keeps_prior_review_feedback(
    plan_dir,  # noqa: F811 - pytest fixture param, not a redefinition
    monkeypatch,
):
    """THE regression guard: a CI-fail rework must not replace review feedback.

    Fails against the pre-change call site, where ``review_feedback`` is
    exactly ``"CI TEXT"`` and the reviewer's findings are gone.
    """
    _stub_tick(monkeypatch)
    _write_manifest(plan_dir, "cimerge_prior", {
        "P1": {
            "summary": "pr open story",
            "status": "pr_open",
            "worktree": "/nonexistent-cimerge-worktree",
            "dependencies": [],
            "review_feedback": PRIOR,
        },
    })

    result = p.advance_pipeline("cimerge_prior")

    assert result["ok"] is True
    story = _read_manifest(plan_dir, "cimerge_prior")["stories"]["P1"]
    assert story["status"] == "changes_requested"
    assert story["ci_rework"] is True
    assert PRIOR in story["review_feedback"], (
        "the earlier review feedback must survive the CI-fail rework; "
        f"got {story['review_feedback']!r}"
    )
    assert CI_TEXT in story["review_feedback"]
