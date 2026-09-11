"""Merge adjudication must run FIRST in the advance tick (LOCKSTARVE-B2).

``_adjudicate_merges`` is model-free and fast, but it currently sits at the
END of the ``with _scoped_repo_root(plan_name):`` body — behind two
synchronous model phases (the test-author phase and the planner call inside
dispatch_story, plus the code-review agent run in section 2), all of which
run while the tick holds the plan's ``_plan_lock``. A story whose CI has
already gone green therefore waits for every sibling dispatch in the same
tick before it can merge. The fix moves the single
``_adjudicate_merges(plan_name, summary)`` call to the TOP of that ``with``
body — before the per-story in-progress interruption gate and before the
dispatch section (the block introduced by
``# 1. Dispatch ready (and resumable-interrupted) stories, capped to``).

These tests pin, mechanically and behaviorally:

  - the call site count stays EXACTLY ONE (no second call may be added to
    compensate for the same-tick pr_open trade — a story that only becomes
    ``pr_open`` later in the tick merges on the NEXT tick, which is the
    intended, acceptable trade);
  - the call sits at the top of the scoped ``with`` body: 8-space indent,
    before the per-story interruption gate comment and before the dispatch
    section comment;
  - a tick with one ``pr_open`` story AND one ``todo`` story merges BEFORE
    it dispatches (the regression guard — this fails against the pre-change
    ordering, where adjudication runs last);
  - a todo-only plan, a pr_open-only plan and an EMPTY manifest all tick
    cleanly (adjudication running first must not short-circuit the rest of
    the tick, and the dispatch section must still be reached afterwards);
  - ``_adjudicate_merges`` is called exactly ONCE per tick.

Mocking policy: only the real seams — ``dispatch_story``, ``review_story``
and the merge helpers. Every merge helper is resolved through a
``_ServerRef`` (the tick dereferences names on ``pipeline.server``), so they
are patched there, NOT on ``pipeline.advance``; the one advance-local seam
used to prove the dispatch section was reached
(``_count_on_device_in_progress_agents``, a plain def in pipeline/advance.py)
is patched on ``pipeline.advance``. ``_advance_pipeline_locked_impl`` itself
is NOT mocked — the real tick runs end to end.
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

_CALL = "_adjudicate_merges(plan_name, summary)"
_DEF = "def _adjudicate_merges("
_WITH = "with _scoped_repo_root(plan_name):"
_GATE_COMMENT = "# Per-story dispatch gate."
_DISPATCH_COMMENT = "# 1. Dispatch ready"


def _advance_source() -> str:
    return Path(adv.__file__).read_text()


class _FakeBackend:
    """Stand-in for ``app.backend`` as advance.py sees it via its _ServerRef.

    Every driver lookup resolves to a driver whose ``resource_status`` is ok,
    so the per-story dispatch gate never interrupts or defers.
    """

    def get_backend(self, role, name=None):
        return self

    def resource_status(self, model_tag=None):
        return {"ok": True, "reason": ""}


def _todo_story():
    return {"summary": "todo story", "status": "todo", "dependencies": []}


def _pr_open_story():
    # worktree points at a path that does NOT exist: the merge gate's
    # `Path(worktree).is_dir()` guard then skips the function-local
    # `from .pr import _resolve_story_branch` probe entirely, so no real
    # git worktree is needed and the (patched) rebase/CI/merge helpers are
    # the only seams the merge path touches.
    return {
        "summary": "pr open story",
        "status": "pr_open",
        "worktree": "/nonexistent-mergefirst-worktree",
        "dependencies": [],
    }


def _stub_tick(monkeypatch, markers):
    """Stub every model/subprocess seam around the tick; record phase order.

    Appends ``(kind, key)`` tuples to ``markers``:
      ("merge", key)     - the merge path merged a pr_open story
      ("dispatch", key)  - the dispatch section dispatched a story
      ("slotcount", None)- the dispatch section's cap math ran (proves
                           section 1 was reached)
      ("notify", msg)    - any notification
    """
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "MAX_CONCURRENT_AGENTS", 4)
    # Blanket dispatch/review gates pass; the per-story gate consults
    # backend.get_backend(...).resource_status itself (fake below).
    monkeypatch.setattr(
        p, "_role_resource_ok",
        lambda role, plan_role_config=None: (True, ""),
    )
    monkeypatch.setattr(p, "backend", _FakeBackend())
    # Polling must not transition anything (no in_progress stories anyway).
    monkeypatch.setattr(
        p, "check_story_status", lambda plan, key: {"status": "running"},
    )
    monkeypatch.setattr(
        p, "_notify_user", lambda plan, msg, **kw: markers.append(("notify", msg)),
    )
    monkeypatch.setattr(
        p, "dispatch_story", lambda plan, key: markers.append(("dispatch", key)),
    )
    monkeypatch.setattr(
        p, "review_story", lambda plan, key: markers.append(("review", key)),
    )
    monkeypatch.setattr(
        p, "interrupt_story",
        lambda plan, key: markers.append(("interrupt", key)) or {"ok": True},
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
        lambda branch, *, sha: {"state": "success", "error": ""},
    )
    monkeypatch.setattr(p, "_ci_rerun", lambda sha: None)
    monkeypatch.setattr(p, "_ci_pending_expired", lambda since: False)
    monkeypatch.setattr(
        p, "_ci_rework_feedback", lambda gate_error, attempts: "ci rework feedback",
    )
    monkeypatch.setattr(
        p, "_mcp_self_source_touched", lambda worktree, ref: False,
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "master")
    monkeypatch.setattr(
        p, "_merge_pr", lambda worktree, key: markers.append(("merge", key)),
    )
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan: None)
    monkeypatch.setattr(p, "_maybe_record_retro", lambda plan, manifest: None)
    monkeypatch.setattr(p, "_mcp_restart_notice", lambda touched: "restart")
    monkeypatch.setattr(
        p, "_atomic_write_json",
        lambda path, data: Path(path).write_text(json.dumps(data, indent=2)),
    )
    # Section-1 seam (plain def in pipeline/advance.py, called by the cap
    # math): proves the dispatch section was reached on this tick.
    monkeypatch.setattr(
        adv, "_count_on_device_in_progress_agents",
        lambda: markers.append(("slotcount", None)) or 0,
    )


# ---------- source-level pins (mechanically checkable requirements) ----------


def test_exactly_one_adjudicate_call_site_and_one_def():
    """Exactly ONE call site: the move is delete-one-line/insert-one-line, and
    no second call may be added to compensate for the next-tick pr_open trade."""
    src = _advance_source()
    assert src.count(_DEF) == 1, (
        "the _adjudicate_merges definition must remain exactly once"
    )
    assert src.count(_CALL) == 1, (
        "exactly one _adjudicate_merges(plan_name, summary) call site is "
        f"allowed; found {src.count(_CALL)}"
    )


def test_call_sits_at_top_of_scoped_with_body_before_dispatch_section():
    """The single call must be the top of the ``with _scoped_repo_root`` body:
    8-space indent (inside the with), before the per-story in-progress
    interruption gate and before the dispatch section."""
    src = _advance_source()
    with_idx = src.rindex(_WITH)
    call_idx = src.index(_CALL)
    gate_idx = src.index(_GATE_COMMENT)
    dispatch_idx = src.index(_DISPATCH_COMMENT)
    assert with_idx < call_idx, (
        "the call must sit inside the with _scoped_repo_root(plan_name) body"
    )
    assert call_idx < gate_idx, (
        "the call must sit at the TOP of the with body, before the "
        f"per-story interruption gate comment; with@{with_idx}, "
        f"call@{call_idx}, gate@{gate_idx}"
    )
    assert call_idx < dispatch_idx, (
        "the call must run before the dispatch section; "
        f"call@{call_idx}, dispatch@{dispatch_idx}"
    )
    line = next(
        l for l in src.splitlines() if l.strip() == _CALL
    )
    assert line == "        " + _CALL, (
        "the call must keep its 8-space indentation inside the with body; "
        f"got {line!r}"
    )


# ---------- behavioral pins (the real tick, seams stubbed) ----------


def test_merge_adjudication_runs_before_dispatch(
    plan_dir,  # noqa: F811 - pytest fixture param, not a redefinition
    monkeypatch,
):
    """THE regression guard: with one pr_open story and one todo story, the
    merge marker must appear BEFORE the dispatch marker. Fails against the
    pre-change ordering, where adjudication runs after the dispatch and
    review sections."""
    markers = []
    _stub_tick(monkeypatch, markers)
    _write_manifest(plan_dir, "mergefirst_order", {
        "S1": _todo_story(),
        "P1": _pr_open_story(),
    })

    result = p.advance_pipeline("mergefirst_order")

    assert result["ok"] is True
    kinds = [kind for kind, _ in markers]
    assert "merge" in kinds, (
        f"the pr_open story must merge this tick; markers={markers}"
    )
    assert "dispatch" in kinds, (
        f"the todo story must dispatch this tick; markers={markers}"
    )
    assert kinds.index("merge") < kinds.index("dispatch"), (
        "merge adjudication must run BEFORE the dispatch section "
        f"(pre-change ordering runs it last); markers={markers}"
    )
    assert result["merged"] == ["P1"]
    assert result["dispatched"] == ["S1"]


def test_todo_only_plan_still_dispatches_when_adjudication_runs_first(
    plan_dir,  # noqa: F811 - pytest fixture param, not a redefinition
    monkeypatch,
):
    """Negative: a plan whose only story is todo (no pr_open story at all)
    still dispatches normally and the tick returns ok — adjudication running
    first must not short-circuit the rest of the tick."""
    markers = []
    _stub_tick(monkeypatch, markers)
    _write_manifest(plan_dir, "mergefirst_todo_only", {"T1": _todo_story()})

    result = p.advance_pipeline("mergefirst_todo_only")

    assert result["ok"] is True
    assert ("dispatch", "T1") in markers, (
        f"the todo story must still dispatch; markers={markers}"
    )
    assert result["dispatched"] == ["T1"]
    assert [kind for kind, _ in markers if kind == "merge"] == [], (
        f"no story is pr_open, so nothing may merge; markers={markers}"
    )


def test_pr_open_only_plan_merges_then_dispatch_section_still_runs(
    plan_dir,  # noqa: F811 - pytest fixture param, not a redefinition
    monkeypatch,
):
    """Negative: a plan whose only story is pr_open still merges, and the
    dispatch section is reached afterwards without raising."""
    markers = []
    _stub_tick(monkeypatch, markers)
    _write_manifest(plan_dir, "mergefirst_pr_only", {"P1": _pr_open_story()})

    result = p.advance_pipeline("mergefirst_pr_only")

    assert result["ok"] is True
    assert ("merge", "P1") in markers, (
        f"the pr_open story must merge; markers={markers}"
    )
    assert result["merged"] == ["P1"]
    # The dispatch section ran afterwards (its cap math appends slotcount)
    # and did not raise.
    assert ("slotcount", None) in markers, (
        f"the dispatch section must still be reached; markers={markers}"
    )
    kinds = [kind for kind, _ in markers]
    assert kinds.index("merge") < kinds.index("slotcount"), (
        f"merge must precede the dispatch section; markers={markers}"
    )
    assert "dispatched" in result
    # The merged story is persisted as done.
    manifest = _read_manifest(plan_dir, "mergefirst_pr_only")
    assert manifest["stories"]["P1"]["status"] == "done"


def test_empty_manifest_tick_is_ok_and_neither_phase_raises(
    plan_dir,  # noqa: F811 - pytest fixture param, not a redefinition
    monkeypatch,
):
    """Boundary: an empty manifest ({"stories": {}}) — the tick returns
    {"ok": True, ...} and neither the merge phase nor dispatch raises."""
    markers = []
    _stub_tick(monkeypatch, markers)
    _write_manifest(plan_dir, "mergefirst_empty", {})

    result = p.advance_pipeline("mergefirst_empty")

    assert result["ok"] is True
    assert result["dispatched"] == []
    assert [kind for kind, _ in markers if kind in ("merge", "dispatch")] == [], (
        f"nothing to merge or dispatch; markers={markers}"
    )


def test_adjudicate_merges_called_exactly_once_per_tick(
    plan_dir,  # noqa: F811 - pytest fixture param, not a redefinition
    monkeypatch,
):
    """Idempotence: _adjudicate_merges runs exactly ONCE per tick — pins the
    'do not leave a second call site' requirement mechanically."""
    markers = []
    _stub_tick(monkeypatch, markers)
    calls = []
    real = adv._adjudicate_merges

    def counting(plan_name, summary):
        calls.append(plan_name)
        return real(plan_name, summary)

    monkeypatch.setattr(adv, "_adjudicate_merges", counting)
    _write_manifest(plan_dir, "mergefirst_once", {
        "S1": _todo_story(),
        "P1": _pr_open_story(),
    })

    result = p.advance_pipeline("mergefirst_once")

    assert result["ok"] is True
    assert len(calls) == 1, (
        "_adjudicate_merges must be called exactly once per tick "
        f"(no second call site); got {len(calls)}"
    )