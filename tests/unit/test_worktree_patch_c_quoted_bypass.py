"""Regression tests for the WAP-7 review finding: C-quoted path bypass.

The reviewer re-raised this as BLOCKING (prior finding 2, only partially
addressed).  ``pipeline/worktree_patch.py::_c_unquote`` treats a path as
quoted only when it BOTH starts and ends with ``"``.  A path that starts
with ``"`` but does not end with ``"`` (or has trailing content after the
closing quote) falls through and is returned UNCHANGED, even though the
docstring promises ``None`` (fail closed) for malformed quoting.

Real ``git apply`` (verified against git 2.55.0) parses the quoted name and
ignores the trailing garbage, so it writes/deletes the UNQUOTED target:

* ``+++ "b/CLAUDE.md"x``            -> writes ``CLAUDE.md``
* ``--- "a/CLAUDE.md"x`` + ``+++ /dev/null`` -> DELETES ``CLAUDE.md``
* ``+++ "b/CLAUDE.md" `` (trailing space)    -> writes ``CLAUDE.md``

Our parser's final component is ``CLAUDE.md"x`` / ``CLAUDE.md" `` -- not in
the deny set -- and the read-half resolver does not reject ``"``, so
``resolve_write_target`` returns a path, gate 6 passes and the write/delete
proceeds.  The bypass is reachable end-to-end through ``apply_patch``.

These tests are RED against the buggy implementation (the patch is accepted
and the file is written/deleted) and must stay green after the fix.  They are
self-contained (own fixtures) so they do not depend on the fixtures defined
in ``test_worktree_patch_apply.py`` / ``test_worktree_patch_deny_delete.py``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from pipeline.worktree_patch import apply_patch, create_patch_record

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

PLAN_NAME = "wap7cquoteplan"
STORY_KEY = "WAP-7"

#: Captured at import time so a spy installed by a test never intercepts the
#: test's own git plumbing.
_REAL_RUN = subprocess.run

#: The deny-listed victim of every malformed-quoting variant below.
DENIED_TARGET = "CLAUDE.md"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a real git command in *repo* (never through a spy)."""
    return _REAL_RUN(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


def _write_manifest(plan_dir: Path, stories: dict, plan_name: str = PLAN_NAME) -> Path:
    path = plan_dir / f"{plan_name}.manifest.json"
    path.write_text(json.dumps({"stories": stories}))
    return path


def _record(diff_text: str, paths: list[str], added_lines: int = 1) -> dict:
    return create_patch_record(PLAN_NAME, STORY_KEY, diff_text, paths, added_lines)


def _write_diff(target: str, old_line: str) -> str:
    """``--- "a/<target>"x`` / ``+++ "b/<target>"x`` -- trailing garbage.

    git apply parses the quoted name, ignores the trailing ``x`` and writes
    the unquoted ``<target>``.
    """
    return (
        f'--- "a/{target}"x\n'
        f'+++ "b/{target}"x\n'
        "@@ -1,1 +1,1 @@\n"
        f"-{old_line}\n"
        "+new\n"
    )


def _delete_diff(target: str, old_line: str) -> str:
    """``--- "a/<target>"x`` / ``+++ /dev/null`` -- deletion-only variant.

    git apply parses the quoted old-side name, ignores the trailing ``x`` and
    DELETES the unquoted ``<target>``.
    """
    return (
        f'--- "a/{target}"x\n'
        "+++ /dev/null\n"
        "@@ -1,1 +0,0 @@\n"
        f"-{old_line}\n"
    )


def _trailing_space_diff(target: str, old_line: str) -> str:
    """``+++ "b/<target>" `` -- a trailing space after the closing quote.

    git apply still parses the quoted name and writes the unquoted target.
    """
    return (
        f'--- "a/{target}" \n'
        f'+++ "b/{target}" \n'
        "@@ -1,1 +1,1 @@\n"
        f"-{old_line}\n"
        "+new\n"
    )


def _assert_refused(result) -> None:
    """The malformed-quoting patch must be REFUSED and nothing written.

    The reviewer's spec names HTTP 403; the parse-level fail-closed path maps
    ``PatchFormatError`` to 400 ("malformed diff").  Both are refusals that
    leave the worktree untouched, so both are accepted here -- what must NOT
    happen is ``ok=True`` (the patch applied) or a 409 context-drift (which
    would mean the file was already clobbered).
    """
    assert isinstance(result, dict), f"refusal must be a dict, got {result!r}"
    assert result["ok"] is False, (
        "malformed C-quoted path was ACCEPTED: git apply parses the quoted "
        "name and ignores the trailing garbage, so the deny check must fail "
        f"closed on the raw token; got {result!r}"
    )
    assert result["status_code"] in (400, 403), result


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


def _patch_store() -> dict:
    """The module-level patch-record store dict."""
    from pipeline import worktree_patch

    store = getattr(worktree_patch, "_PATCH_STORE", None)
    if isinstance(store, dict):
        return store
    for name, value in vars(worktree_patch).items():
        if name.startswith("__") or not isinstance(value, dict):
            continue
        if any(isinstance(v, dict) and "patch_id" in v for v in value.values()):
            return value
    raise AssertionError("pipeline.worktree_patch exposes no patch-record store")


@pytest.fixture(autouse=True)
def _isolate_patch_store():
    """Snapshot/restore the in-process patch store around every test."""
    store = _patch_store()
    snapshot = dict(store)
    yield
    store.clear()
    store.update(snapshot)


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Redirect every PLAN_DIR binding the apply path reads."""
    import pipeline.server as p
    from pipeline import concurrency as pcon
    from pipeline import persistence as ppers

    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real, committed git repository holding the deny-listed file."""
    root = (tmp_path / "worktree").resolve()
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test Author")
    (root / "app.py").write_text("alpha\nbeta\ngamma\n")
    (root / DENIED_TARGET).write_bytes(b"deny me\n")
    (root / "notes.txt").write_bytes(b"a\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


@pytest.fixture
def manifest(plan_dir: Path, repo: Path) -> dict:
    """A manifest whose one story is STUCK (``failed``) with a real worktree."""
    stories = {STORY_KEY: {"status": "failed", "worktree": str(repo)}}
    _write_manifest(plan_dir, stories)
    return stories


# --------------------------------------------------------------------------
# bug: a path that starts with '"' but is not a clean "..." token
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "builder",
    [_write_diff, _delete_diff, _trailing_space_diff],
    ids=["write", "delete_only", "trailing_space"],
)
def test_malformed_c_quoted_path_is_refused_and_target_byte_identical(
    plan_dir, repo, manifest, builder
):
    """``"b/CLAUDE.md"x`` (and friends) must be refused, file byte-identical.

    Buggy: ``_c_unquote`` returns the raw token unchanged, so the final
    component is ``CLAUDE.md"x`` / ``CLAUDE.md" `` -- not in the deny set --
    gate 6 passes and git apply writes/deletes ``CLAUDE.md``.
    """
    denied = repo / DENIED_TARGET
    before = denied.read_bytes()
    old_line = before.decode("utf-8").rstrip("\n")

    diff = builder(DENIED_TARGET, old_line)
    rec = _record(diff, [], added_lines=1)

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    _assert_refused(result)
    assert denied.exists(), f"{DENIED_TARGET} was deleted by a refused patch"
    assert denied.read_bytes() == before, (
        f"{DENIED_TARGET} was modified by a refused patch"
    )


def test_malformed_first_path_refuses_the_entire_patch(plan_dir, repo, manifest):
    """A malformed path in hunk 1 must refuse the WHOLE apply, not just hunk 1.

    Buggy: gate 6 passes both paths and git apply writes ``CLAUDE.md`` AND
    ``notes.txt``.  After the fix the malformed first path aborts the parse,
    so ``notes.txt`` must stay byte-identical too -- skipping only the bad
    path and continuing would modify ``notes.txt``.
    """
    denied = repo / DENIED_TARGET
    notes = repo / "notes.txt"
    denied_before = denied.read_bytes()
    notes_before = notes.read_bytes()

    diff = (
        f'--- "a/{DENIED_TARGET}"x\n'
        f'+++ "b/{DENIED_TARGET}"x\n'
        "@@ -1,1 +1,1 @@\n"
        "-deny me\n"
        "+new\n"
        "--- a/notes.txt\n"
        "+++ b/notes.txt\n"
        "@@ -1,1 +1,1 @@\n"
        "-a\n"
        "+b\n"
    )
    rec = _record(diff, ["notes.txt"], added_lines=2)

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    _assert_refused(result)
    assert denied.read_bytes() == denied_before, (
        f"{DENIED_TARGET} was modified by a refused patch"
    )
    assert notes.read_bytes() == notes_before, (
        "the malformed path must refuse the ENTIRE patch; notes.txt was "
        "modified, so the implementation skipped only the bad path"
    )


def test_c_unquote_fails_closed_on_trailing_garbage():
    """``_c_unquote`` must return ``None`` for a non-clean ``"..."`` token.

    This pins the exact defect: the "startswith-quote-but-not-endswith-quote
    fallthrough" that silently passes the raw token through.  The well-formed
    and unquoted spellings must keep working (an over-broad fix that returns
    ``None`` for every quoted path would break the deny-list check for
    ``+++ "b/CLAUDE.md"``).
    """
    from pipeline.worktree_patch import _c_unquote

    # malformed quoting -> fail closed
    assert _c_unquote('"b/CLAUDE.md"x') is None
    assert _c_unquote('"a/CLAUDE.md"x') is None
    assert _c_unquote('"b/CLAUDE.md" ') is None
    assert _c_unquote('"b/CLAUDE.md"x"') is None

    # well-formed / unquoted -> unchanged behaviour
    assert _c_unquote('"b/CLAUDE.md"') == "b/CLAUDE.md"
    assert _c_unquote("b/CLAUDE.md") == "b/CLAUDE.md"
