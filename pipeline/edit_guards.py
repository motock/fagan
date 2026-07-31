"""
Module providing utilities for detecting and reporting collateral edits.

This module implements two pure functions used by the replace_lines tool to
classify removed lines into deletions or rewrites, and to render a human‑readable
report of those changes.  The implementation follows the behaviour of the
original `_removed_lines_echo` helper in `scripts/local_agent.py`, but exposes it
as reusable public API.

The functions are intentionally lightweight: only the standard library is
used (``difflib``, ``collections`` and ``typing``).  No I/O or subprocesses
are performed, keeping the module safe to import in any context.
"""

from __future__ import annotations

import difflib
import collections
from typing import List, Tuple

# Public API -----------------------------------------------------------------

def classify_removed_lines(
    old_lines: List[str], new_str: str
) -> Tuple[List[str], List[Tuple[str, str, float]]]:
    """Classify lines that are being removed by a replace operation.

    Parameters
    ----------
    old_lines:
        The list of lines (with trailing newlines) that will be replaced.
    new_str:
        The replacement text.  It may contain zero or more lines.

    Returns
    -------
    tuple[list[str], list[tuple[str, str, float]]]
        ``deletions`` – lines that are considered true deletions.
        ``rewrites`` – tuples of (old_line, best_candidate, ratio) for lines
        that were rewritten with a high similarity score.

    The algorithm follows the behaviour of the original `_removed_lines_echo`:
    * Whitespace‑only or empty lines are ignored entirely.
    * A line is considered a verbatim survivor if an identical copy exists in
      ``new_str``.  Multiple copies are handled using a multiset (``Counter``)
      so that each occurrence consumes one match.
    * Remaining lines are compared against every candidate line from the new
      string using :class:`difflib.SequenceMatcher`.  The best similarity
      ratio is used to decide whether the line is a rewrite (ratio >= 0.9) or
      a deletion (ratio < 0.9).  If ``new_str`` has no lines, every remaining
      line is treated as a deletion.
    """

    # Prepare candidate lines from new string
    candidates = new_str.splitlines(keepends=True)
    counter = collections.Counter(candidates)

    deletions: List[str] = []
    rewrites: List[Tuple[str, str, float]] = []

    for old_line in list(old_lines):  # copy to avoid accidental mutation
        if not old_line.strip():
            # Skip whitespace‑only or empty lines – they are never deletions.
            continue

        # Verbatim survivor check using multiset semantics
        if counter[old_line] > 0:
            counter[old_line] -= 1
            continue

        # Compute best similarity ratio against all candidate lines
        if candidates:
            best_ratio = max(
                difflib.SequenceMatcher(None, old_line, cand).ratio()
                for cand in candidates
            )
            # Find the candidate that achieved this best ratio (first match)
            best_candidate = next(cand for cand in candidates if difflib.SequenceMatcher(None, old_line, cand).ratio() == best_ratio)
        else:
            best_ratio = 0.0
            best_candidate = ""

        if best_ratio >= 0.9:
            rewrites.append((old_line, best_candidate, best_ratio))
        else:
            deletions.append(old_line)

    return deletions, rewrites


# Helper constants for rendering -------------------------------------------------
_MAX_LINES_PER_SECTION = 15
_MAX_CHARS_PER_SECTION = 1500
_TRUNCATION_MARKER = "... (truncated, more lines omitted)"


def _render_section(lines: List[str], header: str | None = None) -> str:
    """Render a section of the report with caps and truncation.

    Parameters
    ----------
    lines:
        The list of strings to render.  They are expected to already be fully
        formatted (e.g., diff markers for rewrites).
    header:
        Optional header line that will precede the section content.
    """
    if not lines:
        return ""

    rendered = []
    total_chars = 0
    count = 0
    for line in lines:
        # Stop before exceeding char cap; we still need to add marker later.
        projected_len = len(line) + (1 if rendered else 0)
        if total_chars + projected_len > _MAX_CHARS_PER_SECTION or count >= _MAX_LINES_PER_SECTION:
            break
        rendered.append(line)
        total_chars += projected_len
        count += 1

    section_text = "\n".join(rendered)
    # Add truncation marker if we didn't include all lines.
    if len(lines) > count or len(section_text) >= _MAX_CHARS_PER_SECTION:
        # Ensure marker fits within char cap.
        remaining_space = _MAX_CHARS_PER_SECTION - total_chars
        marker = _TRUNCATION_MARKER[:remaining_space]
        section_text += ("\n" if section_text else "") + marker
    if header:
        return f"{header}\n{section_text}"
    return section_text


# Public API -----------------------------------------------------------------

def render_removal_report(
    deletions: List[str], rewrites: List[Tuple[str, str, float]]
) -> str:
    """Render a human‑readable report of deletions and rewrites.

    Parameters
    ----------
    deletions:
        Lines that were classified as true deletions.
    rewrites:
        Tuples of (old_line, new_line, ratio) for lines rewritten with high
        similarity.

    Returns
    -------
    str
        The formatted report.  If both inputs are empty the function returns an
        empty string.

    The output is capped at 15 entries per section and 1500 characters per
    section, mirroring the behaviour of ``_removed_lines_echo``.
    """

    if not deletions and not rewrites:
        return ""

    # Deletions section – verbatim lines
    deletion_section = _render_section(deletions)

    # Rewrites section – use character‑level diff markers
    rewrite_lines: List[str] = []
    for old, new, ratio in rewrites:
        # Simple diff representation: prefix with '-' and '+'.
        rewrite_lines.append(f"- {old.rstrip()}\n")
        rewrite_lines.append(f"+ {new.rstrip()}\n")

    rewrite_section = _render_section(rewrite_lines)

    parts = []
    if deletion_section:
        parts.append(deletion_section)
    if rewrite_section:
        parts.append(rewrite_section)

    return "\n".join(parts).strip()

# End of module.
