"""Attempt-level helpers extracted from pipeline.dispatch."""

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from .module_ref import _ModuleRef
from .service import _ServerRef

_module_level_function_names = _ServerRef("_module_level_function_names")
collect_attempt_facts = _ServerRef("collect_attempt_facts")
collect_failure_evidence = _ServerRef("collect_failure_evidence")
compose_attempt_facts = _ServerRef("compose_attempt_facts")
compose_rebriefed_instructions = _ServerRef("compose_rebriefed_instructions")
detect_unsatisfiable_signal = _ServerRef("detect_unsatisfiable_signal")
diagnose_failure = _ServerRef("diagnose_failure")

_notify_user = _ModuleRef("pipeline.dispatch", "_notify_user")


def _find_dead_new_functions(worktree: Path, base_branch: str) -> list[str]:
    """Detect newly-added module-level functions (in .py files changed
    since base_branch, excluding test files) whose name appears NOWHERE
    else in the tracked worktree - i.e. defined but never called or
    referenced, not even from a different file (a new public entry point
    called only from elsewhere in the repo must not false-positive here).

    A cheap, conservative text-based heuristic, not a full call-graph
    analysis: a name occurring anywhere else in the worktree (even a
    comment, even in another file) is treated as "referenced", keeping
    false positives near zero. A name occurring ONLY on its own `def`
    line, repo-wide, is a strong, low-noise signal of dead code.

    Root-caused live 2026-07-25 on MODE40-CI-REWORK-FEEDBACK-V2: a
    correctly-implemented, correctly-unit-tested helper function
    (`_ci_rework_feedback`) was added but never wired into the production
    call path it was meant to replace - invisible to any test that only
    exercises the function in isolation, since the story's own tests
    called it directly rather than through the code path that was
    supposed to route to it. The real LLM reviewer caught it, but that
    spends a whole review cycle on something this cheap, static,
    pre-review check catches for free (see _run_lint_gate for the sibling
    pattern this mirrors).

    Best-effort: any git/IO failure returns [] (fail open - a quality
    signal, not a security boundary, must never block or corrupt a
    story's dispatch).
    """
    try:
        diff = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=AM", base_branch, "HEAD"],
            check=False,
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if diff.returncode != 0:
        return []

    dead: list[str] = []
    for rel_path in diff.stdout.splitlines():
        rel_path = rel_path.strip()
        if not rel_path.endswith(".py"):
            continue
        base_name = Path(rel_path).name
        if base_name.startswith("test_") or base_name.endswith("_test.py"):
            continue
        full_path = worktree / rel_path
        if not full_path.is_file():
            continue
        try:
            source = full_path.read_text()
        except OSError:
            continue

        try:
            old_show = subprocess.run(
                ["git", "show", f"{base_branch}:{rel_path}"],
                check=False,
                cwd=worktree,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        old_names = (
            _module_level_function_names(old_show.stdout)
            if old_show.returncode == 0
            else set()
        )
        new_names = _module_level_function_names(source) - old_names

        for fn_name in sorted(new_names):
            if fn_name.startswith("__") and fn_name.endswith("__"):
                continue  # dunder - never a candidate
            try:
                grep = subprocess.run(
                    ["git", "grep", "--count", "-w", fn_name],
                    check=False,
                    cwd=worktree,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            # `git grep --count` prints "path:N" per matching tracked file
            # (exit 1, empty stdout, if no match anywhere - not an error).
            # The function's own def line contributes exactly 1; a repo-
            # wide total <= 1 means "only its own definition, nowhere else
            # in the tracked worktree" - not even a different file.
            total = sum(
                int(line.rsplit(":", 1)[-1])
                for line in grep.stdout.splitlines()
                if line.strip()
            )
            if total <= 1:
                dead.append(f"{rel_path}:{fn_name}")
    return dead



def _rebrief_step_cap_struggle(
    story: dict[str, Any], worktree: str, plan_role_config: dict | None = None,
    plan_name: str | None = None, story_key: str | None = None
) -> None:
    """CLAUDE.md Step 9: when an implementer hits the step cap, diagnose where
    it struggled and fold the root cause into agent_instructions so the resume
    isn't a blind retry. The resume path (_build_dispatch_command) re-reads
    agent_instructions, so the folded diagnosis reaches the next attempt.

    Fail-open by construction: a None/errored diagnosis leaves agent_instructions
    unchanged (compose_rebriefed_instructions is a no-op on None), so this can
    never block or worsen a retry. compose replaces (not stacks) any prior
    diagnosis block, so repeated step-caps keep the prompt bounded and refresh
    with the latest struggle. Must run while the worktree still exists - the
    evidence is the tail of its agent.log."""
    facts = collect_attempt_facts(worktree, story)
    evidence = collect_failure_evidence(worktree, story, facts=facts)
    previous_instructions = story.get("agent_instructions", "")
    try:
        unsat_reason = detect_unsatisfiable_signal(evidence)
        if unsat_reason is not None:
            _notify_user(
                plan_name,
                f"Story may be unsatisfiable as specified: {unsat_reason}. Story {story_key} may need re-planning rather than another retry.",
                story_key=story_key,
            )
    except Exception:
        logging.getLogger("pipeline").debug(
            "detect_unsatisfiable_signal/_notify_user failed during step-cap rebrief",
            exc_info=True)
    diagnosis = diagnose_failure(evidence, story, plan_role_config)
    story["agent_instructions"] = compose_rebriefed_instructions(
        story.get("agent_instructions", ""), diagnosis)
    # After the diagnosis, never before: composing a diagnosis truncates the
    # brief at DIAGNOSIS_HEADER, which would take a facts block appended ahead
    # of it with no replacement.
    story["agent_instructions"] = compose_attempt_facts(
        story.get("agent_instructions", ""), facts)
    if story["agent_instructions"] != previous_instructions:
        try:
            _notify_user(
                plan_name,
                f"{story_key} brief rewritten after a step-cap struggle; the "
                f"resume carries the folded diagnosis.",
                story_key=story_key,
                event="brief_patched",
                **({"correlation_id": story["correlation_id"]} if story.get("correlation_id") else {}),
            )
        except Exception:
            logging.getLogger("pipeline").debug(
                "brief_patched notify failed during step-cap rebrief", exc_info=True)
    


def _transcript_ends_with_done(transcript_path: Path) -> bool:
    """Whether the prior dispatch's transcript ends with the agent calling its
    completion tool ``done``.

    A rework redispatch that resumes such a transcript replays the agent's own
    "I already finished" turn as its most recent state, which dominates the
    reviewer-feedback turn appended after it: the resumed agent re-emits
    ``done`` without doing any work, burning the rework budget against an
    unchanged HEAD. A transcript ending any other way (a tool result, a
    rejection nudge, a plain assistant turn) is still safe to resume.

    Fails open (returns False) on any read/parse problem, so a corrupt
    transcript degrades to the existing resume behavior rather than breaking
    dispatch.
    """
    try:
        msgs = json.loads(transcript_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 (a bad transcript must never break dispatch)
        return False
    if not isinstance(msgs, list) or not msgs:
        return False
    last = msgs[-1]
    if not isinstance(last, dict):
        return False
    for call in last.get("tool_calls") or []:
        if isinstance(call, dict) and (call.get("function") or {}).get("name") == "done":
            return True
    return False
