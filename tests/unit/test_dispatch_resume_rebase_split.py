"""RH-13: the stale-base check moved out of pipeline/dispatch.py."""

from pathlib import Path

from pipeline import dispatch, dispatch_resume_rebase

REPO_ROOT = Path(__file__).resolve().parents[2]
LIMIT = 1000


def test_should_reexport_moved_symbol_as_same_object():
    assert (
        dispatch._rebase_stale_resumed_worktree
        is dispatch_resume_rebase._rebase_stale_resumed_worktree
    )


def test_should_import_moved_symbol_from_new_module():
    from pipeline.dispatch_resume_rebase import _rebase_stale_resumed_worktree

    assert callable(_rebase_stale_resumed_worktree)


def test_should_keep_dispatch_module_under_line_limit():
    lines = (REPO_ROOT / "pipeline/dispatch.py").read_text().count("\n")
    assert lines < LIMIT


def test_should_keep_new_module_under_line_limit():
    lines = (REPO_ROOT / "pipeline/dispatch_resume_rebase.py").read_text().count("\n")
    assert lines < LIMIT


def test_should_not_list_dispatch_in_line_limit_allowlist():
    source = (REPO_ROOT / "scripts/check_line_limit.py").read_text()
    assert '"pipeline/dispatch.py"' not in source


def _call(tmp_path):
    (tmp_path / ".git").mkdir()
    story = {"correlation_id": "abc"}
    manifest = {"stories": {"S-1": story}}
    return dispatch_resume_rebase._rebase_stale_resumed_worktree(
        "plan", "S-1", story, "agent/s-1", tmp_path, tmp_path / "m.json", manifest
    ), story


def test_should_see_patch_on_dispatch_module_inside_moved_code(monkeypatch, tmp_path):
    """Monkeypatch reach: patching pipeline.dispatch._scoped_repo_root must
    change the moved code's behaviour (here: make it raise -> fail open)."""
    calls = []

    def _boom(plan_name):
        calls.append(plan_name)
        raise RuntimeError("boom")

    monkeypatch.setattr(dispatch, "_scoped_repo_root", _boom)
    parked, _story = _call(tmp_path)
    assert calls == ["plan"]
    assert parked is None


def test_should_park_on_rebase_conflict_via_patched_dispatch_names(
    monkeypatch, tmp_path
):
    import contextlib
    import subprocess

    @contextlib.contextmanager
    def _root(plan_name):
        yield tmp_path

    @contextlib.contextmanager
    def _lock(root):
        yield False

    class _Out:
        stdout = "2\n"

    monkeypatch.setattr(dispatch, "_scoped_repo_root", _root)
    monkeypatch.setattr(dispatch, "_try_acquire_git_lock", _lock)
    monkeypatch.setattr(dispatch, "_default_branch", lambda: "master")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Out())
    monkeypatch.setattr(
        dispatch,
        "_rebase_onto_master",
        lambda wt, br: {"ok": False, "conflict": True, "error": "x"},
    )
    monkeypatch.setattr(dispatch, "_atomic_write_json", lambda p, d: None)
    monkeypatch.setattr(dispatch, "_notify_user", lambda *a, **k: None)
    parked, story = _call(tmp_path)
    assert parked["reason"] == "rebase_conflict"
    assert story["status"] == "parked"


def test_should_return_none_when_worktree_is_not_a_git_dir(tmp_path):
    story = {}
    result = dispatch_resume_rebase._rebase_stale_resumed_worktree(
        "plan", "S-1", story, "b", tmp_path, tmp_path / "m.json", {}
    )
    assert result is None
