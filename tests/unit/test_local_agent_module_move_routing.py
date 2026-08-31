"""Routing tests for the LA-GIT module move (scripts/local_agent_git.py).

scripts/local_agent.py is file-execed under MULTIPLE module names in one
pytest process (the shared helper's "local_agent", test_dropped_top_level_vars'
"local_agent_dropped_vars", the acceptance fixtures' variants). The moved
git/wip/suite impls therefore receive `origin` — the originating module's
globals() DICT — as their first parameter, so monkeypatch.setattr on *that*
module (which writes into the same dict) re-binds land at call time.

These tests pin the routing mechanism itself:

- Rebind observation: patching la.git must be observed by the moved
  worktree_dirty / auto_wip_commit / exclude_runtime_artifacts impls — they
  read origin["git"] live, not a frozen copy.
- CWD rebind observation: patching la.CWD must change the cwd the moved
  impls run git with (git_impl injects cwd=origin["CWD"] into subprocess.run,
  so the fake intercepts at the subprocess boundary).
- Routing shape: the impls live in scripts/local_agent_git.py; the original
  names remain in scripts/local_agent.py as one-line delegating wrappers.
- Negative case: an exception from la.git propagates out of
  la.worktree_dirty() (no silent swallow) — today's behavior, preserved.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit._local_agent_test_helpers import la

_REPO_ROOT = Path(__file__).parent.parent.parent


class _FakeProc:
    """Stand-in CompletedProcess: the verbatim bodies call .stdout on git's
    return (worktree_dirty/exclude_runtime_artifacts), so a bare "" fake
    would crash them rather than prove anything about routing."""

    stdout = ""
    stderr = ""
    returncode = 0


def test_rebind_observation(tmp_path, monkeypatch):
    """Patching la.git must be seen by every moved impl that calls git."""
    calls = []

    def fake_git(*args, **kwargs):
        calls.append(args)
        return _FakeProc()

    monkeypatch.setattr(la, "git", fake_git)
    monkeypatch.setattr(la, "CWD", tmp_path)

    for name, args in (("worktree_dirty", ()), ("auto_wip_commit", ("t",)),
                       ("exclude_runtime_artifacts", ())):
        n = len(calls)
        getattr(la, name)(*args)
        assert len(calls) > n, f"la.{name} did not route through la.git"


def test_cwd_rebind(tmp_path, monkeypatch):
    """Patching la.CWD must change the cwd the moved impls run git with.

    git_impl is the only place cwd is injected (cwd=origin["CWD"] into
    subprocess.run), so the fake intercepts subprocess.run inside
    scripts.local_agent_git — proving the impl read the CURRENT la.CWD, not
    a copy frozen at import time."""
    import scripts.local_agent_git as lag_git

    recorded = []

    def fake_run(argv, **kwargs):
        recorded.append(kwargs.get("cwd"))
        return _FakeProc()

    monkeypatch.setattr(lag_git, "subprocess", SimpleNamespace(run=fake_run))
    monkeypatch.setattr(la, "CWD", tmp_path)

    la.git("status")
    assert Path(recorded[-1]) == tmp_path
    la.worktree_dirty()
    assert Path(recorded[-1]) == tmp_path


def test_routing_shape():
    """The impls live in scripts/local_agent_git.py; local_agent.py keeps
    one-line delegating wrappers (original names, no original bodies)."""
    assert (_REPO_ROOT / "scripts" / "local_agent_git.py").exists()
    git_mod = (_REPO_ROOT / "scripts" / "local_agent_git.py").read_text()
    for name in ("git_impl", "worktree_dirty_impl", "auto_wip_commit_impl",
                 "exclude_runtime_artifacts_impl", "_full_suite_result_impl"):
        assert f"def {name}" in git_mod

    agent_src = (_REPO_ROOT / "scripts" / "local_agent.py").read_text()
    assert "def worktree_dirty" in agent_src
    assert "worktree_dirty_impl(globals()" in agent_src
    # worktree_dirty's original body line must be gone from local_agent.py
    # (it moved verbatim into worktree_dirty_impl).
    assert 'bool(git("status", "--porcelain").stdout.strip())' not in agent_src


def test_git_error_propagates(monkeypatch):
    """An exception from la.git must propagate out of la.worktree_dirty()
    (no silent swallow) — today's behavior, preserved by the move."""

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(la, "git", boom)
    with pytest.raises(RuntimeError):
        la.worktree_dirty()