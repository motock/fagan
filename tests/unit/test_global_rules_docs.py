"""Documentation guard for the global rules bundle (GR-7).

``scripts/install_global_rules.py`` is opt-in and writes into per-tool
instruction files. These tests locate the two places that document it by their
unique heading text and assert the externally visible names they must mention,
so the docs cannot silently drift from the CLI.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

README_STEP_HEADING = "# 4. (Optional) Install the global rules bundle for your agent CLIs"
REFERENCE_SECTION_HEADING = "## Global rules bundle"

ENV_OVERRIDES = (
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "XDG_CONFIG_HOME",
    "OPENCODE_CONFIG_DIR",
)

_NEXT_STEP_RE = re.compile(r"^# \d+\.\s")


def _readme_step(text: str, heading: str) -> str:
    """Return the Quickstart step whose heading comment is *heading*.

    The step runs from its heading comment to the next numbered step comment
    (or the end of the file).
    """
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == heading:
            start = index
            break
    assert start is not None, f"README step not found: {heading!r}"
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if _NEXT_STEP_RE.match(lines[index]):
            end = index
            break
    return "\n".join(lines[start:end])


def _h2_section(text: str, heading: str) -> str:
    """Return the body of the H2 *heading*, up to the next H2 (or EOF)."""
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == heading:
            start = index + 1
            break
    assert start is not None, f"REFERENCE section not found: {heading!r}"
    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return "\n".join(lines[start:end])


def test_readme_quickstart_documents_global_rules_install() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    step = _readme_step(readme, README_STEP_HEADING)
    assert "--tools" in step


def test_reference_global_rules_bundle_lists_env_overrides() -> None:
    reference = (REPO_ROOT / "REFERENCE.md").read_text(encoding="utf-8")
    section = _h2_section(reference, REFERENCE_SECTION_HEADING)
    for name in ENV_OVERRIDES:
        assert name in section, f"{name} missing from {REFERENCE_SECTION_HEADING!r}"
