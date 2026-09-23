# ruff: noqa
"""Triage's split_story and mark_done executors, moved verbatim from
pipeline/triage.py.

Every name this code reads from pipeline.triage is bound as a _ModuleRef,
resolved at call time, so monkeypatch.setattr(pipeline.triage, NAME, ...)
keeps landing on the moved code. pipeline.triage re-exports the moved names.
"""

from pathlib import Path

from .module_ref import _ModuleRef

SIBLING_NOTE = _ModuleRef("pipeline.triage", "SIBLING_NOTE")
_coerce_int = _ModuleRef("pipeline.triage", "_coerce_int")
_current_suite_state = _ModuleRef("pipeline.triage", "_current_suite_state")
_notify_user = _ModuleRef("pipeline.triage", "_notify_user")
_park = _ModuleRef("pipeline.triage", "_park")
_worktree_has_new_commits = _ModuleRef("pipeline.triage", "_worktree_has_new_commits")
plan_triage_budget_exhausted = _ModuleRef("pipeline.triage", "plan_triage_budget_exhausted")


def _execute_split_story(plan_name, story_key, story, ruling, manifest, manifest_path) -> str:
    """Execute a ``split_story`` ruling by creating two child stories.

    Guards, in order:

    1. ``ruling['split']`` must carry exactly two non-empty summaries, else the
       story is parked loudly for a human (invalid SPLIT payload).
    2. :func:`plan_triage_budget_exhausted` – the plan's triage-created-story
       budget is spent, so nothing is created and the story is parked loudly.

    On success two children ``{story_key}-split-1`` / ``{story_key}-split-2``
    are appended to ``manifest['stories']``: each carries the payload summary,
    the parent's ``agent_instructions`` extended with a
    ``=== SPLIT FROM PRIOR STORY ===`` block (the ruling's rationale plus the
    sibling note), ``persona``/``risk``/``backend`` copied from the parent when
    present, and status ``todo``.  The parent's ``acceptance`` fixtures cannot
    be mechanically halved, so the children carry no ``acceptance`` field –
    agent-authored tests carry the bar.  The children are independent (no
    dependency between them).  The parent is parked with a provenance reason
    naming the children and ``manifest['triage_created_stories']`` is
    incremented by 2.

    The manifest file is NOT written here: :func:`run_triage_sweep` persists
    the mutated manifest after the tick.
    """
    action = ruling.get("action", "split_story")
    rationale = ruling.get("rationale", "")[:300]

    # Guard 1: the SPLIT payload must be exactly two non-empty summaries.
    split = ruling.get("split")
    summaries = [s for s in split if isinstance(s, str) and s.strip()] if isinstance(split, list) else []
    if len(summaries) != 2 or len(split if isinstance(split, list) else []) != 2:
        story["triage_deferred_action"] = action
        reason = "split_story ruled but SPLIT payload invalid; parked for a human"
        result = _park(plan_name, story_key, story, reason)
        try:
            _notify_user(plan_name, f"{story_key} triage: {action} – {rationale}")
        except Exception:
            pass
        return result

    # Guard 2: the plan's triage-created-story budget.
    if plan_triage_budget_exhausted(manifest):
        story["triage_deferred_action"] = action
        reason = "plan triage budget exhausted"
        result = _park(plan_name, story_key, story, reason)
        try:
            _notify_user(plan_name, f"{story_key} triage: {action} – {rationale}")
        except Exception:
            pass
        return result

    child1_key = f"{story_key}-split-1"
    child2_key = f"{story_key}-split-2"
    split_block = (
        "\n\n=== SPLIT FROM PRIOR STORY ===\n"
        f"rationale: {rationale}\n"
        f"{SIBLING_NOTE}\n"
    )
    parent_instructions = story.get("agent_instructions") or ""

    for child_key, child_summary in ((child1_key, summaries[0]), (child2_key, summaries[1])):
        child = {
            "key": child_key,
            "summary": child_summary,
            "status": "todo",
            "agent_instructions": parent_instructions + split_block,
        }
        for field in ("persona", "risk", "backend"):
            if field in story:
                child[field] = story[field]
        manifest["stories"][child_key] = child

    story["status"] = "parked"
    story["parked_reason"] = f"split into {child1_key}, {child2_key} by triage"
    _park(plan_name, story_key, story, story["parked_reason"])

    manifest["triage_created_stories"] = _coerce_int(
        manifest.get("triage_created_stories", 0)
    ) + 2

    story["_split_children"] = [child1_key, child2_key]
    return "split_story"


# The exact fail-closed park reason for an uncorroborated mark_done ruling.
_MARK_DONE_UNCORROBORATED_REASON = (
    "mark_done ruled but live evidence does not corroborate"
)
# The PASS sentinel returned by _current_suite_state (compared by equality,
# never by truthiness - the FAIL/empty shapes are also truthy strings).
_SUITE_PASSES_SENTINEL = (
    "CURRENT STATE: full test suite PASSES at the worktree's current HEAD."
)


def _execute_mark_done(plan_name, story_key, story, ruling, manifest, manifest_path) -> str:
    """Execute a ``mark_done`` ruling by correcting the story record.

    Fail-closed corroboration comes FIRST, before any mutation: an
    uncorroborated ``mark_done`` is the dangerous direction, so the executor
    acts only on corroborated LIVE evidence - never on the overlord's word
    alone and never on ``parked_reason`` text. The live predicates mirror the
    facts :func:`_current_git_state` surfaces:

      1. the story's branch has NEW COMMITS vs the base branch, via
         :func:`pipeline.git_ops._worktree_has_new_commits` with the base
         resolved the way its existing callers do (lazy
         :func:`pipeline.server._default_branch` import);
      2. the story carries a ``pr_url`` (a merged PR);
      3. :func:`_current_suite_state` reports the suite PASSES at HEAD
         (equality against the PASS sentinel, not truthiness).

    Uncorroborated -> the story is parked loudly with
    ``_MARK_DONE_UNCORROBORATED_REASON`` (status ``parked`` + notify) and
    ``triage_deferred_action`` is left untouched, so a re-dispatch takes the
    same fail-closed path instead of silently skipping.

    Corroborated -> ``story['status'] = 'done'`` and
    ``triage_deferred_action`` is cleared. This executor deliberately does
    NOT append the OPSA-3 execution record itself: the only production
    caller, :func:`_apply_ruling_for_mode`, snapshots
    ``prior_status``/``prior_parked_reason`` before the mutation and records
    the outcome with the real autonomy mode once :func:`execute_ruling`
    returns (recording here as well wrote two records per ruling, the inner
    one hardcoding ``mode='full'``).

    The manifest file is NOT written here: :func:`run_triage_sweep` persists
    the mutated manifest after the tick.
    """
    action = ruling.get("action", "mark_done")
    rationale = ruling.get("rationale", "")[:300]

    # (a) Corroborate FIRST - no story mutation before this point.
    worktree = story.get("worktree")
    has_new_commits = False
    suite_green = False
    if isinstance(worktree, str) and worktree:
        base = ""
        try:
            # Lazy import: pipeline.server imports this module at module
            # level, so a module-level import here would be circular. This is
            # how the existing callers resolve the base branch.
            from .server import _default_branch

            base = _default_branch()
        except Exception:  # noqa: BLE001 - unresolved base fails closed
            base = ""
        if base:
            try:
                has_new_commits = bool(
                    _worktree_has_new_commits(
                        Path(worktree),
                        str(story.get("story_key") or story.get("key") or ""),
                        base,
                    )
                )
            except Exception:  # noqa: BLE001 - probe failure fails closed
                has_new_commits = False
        try:
            suite_green = _current_suite_state(worktree) == _SUITE_PASSES_SENTINEL
        except Exception:  # noqa: BLE001 - probe failure fails closed
            suite_green = False
    has_merged_pr = bool(story.get("pr_url"))
    corroborated = has_new_commits or has_merged_pr or suite_green

    # (b) Not corroborated -> park loudly; the record stays as-is.
    if not corroborated:
        result = _park(plan_name, story_key, story, _MARK_DONE_UNCORROBORATED_REASON)
        try:
            _notify_user(plan_name, f"{story_key} triage: {action} – {rationale}")
        except Exception:  # pragma: no cover – notification failures are ignored
            pass
        return result

    # (c) Corroborated -> correct the record. No execution record here: the
    # only production caller, _apply_ruling_for_mode, already snapshots the
    # prior state and appends the OPSA-3 record with the real autonomy mode
    # once execute_ruling returns (matching _execute_split_story, which also
    # does not record). Recording here too wrote TWO records per ruling and
    # hardcoded mode="full", misstating PIPELINE_AUTONOMY="gated" runs.
    story["status"] = "done"
    story.pop("triage_deferred_action", None)
    return "mark_done"
