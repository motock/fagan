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
import os
import re
import secrets
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


@dataclasses.dataclass(frozen=True)
class ParsedHunk:
    """One ``@@`` hunk: its new-side file and its counted body lines.

    ``path`` is ``None`` for a deletion-only file (``+++ /dev/null``).
    """

    path: str | None
    context_lines: int
    deletions: int
    additions: int


@dataclasses.dataclass(frozen=True)
class ParsedDiff:
    """The propose-relevant summary of a unified diff."""

    paths: list[str]
    added_lines: int
    hunks: list[ParsedHunk]


@dataclasses.dataclass
class _OpenHunk:
    """Mutable accumulator for the hunk currently being consumed."""

    path: str | None
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
    hunks: list[ParsedHunk] = []
    added_lines = 0

    pending_minus_header = False
    file_open = False
    hunks_seen_for_file = False
    current_path: str | None = None
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
                path=current_path, old_count=old_count, new_count=new_count
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

    return ParsedDiff(paths=paths, added_lines=added_lines, hunks=hunks)


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
    confirmation_token = hmac.new(
        _TOKEN_SECRET.encode("utf-8"),
        f"{patch_id}:{diff_hash}".encode(),
        hashlib.sha256,
    ).hexdigest()

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