"""Persona helpers for the pipeline MCP server.

Reads persona .md files from AGENTS_DIR (a path constant imported from
pipeline_paths), extracts frontmatter, builds the dispatch prompt/spec for
a story, and decides whether a story's persona forces Claude dispatch
(security-engineer). AGENTS_DIR is patched by tests via the agents_dir
fixture, which now patches both p.AGENTS_DIR and pipeline_persona.AGENTS_DIR
(Option B - see PIPELINE_MCP_DECOMPOSITION_PLAN.md §4).
"""

import re
from pathlib import Path
from typing import Any

from .config import _LOCAL_SKIP_PERSONAS, DEFAULT_MODEL
from .paths import AGENTS_DIR

_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)


def _persona_path(persona: str) -> Path:
    # Persona files are named lowercase-hyphenated (e.g. "security-engineer.md").
    # Normalize here so every caller matches regardless of the case the persona
    # string arrives in - callers like _persona_requires_claude already lowercase
    # for their own comparison, and this must agree or a mixed-case persona value
    # (e.g. "Security-Engineer") resolves the routing decision correctly but then
    # crashes loading the prompt body (masked on case-insensitive filesystems).
    return AGENTS_DIR / f"{persona.lower()}.md"


def _persona_body(persona: str) -> str:
    """Return a persona's system prompt (the .md body with frontmatter stripped)."""
    path = _persona_path(persona)
    if not path.exists():
        raise FileNotFoundError(f"No persona named {persona} at {path}")
    return _FRONTMATTER_RE.sub("", path.read_text(), count=1).strip()


def _persona_default_model(persona: str) -> str | None:
    """Return the model declared in a persona's frontmatter, or None if unknown."""
    path = _persona_path(persona)
    if not path.exists():
        return None
    m = re.search(r'^model:\s*"?([\w.-]+)"?\s*$', path.read_text(), re.MULTILINE)
    return m.group(1) if m else None


# Tool allow-lists per persona; reviewers must not modify the tree.
_PERSONA_TOOLS = {
    "code-reviewer": "Bash,Read",
}


def _allowed_tools_for(persona: str | None) -> str:
    return _PERSONA_TOOLS.get(persona or "", "Bash,Edit,Write,Read")


def _build_dispatch_command(
    story: dict[str, Any], story_key: str,
    plan_name: str | None = None,
    resume_journal: list[dict[str, Any]] | None = None,
    review_feedback: str | None = None,
) -> dict[str, Any]:
    """Build the backend-agnostic dispatch spec for a story: its prompt,
    persona system prompt, model tier, and tool allow-list. The chosen
    Backend (see backend.py) turns this into whatever it needs to actually
    run - a `claude` argv, an OpenHands invocation, etc.

    If resume_journal is given (a non-empty checkpoint journal from an
    interrupted run), the prompt is seeded with the steps already completed
    and committed plus the last checkpoint's next_hint, so the agent
    continues instead of redoing finished work.

    If plan_name is given, the prompt instructs the agent to call the
    checkpoint tool after each idempotent step, so a kill (e.g. the usage
    gate interrupting it) leaves it resumable rather than losing the run.

    Raises FileNotFoundError if the story names a persona that does not exist.
    """
    persona = story.get("persona")
    checkpoint_instruction = ""
    if plan_name:
        checkpoint_instruction = (
            f"After completing each meaningful, idempotent step, call the "
            f'checkpoint tool (plan_name="{plan_name}", story_key="{story_key}", '
            f"step=<short id>, summary=<what you did>, next_hint=<what to do "
            f"next>) so your progress is resumable if you are interrupted.\n\n"
        )
    no_wakeup_instruction = (
        "You are running as a one-shot headless session: there is no external "
        "harness that will ever revisit a ScheduleWakeup or resume you after a "
        "background task finishes. Do not call ScheduleWakeup (it is disabled) "
        "and do not launch a long-running command in the background and end "
        "your turn to \"wait for it to notify you\" — that notification will "
        "never arrive, the command gets killed when this process exits, and "
        "your work is lost uncommitted. Run commands synchronously (wait for "
        "them to finish in this same turn) and do not end your turn until your "
        "work is committed.\n\n"
    )
    rework_instruction = ""
    if review_feedback:
        rework_attempts = story.get("rework_attempts", 0)
        if rework_attempts >= 2:
            rework_instruction = (
                f"This is rework round {rework_attempts}. A previous attempt "
                f"already redispatched on this same feedback and did not fully "
                f"resolve it -- read the feedback below carefully rather than "
                f"repeating the same partial fix. "
            )
        rework_instruction += (
            f"The code reviewer REQUESTED CHANGES on the previous attempt. "
            f"Address this feedback before finishing:\n{review_feedback}\n\n"
        )
    if resume_journal:
        completed = "\n".join(
            f"  - [{e['step']}] {e['summary']}" for e in resume_journal
        )
        next_hint = resume_journal[-1].get("next_hint") or "Your WIP is committed — see the completed steps above and git log/git diff, do not re-read the worktree. Run the test suite to see current state, then continue with the next concrete step toward the story goal."
        prompt = (
            f"You are RESUMING issue {story_key}: {story['summary']}\n\n"
            f"{story.get('agent_instructions', '')}\n\n"
            f"This story was previously interrupted. The following steps are "
            f"already completed and committed — do not redo them:\n{completed}\n\n"
            f"Continue from here: {next_hint}\n\n"
            f"{rework_instruction}"
            f"{checkpoint_instruction}"
            f"{no_wakeup_instruction}"
            f"The pipeline owns this branch's remote state: never run git pull, "
            f"git merge, or git rebase against origin — just commit and push. "
            f"If a push is rejected, stop and report it rather than merging.\n\n"
            f"When finished, commit your work, push the branch, and exit.",
        )
    else:
        prompt = (
            f"You are completing issue {story_key}: {story['summary']}\n\n"
            f"{story.get('agent_instructions', '')}\n\n"
            f"{rework_instruction}"
            f"{checkpoint_instruction}"
            f"{no_wakeup_instruction}"
            f"The pipeline owns this branch's remote state: never run git pull, "
            f"git merge, or git rebase against origin — just commit and push. "
            f"If a push is rejected, stop and report it rather than merging.\n\n"
            f"When finished, commit your work, push the branch, and exit.",
        )
    model = (
        story.get("model")
        or (_persona_default_model(persona) if persona else None)
        or DEFAULT_MODEL
    )
    return {
        "prompt": prompt,
        "system": _persona_body(persona) if persona else None,
        "model": model,
        "allowed_tools": _allowed_tools_for(persona),
    }


def _persona_requires_claude(story: dict[str, Any]) -> bool:
    """Whether story["persona"] (case-insensitive) is in _LOCAL_SKIP_PERSONAS.

    Shared by _route_dispatch_backend (auto-mode a-priori routing) and
    dispatch_story's explicit-mode override, so both apply the identical
    normalization to the same safety boundary instead of drifting apart.
    """
    persona = (story.get("persona") or "").lower()
    return persona in _LOCAL_SKIP_PERSONAS


# A repo-wide, unscoped lint/fix sweep (e.g. RUFF-016-ADOPTION: "run `ruff
# check . --fix` ... fix every remaining finding") is structurally unwinnable
# for local dispatch, independent of executor capability: the done-bar
# demands every finding fixed, but on a real ruff-default-ruleset-adoption
# baseline roughly 40% of the files with findings are test files (35/83,
# confirmed live 2026-07-25) - and _NEVER_TOUCH_TESTS_STEERING forbids the
# local executor from editing them. A story scoped to specific path(s)
# doesn't hit this bind, so only the unscoped ". "/repo-wide phrasing matches.
_UNWINNABLE_SCOPE_PATTERNS = (
    re.compile(r"ruff check \.(?:\s|$)", re.IGNORECASE),
    re.compile(r"eslint \.(?:\s|$)", re.IGNORECASE),
    re.compile(r"\b(?:fix|clean\s*up|resolve|address|sweep|eliminate)\b[^.]*\brepo[- ]wide\b|\brepo[- ]wide\b[^.]*\b(?:fix|clean\s*up|resolve|address|sweep|eliminate)\b", re.IGNORECASE),
    re.compile(r"\bacross the (?:entire |whole )?repo(?:sitory)?\b", re.IGNORECASE),
)

# Unbounded-remediation signals that must accompany the bare repo-root lint
# command (pattern 0) for it to indicate an imperative sweep. A plain mention
# of the repo lint gate ("run ruff check . and it must be green") is bounded
# and must not trip the override; only command + sweep signal in the same
# sentence does (2026-08-31 glm misrouting fix).
_SWEEP_REMEDIATION_PATTERN = re.compile(
    r"--fix|fix every|fix each|every remaining finding|all findings"
    r"|clean\s*up|repo-wide",
    re.IGNORECASE,
)

# Sentence splitter for the pattern-0 co-occurrence check in
# _story_has_unwinnable_local_scope. A dot preceded by whitespace belongs to
# the bare repo-root command itself ("ruff check ."), not to a sentence
# boundary, so the lookbehind keeps the command intact within its sentence.
_SENTENCE_SPLIT_PATTERN = re.compile(r"(?<!\s)[.!?]+\s*")


def _story_has_unwinnable_local_scope(story: dict[str, Any]) -> bool:
    """Whether story["agent_instructions"] describes a repo-wide, unscoped
    lint/fix sweep - a scope local dispatch cannot complete without either
    blowing past reasonable step budgets or violating the never-touch-tests
    rule. See _UNWINNABLE_SCOPE_PATTERNS for the concrete signals.

    Shared by _route_dispatch_backend and dispatch_story's explicit-mode
    override, mirroring _persona_requires_claude's dual call sites exactly.
    """
    text = story.get("agent_instructions") or ""
    command_pattern = _UNWINNABLE_SCOPE_PATTERNS[0]
    for sentence in _SENTENCE_SPLIT_PATTERN.split(text):
        if (command_pattern.search(sentence)
                and _SWEEP_REMEDIATION_PATTERN.search(sentence)):
            return True
    return any(
        pattern.search(text) for pattern in _UNWINNABLE_SCOPE_PATTERNS[1:]
    )


__all__ = [
    "_FRONTMATTER_RE",
    "_PERSONA_TOOLS",
    "_allowed_tools_for",
    "_build_dispatch_command",
    "_persona_body",
    "_persona_default_model",
    "_persona_path",
    "_persona_requires_claude",
    "_story_has_unwinnable_local_scope",
]