"""Opt-in ``PIPELINE_WORKTREE_NAMESPACE`` for worktree directory names.

Mirrors ``PIPELINE_BRANCH_NAMESPACE`` (tests/unit/test_branch_namespace.py) but
namespaces the worktree *directory* instead of the git ref. Behaviour is
preserved when the variable is unset: every path stays ``WORKTREE_ROOT /
story_key`` exactly as before.

Unlike the branch helper, the key's case is preserved: git refs are
case-insensitive on macOS, directory names are not, and dispatch must keep
using the exact key spelling it always has.
"""

import contextlib

import pytest

from pipeline import dispatch as pdisp
from pipeline.branch_names import worktree_dir_name

ENV = "PIPELINE_WORKTREE_NAMESPACE"


@pytest.fixture(autouse=True)
def _clear_namespace(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)


# ---------- worktree_dir_name ----------


def test_should_keep_plain_name_when_namespace_unset():
    assert worktree_dir_name("ABC-1") == "ABC-1"


def test_should_keep_plain_name_when_namespace_empty(monkeypatch):
    monkeypatch.setenv(ENV, "")
    assert worktree_dir_name("ABC-1") == "ABC-1"


def test_should_insert_namespace_when_set(monkeypatch):
    monkeypatch.setenv(ENV, "alice")
    assert worktree_dir_name("ABC-1") == "alice/ABC-1"


def test_should_preserve_key_case_when_namespaced(monkeypatch):
    monkeypatch.setenv(ENV, "alice")
    assert worktree_dir_name("Mixed-Case-Key") == "alice/Mixed-Case-Key"


def test_should_preserve_key_case_when_unset():
    assert worktree_dir_name("Mixed-Case-Key") == "Mixed-Case-Key"


@pytest.mark.parametrize("bad", ["Alice", "-a", "a/b", "a b", "x" * 33, "a_b"])
def test_should_reject_invalid_namespace_naming_variable(monkeypatch, bad):
    monkeypatch.setenv(ENV, bad)
    with pytest.raises(ValueError, match=ENV):
        worktree_dir_name("ABC-1")


def test_should_accept_32_char_namespace(monkeypatch):
    monkeypatch.setenv(ENV, "a" * 32)
    assert worktree_dir_name("ABC-1") == f"{'a' * 32}/ABC-1"


# ---------- dispatch wiring ----------


class _FakeStore:
    """Minimal stand-in for ``pipeline.server._store``.

    ``_dispatch_story_impl`` only needs a transaction, a manifest path and the
    manifest itself before it derives the worktree path and asks
    ``cross_plan.key_conflicts`` about collisions.
    """

    def __init__(self, manifest_path, manifest):
        self._manifest_path = manifest_path
        self._manifest = manifest

    def transaction(self, plan_name):
        return contextlib.nullcontext(True)

    def manifest_path(self, plan_name):
        return self._manifest_path

    def get_manifest(self, plan_name):
        return self._manifest


@pytest.fixture
def dispatch_probe(tmp_path, monkeypatch):
    """Drive the real ``_dispatch_story_impl`` up to the collision check.

    Returns ``(worktree_root, captured_roots)`` where ``captured_roots``
    collects the ``worktrees`` argument passed to ``cross_plan.key_conflicts``.
    The collision check is forced to report a conflict so dispatch returns
    before any git or worktree work happens.
    """
    worktree_root = tmp_path / "worktrees"
    manifest_path = tmp_path / "plans" / "plan-a" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest = {"stories": {"ABC-1": {"status": "todo"}}}
    monkeypatch.setattr(pdisp, "_store", _FakeStore(manifest_path, manifest))
    monkeypatch.setattr(pdisp, "WORKTREE_ROOT", worktree_root)
    monkeypatch.setattr(pdisp, "_notify_user", lambda *a, **k: None)

    captured = []

    def _fake_key_conflicts(keys, manifests, worktrees):
        captured.append(worktrees)
        return [{"plan": "other-plan", "other_key": "ABC-1"}]

    monkeypatch.setattr(pdisp.cross_plan, "key_conflicts", _fake_key_conflicts)
    return worktree_root, captured


def test_dispatch_should_use_plain_key_path_when_namespace_unset(dispatch_probe):
    worktree_root, captured = dispatch_probe
    result = pdisp._dispatch_story_impl("plan-a", "ABC-1")
    assert result["ok"] is False  # the forced collision short-circuits
    assert captured == [worktree_root]


def test_dispatch_should_use_namespaced_path_when_set(dispatch_probe, monkeypatch):
    worktree_root, captured = dispatch_probe
    monkeypatch.setenv(ENV, "alice")
    result = pdisp._dispatch_story_impl("plan-a", "ABC-1")
    assert result["ok"] is False
    assert captured == [worktree_root / "alice"]


def test_dispatch_should_pass_worktree_parent_as_collision_root(
    dispatch_probe, monkeypatch
):
    """``key_conflicts`` gets ``worktree_path.parent``: WORKTREE_ROOT today,
    WORKTREE_ROOT/<ns> when namespaced (the right root for same-namespace
    collisions)."""
    worktree_root, captured = dispatch_probe
    pdisp._dispatch_story_impl("plan-a", "ABC-1")
    assert captured[0] == worktree_root

    monkeypatch.setenv(ENV, "alice")
    pdisp._dispatch_story_impl("plan-a", "ABC-1")
    assert captured[1] == worktree_root / "alice"


def test_dispatch_should_reject_invalid_namespace(dispatch_probe, monkeypatch):
    """An invalid namespace fails loudly, naming the variable, before any
    collision check or git work."""
    _, captured = dispatch_probe
    monkeypatch.setenv(ENV, "Alice")
    with pytest.raises(ValueError, match=ENV):
        pdisp._dispatch_story_impl("plan-a", "ABC-1")
    assert captured == []
