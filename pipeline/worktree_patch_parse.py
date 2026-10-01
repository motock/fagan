"""Unified-diff parsing for the patch PROPOSE half.

Pure parsing helpers split out of :mod:`pipeline.worktree_patch`, which
re-exports every public and private name defined here so existing imports
and attribute lookups on that module keep working.
"""

from __future__ import annotations

import dataclasses
import re


class PatchFormatError(ValueError):
    """Raised when a diff is malformed or refused at PROPOSE time.

    Deliberately distinct from :class:`PatchSecurityError`: a format refusal
    means the diff TEXT itself is unusable or over-limit (the route maps
    ``diff too large`` to HTTP 413), while a security refusal means the diff
    is well formed but names a path that may never be written.
    """


# ``@@ -a,b +c,d @@`` with the counts optional (a bare ``@@ -a +c @@`` means
# 1 line on that side).  Anything after the second ``@@`` (git's function
# context) is ignored.
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

_SIMPLE_C_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    '"': '"',
    "\\": "\\",
}


def _c_unquote(path: str) -> str | None:
    """C-unquote a git-quoted diff path (``"b/CLAUDE.md"`` -> ``b/CLAUDE.md``).

    ``git apply`` C-unquotes ``--- ``/``+++ `` headers before touching the
    filesystem, so the deny check must run on the UNQUOTED spelling: a quoted
    path whose final component is ``CLAUDE.md"`` otherwise slips past the
    final-component deny rules while git writes the unquoted ``CLAUDE.md``.

    Returns the unquoted string, the input unchanged when it was never
    quoted, or ``None`` when the quoting is malformed (caller fails closed).
    """
    if not path.startswith('"'):
        return path  # never quoted: unchanged
    # A leading quote makes this a QUOTED token, so it must be a CLEAN
    # ``"..."``: no trailing garbage after the closing quote and no stray
    # inner quote.  ``git apply`` parses the quoted name and IGNORES
    # trailing garbage (``"b/CLAUDE.md"x`` and ``"b/CLAUDE.md" `` both write
    # the unquoted ``CLAUDE.md``), so passing the raw token through would
    # run the deny check on ``CLAUDE.md"x`` instead of ``CLAUDE.md`` --
    # fail closed instead.
    if len(path) < 2 or not path.endswith('"') or '"' in path[1:-1]:
        return None
    body = path[1:-1]
    out: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= len(body):
            return None  # dangling backslash: malformed quoting
        esc = body[i]
        if esc in _SIMPLE_C_ESCAPES:
            out.append(_SIMPLE_C_ESCAPES[esc])
            i += 1
            continue
        if esc in "01234567":
            digits = esc
            i += 1
            while i < len(body) and len(digits) < 3 and body[i] in "01234567":
                digits += body[i]
                i += 1
            out.append(chr(int(digits, 8) & 0xFF))
            continue
        return None  # unknown escape: unparseable, fail closed
    return "".join(out)


@dataclasses.dataclass(frozen=True)
class ParsedHunk:
    """One ``@@`` hunk: its new-side file and its counted body lines.

    ``path`` is ``None`` for a deletion-only file (``+++ /dev/null``).
    ``old_path`` is the OLD-side path with the leading ``a/`` stripped, or
    ``None`` for a creation-only file (``--- /dev/null``).
    """

    path: str | None
    old_path: str | None
    context_lines: int
    deletions: int
    additions: int


@dataclasses.dataclass(frozen=True)
class ParsedDiff:
    """The propose-relevant summary of a unified diff.

    ``paths`` are the NEW-side paths (``/dev/null`` excluded); ``old_paths``
    are the OLD-side paths (``/dev/null`` excluded, duplicates kept).  The
    APPLY gate checks BOTH sides: a deletion-only block contributes no
    new-side path, so its old-side path is the only one that names the file
    ``git apply`` will delete.
    """

    paths: list[str]
    old_paths: list[str]
    added_lines: int
    hunks: list[ParsedHunk]


@dataclasses.dataclass
class _OpenHunk:
    """Mutable accumulator for the hunk currently being consumed."""

    path: str | None
    old_path: str | None
    old_count: int
    new_count: int
    context: int = 0
    deletions: int = 0
    additions: int = 0

    @property
    def complete(self) -> bool:
        return (
            self.context + self.deletions == self.old_count
            and self.context + self.additions == self.new_count
        )

    def to_parsed(self) -> ParsedHunk:
        return ParsedHunk(
            path=self.path,
            old_path=self.old_path,
            context_lines=self.context,
            deletions=self.deletions,
            additions=self.additions,
        )


def _consume_hunk_line(hunk: _OpenHunk, line: str) -> int:
    """Fold one body line into *hunk*; return 1 if it is an addition else 0.

    ``'\\'`` (git's ``\\ No newline at end of file``) is ignored and counts
    toward neither side; an empty line is an empty context line, matching
    git's tolerance.  Unknown prefixes raise.
    """
    first = line[:1]
    if first == "\\":
        return 0
    if first in ("", " "):
        hunk.context += 1
    elif first == "-":
        hunk.deletions += 1
    elif first == "+":
        hunk.additions += 1
        return 1
    else:
        raise PatchFormatError(
            f"malformed diff: unexpected hunk body line {line[:1]!r}"
        )
    return 0


def parse_unified_diff(diff_text: str) -> ParsedDiff:
    """Parse a unified diff into the propose-side summary structure.

    Recognizes per-file blocks -- a ``--- `` old-side header, a ``+++ ``
    new-side header, then one or more ``@@ -a,b +c,d @@`` hunks -- and
    classifies every body line as context (``' '``), deletion (``'-'``),
    addition (``'+'``) or the ``'\\'`` no-newline marker (ignored, never
    counted).  ``+++ /dev/null`` marks a deletion-only file: it contributes
    NO new-side path.

    Returns a :class:`ParsedDiff` with:

    * ``paths`` -- every new-side path in order of appearance with the
      leading ``b/`` stripped, duplicates kept (``/dev/null`` excluded);
    * ``added_lines`` -- the total number of ``+`` body lines;
    * ``hunks`` -- one :class:`ParsedHunk` per ``@@`` header.

    While a hunk is open its declared counts decide what a line IS, so a
    body line spelled like a header is still hunk content (git's rule).

    Raises
    ------
    PatchFormatError
        for a ``+++ `` header without a preceding ``--- `` header, a hunk
        header outside a ``--- /+++ `` file block, an unparseable ``@@``
        range, a body line outside any hunk, an incomplete file block, or
        body lines whose counts contradict the declared hunk ranges (too
        few when the input ends, too many once the hunk is complete).
    """
    if not isinstance(diff_text, str):
        raise PatchFormatError("malformed diff: diff text must be a string")

    lines = diff_text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # artifact of the trailing newline, not a body line

    paths: list[str] = []
    old_paths: list[str] = []
    hunks: list[ParsedHunk] = []
    added_lines = 0

    pending_minus_header = False
    file_open = False
    hunks_seen_for_file = False
    current_path: str | None = None
    current_old_path: str | None = None
    open_hunk: _OpenHunk | None = None

    for line in lines:
        # Inside an INCOMPLETE hunk every line is body content, whatever it
        # is spelled like: the declared counts, not the prefixes, decide.
        if open_hunk is not None:
            added_lines += _consume_hunk_line(open_hunk, line)
            if open_hunk.complete:
                hunks.append(open_hunk.to_parsed())
                open_hunk = None
            continue

        if line.startswith("--- "):
            if pending_minus_header or (file_open and not hunks_seen_for_file):
                raise PatchFormatError("malformed diff: incomplete file block")
            pending_minus_header = True
            file_open = False
            hunks_seen_for_file = False
            source = line[4:].split("\t", 1)[0]
            unquoted_source = _c_unquote(source)
            if unquoted_source is None:
                raise PatchFormatError("malformed diff: unparseable quoted path")
            source = unquoted_source
            if source == "/dev/null":
                current_old_path = None  # creation-only file: no old-side path
            else:
                current_old_path = source.removeprefix("a/")
                old_paths.append(current_old_path)
            continue

        if line.startswith("+++ "):
            if not pending_minus_header:
                raise PatchFormatError(
                    "malformed diff: '+++' header without a preceding '---' header"
                )
            pending_minus_header = False
            file_open = True
            hunks_seen_for_file = False
            target = line[4:].split("\t", 1)[0]
            unquoted = _c_unquote(target)
            if unquoted is None:
                raise PatchFormatError("malformed diff: unparseable quoted path")
            target = unquoted
            if target == "/dev/null":
                current_path = None  # deletion-only file: no new-side path
            else:
                current_path = target.removeprefix("b/")
                paths.append(current_path)
            continue

        if line.startswith("@@"):
            if not file_open:
                raise PatchFormatError(
                    "malformed diff: hunk header outside a '--- '/'+++ ' file block"
                )
            match = _HUNK_HEADER_RE.match(line)
            if match is None:
                raise PatchFormatError("malformed diff: unparseable hunk header")
            old_count = int(match.group(2)) if match.group(2) is not None else 1
            new_count = int(match.group(4)) if match.group(4) is not None else 1
            open_hunk = _OpenHunk(
                path=current_path,
                old_path=current_old_path,
                old_count=old_count,
                new_count=new_count,
            )
            hunks_seen_for_file = True
            if open_hunk.complete:  # degenerate -0,0 +0,0 hunk
                hunks.append(open_hunk.to_parsed())
                open_hunk = None
            continue

        if line.startswith("\\"):
            # "\ No newline at end of file" trailing a completed hunk.
            continue

        raise PatchFormatError("malformed diff: body line outside any hunk")

    if pending_minus_header or (file_open and not hunks_seen_for_file):
        raise PatchFormatError("malformed diff: incomplete file block")
    if open_hunk is not None:
        raise PatchFormatError(
            "malformed diff: hunk supplies fewer lines than its header declares"
        )

    return ParsedDiff(paths=paths, old_paths=old_paths, added_lines=added_lines, hunks=hunks)
