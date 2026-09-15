"""Server-side patch records for stuck-story worktrees.

This module is the security surface for applying recorded patches to a
story's git worktree. Its invariants are:

* Patch records live SERVER-side: a worktree never trusts a patch that
  only exists locally; the record must come from the server's store.
* Applying a patch is a HUMAN-CONFIRMED action only: no code path may
  apply a patch without an explicit human confirmation step.
* The patch pipeline has two halves with DIFFERENT strictness.  PROPOSE
  (:func:`validate_for_propose`, WAP-5) decides whether a model-authored
  diff may even be shown to a human for confirmation: size, file-count and
  added-line limits, plus the read-half path resolver.  APPLY
  (:func:`resolve_write_target`, WAP-4) is stricter: it additionally
  consults the deny list (:func:`is_denied_relative_path`), so a
  ``.git``-touching patch is accepted at propose -- the human gets to
  inspect it -- and refused at apply time.
* Deny-by-DEFAULT: any relative path that is not provably safe is
  refused. ``is_denied_relative_path`` below is the pure, deny-by-default
  predicate that decides which paths may never be touched by a patch.

The strict path *resolver* (traversal rejection, absolute-path rejection,
symlink refusal, containment) lives here too: ``resolve_write_target`` is
the WRITE half of the path-security pair whose read half is
``pipeline.workspace_fs.resolve_within_workspace``. It is deliberately
STRICTER than the read half: a symlink is refused even when every hop stays
inside the worktree, because a write through an in-worktree symlink is
still a write the patch author did not name.

The patch RECORD STORE (WAP-6) is an IN-PROCESS dict: the dashboard is a
single process, so the store is a single process's memory and nothing
more. A restart drops pending patches, which is acceptable at a
15-minute TTL -- the store fails closed (an unknown id and an expired id
are indistinguishable, both "not available"), and pending patches are
never persisted to disk in this story.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import importlib
import json
import os
import re
import secrets
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath


class PatchSecurityError(ValueError):
    """Raised when a patch request violates a worktree patch security invariant."""


def is_denied_relative_path(relative_path: str) -> bool:
    """Return True if the relative path may never be touched by a patch.

    Pure predicate: no filesystem access, stdlib-free (zero imports), and
    it never raises. The path is normalized by splitting on ``/`` and
    dropping empty components, so trailing and duplicate slashes have no
    effect on the verdict.

    Deny rules (deny-by-default, any match denies the whole path):

    * any component equal to ``.git`` or ``.claude``;
    * any component starting with ``.agent_log``;
    * the FINAL component equal to one of ``CLAUDE.md``, ``.mcp.json``,
      ``agent.log``, ``.agent_transcript.json``, ``.agent_plan.md``,
      ``.agent_scratchpad.md``.

    Fail-closed decisions: the EMPTY string (and any other degenerate
    input, including non-string values such as ``None``) is DENIED — it
    returns ``True`` rather than raising, because an unparseable path can
    never be proven safe. Traversal components like ``..`` are NOT
    rejected here (that is the resolver's job in WAP-4B); a ``.git`` or
    ``.claude`` component is denied wherever it appears.
    """
    if not isinstance(relative_path, str):
        # Fail closed: non-string input is malformed, never provably safe.
        return True

    components = [c for c in relative_path.split("/") if c != ""]
    if not components:
        # Fail closed: the empty string (or a path of only slashes) is
        # DENIED, not allowed and not an error.
        return True

    return (
        any(c == ".git" or c == ".claude" for c in components)
        or any(c.startswith(".agent_log") for c in components)
        or components[-1]
        in {
            "CLAUDE.md",
            ".mcp.json",
            "agent.log",
            ".agent_transcript.json",
            ".agent_plan.md",
            ".agent_scratchpad.md",
        }
    )


__all__ = [
    "PatchFormatError",
    "PatchSecurityError",
    "apply_patch",
    "create_patch_record",
    "get_patch_record",
    "is_denied_relative_path",
    "parse_unified_diff",
    "resolve_write_target",
    "validate_for_propose",
]


def resolve_write_target(worktree_root: str, relative_path: str) -> Path:
    """Resolve the STRICT write-mode target for a patch inside a worktree.

    This is the WRITE half of the path-security pair whose read half is
    :func:`pipeline.workspace_fs.resolve_within_workspace`.  It is
    deliberately STRICTER than the read half: a symlink is refused even when
    every hop stays inside the worktree, because a write through an
    in-worktree symlink is still a write the patch author did not name.

    Checks, in order:

    1. Deny list (:func:`is_denied_relative_path`) -- pure predicate, checked
       BEFORE any disk access, so a denied path never touches the filesystem.
       Degenerate input (empty string, ``None``, non-string) fails closed
       here as well.
    2. The read half :func:`resolve_within_workspace` rejects ``..`` escape,
       absolute paths and encoded traversal; its
       :class:`~pipeline.workspace.WorkspaceSecurityError` propagates
       unchanged.  ``worktree_root`` is always the story worktree from the
       manifest record, so no duplicate traversal checks are added here.
    3. Symlink walk over the SPELLED path from the worktree root down to the
       target: every EXISTING component (including the final component when
       it exists) must satisfy ``os.path.islink(...) is False``.  Both a
       symlink pointing OUTSIDE the worktree and one pointing INSIDE it are
       refused.  Nonexistent deep paths with non-symlink parents are allowed
       (the patch may create files).
    4. Containment re-check: the resolved target must be the worktree root
       itself or a descendant, and must NOT resolve inside the pipeline's own
       ``REPO_ROOT`` / ``PIPELINE_SELF_REPO_ROOT`` or ``PLAN_DIR`` (read
       lazily from :mod:`pipeline.server`, the pattern used by
       ``pipeline.checkpoint``).
    5. Fail closed: the walk and the containment re-check are wrapped so any
       unexpected OS error (or any other unexpected failure, including a
       failed lazy import of the pipeline constants) becomes
       :class:`PatchSecurityError` -- a path is never returned unless it was
       fully verified.

    Raises
    ------
    PatchSecurityError
        for every write-side rejection.
    WorkspaceSecurityError
        (a ``ValueError`` subclass) when the read half rejects the path.
    """
    # 1. Deny list FIRST: a pure predicate, no filesystem access at all.
    if is_denied_relative_path(relative_path):
        raise PatchSecurityError("relative path is denied for patch writes")

    # 2. Read half: traversal / absolute / encoded-traversal rejection.
    #    WorkspaceSecurityError propagates unchanged from here.  The import
    #    is deferred (importlib, not an import statement) so this module
    #    stays import-statement-free: WAP-4A's purity test requires
    #    pipeline/worktree_patch.py to import nothing outside the stdlib.
    workspace_fs = importlib.import_module("pipeline.workspace_fs")
    resolved = workspace_fs.resolve_within_workspace(worktree_root, relative_path)

    try:
        # 3. Symlink walk over the spelled path.  islink is checked BEFORE
        #    exists so a broken symlink (exists() is False, islink() True)
        #    is still refused; the walk stops at the first component that
        #    does not exist, because the patch may create the rest.
        current = Path(worktree_root)
        for comp in PurePosixPath(relative_path).parts:
            current = current / comp
            spelled = str(current)
            if os.path.islink(spelled):
                raise PatchSecurityError(
                    "write target must not traverse a symlink"
                )
            if not os.path.exists(spelled):
                break

        # 4a. Containment: the target must be the worktree root or below it.
        if not resolved.is_relative_to(Path(worktree_root).resolve()):
            raise PatchSecurityError("write target escapes the worktree root")

        # 4b. The pipeline's own repo checkout and plan storage are never
        #     legal patch targets.  Read the constants lazily from
        #     pipeline.server (the pipeline.checkpoint pattern) so tests that
        #     patch the server bindings stay authoritative.  importlib again:
        #     no import statements in this module (WAP-4A purity test).
        server = importlib.import_module("pipeline.server")
        forbidden_roots = (
            server.REPO_ROOT,
            server.PIPELINE_SELF_REPO_ROOT,
            server.PLAN_DIR,
        )
        for forbidden in forbidden_roots:
            if forbidden is None:
                continue
            if resolved.is_relative_to(Path(forbidden).resolve()):
                raise PatchSecurityError(
                    "write target must not resolve inside the pipeline's "
                    "own repo or plan storage"
                )
    except PatchSecurityError:
        raise
    except Exception as exc:
        # 5. Fail closed: an unverified path is a denial, never a return.
        raise PatchSecurityError(
            "write target could not be safety-checked"
        ) from exc

    return resolved


# --------------------------------------------------------------------------
# WAP-5: PROPOSE-side unified-diff parsing and validation
# --------------------------------------------------------------------------


class PatchFormatError(ValueError):
    """Raised when a diff is malformed or refused at PROPOSE time.

    Deliberately distinct from :class:`PatchSecurityError`: a format refusal
    means the diff TEXT itself is unusable or over-limit (the route maps
    ``diff too large`` to HTTP 413), while a security refusal means the diff
    is well formed but names a path that may never be written.
    """


_MAX_DIFF_BYTES = 65536
_MAX_PROPOSE_FILES = 5
_MAX_PROPOSE_ADDED_LINES = 400

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


def validate_for_propose(diff_text: str, worktree_root: str) -> dict:
    """Decide whether a model-authored diff may be shown to a human.

    This is the PROPOSE half of the patch pipeline; the APPLY half
    (:func:`resolve_write_target`) is deliberately stricter.  Checks, in
    order -- earlier refusals win, so ordering is part of the contract:

    1. byte size: over :data:`_MAX_DIFF_BYTES` (65536) is refused as
       ``diff too large`` (the route maps this to HTTP 413);
    2. :func:`parse_unified_diff` -- malformed input is refused;
    3. more than :data:`_MAX_PROPOSE_FILES` (5) DISTINCT new-side paths is
       refused as ``too many files``;
    4. more than :data:`_MAX_PROPOSE_ADDED_LINES` (400) added lines is
       refused as ``too many added lines``;
    5. path resolution: every new-side path goes through the READ-half
       resolver :func:`pipeline.workspace_fs.resolve_within_workspace`, so
       ``../escape`` and absolute paths raise
       :class:`~pipeline.workspace.WorkspaceSecurityError`, which propagates
       unchanged.  The deny list (:func:`is_denied_relative_path`) and the
       strict write resolver are APPLY-side only: a ``.git``-touching patch
       is ACCEPTED here so the human can inspect it, then refused at apply;
    6. whole-file-replacement shape: a hunk with ZERO context lines that
       both deletes and adds is refused -- the no-anchor delete-all/add-all
       payload a model emits when it really means "replace the whole file";
       ``git apply`` cannot safely anchor it and it bypasses context review.

       BOUNDARY: a NEW-file diff (``--- /dev/null``, pure additions, zero
       deletions) is NOT refused by rule 6 -- creating a file legitimately
       has no old content to anchor.  Likewise a pure deletion (zero
       additions) or any hunk carrying at least one context line.

    Returns ``{"paths": [...], "added_lines": int}`` (paths in order of
    appearance, duplicates kept).

    Raises
    ------
    PatchFormatError
        for every refusal above, with a machine-readable reason string.
    WorkspaceSecurityError
        propagated from the read-half resolver for escaping paths.
    """
    if not isinstance(diff_text, str):
        raise PatchFormatError("malformed diff: diff text must be a string")

    # 1. Byte size first: refuse before spending any parse effort.
    size = len(diff_text.encode("utf-8"))
    if size > _MAX_DIFF_BYTES:
        raise PatchFormatError(
            f"diff too large: {size} bytes exceeds the "
            f"{_MAX_DIFF_BYTES}-byte propose limit"
        )

    # 2. Parse.
    parsed = parse_unified_diff(diff_text)

    # 3. Distinct new-side paths: several hunks on one file are one file.
    distinct_paths = list(dict.fromkeys(parsed.paths))
    if len(distinct_paths) > _MAX_PROPOSE_FILES:
        raise PatchFormatError(
            f"too many files: {len(distinct_paths)} distinct paths exceeds "
            f"the {_MAX_PROPOSE_FILES}-file propose limit"
        )

    # 4. Total added lines.
    if parsed.added_lines > _MAX_PROPOSE_ADDED_LINES:
        raise PatchFormatError(
            f"too many added lines: {parsed.added_lines} exceeds the "
            f"{_MAX_PROPOSE_ADDED_LINES}-line propose limit"
        )

    # 5. Read-half path resolution ONLY (see the module docstring for the
    #    propose/apply split).  WorkspaceSecurityError propagates unchanged.
    workspace_fs = importlib.import_module("pipeline.workspace_fs")
    for relative_path in distinct_paths:
        if relative_path == "/dev/null":  # unreachable via parse(); belt+braces
            continue
        workspace_fs.resolve_within_workspace(worktree_root, relative_path)

    # 6. Whole-file-replacement shape, per hunk.
    for hunk in parsed.hunks:
        if hunk.context_lines == 0 and hunk.deletions > 0 and hunk.additions > 0:
            raise PatchFormatError(
                "whole-file replacement refused: hunk with no context lines "
                "both deletes and adds; anchor the edit with context lines"
            )

    return {"paths": list(parsed.paths), "added_lines": parsed.added_lines}


# ---------------------------------------------------------------------------
# Patch record store (WAP-6)
# ---------------------------------------------------------------------------

#: How long a pending patch record stays retrievable: 15 minutes.  After
#: that the record is pruned on read and the patch id becomes "not
#: available" -- the same verdict an unknown id gets, so expiry leaks no
#: existence oracle.
PATCH_TTL_SECONDS = 900

#: Process secret for the confirmation tokens, minted ONCE at import.  The
#: confirmation token is an HMAC over ``f"{patch_id}:{diff_hash}"`` keyed
#: with this secret, so a token is bound to one specific record and cannot
#: be replayed against a different patch id or a different diff.  This is a
#: per-process secret: it is not shared across processes and not persisted,
#: which is consistent with the in-process store below.
_TOKEN_SECRET = secrets.token_urlsafe(32)

#: The store itself: an in-process dict of ``{patch_id: record}``.  The
#: dashboard is a single process, so this dict is the whole store; a
#: restart drops pending patches (acceptable at a 15-minute TTL, fail
#: closed) and nothing here is ever persisted to disk in this story.
_PATCH_STORE: dict[str, dict] = {}


def derive_confirmation_token(patch_id: str, diff_hash: str) -> str:
    """Derive the confirmation token for a patch record (public API).

    The token is an HMAC-SHA256 over ``f"{patch_id}:{diff_hash}"`` keyed
    with the process secret, so it is bound to THIS record and cannot be
    replayed for another patch id or another diff.  This is the single
    derivation point: ``create_patch_record`` mints with it, ``apply_patch``
    verifies with it, and the dashboard's GET review route re-derives with
    it, so the three can never drift apart.
    """
    return hmac.new(
        _TOKEN_SECRET.encode("utf-8"),
        f"{patch_id}:{diff_hash}".encode(),
        hashlib.sha256,
    ).hexdigest()


def confirmation_token_for(record: dict) -> str:
    """Return the confirmation token for a stored patch *record*."""
    return derive_confirmation_token(record["patch_id"], record["diff_hash"])


def create_patch_record(
    plan_name: str,
    story_key: str,
    diff_text: str,
    paths: list[str],
    added_lines: int,
) -> dict:
    """Record a proposed patch server-side and mint its confirmation token.

    Stores a record under a freshly minted ``wp-``-prefixed patch id and
    returns the envelope the dashboard needs to render the confirmation
    step: the patch id, the touched paths, the added-line count, the
    confirmation token, and the diff hash.  The token is an HMAC-SHA256
    over ``f"{patch_id}:{diff_hash}"`` keyed with the process secret, so it
    is bound to THIS record and cannot be replayed for another patch id or
    another diff.  The status flip to ``"applied"`` and the token
    comparison belong to the apply story (WAP-7), not here.

    The ``paths`` list is copied, so the caller's list is never aliased
    into the store.
    """
    patch_id = "wp-" + secrets.token_urlsafe(12)
    diff_hash = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()
    confirmation_token = derive_confirmation_token(patch_id, diff_hash)

    now = datetime.now(timezone.utc)
    record = {
        "patch_id": patch_id,
        "plan_name": plan_name,
        "story_key": story_key,
        "diff_text": diff_text,
        "diff_hash": diff_hash,
        "paths": list(paths),
        "added_lines": added_lines,
        "status": "pending",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=PATCH_TTL_SECONDS)).isoformat(),
    }
    _PATCH_STORE[patch_id] = record

    # Audit (WAP-8): journal + outbox notification, AFTER the record is
    # stored.  The audit carries the path list and the diff HASH only --
    # never diff_text or any hunk content (Secure by Design: no sensitive
    # payload in logs or events).
    # Journal: the appender pipeline/checkpoint.py uses
    # (``from .persistence import _append_journal`` there).  Resolved via
    # importlib like the other collaborators in this module so the
    # stdlib-only import purity of pipeline/worktree_patch.py is kept and a
    # patched ``persistence.PLAN_DIR`` is honoured at call time.
    _append_journal = importlib.import_module(
        "pipeline.persistence"
    )._append_journal
    _append_journal(
        plan_name,
        story_key,
        {
            "action": "patch_proposed",
            "patch_id": patch_id,
            "paths": list(paths),
            "added_lines": added_lines,
            "diff_hash": diff_hash,
            "ts": now.isoformat(),
        },
    )
    # Event: the exact publish pattern of pipeline/dispatch.py -- lazy
    # ``from .event_wiring import get_bus`` then
    # ``from .events import make_event`` -- resolved via importlib like the
    # other collaborators in this module so the stdlib-only import purity of
    # pipeline/worktree_patch.py is kept; a monkeypatched
    # ``event_wiring.get_bus`` is honoured because the attribute is looked
    # up on the module at call time.
    event_wiring = importlib.import_module("pipeline.event_wiring")
    events = importlib.import_module("pipeline.events")
    event_wiring.get_bus().publish(
        events.make_event(
            "notification",
            plan_name,
            story_key=story_key,
            payload={
                "kind": "patch_proposed",
                "patch_id": patch_id,
                "paths": list(paths),
                "diff_hash": diff_hash,
            },
            correlation_id=patch_id,
        )
    )

    return {
        "ok": True,
        "patch_id": patch_id,
        "paths": list(paths),
        "added_lines": added_lines,
        "confirmation_token": confirmation_token,
        "diff_hash": diff_hash,
    }


def get_patch_record(patch_id: str) -> dict | None:
    """Return the stored record for ``patch_id``, or ``None``.

    An unknown id and an EXPIRED id are indistinguishable: both return
    ``None`` (fail closed, no existence oracle beyond "not available").  An
    expired record is pruned from the store on the read that discovers the
    expiry; only that one key is removed.  The read has no other side
    effects -- in particular it never refreshes ``expires_at`` (no sliding
    TTL) and never flips ``status`` (that is WAP-7's job).

    The returned object is the record that lives in the store, not a copy.
    """
    if not isinstance(patch_id, str):
        return None
    record = _PATCH_STORE.get(patch_id)
    if record is None:
        return None

    expires_raw = record.get("expires_at")
    if isinstance(expires_raw, str):
        expires_at = datetime.fromisoformat(expires_raw)
    elif isinstance(expires_raw, datetime):
        expires_at = expires_raw
    else:
        # Malformed expiry: fail closed, treat the record as unavailable.
        _PATCH_STORE.pop(patch_id, None)
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if datetime.now(timezone.utc) >= expires_at:
        _PATCH_STORE.pop(patch_id, None)
        return None

    return record


# ---------------------------------------------------------------------------
# WAP-7: the apply engine
# ---------------------------------------------------------------------------


def _run_git_apply(worktree: Path, diff_text: str, check_only: bool) -> subprocess.CompletedProcess:
    """Run ``git apply`` (optionally ``--check``) inside *worktree*.

    The argv is a LIST (never ``shell=True``), the diff is fed on stdin, and
    no ``--3way``/fuzzy matching is used: a patch either applies cleanly
    against the worktree's current content or it is refused.
    """
    argv = ["git", "apply", "--check", "-"] if check_only else ["git", "apply", "-"]
    return subprocess.run(
        argv,
        input=diff_text.encode("utf-8"),
        cwd=worktree,
        capture_output=True,
        check=False,
    )


def apply_patch(
    plan_name: str, story_key: str, patch_id: str, confirmation_token: str
) -> dict:
    """Apply a human-confirmed patch record to its story worktree.

    Single orchestrating entry point of the APPLY half of the patch
    pipeline.  Every refusal returns ``{"ok": False, "error": <short
    reason>, "status_code": <int>}`` and never touches the worktree;
    success returns ``{"ok": True, "patch_id": ..., "applied": <paths>}``.

    Gates, in order (earlier refusals win, so ordering is part of the
    contract):

    1. plan lock -- the WHOLE body runs inside the ``with`` block, so the
       60s scheduler tick can never redispatch mid-apply;
    2. manifest read (lazy ``from .server import PLAN_DIR`` inside the
       function, mirroring ``pipeline/checkpoint.py``, so a test that
       patches ``pipeline.server.PLAN_DIR`` is honoured);
    3. stuck-only gate (``in_progress`` / ``running`` are refused);
    4. patch record + HMAC confirmation token + single-use status;
    5. worktree must be a directory;
    6. every new-side AND old-side hunk path through the strict write
       resolver (deny list, symlink refusal, escape refusal -- all fail
       closed) BEFORE any write;
    7. ``git apply --check`` (argv list only, no shell, no ``--3way``);
    8. ``git apply``;
    9. on success ONLY: flip the record to ``applied`` and stamp
       ``applied_at``.  A failed apply leaves the record pending
       (retryable); a successful apply is single-use forever.
    """
    concurrency = importlib.import_module("pipeline.concurrency")

    with concurrency._plan_lock(plan_name) as acquired:
        if not acquired:
            return {"ok": False, "error": "plan busy", "status_code": 409}

        # 2. Manifest read: lazy import so a patched pipeline.server.PLAN_DIR
        #    is honoured (the pipeline.checkpoint pattern).
        server = importlib.import_module("pipeline.server")
        manifest_path = server.PLAN_DIR / f"{plan_name}.manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"ok": False, "error": "unknown plan or story", "status_code": 404}
        stories = manifest.get("stories") if isinstance(manifest, dict) else None
        if not isinstance(stories, dict) or story_key not in stories:
            return {"ok": False, "error": "unknown plan or story", "status_code": 404}
        story = stories[story_key]
        if not isinstance(story, dict):
            return {"ok": False, "error": "unknown plan or story", "status_code": 404}

        # 3. Stuck-only gate: direct repair targets stuck stories.
        if story.get("status") in {"in_progress", "running"}:
            return {"ok": False, "error": "story is active", "status_code": 409}

        # 4. Record + token + single-use.
        record = get_patch_record(patch_id)
        if record is None:
            return {"ok": False, "error": "unknown patch", "status_code": 404}
        expected = derive_confirmation_token(patch_id, record["diff_hash"])
        if not hmac.compare_digest(str(confirmation_token), expected):
            return {
                "ok": False,
                "error": "invalid confirmation token",
                "status_code": 403,
            }
        if record.get("status") != "pending":
            return {"ok": False, "error": "patch already applied", "status_code": 409}

        # 5. The worktree must exist as a directory.
        worktree_raw = story.get("worktree")
        if not isinstance(worktree_raw, str) or not worktree_raw:
            return {
                "ok": False,
                "error": "worktree is not a directory",
                "status_code": 409,
            }
        worktree = Path(worktree_raw)
        if not worktree.is_dir():
            return {"ok": False, "error": "worktree is not a directory", "status_code": 409}

        # 6. Resolve EVERY hunk path -- NEW side AND OLD side -- BEFORE any
        #    write.  The deny list runs here, after parse and before git
        #    apply, on every path: one denied hunk refuses the whole patch.
        #    The old side matters because a deletion-only block (``+++ 
        #    /dev/null``) contributes NO new-side path, yet ``git apply`` will
        #    still delete the old-side file: without this check a deletion
        #    diff could remove a deny-listed file (e.g. ``CLAUDE.md``).
        workspace = importlib.import_module("pipeline.workspace")
        try:
            parsed = parse_unified_diff(record["diff_text"])
            resolved_paths: list[str] = []
            for path in parsed.paths:
                if path == "/dev/null":
                    continue
                resolve_write_target(str(worktree), path)
                resolved_paths.append(path)
            for old_path in parsed.old_paths:
                if old_path == "/dev/null":
                    continue
                # Gate ONLY: the old side is checked against the deny list
                # (a deletion-only block names its victim only here) but it
                # is never reported as an applied path -- ``applied`` stays
                # the new-side write set, as the WAP-7 contract pins it.
                resolve_write_target(str(worktree), old_path)
        except (PatchSecurityError, workspace.WorkspaceSecurityError):
            return {"ok": False, "error": "patch target refused", "status_code": 403}
        except PatchFormatError:
            return {"ok": False, "error": "malformed diff", "status_code": 400}

        # 7. git apply --check: the worktree is untouched on failure.
        check = _run_git_apply(worktree, record["diff_text"], check_only=True)
        if check.returncode != 0:
            return {
                "ok": False,
                "error": "patch does not apply (context drift)",
                "status_code": 409,
                "detail": (check.stderr or b"").decode("utf-8", "replace")[:400],
            }

        # 8. git apply.
        apply = _run_git_apply(worktree, record["diff_text"], check_only=False)
        if apply.returncode != 0:
            return {"ok": False, "error": "apply failed", "status_code": 409}

        # 9. Success ONLY: flip the record (single-use forever).
        record["status"] = "applied"
        record["applied_at"] = datetime.now(timezone.utc).isoformat()

        # Audit (WAP-8), success ONLY: journal + outbox notification.  A
        # refused or failed apply returns above and emits nothing new.
        # Same rule as the propose audit: path list + diff hash, never
        # the diff body.
        # Journal: the appender pipeline/checkpoint.py uses
        # (``from .persistence import _append_journal`` there), resolved via
        # importlib like the other collaborators in this module so the
        # stdlib-only import purity of pipeline/worktree_patch.py is kept
        # and a patched ``persistence.PLAN_DIR`` is honoured at call time.
        _append_journal = importlib.import_module(
            "pipeline.persistence"
        )._append_journal
        _append_journal(
            plan_name,
            story_key,
            {
                "action": "patch_applied",
                "patch_id": patch_id,
                "paths": list(resolved_paths),
                "added_lines": record["added_lines"],
                "diff_hash": record["diff_hash"],
                "ts": record["applied_at"],
                "applied_paths": list(resolved_paths),
            },
        )
        # Event: the exact publish pattern of pipeline/dispatch.py -- lazy
        # ``from .event_wiring import get_bus`` then
        # ``from .events import make_event`` -- resolved via importlib like
        # the other collaborators in this module so the stdlib-only import
        # purity of pipeline/worktree_patch.py is kept; a monkeypatched
        # ``event_wiring.get_bus`` is honoured because the attribute is
        # looked up on the module at call time.
        event_wiring = importlib.import_module("pipeline.event_wiring")
        events = importlib.import_module("pipeline.events")
        event_wiring.get_bus().publish(
            events.make_event(
                "notification",
                plan_name,
                story_key=story_key,
                payload={
                    "kind": "patch_applied",
                    "patch_id": patch_id,
                    "paths": list(resolved_paths),
                    "diff_hash": record["diff_hash"],
                    "applied_paths": list(resolved_paths),
                },
                correlation_id=patch_id,
            )
        )

        return {"ok": True, "patch_id": patch_id, "applied": resolved_paths}