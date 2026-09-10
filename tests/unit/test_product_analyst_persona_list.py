"""Change 2 of the standalone-persona-provisioning story: the repo-tracked
``agents/product-analyst.md`` must carry the CLOSED persona list.

Why this file exists: ``scripts/standalone-setup.sh up`` copies the repo's
bundled ``agents/*.md`` into the instance's own ``AGENTS_DIR`` (Change 1), so
the repo-tracked copy of ``product-analyst.md`` is what a fresh install
actually ships. Its old, open-ended persona wording ("``software-engineer``,
``solution-architect``, ... ``etc.``") is the root cause that let a decompose
call invent the nonexistent ``backend-engineer`` persona, which fails at
dispatch time with ``FileNotFoundError: No persona named ...``. The same fix
was already applied to the operator-global copy; this pins the repo-tracked
one.

House rules: this is a membership probe over a persona system-prompt (an LLM
prompt, not executable code) that later stories may extend -- assertions are
membership/ordering based against fixed anchors, never on the file's total
contents or a hash. Only ``product-analyst.md`` is pinned; the other 11
persona files are only probed for EXISTENCE (loudly), never for contents.
"""

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PERSONA_FILE = _REPO_ROOT / "agents" / "product-analyst.md"
_AGENTS_DIR = _REPO_ROOT / "agents"

# The closed list the persona guidance must name, verbatim. Every entry must
# correspond to a real agents/<name>.md file (asserted below), so the list
# cannot drift away from what dispatch can actually load.
_CLOSED_PERSONAS = (
    "software-engineer",
    "solution-architect",
    "mobile-engineer",
    "mobile-architect",
    "security-engineer",
    "qa-test-engineer",
    "tech-writer",
    "devops-release-engineer",
    "ux-mobile-principal",
    "code-reviewer",
)

# The old, open-ended wording this story removes (the exact block it replaces).
_OLD_BLOCK_SNIPPETS = (
    "- `persona` — which SDLC role should implement this story (`software-engineer`,",
    "  `solution-architect`, `mobile-engineer`, `security-engineer`, etc.).",
)


def _persona_source():
    if not _PERSONA_FILE.exists():
        raise ImportError(
            f"TDD dependency missing: {_PERSONA_FILE} does not exist. "
            "agents/product-analyst.md is the persona definition this suite "
            "pins."
        )
    return _PERSONA_FILE.read_text(encoding="utf-8")


def test_persona_file_exists_and_names_the_persona_field():
    """The persona definition exists and still documents the `persona` field."""
    src = _persona_source()
    assert "`persona`" in src, (
        "product-analyst.md must still document the `persona` story field"
    )


def test_old_open_ended_persona_wording_is_gone():
    """NEGATIVE: the old open-ended block (the '... etc.' list that let a
    decompose call invent 'backend-engineer') must be fully removed."""
    src = _persona_source()
    for snippet in _OLD_BLOCK_SNIPPETS:
        assert snippet not in src, (
            "the old open-ended persona wording must be replaced by the "
            f"closed list; still present: {snippet!r}"
        )
    # The trailing "etc." invite-to-invent specifically must not survive on
    # the persona bullet.
    persona_lines = [
        ln
        for ln in src.splitlines()
        if ln.lstrip().startswith("- `persona`")
    ]
    assert persona_lines, "the `persona` bullet must still exist"
    assert not any("etc." in ln for ln in persona_lines), (
        "the `persona` bullet must not end in an open-ended 'etc.' list"
    )


def test_persona_guidance_names_every_dispatchable_persona():
    """The closed list is present: every dispatchable persona is named."""
    src = _persona_source()
    missing = [name for name in _CLOSED_PERSONAS if f"`{name}`" not in src]
    assert not missing, (
        "the closed persona list must name every dispatchable persona; "
        f"missing: {missing}"
    )


def test_closed_list_matches_the_bundled_agent_files():
    """Every persona the guidance names exists as agents/<name>.md, and every
    dispatchable agent file (except overlord.md, the orchestrator, and
    product-analyst.md itself, the decomposer that writes this guidance) is
    named - the list is closed AND accurate, not just closed."""
    src = _persona_source()
    for name in _CLOSED_PERSONAS:
        assert (_AGENTS_DIR / f"{name}.md").exists(), (
            f"the guidance names {name!r} but agents/{name}.md does not exist"
        )
    not_dispatch_targets = {"overlord", "product-analyst"}
    dispatchable = sorted(
        p.stem
        for p in _AGENTS_DIR.glob("*.md")
        if p.stem not in not_dispatch_targets
    )
    unnamed = [name for name in dispatchable if f"`{name}`" not in src]
    assert not unnamed, (
        "agents/ ships dispatchable personas the guidance fails to name; "
        f"unnamed: {unnamed}"
    )


def test_persona_guidance_forbids_inventing_personas_and_defaults_to_software_engineer():
    """The guidance closes the list explicitly: never invent a persona name
    outside it, no separate backend-/frontend-engineer persona, and
    software-engineer is the default for ordinary backend/API work."""
    src = _persona_source()
    assert "Never invent a persona name outside this list." in src, (
        "the guidance must explicitly forbid inventing persona names"
    )
    assert 'no separate "backend-engineer"' in src, (
        "the guidance must say there is no separate backend-engineer persona"
    )
    assert '"frontend-engineer"' in src, (
        "the guidance must say there is no separate frontend-engineer persona"
    )
    default_line = _line_containing_default(src)
    assert default_line is not None, (
        "the guidance must name software-engineer as the default persona for "
        "ordinary backend/API/service/library work"
    )


def _line_containing_default(src):
    for ln in src.splitlines():
        if "Default" in ln and "`software-engineer`" in ln:
            return ln
    return None


def test_persona_guidance_warns_about_the_live_dispatch_failure():
    """The guidance explains WHY the list is closed: an invented name fails at
    dispatch time (FileNotFoundError inside _build_dispatch_command)."""
    src = _persona_source()
    assert "FileNotFoundError" in src, (
        "the guidance must cite the live failure mode "
        "(FileNotFoundError: No persona named ...)"
    )
    assert "_build_dispatch_command" in src, (
        "the guidance must point at where the failure surfaces "
        "(_build_dispatch_command)"
    )


def test_em_dashes_not_double_hyphens_in_the_persona_bullet():
    """The replaced block keeps the file's prose style: em-dashes (—), never
    '--' stand-ins, on the `persona` bullet."""
    src = _persona_source()
    bullet_start = None
    for i, ln in enumerate(src.splitlines()):
        if ln.lstrip().startswith("- `persona`"):
            bullet_start = i
            break
    assert bullet_start is not None, "the `persona` bullet must still exist"
    block = "\n".join(src.splitlines()[bullet_start : bullet_start + 12])
    assert "—" in block, (
        "the rewritten `persona` bullet must use em-dashes (—) like the rest "
        "of the file's prose, not '--'"
    )
    assert "--" not in block, (
        f"the rewritten `persona` bullet must not use '--' where an em-dash "
        f"belongs: {block!r}"
    )
    assert re.search(r"Choose EXACTLY one", block), (
        "the `persona` bullet must instruct choosing EXACTLY one of the "
        "listed personas"
    )