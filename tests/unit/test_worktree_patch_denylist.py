"""TDD tests for the deny-by-default path predicate in ``pipeline.worktree_patch``.

WAP-4A is the first half of the split of the parked WAP-4 story. It creates the
NEW module ``pipeline/worktree_patch.py``, which will hold the *server-side*
patch records for stuck-story worktrees. Applying a patch to a worktree is a
human-confirmed action only, and the path predicate below is the deny-by-default
gate that decides which relative paths may never be touched by a patch.

This file grades ONLY WAP-4A:

* ``PatchSecurityError`` (a ``ValueError`` subclass) exists and is exported;
* ``is_denied_relative_path`` exists, is exported, is PURE (no filesystem
  access, stdlib-only imports) and fails CLOSED on malformed input;
* the deny spellings / boundary negatives / slash-normalisation behaviour.

The strict path *resolver* (traversal rejection, absolute-path rejection,
symlink escape) is WAP-4B and is deliberately NOT graded here: this predicate
only answers "does a denied component appear anywhere in this relative path?".

Written TDD-first: every test below fails at import time
(``ModuleNotFoundError`` / ``AttributeError`` on ``pipeline.worktree_patch``)
until the implementation lands. Assertions on ``__all__`` and on the module
docstring are MEMBERSHIP-only - never exact-match - because later sibling
stories (WAP-4B and beyond) legitimately extend both.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

from pipeline import worktree_patch as wp

_MODULE_PATH = Path(__file__).resolve().parents[2] / "pipeline" / "worktree_patch.py"

# Every spelling the brief requires to be denied. The last four are derived
# directly from the stated rules (component equality, prefix match, final
# component equality) and guard against a substring/endswith implementation.
_DENY_SPELLINGS = [
    ".git/config",
    ".git/hooks/pre-commit",
    "src/.git/x",
    ".claude/settings.json",
    "CLAUDE.md",
    "src/CLAUDE.md",
    ".mcp.json",
    "agent.log",
    ".agent_log.20260914",
    ".agent_transcript.json",
    ".agent_plan.md",
    ".agent_scratchpad.md",
    # derived from the stated rules
    "src/.git",
    "x/.claude",
    ".agent_logs/2026-09-14.log",
    "nested/dir/agent.log",
]

# Boundary NEGATIVES: near-misses that must NOT be denied. A predicate that
# denies these is over-broad and would block legitimate patch targets.
_ALLOW_SPELLINGS = [
    "git/config",
    ".gitignore",
    ".github/workflows/ci.yml",
    "docs/CLAUDE",
    "src/app/model.py",
    # exact-match boundaries: the FINAL component must equal a deny spelling
    "logs/agent.log.bak",
    "docs/.claude.md",
    "CLAUDE.md.bak",
]


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------
def test_module_exists_at_expected_path():
    assert _MODULE_PATH.is_file(), f"missing new module: {_MODULE_PATH}"


def test_module_docstring_states_the_security_invariants():
    doc = (wp.__doc__ or "").lower()
    assert doc.strip(), "pipeline/worktree_patch.py needs a module docstring"
    # server-side patch records for stuck-story worktrees
    assert "server" in doc
    assert "worktree" in doc
    # human-confirmed apply only
    assert "human" in doc
    # deny-by-default
    assert "deny" in doc
    assert "default" in doc


def test_patch_security_error_is_a_value_error_subclass():
    assert issubclass(wp.PatchSecurityError, ValueError)
    with pytest.raises(ValueError):
        raise wp.PatchSecurityError("denied")


def test_patch_security_error_has_a_one_line_docstring():
    doc = wp.PatchSecurityError.__doc__
    assert doc and doc.strip(), "PatchSecurityError needs a docstring"
    assert len(doc.strip().splitlines()) == 1, "docstring must be one line"


def test_all_exports_both_public_symbols():
    exported = set(wp.__all__)
    assert "PatchSecurityError" in exported
    assert "is_denied_relative_path" in exported


def test_is_denied_relative_path_is_callable_and_documented():
    assert callable(wp.is_denied_relative_path)
    doc = (wp.is_denied_relative_path.__doc__ or "").lower()
    assert doc.strip(), "is_denied_relative_path needs a docstring"
    # the docstring must state the fail-closed decision for the empty string
    assert "empty" in doc or "''" in doc or '""' in doc


def test_module_imports_only_stdlib():
    """Purity: no pipeline/app imports, nothing outside the stdlib."""
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                imported.add("." * node.level)
            elif node.module:
                imported.add(node.module.split(".")[0])
    assert not (imported & {"pipeline", "app"}), f"non-stdlib import: {imported}"
    non_stdlib = imported - set(sys.stdlib_module_names)
    assert not non_stdlib, f"non-stdlib import(s): {sorted(non_stdlib)}"


# ---------------------------------------------------------------------------
# Deny spellings
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("relative_path", _DENY_SPELLINGS)
def test_denied_spellings_return_true(relative_path):
    assert wp.is_denied_relative_path(relative_path) is True


@pytest.mark.parametrize("relative_path", _ALLOW_SPELLINGS)
def test_boundary_near_misses_return_false(relative_path):
    assert wp.is_denied_relative_path(relative_path) is False


def test_verdict_is_a_real_bool():
    assert isinstance(wp.is_denied_relative_path(".git/config"), bool)
    assert isinstance(wp.is_denied_relative_path("src/app/model.py"), bool)


# ---------------------------------------------------------------------------
# Slash normalisation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("messy", "clean"),
    [
        ("src/.git/", "src/.git"),
        (".git//config", ".git/config"),
        ("src//.git//x/", "src/.git/x"),
        (".claude//settings.json/", ".claude/settings.json"),
    ],
)
def test_trailing_and_duplicate_slashes_match_clean_spelling(messy, clean):
    assert wp.is_denied_relative_path(messy) is wp.is_denied_relative_path(clean)
    assert wp.is_denied_relative_path(messy) is True


def test_traversal_prefix_still_denies_on_component_match():
    """Traversal REJECTION is WAP-4B's job; this predicate only sees '.git'."""
    assert wp.is_denied_relative_path("../.git/config") is True


# ---------------------------------------------------------------------------
# Fail-closed on malformed input
# ---------------------------------------------------------------------------
def test_empty_string_fails_closed_without_raising():
    assert wp.is_denied_relative_path("") is True


@pytest.mark.parametrize("malformed", [None, 0, b".git/config", ["src/app.py"]])
def test_non_string_input_fails_closed_without_raising(malformed):
    assert wp.is_denied_relative_path(malformed) is True


# ---------------------------------------------------------------------------
# Purity: no filesystem access
# ---------------------------------------------------------------------------
def test_no_filesystem_access(monkeypatch):
    def _boom(*args, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("is_denied_relative_path touched the filesystem")

    monkeypatch.setattr(os.path, "exists", _boom)

    for relative_path in _DENY_SPELLINGS:
        assert wp.is_denied_relative_path(relative_path) is True
    for relative_path in _ALLOW_SPELLINGS:
        assert wp.is_denied_relative_path(relative_path) is False
    assert wp.is_denied_relative_path("") is True
