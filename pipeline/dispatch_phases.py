"""Planner/checklist prompt augmentation carved out of ``pipeline.dispatch``.

Holds the tech-lead planner block of ``_dispatch_story_impl``: the scratchpad
toggle, the one-shot planner call that writes ``.agent_plan.md``, and the
prompt-augmentation chain that injects the checklist (or the scratchpad-only
parity note) into ``spec["prompt"]``.

Every free name it reads that is a module-level binding of ``pipeline.server``
is resolved lazily through ``_ServerRef``, so
``monkeypatch.setattr(pipeline.server, "NAME", ...)`` still lands.
"""

import hashlib
import os
from pathlib import Path
from typing import Any

from .service import _ServerRef

# Server-sourced members this module references as free variables. Each
# resolves to the live ``pipeline.server`` binding at call time so
# ``monkeypatch.setattr(pipeline.server, "NAME", ...)`` still lands. This
# mirrors the ``_ServerRef`` pattern already used by pipeline/dispatch.py.
_LOCAL_BACKEND_NAMES = _ServerRef("_LOCAL_BACKEND_NAMES")
_default_branch = _ServerRef("_default_branch")
_plan_role_config = _ServerRef("_plan_role_config")
_run_planner = _ServerRef("_run_planner")
_test_files_added_on_branch = _ServerRef("_test_files_added_on_branch")
_test_names_in_file = _ServerRef("_test_names_in_file")


def _apply_planner_checklist(
    story: dict[str, Any],
    spec: dict[str, Any],
    *,
    dispatch_backend: str,
    resuming: bool,
    worktree_path: Path,
    test_author_marker: Path,
    plan_name: str,
) -> None:
    """Run the tech-lead planner and fold its checklist into ``spec["prompt"]``.

    Mutates ``spec["prompt"]`` in place, exactly as the inline block in
    ``_dispatch_story_impl`` did. ``resuming`` is read only by the planner
    gate below.
    """
    scratchpad_on = (
        os.environ.get("PIPELINE_DECOMPOSE_SCRATCHPAD", "on").strip().lower()
        != "off"
    )
    plan_path = worktree_path / ".agent_plan.md"
    plan_hash_path = worktree_path / ".agent_plan_src_hash"
    if (
        dispatch_backend in _LOCAL_BACKEND_NAMES
        and not resuming
        and not plan_path.exists()
    ):
        # Ground the planner in the test-author's ACTUAL committed test
        # file(s) when the phase ran this branch (root-caused live
        # 2026-07-25 on MODE40-CI-REWORK-FEEDBACK-V2's THIRD reset: the
        # prohibition-only tests_already_authored clause still let the
        # planner re-derive a "Write the test file" step with invented
        # test-case names, because it had no concrete grounding in
        # which file/tests exist). Detect the test_*.py files added on
        # this branch and their top-level test-case names, and hand
        # them to the planner so it can point the executor at READING
        # the real files. Fail open: if git detects nothing (or the
        # phase ran but committed no test_*.py), authored_test_files is
        # empty and the planner degrades to the prohibition-only clause.
        authored_test_files: list[tuple[str, list[str]]] | None = None
        if test_author_marker.exists():
            try:
                added = _test_files_added_on_branch(
                    worktree_path,
                    _default_branch(),
                )
                authored_test_files = [
                    (path, _test_names_in_file(worktree_path, path))
                    for path in added
                ]
            except Exception:  # noqa: BLE001 (best-effort grounding enrichment; a git hiccup here must degrade to the prohibition-only planner clause, not raise)
                authored_test_files = []
        plan_text = _run_planner(
            story.get("agent_instructions", ""),
            dispatch_backend=dispatch_backend,
            local_model=spec["model"],
            include_scratchpad=scratchpad_on,
            plan_role_config=_plan_role_config(plan_name),
            tests_already_authored=test_author_marker.exists(),
            authored_test_files=authored_test_files,
            worktree=str(worktree_path),
        )
        if plan_text:
            plan_path.write_text(plan_text)
            plan_hash_path.write_text(
                hashlib.sha256(
                    story.get("agent_instructions", "").encode()
                ).hexdigest()
            )
    # Referencing an existing plan is independent of generating one, so
    # a resumed dispatch that rebuilds its prompt from scratch (no
    # transcript to resume) still sees the checklist from the story's
    # first dispatch, without spending a second planner call for it.
    # Referencing an existing plan is independent of generating one, so
    # a resumed dispatch that rebuilds its prompt from scratch (no
    # transcript to resume) still sees the checklist from the story's
    # first dispatch, without spending a second planner call for it.
    # Reuse requires BOTH a local-family backend (the crutch was never
    # meant for Claude -- see the generation guard above) AND a hash of
    # the CURRENT agent_instructions matching what the checklist was
    # generated from -- a patch_story rewrite of agent_instructions
    # (e.g. a corrected rework brief) must silently drop the now-stale
    # checklist rather than inject contradictory instructions.
    current_instructions_hash = hashlib.sha256(
        story.get("agent_instructions", "").encode()
    ).hexdigest()
    checklist_is_fresh = (
        plan_path.exists()
        and dispatch_backend in _LOCAL_BACKEND_NAMES
        and plan_hash_path.exists()
        and plan_hash_path.read_text().strip() == current_instructions_hash
    )
    if checklist_is_fresh:
        scratchpad_instruction = ""
        # Backstop to the planner-woven scratchpad steps above: even with
        # the clause folded into the checklist, keep the explicit trailing
        # reminder so a resumed dispatch (whose stored .agent_plan.md may
        # predate the clause) and any run whose planner under-emitted it
        # still get told to maintain the scratchpad.
        if scratchpad_on:
            scratchpad_instruction = (
                " After finishing each step, keep .agent_scratchpad.md "
                "up to date with a short running summary of what you've "
                "done and which step is next (create_file for the first "
                "note, str_replace to rewrite it after that) before "
                "moving on to the next step. The FIRST line must be "
                "PROGRESS: <done>/<total> showing how many checklist "
                "items you've completed (e.g. PROGRESS: 2/5)."
                " The scratchpad is gitignored by design: never `git add` "
                "it - not even `git add -f` - and never commit it."
            )
        spec["prompt"] = (
            f"{spec['prompt']}\n\n"
            "--- Implementation checklist from your tech lead ---\n"
            f"{plan_path.read_text()}\n\n"
            f"Work through these steps in order.{scratchpad_instruction}"
        )
    elif scratchpad_on and dispatch_backend not in _LOCAL_BACKEND_NAMES:
        # Prompt-only parity for non-local-family backends (Claude): no
        # tech-lead checklist exists here (that phase stays local-only),
        # so there is no numbered-step total to report progress against
        # -- do not reuse the "PROGRESS: <done>/<total>" line from above.
        spec["prompt"] = (
            f"{spec['prompt']}\n\n"
            "As you work, keep .agent_scratchpad.md up to date with a "
            "short running summary of what you've done and what's next "
            "(create_file for the first note, str_replace to rewrite it "
            "after that)."
        )
