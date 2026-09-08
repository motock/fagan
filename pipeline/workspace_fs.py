"""Workspace-scoped path resolution.

Public API
----------
* :func:`resolve_within_workspace` -- joins an untrusted relative path onto a
  workspace root and returns the resolved absolute :class:`~pathlib.Path`,
  guaranteeing the result is the root itself or a descendant of it.  Raises
  :class:`~pipeline.workspace.WorkspaceSecurityError` for every rejection.

Relationship to :mod:`pipeline.workspace`
-----------------------------------------
:mod:`pipeline.workspace` guards WHICH directory may be selected as a
workspace (absolute path, deny list, no symlinked workspace).  This module
guards WHAT may be addressed INSIDE an already-selected workspace.  They are
the two halves of the same failure class, so this module reuses
``WorkspaceSecurityError`` and the bounded percent-decoding approach rather
than introducing a competing exception or a second decode loop.

Because the workspace root was already validated as non-symlinked at
selection time, this module's symlink policy is narrower than
:mod:`pipeline.workspace`'s: a symlink is rejected the moment ANY
intermediate component it resolves through leaves the workspace, even if
a later link in the chain leads back inside (a leave-and-re-enter chain).
Containment alone (step 3) would miss such a chain -- its final
``.resolve()`` lands back inside the root -- so the independent component
walk (step 4) rejects it.  A symlink whose every hop stays inside the
workspace is allowed.

Security model
--------------
1. Structural rejection before any filesystem access: ``relative_path`` must
   be a non-empty string with no NUL/control characters, no backslash
   separators, no ``..`` segment, and it must not be absolute (in either the
   POSIX or the Windows spelling, checked regardless of host platform).
2. Bounded percent-decoding (:data:`pipeline.workspace._MAX_DECODE_PASSES`
   passes) with the structural checks re-run on every decoded form, so
   ``%2e%2e``, ``%252e%252e`` and ``..%2f`` are caught the same way
   :mod:`pipeline.workspace` catches them.  The RAW spelling is what gets
   joined onto the root -- decoding is a detection aid only, which makes a
   literal ``%2e%2e`` filename a deliberate (conservative) denial.
3. Containment: root and candidate are both resolved (symlinks followed) and
   the candidate must be the root or a descendant.  This alone catches
   symlink escapes.
4. Defense in depth: an INDEPENDENT walk over the existing components checks
   each symlink's ``os.path.realpath`` against the root, using a different
   resolution primitive than step 3.  This catches an escape that step 3
   could miss if a link pointed out of the workspace and a second link led
   back in.
5. Fail closed: every branch is wrapped so that an unexpected exception or
   filesystem error results in a raise, never in a returned path.
6. Error hygiene: messages are fixed, generic strings that never contain a
   resolved path.
"""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import unquote

from pipeline.workspace import _MAX_DECODE_PASSES, WorkspaceSecurityError

__all__ = ["resolve_within_workspace"]


def _reject_unsafe_relative(text: str) -> None:
    """Raise :class:`WorkspaceSecurityError` if *text* is structurally unsafe.

    Run on the raw spelling and again on every percent-decoded form.  Unlike
    :mod:`pipeline.workspace`'s equivalent, a lone ``"."`` segment is allowed
    (it addresses the workspace root, which callers legitimately need).
    """
    for ch in text:
        if ord(ch) < 32 or ord(ch) == 127:
            raise WorkspaceSecurityError(
                "relative path must not contain control characters"
            )
    if "\\" in text:
        raise WorkspaceSecurityError(
            "relative path must not contain backslash separators"
        )
    # Absolute in EITHER spelling, whatever the host platform: PurePosixPath
    # catches "/etc/passwd", PureWindowsPath catches "C:/x" and UNC roots.
    if (
        text.startswith("/")
        or PurePosixPath(text).is_absolute()
        or PureWindowsPath(text).is_absolute()
    ):
        raise WorkspaceSecurityError("relative path must not be absolute")

    for comp in PurePosixPath(text).parts:
        if comp == "..":
            raise WorkspaceSecurityError(
                "relative path must not contain '..' segments"
            )
        # Unicode look-alikes that a downstream decoder could fold into a
        # traversal segment (U+2025 -> "..", fullwidth dots, fullwidth
        # solidus), mirroring pipeline.workspace's defense.
        folded = unicodedata.normalize("NFKC", comp)
        if "/" in folded or "\\" in folded:
            raise WorkspaceSecurityError(
                "relative path must not contain separator look-alikes"
            )
        if folded and set(folded) == {"."} and len(folded) >= 2:
            raise WorkspaceSecurityError(
                "relative path must not contain '..' segments"
            )


def resolve_within_workspace(workspace_root: str, relative_path: str) -> Path:
    """Resolve *relative_path* inside *workspace_root*, or raise.

    Returns the resolved absolute path, which is guaranteed to be
    *workspace_root* itself or a descendant of it.  The target need not
    exist -- callers may use this to resolve a path they are about to create.

    ``relative_path`` of ``"."`` addresses the workspace root itself.  An
    empty string is rejected: an unset parameter must not silently mean "the
    root", so callers asking for the root have to say so explicitly.

    Raises
    ------
    WorkspaceSecurityError
        (a ``ValueError`` subclass) for every rejection, including any
        unexpected error encountered while checking -- this function never
        returns a path it could not fully verify.
    """
    if not isinstance(workspace_root, str) or workspace_root.strip() == "":
        raise WorkspaceSecurityError("workspace root must be a non-empty string")
    if not isinstance(relative_path, str):
        raise WorkspaceSecurityError("relative path must be a non-empty string")
    if relative_path == "":
        raise WorkspaceSecurityError("relative path must not be empty")

    try:
        # 1. Structural checks on the raw spelling, before touching the disk.
        _reject_unsafe_relative(relative_path)

        # 2. Bounded percent-decoding; re-check every decoded form so encoded
        #    traversal is caught even though the raw spelling looks harmless.
        decoded = relative_path
        for _ in range(_MAX_DECODE_PASSES):
            step = unquote(decoded)
            if step == decoded:
                break
            decoded = step
            _reject_unsafe_relative(decoded)

        # 3. Resolve root and candidate, following symlinks. The RAW spelling
        #    is joined: decoding above was detection, not normalization.
        root = Path(workspace_root).resolve()
        candidate = (root / relative_path).resolve()

        # 4. Containment: the candidate must be the root or below it.
        if not candidate.is_relative_to(root):
            raise WorkspaceSecurityError("relative path escapes the workspace root")

        # 5. Defense in depth: an independent walk over the components that
        #    already exist, rejecting any symlink whose realpath leaves the
        #    workspace. A symlink pointing back inside the workspace is fine.
        root_real = Path(os.path.realpath(str(root)))
        current = root
        for comp in Path(relative_path).parts:
            current = current / comp
            spelled = str(current)
            if os.path.islink(spelled):
                target = Path(os.path.realpath(spelled))
                if not target.is_relative_to(root_real):
                    raise WorkspaceSecurityError(
                        "relative path escapes the workspace root"
                    )
            elif not os.path.lexists(spelled):
                break  # nothing beyond this component exists yet

        return candidate
    except WorkspaceSecurityError:
        raise
    except Exception as exc:
        # Fail closed: any unexpected failure while checking is a denial.
        raise WorkspaceSecurityError(
            "relative path could not be safety-checked"
        ) from exc
