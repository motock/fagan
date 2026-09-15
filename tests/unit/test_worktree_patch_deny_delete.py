"""Regression tests for the WAP-7 review findings (deny-list bypasses).

Three bugs, all in ``pipeline/worktree_patch.py``:

1. ``apply_patch()``'s deny-list gate (gate 6) only inspects NEW-side paths,
   so a deletion-only diff (``--- a/CLAUDE.md`` / ``+++ /dev/null``) never has
   its OLD-side path checked and ``git apply`` deletes the denied file.
2. ``parse_unified_diff()`` does not C-unquote paths, so ``+++ "b/CLAUDE.md"``
   yields the path ``"b/CLAUDE.md"`` whose final component ``CLAUDE.md"``
   misses the final-component deny rules while ``git apply`` writes the
   unquoted ``CLAUDE.md``.
3. ``apply_patch()`` indexes ``story["worktree"]`` directly, so a manifest
   story without the key raises an unhandled ``KeyError`` (HTTP 500) instead
   of the documented refusal dict.

These tests are RED against the buggy implementation and must stay green
after the fix.  They are self-contained (own fixtures) so they do not depend
on the fixtures defined in ``test_worktree_patch_apply.py``.
"""

from __future__ import annotations

import difflib
import json
import subprocess
from pathlib import Path

import pytest

from pipeline.worktree_patch import apply_patch, create_patch_record

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

PLAN_NAME = "wap7denyplan"
STORY_KEY = "WAP-7"

#: Captured at import time so a spy installed by a test never intercepts the
#: test's own git plumbing.
_REAL_RUN = subprocess.run

#: The three deny-listed targets the reviewer named.  ``.git/config`` is
#: denied by the ``.git`` COMPONENT rule; the other two by the FINAL-component
#: rule.
DENIED_TARGETS = ["CLAUDE.md", ".mcp.json", ".git/config"]


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


def _unified(rel_path: str, old_text: str, new_text: str) -> str:
    """A plain unified diff (no ``diff --git``/``index`` lines)."""
    return "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
            n=3,
        )
    )


def _delete_file_diff(rel_path: str, old_text: str) -> str:
    """A deletion-only diff: ``--- a/<rel_path>`` / ``+++ /dev/null``."""
    return "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            [],
            fromfile=f"a/{rel_path}",
            tofile="/dev/null",
            n=3,
        )
    )


def _quoted_edit_diff(rel_path: str, old_text: str, new_text: str) -> str:
    """A C-QUOTED edit diff: ``--- "a/<rel_path>"`` / ``+++ "b/<rel_path>"``.

    ``git apply`` C-unquotes these headers and writes the unquoted path, so
    the deny check must run on the unquoted path too.
    """
    return "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f'"a/{rel_path}"',
            tofile=f'"b/{rel_path}"',
            n=3,
        )
    )


def _assert_refusal(result, *, status_code: int | None = None):
    """Every refusal has the same shape: ok/error/status_code."""
    assert isinstance(result, dict), f"refusal must be a dict, got {result!r}"
    assert result["ok"] is False, result
    assert isinstance(result["error"], str) and result["error"], result
    assert isinstance(result["status_code"], int), result
    if status_code is not None:
        assert result["status_code"] == status_code, result


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


def _patch_store() -> dict:
    """The module-level patch-record store dict.

    The brief names it ``_PATCH_STORE``; the fallback discovery keeps this
    file working if a later refactor renames the private attribute.
    """
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
    """A real, committed git repository holding the deny-listed files."""
    root = (tmp_path / "worktree").resolve()
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test Author")
    (root / "app.py").write_text("alpha\nbeta\ngamma\n")
    (root / "CLAUDE.md").write_bytes(b"deny me\n")
    (root / ".mcp.json").write_bytes(b'{"mcp": 1}\n')
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
# bug 1: deletion-only diffs bypass the deny list
# --------------------------------------------------------------------------


@pytest.mark.parametrize("target", DENIED_TARGETS)
def test_deletion_only_diff_targeting_denied_file_is_refused_403(
    plan_dir, repo, manifest, target
):
    """A deletion-only diff must be denied on its OLD-side path.

    Buggy: gate 6 only sees the new side (``/dev/null``), so the deny list is
    never consulted and ``git apply`` deletes the file.
    """
    denied = repo / target
    before = denied.read_bytes()
    diff = _delete_file_diff(target, before.decode("utf-8"))
    rec = _record(diff, [], added_lines=0)

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    _assert_refusal(result, status_code=403)
    assert denied.exists(), f"{target} was deleted by a refused patch"
    assert denied.read_bytes() == before, f"{target} was modified by a refused patch"


def test_deletion_only_refusal_leaves_state_correct_for_a_followup_edit(
    plan_dir, repo, manifest
):
    """The worked example: Call 1 refuses, so the worktree state stays correct.

    Buggy: Call 1 deletes ``CLAUDE.md`` and returns 200, so the follow-up
    call runs against a worktree whose ``CLAUDE.md`` is gone.

    Call 2 targets ``CLAUDE.md`` too, and ``CLAUDE.md`` is deny-listed for
    ANY touch (pinned by ``test_deny_list_refused_403_and_target_byte_identical``),
    so the correct outcome for Call 2 is the 403 deny refusal -- NOT a 409
    "patch does not apply (context drift)", which is what a missing file
    would produce.  The file must still be byte-identical after both calls.
    """
    denied = repo / "CLAUDE.md"
    before = denied.read_bytes()

    delete_diff = _delete_file_diff("CLAUDE.md", before.decode("utf-8"))
    delete_rec = _record(delete_diff, [], added_lines=0)
    first = apply_patch(
        PLAN_NAME, STORY_KEY, delete_rec["patch_id"], delete_rec["confirmation_token"]
    )

    _assert_refusal(first, status_code=403)
    assert denied.read_bytes() == before

    edit_diff = _unified("CLAUDE.md", before.decode("utf-8"), "changed\n")
    edit_rec = _record(edit_diff, ["CLAUDE.md"])
    second = apply_patch(
        PLAN_NAME, STORY_KEY, edit_rec["patch_id"], edit_rec["confirmation_token"]
    )

    # CLAUDE.md is deny-listed, so the follow-up edit is refused on the deny
    # list -- and refused because of the DENY LIST (403), not because Call 1
    # corrupted the worktree state (that would be the 409 context-drift
    # refusal a deleted file produces).
    _assert_refusal(second, status_code=403)
    assert second["error"] == "patch target refused", second
    assert denied.read_bytes() == before, "Call 1's refusal did not preserve CLAUDE.md"


# --------------------------------------------------------------------------
# bug 2: C-quoted paths bypass the final-component deny rules
# --------------------------------------------------------------------------


def test_c_quoted_path_targeting_denied_file_is_refused_403(plan_dir, repo, manifest):
    """``+++ "b/CLAUDE.md"`` must be denied on the path git apply will write.

    Buggy: ``parse_unified_diff`` keeps the quotes, so the final component is
    ``CLAUDE.md"`` -- not in the deny set -- while ``git apply`` C-unquotes
    the header and writes ``CLAUDE.md``.
    """
    denied = repo / "CLAUDE.md"
    before = denied.read_bytes()
    diff = _quoted_edit_diff("CLAUDE.md", before.decode("utf-8"), "changed\n")
    rec = _record(diff, ['"b/CLAUDE.md"'])

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    _assert_refusal(result, status_code=403)
    assert denied.read_bytes() == before, "C-quoted path wrote the denied file"


# --------------------------------------------------------------------------
# bug 3: a manifest story without "worktree" must not raise KeyError
# --------------------------------------------------------------------------


def test_manifest_story_without_worktree_returns_refusal_not_keyerror(
    plan_dir, repo, manifest
):
    """A story lacking ``worktree`` must yield the refusal dict, not a 500."""
    _write_manifest(plan_dir, {STORY_KEY: {"status": "failed"}})
    diff = _unified("app.py", (repo / "app.py").read_text(), "alpha\nBETA\ngamma\n")
    rec = _record(diff, ["app.py"])

    try:
        result = apply_patch(
            PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
        )
    except KeyError as exc:  # pragma: no cover - the bug under test
        pytest.fail(
            f"apply_patch raised KeyError for a story without 'worktree': {exc!r}"
        )

    _assert_refusal(result)
    assert result["status_code"] in (404, 409), result
