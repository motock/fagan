"""Ingest-time check: does a story declare a file with no line-limit headroom?

A story whose ``files`` names a production module already near the file-size
gate cannot add its code there without an out-of-scope split, which parks it
(CL-14: ``pipeline/dispatch.py`` at 999 lines against a 1000-line gate).
Advisory only; never raises.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from pipeline.scope_gate import is_test_path

logger = logging.getLogger("pipeline")

_ADVICE = "declare the sibling module the new code will move to, or split the story"

_DOCS_ADVICE = (
    "the brief's Docs section edits files not declared in `files`: {paths}; "
    "add them or the scope gate will park the story"
)

# "Docs:", "Docs (pin each with an assertion):"
_DOCS_SEGMENT = re.compile(r"\bDocs[^:]{0,40}:")
_DOC_PATH = re.compile(r"docs/[\w./-]+\.md|\b[A-Z][A-Z_]+\.md")
_SEGMENT_CHARS = 800


def _line_count(path: Path) -> int | None:
    try:
        return len(path.read_bytes().splitlines())
    except OSError:
        return None


def _brief_docs_outside_files(story: dict) -> list[str]:
    """Return doc paths the brief's ``Docs`` section edits but ``files`` omits."""
    files = story.get("files")
    if not isinstance(files, list) or not files:
        return []
    instructions = story.get("agent_instructions")
    if not isinstance(instructions, str):
        return []
    declared = {f for f in files if isinstance(f, str)}
    omitted: set[str] = set()
    for match in _DOCS_SEGMENT.finditer(instructions):
        segment = instructions[match.end() : match.end() + _SEGMENT_CHARS]
        for doc in _DOC_PATH.findall(segment):
            if doc not in declared and not is_test_path(doc):
                omitted.add(doc)
    return sorted(omitted)


def scope_headroom_warning(
    story: dict, repo_root: str, limit: int = 1000, margin: int = 50
) -> str | None:
    """Warn naming each declared ``.py`` file with less than *margin* lines of headroom."""
    try:
        warnings: list[str] = []
        files = story.get("files")
        if isinstance(files, list):
            tight = []
            for rel in files:
                if not isinstance(rel, str) or not rel.endswith(".py") or is_test_path(rel):
                    continue
                lines = _line_count(Path(repo_root) / rel)
                if lines is not None and limit - lines < margin:
                    tight.append(f"{rel} ({lines} lines, headroom {limit - lines})")
            if tight:
                warnings.append(
                    f"declared files near the {limit}-line limit: "
                    f"{', '.join(tight)}; {_ADVICE}"
                )
        omitted_docs = _brief_docs_outside_files(story)
        if omitted_docs:
            warnings.append(_DOCS_ADVICE.format(paths=", ".join(omitted_docs)))
        if not warnings:
            return None
        return "; ".join(warnings)
    except Exception:  # advisory check must never fail an ingest
        logger.debug("scope headroom check failed", exc_info=True)
        return None
