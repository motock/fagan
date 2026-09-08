"""Guards against a hardcoded author home directory leaking into the
persona prompt files under agents/ (PP-07)."""
from pathlib import Path

import pytest

_AGENTS_DIR = Path(__file__).resolve().parents[2] / "agents"

_PERSONA_FILES = sorted(_AGENTS_DIR.glob("*.md"))

_NAMED_PERSONAS = [
    "mobile-architect",
    "mobile-engineer",
    "qa-test-engineer",
    "ux-mobile-principal",
]


@pytest.mark.parametrize("path", _PERSONA_FILES, ids=lambda p: p.name)
def test_no_persona_file_contains_a_users_path(path):
    text = path.read_text(encoding="utf-8")
    assert "/Users/" not in text, (
        f"{path.name} contains a hardcoded absolute home directory path"
    )


@pytest.mark.parametrize("path", _PERSONA_FILES, ids=lambda p: p.name)
def test_no_persona_file_asserts_directory_already_exists(path):
    text = path.read_text(encoding="utf-8")
    assert "This directory already exists" not in text, (
        f"{path.name} asserts its memory directory already exists, which is "
        "false on a fresh install"
    )


@pytest.mark.parametrize("persona_name", _NAMED_PERSONAS)
def test_named_persona_uses_home_relative_memory_path(persona_name):
    path = _AGENTS_DIR / f"{persona_name}.md"
    text = path.read_text(encoding="utf-8")
    expected = f"~/.claude/agent-memory/{persona_name}/"
    assert expected in text, (
        f"{path.name} does not reference its home-relative memory path "
        f"{expected!r}"
    )
