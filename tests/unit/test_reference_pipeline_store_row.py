"""REFERENCE.md's ``PIPELINE_STORE`` row names the workspace reader.

The story log and the worktree files are read by the ``WorkspaceReader``
seam, not by the store (ADR 0012 decision 1). The operator-facing row that
describes ``PIPELINE_STORE`` must therefore point at that seam instead of
crediting the store with those reads.

Only membership and absence are asserted: the wording, the row order and the
rest of the document are free to change.
"""

from __future__ import annotations

from pathlib import Path

REFERENCE = Path(__file__).resolve().parents[2] / "REFERENCE.md"

#: The clause the row used to carry: it attributed the story log and the
#: worktree files to the store's own filesystem reads.
OLD_CLAUSE = (
    "the story log, worktree files and the recent/active workspace records "
    "are read from the filesystem"
)


def _pipeline_store_row(text: str) -> str:
    """The REFERENCE.md table row that documents ``PIPELINE_STORE``."""
    rows = [line for line in text.splitlines() if line.startswith("| `PIPELINE_STORE`")]
    assert rows, "REFERENCE.md has no PIPELINE_STORE row"
    return rows[0]


def test_the_pipeline_store_row_names_the_workspace_reader():
    text = REFERENCE.read_text(encoding="utf-8")

    assert "WorkspaceReader" in _pipeline_store_row(text)


def test_the_pipeline_store_row_no_longer_credits_the_store_with_those_reads():
    text = REFERENCE.read_text(encoding="utf-8")

    assert OLD_CLAUSE not in _pipeline_store_row(text)
