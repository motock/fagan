"""Regression tests: no scratchpad path may be tracked, in any mangled form.

Companion to ``tests/unit/test_agent_scratchpad_not_tracked.py``. That file
pins the *exact* name ``.agent_scratchpad.md``; this file pins the whole class
of per-story agent scratch paths, because the exact-name guard was too narrow
and let a variant through.

Regression being pinned here: commit ``3e3b5aa`` added a file literally named
``\\.agent_scratchpad.md`` - a backslash-mangled variant of the scratch path
(the leading ``\\`` was not consumed by whatever created it) that a
``git add -A`` swept into the commit. It is tracked in HEAD, absent from the
base commit, and *not* ignored, because ``.gitignore``'s
``.agent_scratchpad*.md`` rule is anchored to a literal leading dot and so
never matches a name whose first character is a backslash. Its content is raw
agent scratch state ("Goal: ... Next: ...").

That is exactly the per-story scratch state that ``.gitignore``,
``pipeline/paths.py``'s ``_WORKTREE_LOG_EXCLUDES`` and the existing regression
test forbid from being tracked - tracking it re-introduces the spurious
merge-gate rebase conflicts between unrelated concurrent stories.

These tests are RED until the fix lands (``git rm`` the mangled file and
harden the ignore rule so backslash-mangled variants cannot recur):

  1. ``git ls-files`` lists no path matching ``*scratchpad*`` (case
     insensitive).
  2. ``git ls-files`` lists no path containing a literal backslash (the whole
     class of shell-mangled names).
  3. HEAD's tree contains no scratchpad path.
  4. ``git check-ignore`` reports the backslash-mangled variant (and other
     variants) as ignored.
  5. ``.gitignore`` contains a rule that actually matches the mangled
     basename - i.e. a rule that is not anchored to a leading dot.
  6. The guard survives the *next* ``git add -A``: recreating the mangled name
     and running ``git add -A`` must leave the index clean.
"""

from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GITIGNORE = REPO_ROOT / ".gitignore"

# The exact tracked basename from commit 3e3b5aa: a literal backslash followed
# by ".agent_scratchpad.md" (21 characters). Written as an escape so the
# Python source itself is unambiguous.
MANGLED_SCRATCHPAD = "\\" + ".agent_scratchpad.md"

# Variants that must all be ignored: the plain name, the backslash-mangled
# name, a prefixed name, and the plain name nested in a subdirectory.
SCRATCHPAD_VARIANTS = (
    ".agent_scratchpad.md",
    MANGLED_SCRATCHPAD,
    "foo.agent_scratchpad.md",
    "nested/dir/.agent_scratchpad.md",
)

# Glob matching the scratchpad *artifacts* (markdown scratch state). Scoped to
# ``.md`` so the regression-test modules that merely mention "scratchpad" in
# their own filenames are not mistaken for the artifact.
SCRATCHPAD_ARTIFACT_GLOB = "*scratchpad*.md"


def _is_scratchpad_artifact(path: str) -> bool:
    """True if ``path``'s basename is a scratchpad artifact."""
    return fnmatch.fnmatch(Path(path).name.lower(), SCRATCHPAD_ARTIFACT_GLOB)


def _git(*args: str) -> subprocess.CompletedProcess:
    """Run git in the repo root, capturing output without raising."""
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _tracked_paths() -> list[str]:
    """Return the raw (unquoted) paths git has in the index.

    ``-z`` is used so git does not C-quote names containing a backslash.
    """
    result = _git("ls-files", "-z")
    assert result.returncode == 0, f"git ls-files failed: {result.stderr}"
    return [p for p in result.stdout.split("\0") if p]


def _head_paths() -> list[str]:
    """Return the raw (unquoted) paths in HEAD's tree."""
    result = _git("ls-tree", "-r", "--name-only", "-z", "HEAD")
    assert result.returncode == 0, f"git ls-tree failed: {result.stderr}"
    return [p for p in result.stdout.split("\0") if p]


def test_no_tracked_path_matches_scratchpad():
    """``git ls-files`` must not list any scratchpad artifact, mangled or not."""
    offenders = [p for p in _tracked_paths() if _is_scratchpad_artifact(p)]
    assert offenders == [], (
        f"git ls-files lists scratchpad artifact(s) {offenders!r}. Per-story "
        "agent scratch state must never be tracked: .gitignore and "
        "pipeline/paths.py's _WORKTREE_LOG_EXCLUDES both exclude it, and "
        "tracking it re-introduces spurious merge-gate rebase conflicts "
        "between concurrent stories. Fix: git rm -- <path> for each offender."
    )


def test_no_tracked_path_contains_a_backslash():
    """No tracked path may contain a literal backslash.

    A backslash in a tracked name means a shell-mangled path (e.g. the literal
    string ``\\.agent_scratchpad.md``) was swept in by ``git add -A``.
    """
    offenders = [p for p in _tracked_paths() if "\\" in p]
    assert offenders == [], (
        f"git ls-files lists path(s) containing a literal backslash: "
        f"{offenders!r}. These are shell-mangled names (the leading backslash "
        "was never consumed) that git add -A swept into a commit. Fix: "
        "git rm -- <path> and harden .gitignore so the variant is ignored."
    )


def test_head_tree_has_no_scratchpad_paths():
    """HEAD's tree must not carry any scratchpad artifact."""
    offenders = [p for p in _head_paths() if _is_scratchpad_artifact(p)]
    assert offenders == [], (
        f"HEAD's tree contains scratchpad artifact(s) {offenders!r}; they must "
        "not be committed (a staged-but-uncommitted `git rm --cached` is not "
        "enough)."
    )


def test_backslash_mangled_scratchpad_is_ignored():
    """``git check-ignore`` must report the backslash-mangled variant ignored.

    ``.agent_scratchpad*.md`` is anchored to a literal leading dot, so it does
    not match a name whose first character is a backslash; the ignore rule must
    be widened (e.g. ``*agent_scratchpad*.md``).
    """
    result = _git("check-ignore", "--", MANGLED_SCRATCHPAD)
    assert result.returncode == 0, (
        f"{MANGLED_SCRATCHPAD!r} is not reported as ignored by git "
        f"check-ignore (exit {result.returncode}). The existing "
        "'.agent_scratchpad*.md' rule is anchored to a leading dot and misses "
        "the backslash-mangled name, so the next `git add -A` stages it again. "
        "Fix: add an unanchored glob such as '*agent_scratchpad*.md' to "
        ".gitignore."
    )


def test_all_scratchpad_variants_are_ignored():
    """Every scratchpad variant must be ignored, not just the plain name."""
    not_ignored = [
        variant
        for variant in SCRATCHPAD_VARIANTS
        if _git("check-ignore", "--", variant).returncode != 0
    ]
    assert not_ignored == [], (
        f"git check-ignore does not report these scratchpad variants as "
        f"ignored: {not_ignored!r}. The ignore rule must match the basename at "
        "any depth and with any prefix, including a literal backslash (e.g. "
        "'*agent_scratchpad*.md')."
    )


def test_gitignore_rule_matches_the_mangled_basename():
    """A .gitignore rule must actually match the mangled basename.

    Checked with fnmatch against the basename so any correct unanchored glob
    passes, while the dot-anchored ``.agent_scratchpad*.md`` (and the escaped
    ``\\.agent_scratchpad.md``, where a leading backslash is a gitignore
    *escape*) fails.
    """
    rules = [
        line.strip()
        for line in GITIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    matching = [r for r in rules if fnmatch.fnmatch(MANGLED_SCRATCHPAD, r)]
    assert matching, (
        f"No .gitignore rule matches the mangled basename "
        f"{MANGLED_SCRATCHPAD!r}. Rules present: {rules!r}. The guard must not "
        "be anchored to a leading dot; add an unanchored glob such as "
        "'*agent_scratchpad*.md'."
    )


def test_git_add_all_does_not_stage_backslash_mangled_scratchpad():
    """The guard must survive the next ``git add -A``.

    Recreates the mangled name (exactly how the bug got in), runs
    ``git add -A``, and asserts nothing is staged and nothing is tracked. This
    is the follow-up-call trace: the immediate ``git rm`` is not sufficient if
    the ignore rule still misses the variant.
    """
    path = REPO_ROOT / MANGLED_SCRATCHPAD
    try:
        path.write_text("Goal: reproduce the mangled scratchpad bug\n", encoding="utf-8")
        _git("add", "-A", "--", MANGLED_SCRATCHPAD)

        tracked = _tracked_paths()
        status = _git("status", "--porcelain", "--", MANGLED_SCRATCHPAD)

        assert MANGLED_SCRATCHPAD not in tracked, (
            f"after recreating {MANGLED_SCRATCHPAD!r} and running `git add -A`, "
            "git ls-files lists it again - the ignore guard does not cover the "
            "backslash-mangled variant, so the regression recurs on the very "
            "next `git add -A`."
        )
        assert status.stdout.strip() == "", (
            f"after recreating {MANGLED_SCRATCHPAD!r} and running `git add -A`, "
            f"git status --porcelain reports it as staged:\n{status.stdout}"
        )
    finally:
        _git("reset", "-q", "--", MANGLED_SCRATCHPAD)
        if _git("cat-file", "-e", f"HEAD:{MANGLED_SCRATCHPAD}").returncode == 0:
            _git("checkout", "--", MANGLED_SCRATCHPAD)
        else:
            path.unlink(missing_ok=True)
