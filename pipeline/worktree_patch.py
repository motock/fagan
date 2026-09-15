"""Server-side patch records for stuck-story worktrees.

This module is the security surface for applying recorded patches to a
story's git worktree. Its invariants are:

* Patch records live SERVER-side: a worktree never trusts a patch that
  only exists locally; the record must come from the server's store.
* Applying a patch is a HUMAN-CONFIRMED action only: no code path may
  apply a patch without an explicit human confirmation step.
* Deny-by-DEFAULT: any relative path that is not provably safe is
  refused. ``is_denied_relative_path`` below is the pure, deny-by-default
  predicate that decides which paths may never be touched by a patch.

The strict path *resolver* (traversal rejection, absolute-path rejection,
symlink escape) is a separate story (WAP-4B); this module only answers
"does a denied component appear anywhere in this relative path?".
"""


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


__all__ = ["PatchSecurityError", "is_denied_relative_path"]