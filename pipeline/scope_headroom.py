"""Ingest-time check: does a story declare a file with no line-limit headroom?

A story whose ``files`` names a production module already near the file-size
gate cannot add its code there without an out-of-scope split, which parks it
(CL-14: ``pipeline/dispatch.py`` at 999 lines against a 1000-line gate).
Advisory only; never raises.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pipeline.scope_gate import is_test_path

logger = logging.getLogger("pipeline")

_ADVICE = "declare the sibling module the new code will move to, or split the story"


def _line_count(path: Path) -> int | None:
    try:
        return len(path.read_bytes().splitlines())
    except OSError:
        return None


def scope_headroom_warning(
    story: dict, repo_root: str, limit: int = 1000, margin: int = 50
) -> str | None:
    """Warn naming each declared ``.py`` file with less than *margin* lines of headroom."""
    try:
        files = story.get("files")
        if not isinstance(files, list):
            return None
        tight = []
        for rel in files:
            if not isinstance(rel, str) or not rel.endswith(".py") or is_test_path(rel):
                continue
            lines = _line_count(Path(repo_root) / rel)
            if lines is not None and limit - lines < margin:
                tight.append(f"{rel} ({lines} lines, headroom {limit - lines})")
        if not tight:
            return None
        return f"declared files near the {limit}-line limit: {', '.join(tight)}; {_ADVICE}"
    except Exception:  # noqa: BLE001 - advisory check must never fail an ingest
        logger.debug("scope headroom check failed", exc_info=True)
        return None
