"""
Module providing utilities for detecting and reporting collateral edits.

This module implements two pure functions used by the replace_lines tool to
classify removed lines into deletions or rewrites, and to render a human‑readable
report of those changes.  The implementation follows the behaviour of the
original `_removed_lines_echo` helper in `scripts/local_agent.py`, but exposes it
as reusable public API.

The functions are intentionally lightweight: only the standard library is
used (``difflib``, ``collections``).  No file I/O or child processes are
performed, keeping the module safe to import in any context.
"""

import collections
import difflib

# Public API -----------------------------------------------------------------

def classify_removed_lines(
    old_lines: list[str], new_str: str
) -> tuple[list[str], list[tuple[str, str, float]]]:
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

    deletions: list[str] = []
    rewrites: list[tuple[str, str, float]] = []

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


def _render_section(lines: list[str], header: str | None = None) -> str:
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

    rendered: list[str] = []
    total_chars = 0
    count = 0
    for line in lines:
        projected_len = len(line) + (1 if rendered else 0)
        if total_chars + projected_len > _MAX_CHARS_PER_SECTION or count >= _MAX_LINES_PER_SECTION:
            break
        rendered.append(line)
        total_chars += projected_len
        count += 1

    section_text = "\n".join(rendered)
    # Add truncation marker if we didn't include all lines.
    if len(lines) > count or len(section_text) >= _MAX_CHARS_PER_SECTION:
        remaining_space = _MAX_CHARS_PER_SECTION - total_chars
        marker = _TRUNCATION_MARKER[:remaining_space]
        section_text += ("\n" if section_text else "") + marker
    if header:
        return f"{header}\n{section_text}"
    return section_text

# Public API -----------------------------------------------------------------

def render_removal_report(
    deletions: list[str], rewrites: list[tuple[str, str, float]]
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
    rewrite_lines: list[str] = []
    for old, new, ratio in rewrites:
        rewrite_lines.append(f"- {old.rstrip()}\n")
        rewrite_lines.append(f"+ {new.rstrip()}\n")

    rewrite_section = _render_section(rewrite_lines)

    parts: list[str] = []
    if deletion_section:
        parts.append(deletion_section)
    if rewrite_section:
        parts.append(rewrite_section)

    return "\n".join(parts).strip()

# -----------------------------------------------------------------------------
# New function – duplicated block warning
# -----------------------------------------------------------------------------

def duplicated_block_warning(
    new_str: str,
    surrounding_text: str,
    *,
    min_lines: int = 2,
) -> str:
    """Return a warning if ``new_str`` contains a verbatim block that also
    appears in ``surrounding_text``.

    Parameters
    ----------
    new_str:
        The replacement text that will be inserted.  It may contain zero or more
        lines.
    surrounding_text:
        The file content *outside* the replaced range (the caller passes the
        untouched prefix plus suffix).
    min_lines:
        Minimum number of consecutive non‑blank lines required to trigger a
        warning.  Defaults to ``2``.

    Returns
    -------
    str
        An empty string if no duplicated block is found; otherwise a short
        warning that names the number of duplicated lines and quotes the block.
        The quoted block is capped at 15 lines or 1500 characters, whichever
        limit is hit first.  A truncation marker consistent with
        :func:`_render_section` is appended if the block exceeds the cap.
    """

    # Normalise line breaks and split into lists
    new_lines = new_str.splitlines()
    surrounding_lines = surrounding_text.splitlines()

    # Strip leading/trailing blank lines from new_lines only
    def _strip_blank(lines: list[str]) -> list[str]:
        start, end = 0, len(lines)
        while start < end and not lines[start].strip():
            start += 1
        while end > start and not lines[end - 1].strip():
            end -= 1
        return lines[start:end]

    stripped_new = _strip_blank(new_lines)

    # Early exit if too few lines or no surrounding content
    if len(stripped_new) < min_lines or not surrounding_lines:
        return ""

    # Helper to check if a run of lines appears consecutively in another list
    def _run_in(lines: list[str], target: list[str]) -> bool:
        n = len(lines)
        for i in range(len(target) - n + 1):
            if target[i : i + n] == lines:
                return True
        return False

    # Search for the longest run that meets criteria, starting from full length
    best_run: list[str] | None = None
    # Iterate over all possible runs of length >= min_lines, starting from longest
    for size in range(len(stripped_new), min_lines - 1, -1):
        for start in range(len(stripped_new) - size + 1):
            run = stripped_new[start : start + size]
            # Skip runs that are all whitespace
            if all(not line.strip() for line in run):
                continue
            if _run_in(run, surrounding_lines):
                best_run = run
                break
        if best_run is not None:
            break
    if not best_run:
        return ""

    # Build warning string
    num_lines = len(best_run)
    header = f"{num_lines} duplicated line{'s' if num_lines != 1 else ''} detected."
    
    # Apply caps similar to _render_section logic
    lines_to_show: list[str] = []
    total_chars = 0
    for line in best_run:
        projected_len = len(line) + (1 if lines_to_show else 0)
        if total_chars + projected_len > _MAX_CHARS_PER_SECTION or len(lines_to_show) >= _MAX_LINES_PER_SECTION:
            break
        lines_to_show.append(line)
        total_chars += projected_len
    truncated = len(best_run) != len(lines_to_show)

    block_text = "\n".join(lines_to_show)
    if truncated:
        remaining_space = _MAX_CHARS_PER_SECTION - total_chars
        marker = _TRUNCATION_MARKER[:remaining_space]
        block_text += ("\n" if block_text else "") + marker

    return f"{header}\n{block_text}".strip()

def verify_range_anchors(
    lines: list[str],
    start: int,
    end: int,
    expect_first: str | None,
    expect_last: str | None,
) -> str:
    """Verify optional boundary expectations for a line range."""
    # Early exit
    if expect_first is None and expect_last is None:
        return ""

    def _norm(s: str) -> str:
        # Strip trailing newlines and whitespace, preserve leading indentation
        return s.rstrip("\r\n").rstrip()

    def _nearest(line_num: int, target_norm: str) -> list[int]:
        matches = []
        for idx, line in enumerate(lines, start=1):
            if idx == line_num:
                continue
            if _norm(line) == target_norm:
                matches.append((abs(idx - line_num), idx))
        matches.sort()
        return [idx for _, idx in matches[:3]]

    # First anchor
    if expect_first is not None:
        if start < 1 or start > len(lines):
            return f"First anchor at line {start} out of range (file has {len(lines)} lines)."
        actual = lines[start - 1]
        if _norm(actual) != _norm(expect_first):
            suggestions = _nearest(start, _norm(expect_first))
            sugg_str = ""
            if suggestions:
                sugg_str = f" Suggested line(s): {', '.join(map(str, suggestions))}."
            return (f"First anchor mismatch at line {start}. "
                    f"Expected '{expect_first}', found '{actual.rstrip()}'."
                    + sugg_str)

    # Last anchor
    if expect_last is not None:
        if end < 1 or end > len(lines):
            return f"Last anchor at line {end} out of range (file has {len(lines)} lines)."
        actual = lines[end - 1]
        if _norm(actual) != _norm(expect_last):
            suggestions = _nearest(end, _norm(expect_last))
            sugg_str = ""
            if suggestions:
                sugg_str = f" Suggested line(s): {', '.join(map(str, suggestions))}."
            return (f"Last anchor mismatch at line {end}. "
                    f"Expected '{expect_last}', found '{actual.rstrip()}'."
                    + sugg_str)

    return ""
# End of module.
