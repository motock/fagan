"""Utility for inserting or refreshing a fenced block in a text file's contents.

The module is intentionally minimal: it only provides the constants
``BEGIN_MARKER`` and ``END_MARKER`` and the function
``apply_managed_block(existing: str, block: str) -> str``.

The implementation follows the contract expressed in the tests:

* If the existing text contains no markers, the fenced block is appended
  after a blank line (or immediately if the file is empty).
* If the existing text contains exactly one well‑formed marker pair, the
  text between the markers is replaced.
* The result always ends with exactly one newline.
* The operation is idempotent – applying the same block twice yields
  byte‑identical output.
* All bytes outside the markers are preserved verbatim (including
  CRLF line endings).
* Malformed marker layouts raise ``ValueError``.
* The ``block`` argument must not contain either marker string.

The module contains no I/O and only uses the standard library.
"""

from __future__ import annotations

BEGIN_MARKER = "<!-- fagan:begin (managed block - edits inside are overwritten on update) -->"
END_MARKER = "<!-- fagan:end -->"


def _ends_with_two_newlines(text: str) -> bool:
    """Return ``True`` if *text* ends with two consecutive newline
    sequences, regardless of whether they are ``\n`` or ``\r\n``.
    """
    # Common patterns: \n\n, \r\n\r\n, \r\n\n, \n\r\n
    return text.endswith(("\n\n", "\r\n\r\n", "\r\n\n", "\n\r\n"))


def apply_managed_block(existing: str, block: str) -> str:
    """Insert or refresh a fenced block in *existing*.

    Parameters
    ----------
    existing:
        The original file contents.
    block:
        The new block content that will replace the existing block.

    Returns
    -------
    str
        The updated file contents.

    Raises
    ------
    ValueError
        If the *block* contains a marker, or if the marker layout in
        *existing* is malformed.
    """
    # Fail closed on the block first.
    if BEGIN_MARKER in block or END_MARKER in block:
        raise ValueError("block contains a marker string")

    bc = existing.count(BEGIN_MARKER)
    ec = existing.count(END_MARKER)

    # Helper to construct the fenced block.
    fenced = BEGIN_MARKER + "\n" + block + "\n" + END_MARKER

    if bc == 0 and ec == 0:
        # No markers – append after a blank line.
        if existing == "":
            result = fenced + "\n"
        else:
            if _ends_with_two_newlines(existing):
                sep = ""
            elif existing.endswith("\n"):
                sep = "\n"
            else:
                sep = "\n\n"
            result = existing + sep + fenced + "\n"
    elif bc == 1 and ec == 1:
        b = existing.find(BEGIN_MARKER)
        e = existing.find(END_MARKER)
        if e < b:
            raise ValueError("END marker appears before BEGIN marker")
        result = existing[:b] + fenced + existing[e + len(END_MARKER):]
    else:
        raise ValueError("malformed marker layout")

    # Ensure a single trailing newline.
    if not result.endswith("\n"):
        result += "\n"
    return result

__all__ = ["BEGIN_MARKER", "END_MARKER", "apply_managed_block"]
