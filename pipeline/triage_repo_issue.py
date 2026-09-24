"""Triage's repo_issue executor.

A ``repo_issue`` ruling means the story failed for an environmental reason (a
red lint baseline, a red suite at a clean baseline, a born-broken oracle, CI
unavailable), not because of its own scope. The overlord never edits the repo:
the executor files the fix as a normal pipeline story that goes through TDD,
review and CI, and parks the original with a reason naming it.

Every name this code reads from pipeline.triage is bound as a _ModuleRef,
resolved at call time, so monkeypatch.setattr(pipeline.triage, NAME, ...)
keeps landing here.
"""
from .module_ref import _ModuleRef

_coerce_int = _ModuleRef("pipeline.triage", "_coerce_int")
_park = _ModuleRef("pipeline.triage", "_park")
plan_triage_budget_exhausted = _ModuleRef("pipeline.triage", "plan_triage_budget_exhausted")

REPO_ISSUE_HEADER = "=== REPO ISSUE FILED BY TRIAGE ==="
_SUMMARY_RULING_LIMIT = 120


def _execute_repo_issue(plan_name, story_key, story, ruling, manifest, manifest_path) -> str:
    """Execute a ``repo_issue`` ruling by filing one follow-up story.

    Guards, in order:
    1. :func:`plan_triage_budget_exhausted` - nothing is created and the story
       is parked with ``"plan triage budget exhausted"``.
    2. ``<story_key>-repo-issue`` already exists - it is never overwritten or
       duplicated; the story is parked naming it.

    On success the child ``<story_key>-repo-issue`` is added as a ``todo``
    story whose brief is the ruling and rationale (not the parent's brief),
    with ``persona``/``risk``/``backend`` copied from the parent. It carries
    no ``acceptance``, ``files`` or ``dependencies``: the parent's oracle and
    scope grade the parent's deliverable, not the repo fix. The parent does
    not depend on the child either, so a fix that never lands cannot deadlock
    the plan; a human re-dispatches the parent once the fix merges.
    ``manifest['triage_created_stories']`` is incremented by 1.

    The manifest file is NOT written here: :func:`run_triage_sweep` persists
    the mutated manifest after the tick.
    """
    if plan_triage_budget_exhausted(manifest):
        story["triage_deferred_action"] = "repo_issue"
        return _park(plan_name, story_key, story, "plan triage budget exhausted")

    child_key = f"{story_key}-repo-issue"
    if child_key in manifest["stories"]:
        return _park(
            plan_name, story_key, story,
            f"repo issue already filed as {child_key}; re-dispatch {story_key} after it merges",
        )

    ruling_line = (ruling.get("ruling") or "").strip()[:_SUMMARY_RULING_LIMIT]
    child = {
        "key": child_key,
        "summary": f"Repo issue from {story_key}: {ruling_line or 'environmental failure'}",
        "status": "todo",
        "agent_instructions": (
            f"{REPO_ISSUE_HEADER}\n"
            f"Triage ruled that story {story_key} failed for an environmental reason, "
            f"not because of its own scope. Fix the repository condition described "
            f"below. Do not implement {story_key}'s own deliverable.\n"
            f"ruling: {ruling.get('ruling', '')}\n"
            f"rationale: {(ruling.get('rationale') or '')[:300]}\n"
        ),
    }
    for field in ("persona", "risk", "backend"):
        if field in story:
            child[field] = story[field]
    manifest["stories"][child_key] = child
    manifest["triage_created_stories"] = _coerce_int(manifest.get("triage_created_stories", 0)) + 1
    _park(
        plan_name, story_key, story,
        f"repo issue filed as {child_key} by triage; re-dispatch {story_key} after it merges",
    )
    return "repo_issue"
