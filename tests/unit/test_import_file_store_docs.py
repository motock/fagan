"""The operator command for the one-time file-store import is pinned in the docs.

The literal is asserted, not derived from the tool, so a change to the command
has to change REFERENCE.md and the CHANGELOG in the same commit.
"""

from __future__ import annotations

from pathlib import Path

from pipeline.tools.import_file_store import COMMAND

REPO = Path(__file__).resolve().parents[2]
PINNED = "python -m pipeline.tools.import_file_store --db <absolute path> [--dry-run]"


def test_the_tool_publishes_the_pinned_command():
    assert COMMAND == PINNED


def test_reference_documents_the_pinned_command_on_the_pipeline_store_row():
    rows = [
        line
        for line in (REPO / "REFERENCE.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("| `PIPELINE_STORE`")
    ]
    assert len(rows) == 1
    assert f"`{PINNED}`" in rows[0]


def test_changelog_upgrading_names_the_pinned_command():
    text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    upgrading = text.split("### Upgrading", 1)[1].split("\n### ", 1)[0]

    assert f"`{PINNED}`" in upgrading
