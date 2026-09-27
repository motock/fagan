"""Dispatch must refuse a plan whose repo_root no longer exists.

A plan's ``repo_root`` is recorded at ingest time and then trusted forever.
``_create_fresh_worktree`` runs ``git fetch`` / ``git worktree add`` with
``cwd=<repo_root>``, so when that directory is gone - a plan ingested
against a temp directory, a moved checkout, an unmounted volume - dispatch
does not fail cleanly: ``subprocess`` raises **FileNotFoundError**, which the
function's ``except subprocess.CalledProcessError`` does not catch, so a raw
exception escapes where every caller expects the structured
``{"ok": False, "error": ...}``. (If the directory still exists but is not a
git repository, the same cwd instead yields the confusing
``fatal: not a git repository``.)

Observed live 2026-09-27 on the ``bench_ok`` plan, whose repo_root was a
pytest temp directory deleted minutes after ingest: three dispatch attempts
died on ``git fetch`` failures, a fourth on an escaping FileNotFoundError,
and triage then filed a follow-up story to repair a repository that no
longer existed.

These tests drive the production function ``dispatch`` itself calls, so they
grade the real entry point rather than a helper beside it.
"""
import contextlib

from pipeline import dispatch_worktree

MISSING = "deleted-repo-root"


@contextlib.contextmanager
def _no_git_lock(_repo_root):
    """Stand in for the real flock, which needs a repo_root that exists."""
    yield True


def _recorder(calls):
    def _record(*args, **_kwargs):
        calls.append(args[0] if args else None)

    return _record


def _stub_collaborators(monkeypatch, repo_root, calls):
    """Point the scoped root at ``repo_root`` and record every subprocess."""
    monkeypatch.setattr(
        dispatch_worktree,
        "_scoped_repo_root",
        lambda _plan_name: contextlib.nullcontext(repo_root),
    )
    monkeypatch.setattr(dispatch_worktree, "_try_acquire_git_lock", _no_git_lock)
    monkeypatch.setattr(dispatch_worktree, "_default_branch", lambda: "main")
    monkeypatch.setattr(
        dispatch_worktree, "_provision_worktree_venv", lambda _path: None
    )
    monkeypatch.setattr(dispatch_worktree.subprocess, "run", _recorder(calls))


def test_missing_repo_root_is_refused(tmp_path, monkeypatch):
    calls = []
    _stub_collaborators(monkeypatch, tmp_path / MISSING, calls)

    result = dispatch_worktree._create_fresh_worktree(
        "bench_ok", "agent/x", tmp_path / "wt"
    )

    assert result is not None, (
        "a plan whose repo_root is gone must be refused, not set up as though "
        "the repository were there - dispatch then launches an agent against a "
        "directory that does not exist"
    )
    assert result["ok"] is False
    assert str(tmp_path / MISSING) in result["error"]
    assert "does not exist" in result["error"]


def test_refusal_happens_before_git_is_ever_run(tmp_path, monkeypatch):
    calls = []
    _stub_collaborators(monkeypatch, tmp_path / MISSING, calls)

    dispatch_worktree._create_fresh_worktree("bench_ok", "agent/x", tmp_path / "wt")

    assert calls == [], (
        f"git ran with a deleted repo_root as cwd: {calls!r} - subprocess raises "
        "FileNotFoundError there, which the CalledProcessError handler misses, "
        "so a raw exception escapes dispatch instead of a structured refusal"
    )


def test_a_file_at_the_repo_root_path_is_refused(tmp_path, monkeypatch):
    """Boundary: the path exists, but is not a directory."""
    calls = []
    not_a_dir = tmp_path / "repo-root-is-a-file"
    not_a_dir.write_text("x\n")
    _stub_collaborators(monkeypatch, not_a_dir, calls)

    result = dispatch_worktree._create_fresh_worktree(
        "plan", "agent/x", tmp_path / "wt"
    )

    assert result is not None
    assert result["ok"] is False
    assert calls == []


def test_an_existing_repo_root_still_runs_the_git_setup(tmp_path, monkeypatch):
    """Negative control: the guard is keyed on existence, not a blanket refusal."""
    calls = []
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _stub_collaborators(monkeypatch, repo_root, calls)

    result = dispatch_worktree._create_fresh_worktree(
        "plan", "agent/x", tmp_path / "wt"
    )

    assert result is None, "an existing repo_root must not be refused"
    assert calls, "the git setup must still run for an existing repo_root"
