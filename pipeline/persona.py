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

from .paths import AGENTS_DIR
from .config import DEFAULT_MODEL, _LOCAL_SKIP_PERSONAS


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
    rework_instruction = ""
    if review_feedback:
        rework_instruction = (
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
            f"When finished, commit your work, push the branch, and exit."
        )
    else:
        prompt = (
            f"You are completing issue {story_key}: {story['summary']}\n\n"
            f"{story.get('agent_instructions', '')}\n\n"
            f"{rework_instruction}"
            f"{checkpoint_instruction}"
            f"When finished, commit your work, push the branch, and exit."
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


__all__ = [
    "_FRONTMATTER_RE",
    "_persona_path",
    "_persona_body",
    "_persona_default_model",
    "_PERSONA_TOOLS",
    "_allowed_tools_for",
    "_build_dispatch_command",
    "_persona_requires_claude",
]