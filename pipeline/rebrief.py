"""Automate the diagnosis CLAUDE.md Step 9 asks for: "encode the diagnosis
into the next attempt's instructions - state the exact defect and the minimal
fix required, not an open-ended retry." Before this module, that step was
done by hand: an operator read agent.log after a failed/stalled dispatch and
hand-wrote a root-cause block into agent_instructions before re-dispatching.

collect_failure_evidence gathers a bounded summary of why a dispatched attempt
failed, from the artifacts a run leaves behind (the story's own summary, its
last recorded test failure, and the tail of its agent.log - the tail is where
a failure is, the head is orientation).

diagnose_failure turns that evidence into a short root-cause statement via a
configurable "diagnosis" role, and FAILS OPEN (returns None) on every error
path - a diagnosis is an optimization that saves the next attempt some steps,
never a gate. It must never block, crash, or change whether a redispatch
happens.

compose_rebriefed_instructions folds a diagnosis into a story's
agent_instructions, replacing any prior diagnosis block rather than stacking -
a rework loop can call this several times on the same story, and a growing
prompt defeats the point.
"""

import logging
import os
from pathlib import Path
from typing import Any

from app import backend, role_registry

DIAGNOSIS_HEADER = "=== PRIOR-ATTEMPT DIAGNOSIS (read this FIRST) ==="

CLEANUP_HEADER = "=== WORKTREE HYGIENE (read this too) ==="


def collect_failure_evidence(worktree, story: dict[str, Any], limit: int = 6000) -> str:
    """Gather a bounded summary of why a dispatched attempt on `story` failed,
    from `worktree`'s agent.log and the story's own recorded state. Never
    raises - a missing worktree, missing agent.log, or unreadable file all
    yield a valid (possibly minimal) string."""
    sections = [f"STORY: {story.get('summary', '?')}"]

    last_error = (story.get("last_test_check") or {}).get("error")
    if last_error:
        sections.append(f"LAST TEST FAILURE:\n{last_error}")

    try:
        log_path = Path(worktree) / "agent.log"
        if log_path.is_file():
            text = log_path.read_text(errors="replace")
            sections.append(f"AGENT LOG TAIL:\n{text[-4000:]}")
    except OSError:
        pass

    evidence = "\n\n".join(sections)
    if len(evidence) <= limit:
        return evidence

    # Over budget: trim the log tail first, keep the summary/test-error
    # sections (a large log dominates the length; those two are compact and
    # carry the most task-identifying context per character).
    head = "\n\n".join(sections[:-1]) if len(sections) > 1 else ""
    remaining = max(limit - len(head) - 2, 0)
    tail = sections[-1][-remaining:] if remaining else ""
    return (head + "\n\n" + tail).strip()[:limit] if head else tail[:limit]


# Backends that must NOT be silently selected as the diagnosis diagnoser by the
# story-backend default below: "claude" would spend an operator's Claude budget
# on a diagnosis they never opted into, and "auto" is not a concrete driver
# (get_backend rejects it). Anything else (local/ollama/mlx/lmstudio) is a local,
# free model the story already spent — a safe default diagnoser.
_CLAUDE_SPEND_BACKENDS = ("claude", "auto")


def _run_diagnosis_role(
    evidence: str, story: dict[str, Any], plan_role_config: dict | None = None
) -> str | None:
    """Resolve and dispatch the "diagnosis" role, asking for the root cause
    and minimal fix in a few sentences. Returns None when no diagnosis provider
    can be resolved safely. May raise on backend failure - diagnose_failure is
    responsible for catching that.

    Resolution priority:
      1. An explicit "diagnosis" role config (plan_role_config, the
         PIPELINE_BACKEND_DIAGNOSIS env var, or the registry's roles.diagnosis).
      2. DEFAULT (when none of the above is set): reuse the story's OWN local
         backend + the concrete model that ran it (story["backend"] +
         dispatched_model / declared model). The model that ran the struggle is
         the cheapest sensible diagnoser, and step-cap / escalation-to-claude
         only reach this default on local backends, so it never surprises an
         operator with Claude spend. A claude/auto/absent backend or a missing
         model fails open (None) - no diagnosis, plain resume, exactly as before
         this default existed. Operators who want a stronger diagnoser set
         roles.diagnosis or PIPELINE_BACKEND_DIAGNOSIS."""
    registry = role_registry.load_registry()
    provider_override = (
        (plan_role_config or {}).get("diagnosis", {}).get("provider")
        or os.environ.get("PIPELINE_BACKEND_DIAGNOSIS")
        or registry.get("roles", {}).get("diagnosis", {}).get("provider")
    )
    prompt = (
        "A dispatched coding attempt failed or stalled. Given the evidence "
        "below, state the ROOT CAUSE and the MINIMAL fix required, in a few "
        "sentences. Do not restate the evidence; be specific and actionable.\n\n"
        "The next attempt only has targeted line-ranged file reads, search, "
        "and anchored str_replace/replace_lines-style edits available; it "
        "does NOT have `git apply` and cannot reliably rewrite a whole file "
        "at once. The suggested fix MUST be achievable with those tools: "
        "recommend targeted reads/searches and small anchored edits. NEVER "
        "recommend `git apply`, a full-file rewrite, or an in-place re-indent "
        "of a large existing function.\n\n"
        f"{evidence}"
    )
    if provider_override:
        resolution = role_registry.resolve_role(
            "diagnosis",
            plan_role_config=plan_role_config,
            registry=registry,
            model_fallback=lambda: None,
        )
        return backend.get_backend("diagnosis", name=resolution.provider).complete(
            prompt=prompt, system=None, model=resolution.model,
        )

    # No explicit diagnosis role configured: fall back to the story's own local
    # backend + the model that ran it. Fail open for claude/auto/absent or a
    # missing model so this never spends Claude by default and never dispatches
    # a None model to a driver.
    provider = (story.get("backend") or "").strip().lower()
    model = story.get("dispatched_model") or story.get("model")
    if not provider or provider in _CLAUDE_SPEND_BACKENDS or not model:
        return None
    return backend.get_backend("diagnosis", name=provider).complete(
        prompt=prompt, system=None, model=model,
    )


def diagnose_failure(
    evidence: str, story: dict[str, Any], plan_role_config: dict | None = None
) -> str | None:
    """Turn `evidence` into a short root-cause statement via the "diagnosis"
    role. FAILS OPEN (returns None) when the role is unconfigured, raises, or
    returns empty/whitespace-only text - this must never gate a redispatch."""
    if not evidence or not evidence.strip():
        return None
    try:
        diagnosis = _run_diagnosis_role(evidence, story, plan_role_config)
    except Exception as exc:  # noqa: BLE001 (fail-open by design; log type only)
        logging.getLogger("pipeline").warning(
            f"diagnosis role failed with {type(exc).__name__}; "
            "falling back to an undiagnosed redispatch"
        )
        return None
    if diagnosis is None or not diagnosis.strip():
        return None
    return diagnosis.strip()

    def compose_rebriefed_instructions(agent_instructions: str, diagnosis: str | None) -> str:
        """Return `agent_instructions` with exactly one PRIOR-ATTEMPT DIAGNOSIS
        block appended, replacing any existing one rather than stacking. Returns
        `agent_instructions` unchanged when `diagnosis` is None/empty."""
        if not diagnosis or not diagnosis.strip():
            return agent_instructions

        base = agent_instructions
        existing = base.find(DIAGNOSIS_HEADER)
        if existing != -1:
            base = base[:existing].rstrip()

        block = f"{DIAGNOSIS_HEADER}\n{diagnosis.strip()}"
        return f"{base}\n\n{block}" if base else block
def append_cleanup_guidance(agent_instructions: str) -> str:
    """Append or replace a worktree hygiene guidance block.

    The block is prefixed by :data:`CLEANUP_HEADER`. If the header already
    exists in *agent_instructions*, the existing block (including any prior
    content) is removed and replaced with a fresh one.  This mirrors the
    behaviour of :func:`compose_rebriefed_instructions`.

    Parameters
    ----------
    agent_instructions:
        The current instruction string to augment.

    Returns
    -------
    str
        The augmented instruction string.
    """
    if not agent_instructions:
        base = ""
    else:
        base = agent_instructions
    existing = base.find(CLEANUP_HEADER)
    if existing != -1:
        # Remove the old block and any trailing whitespace.
        base = base[:existing].rstrip()
    guidance = (
        f"{CLEANUP_HEADER}\n"
        "This worktree may still contain files from an earlier, interrupted attempt at this story.\n"
        "The step-cap checkpoint commits whatever was in progress, including off-track experiments;\n"
        "before finishing, check `git status` / diff against the default branch for anything not needed for this task, and remove or revert stray files (especially stray test files) left over from the earlier attempt, since they can break review even when your own changes are correct."
    )
    return f"{base}\n\n{guidance}" if base else guidance



__all__ = [
    "DIAGNOSIS_HEADER",
    "collect_failure_evidence",
    "CLEANUP_HEADER",
    "append_cleanup_guidance",
]
