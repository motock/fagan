"""RC-02: a merge-gate rebase CONFLICT routes to the implementer, not a park.

The merge gate rebases the story branch onto the default branch before
merging. When that rebase conflicts it aborts and returns
``gate_error = "rebase: ..."``; without a rework route the story retries the
identical merge until ``merge_attempts`` exhausts and it parks (CL-12/CL-20).
``_try_auto_resolve_conflict`` only handles pure additive-import conflicts, so
a real conflict needs the implementer told exactly what to do.

This reuses the CI-fail rework machinery (``ci_rework``, ``review_feedback``,
``_merge_ci_rework_feedback``) rather than building a second one, but bounds
it with its own ``conflict_reworks`` counter and
``PIPELINE_CONFLICT_REWORK_MAX`` budget so a conflict rework does NOT consume
``merge_attempts``/``rework_attempts``.

Mocking policy: the integration tests drive the REAL merge-adjudication tick
and stub only the external seams, following
``tests/unit/test_ci_rework_feedback_merge.py::_stub_tick``.
"""

import json
from pathlib import Path

import pipeline.advance as adv
from pipeline import server as p
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _read_manifest,
    _write_manifest,
    plan_dir,
)

CONFLICT_ERROR = (
    "rebase: CONFLICT (content): Merge conflict in src/app.py\n"
    "CONFLICT (content): Merge conflict in docs/guide.md"
)
CONFLICT_FILES = ("src/app.py", "docs/guide.md")
FETCH_ERROR = "rebase: fatal: unable to access 'https://github.com/x/y': Could not resolve host"
PUSH_ERROR = "push: ! [rejected] agent/P1 -> agent/P1 (fetch first)"


class _FakeBackend:
    """Stand-in for ``app.backend`` as advance.py sees it via its _ServerRef."""

    def get_backend(self, role, name=None):
        return self

    def resource_status(self, model_tag=None):
        return {"ok": True, "reason": ""}


def _stub_tick(monkeypatch, *, gate_error=CONFLICT_ERROR, notify_spy=None):
    """Stub every model/subprocess seam around the tick.

    Only the external seams are replaced: the rebase/push gate (which returns
    the ``gate_error`` under test), the CI status, the PR merge and the
    notification sink. The real tick and the real merge-gate branch run end to
    end.
    """
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
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
    if notify_spy is None:
        monkeypatch.setattr(p, "_notify_user", lambda plan, msg, **kw: None)
    else:
        def _record(plan, msg, **kw):
            notify_spy.append({"plan": plan, "message": msg, **kw})

        monkeypatch.setattr(p, "_notify_user", _record)
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: None)
    monkeypatch.setattr(p, "review_story", lambda plan, key: None)
    monkeypatch.setattr(p, "interrupt_story", lambda plan, key: {"ok": True})
    monkeypatch.setattr(
        p, "_merge_decision", lambda story: {"action": "merge", "reason": ""},
    )
    monkeypatch.setattr(
        p, "_rebase_and_push_for_merge",
        lambda plan, key, branch, worktree: (gate_error, ""),
    )
    monkeypatch.setattr(
        p, "_merge_gate_ci_status",
        lambda branch, *, sha: {"state": "none", "error": ""},
    )
    monkeypatch.setattr(p, "_ci_rerun", lambda sha: None)
    monkeypatch.setattr(p, "_default_branch", lambda: "master")
    monkeypatch.setattr(p, "_ci_pending_expired", lambda since: False)
    monkeypatch.setattr(
        p, "_ci_rework_feedback", lambda gate_error, attempts: "CI TEXT",
    )
    monkeypatch.setattr(p, "_merge_pr", lambda worktree, key: None)
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan: None)
    monkeypatch.setattr(p, "_maybe_record_retro", lambda plan, manifest: None)
    monkeypatch.setattr(p, "_mcp_restart_notice", lambda touched: "restart")
    monkeypatch.setattr(
        p, "_atomic_write_json",
        lambda path, data: Path(path).write_text(json.dumps(data, indent=2)),
    )
    monkeypatch.setattr(adv, "_count_on_device_in_progress_agents", lambda: 0)


def _pr_open_story(**over):
    story = {
        "summary": "pr open story",
        "status": "pr_open",
        "worktree": "/nonexistent-rc02-worktree",
        "dependencies": [],
        "review_verdict": "APPROVE",
        "risk": "low",
    }
    story.update(over)
    return story


# ---------- flag off: unchanged retry-then-park ----------


def test_flag_off_keeps_the_ordinary_retry_path(
    plan_dir,  # noqa: F811 - pytest fixture param, not a redefinition
    monkeypatch,
):
    """``PIPELINE_REWORK_ON_CONFLICT=0`` disables the route entirely."""
    monkeypatch.setenv("PIPELINE_REWORK_ON_CONFLICT", "0")
    _stub_tick(monkeypatch)
    _write_manifest(plan_dir, "rc02_off", {"P1": _pr_open_story()})

    result = p.advance_pipeline("rc02_off")

    assert result["ok"] is True
    story = _read_manifest(plan_dir, "rc02_off")["stories"]["P1"]
    assert story["status"] == "pr_open", "flag off must leave the retry path"
    assert story.get("ci_rework") is not True
    assert story.get("conflict_reworks", 0) == 0
    assert story["merge_attempts"] == 1, "the ordinary retry counter still runs"


# ---------- flag on + conflict: routed to the implementer ----------


def test_conflict_routes_to_rework_without_consuming_merge_attempts(
    plan_dir,  # noqa: F811
    monkeypatch,
):
    """THE regression guard: a rebase conflict becomes implementer rework."""
    notify_spy: list[dict] = []
    _stub_tick(monkeypatch, notify_spy=notify_spy)
    _write_manifest(plan_dir, "rc02_on", {"P1": _pr_open_story()})

    result = p.advance_pipeline("rc02_on")

    assert result["ok"] is True
    story = _read_manifest(plan_dir, "rc02_on")["stories"]["P1"]
    assert story["status"] == "changes_requested"
    assert story["ci_rework"] is True, "raises the agent's done-bar to full-suite green"
    assert story["conflict_reworks"] == 1
    feedback = story["review_feedback"]
    for path in CONFLICT_FILES:
        assert path in feedback, f"feedback must name {path}; got {feedback!r}"
    assert "1" in feedback and "2" in feedback, (
        f"feedback must name the attempt and the cap; got {feedback!r}"
    )
    # A conflict rework is NOT a merge attempt: the merge gate's own budget is
    # untouched so the story can come back through tests -> review -> merge.
    assert story.get("merge_attempts", 0) == 0
    assert story.get("rework_attempts", 0) == 0
    assert "P1" in result["notify"]

    events = [c.get("event") for c in notify_spy]
    assert "merge_conflict_rework" in events, (
        f"expected a merge_conflict_rework event; got {events!r}"
    )
    notice = next(c for c in notify_spy if c.get("event") == "merge_conflict_rework")
    for path in CONFLICT_FILES:
        assert path in notice["message"], (
            f"the notice must name {path}; got {notice['message']!r}"
        )
    assert "1/2" in notice["message"], (
        f"the notice must name the attempt; got {notice['message']!r}"
    )


def test_conflict_rework_preserves_prior_review_feedback(
    plan_dir,  # noqa: F811
    monkeypatch,
):
    """The existing merge helper carries earlier review findings forward."""
    prior = "REQUEST_CHANGES: keep the README table's four columns."
    _stub_tick(monkeypatch)
    _write_manifest(
        plan_dir, "rc02_prior", {"P1": _pr_open_story(review_feedback=prior)},
    )

    p.advance_pipeline("rc02_prior")

    story = _read_manifest(plan_dir, "rc02_prior")["stories"]["P1"]
    assert prior in story["review_feedback"]
    assert "src/app.py" in story["review_feedback"]


# ---------- the cap: a third conflict falls through ----------


def test_third_conflict_falls_through_to_the_ordinary_path(
    plan_dir,  # noqa: F811
    monkeypatch,
):
    """Past ``PIPELINE_CONFLICT_REWORK_MAX`` (default 2) the retry/park path runs."""
    _stub_tick(monkeypatch)
    _write_manifest(
        plan_dir, "rc02_cap", {"P1": _pr_open_story(conflict_reworks=2)},
    )

    p.advance_pipeline("rc02_cap")

    story = _read_manifest(plan_dir, "rc02_cap")["stories"]["P1"]
    assert story["status"] == "pr_open", "past the cap it retries, not reworks"
    assert story["conflict_reworks"] == 2, "the cap must not be exceeded"
    assert story.get("ci_rework") is not True
    assert story["merge_attempts"] == 1


def test_conflict_rework_max_env_overrides_the_default(
    plan_dir,  # noqa: F811
    monkeypatch,
):
    """``PIPELINE_CONFLICT_REWORK_MAX=1`` caps the route at one round."""
    monkeypatch.setenv("PIPELINE_CONFLICT_REWORK_MAX", "1")
    _stub_tick(monkeypatch)
    _write_manifest(
        plan_dir, "rc02_max1", {"P1": _pr_open_story(conflict_reworks=1)},
    )

    p.advance_pipeline("rc02_max1")

    story = _read_manifest(plan_dir, "rc02_max1")["stories"]["P1"]
    assert story["status"] == "pr_open"
    assert story["conflict_reworks"] == 1


def test_invalid_conflict_rework_max_falls_back_to_two(
    plan_dir,  # noqa: F811
    monkeypatch,
):
    """A non-numeric cap falls back to the default of 2, not to zero."""
    monkeypatch.setenv("PIPELINE_CONFLICT_REWORK_MAX", "not-a-number")
    _stub_tick(monkeypatch)
    _write_manifest(
        plan_dir, "rc02_badmax", {"P1": _pr_open_story(conflict_reworks=1)},
    )

    p.advance_pipeline("rc02_badmax")

    story = _read_manifest(plan_dir, "rc02_badmax")["stories"]["P1"]
    assert story["status"] == "changes_requested", (
        "an invalid cap must fall back to 2, so a second conflict still routes"
    )
    assert story["conflict_reworks"] == 2


def test_conflict_rework_max_zero_disables_the_route(
    plan_dir,  # noqa: F811
    monkeypatch,
):
    """``PIPELINE_CONFLICT_REWORK_MAX=0`` is a valid explicit "never route"."""
    monkeypatch.setenv("PIPELINE_CONFLICT_REWORK_MAX", "0")
    _stub_tick(monkeypatch)
    _write_manifest(plan_dir, "rc02_max0", {"P1": _pr_open_story()})

    p.advance_pipeline("rc02_max0")

    story = _read_manifest(plan_dir, "rc02_max0")["stories"]["P1"]
    assert story["status"] == "pr_open"
    assert story.get("ci_rework") is not True
    assert story.get("conflict_reworks", 0) == 0
    assert story["merge_attempts"] == 1


# ---------- negative: only a CONFLICT rebase error is routed ----------


def test_non_conflict_rebase_error_is_not_routed(
    plan_dir,  # noqa: F811
    monkeypatch,
):
    """A fetch failure is a ``rebase:`` error but not a conflict."""
    _stub_tick(monkeypatch, gate_error=FETCH_ERROR)
    _write_manifest(plan_dir, "rc02_fetch", {"P1": _pr_open_story()})

    p.advance_pipeline("rc02_fetch")

    story = _read_manifest(plan_dir, "rc02_fetch")["stories"]["P1"]
    assert story["status"] == "pr_open"
    assert story.get("ci_rework") is not True
    assert story.get("conflict_reworks", 0) == 0
    assert story["merge_attempts"] == 1


def test_push_error_is_not_routed(
    plan_dir,  # noqa: F811
    monkeypatch,
):
    """A push rejection is not a rebase conflict even if it says 'conflict'."""
    _stub_tick(
        monkeypatch,
        gate_error=PUSH_ERROR + " (non-fast-forward conflict)",
    )
    _write_manifest(plan_dir, "rc02_push", {"P1": _pr_open_story()})

    p.advance_pipeline("rc02_push")

    story = _read_manifest(plan_dir, "rc02_push")["stories"]["P1"]
    assert story["status"] == "pr_open"
    assert story.get("ci_rework") is not True
    assert story.get("conflict_reworks", 0) == 0
    assert story["merge_attempts"] == 1


# ---------- the human hold is not weakened ----------


def test_conflict_reworked_high_risk_story_is_not_auto_merged(
    plan_dir,  # noqa: F811
    monkeypatch,
):
    """A conflict rework returns a high-risk story to the human hold.

    Phase 1: the merge gate hits a rebase conflict and routes it to the
    implementer. Phase 2: the agent commits new code, so the story re-enters
    tests -> review -> merge. At that merge the real ``_merge_decision`` must
    still park a high-risk story for human review - the conflict route must
    not become a back door around the risk hold.
    """
    from pipeline import config as config_mod
    from pipeline.merge import _merge_decision as real_merge_decision

    _stub_tick(monkeypatch)
    merged: list[str] = []
    monkeypatch.setattr(p, "_merge_pr", lambda worktree, key: merged.append(key))
    monkeypatch.setattr(config_mod, "PIPELINE_AUTO_MERGE", True)
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    _write_manifest(
        plan_dir, "rc02_highrisk", {"P1": _pr_open_story(risk="high")},
    )

    # Phase 1: the story reached the merge gate (as it does in full autonomy
    # when the overlord rules "merge") and the rebase conflicted. The conflict
    # is routed to the implementer.
    p.advance_pipeline("rc02_highrisk")
    story = _read_manifest(plan_dir, "rc02_highrisk")["stories"]["P1"]
    assert story["status"] == "changes_requested"
    assert story["conflict_reworks"] == 1

    # Phase 2: the agent committed; the story is back at the merge gate and
    # the REAL decision function runs. Gated + high risk must park.
    monkeypatch.setattr(p, "_merge_decision", real_merge_decision)
    story["status"] = "pr_open"
    story["review_verdict"] = "APPROVE"
    _write_manifest(plan_dir, "rc02_highrisk", {"P1": story})

    p.advance_pipeline("rc02_highrisk")

    story = _read_manifest(plan_dir, "rc02_highrisk")["stories"]["P1"]
    assert merged == [], "a high-risk story must never be auto-merged"
    assert story["status"] == "parked"
    assert "high risk" in story["parked_reason"]
