"""FORGE-S1B: the merge path routes its rebase + force-push through
``Forge.prepare_merge``, so ``merge_pr`` means only "land it".

Seam invariant under test: ``pipeline.merge._rebase_and_push_for_merge`` keeps
its name, its location and its ``(gate_error, pushed_sha)`` return value; its
body now delegates the rebase + force-push to the resolved forge's
``prepare_merge``. Six pre-existing tests patch that exact name on
``pipeline.advance`` / ``pipeline.merge``, so it must not move or be renamed.

The runner seam is ``subprocess.run``: ``pipeline.rebase`` and
``pipeline.forge_github`` both resolve it as an attribute at call time, so one
patch on the shared ``subprocess`` module observes the whole argv sequence.
"""

from __future__ import annotations

import subprocess

import pipeline.advance as adv
import pipeline.server as p
from pipeline import merge
from pipeline.forge_github import GitHubForge

BRANCH = "agent/s1"
WORKTREE_MISSING = "/nonexistent-worktree"
CONFLICT_STDERR = "CONFLICT (content): Merge conflict in pipeline/merge.py"
PR_TITLE = "Fix flaky retry (#42)"


class _Recorder:
    """Stand-in for ``subprocess.run`` that records argv and never spawns.

    ``fail_on`` names the argv prefix that should fail; everything else
    succeeds, so a test can fail exactly one git step (e.g. the rebase) while
    the fetch that precedes it still succeeds.
    """

    def __init__(self, *, fail_on=None, stdout="", stderr=""):
        self.calls = []
        self._fail_on = list(fail_on) if fail_on else None
        self._stdout = stdout
        self._stderr = stderr

    def __call__(self, argv, *args, **kwargs):
        self.calls.append(list(argv))
        failed = self._fail_on is not None and list(argv)[: len(self._fail_on)] == self._fail_on
        return subprocess.CompletedProcess(
            argv,
            1 if failed else 0,
            "" if failed else self._stdout,
            self._stderr if failed else "",
        )

    def argv_starting(self, *prefix):
        """Every recorded argv whose leading elements equal ``prefix``."""
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]


class _FakeForge:
    """A forge whose ``prepare_merge`` records its calls."""

    def __init__(self, result=None):
        self.calls = []
        self._result = result or {
            "ok": True, "error": "", "pushed_sha": "deadbeef",
            "auto_resolved": False,
        }

    def prepare_merge(self, worktree, branch):
        self.calls.append((worktree, branch))
        return self._result


def _patch_rebase_boundaries(monkeypatch, repo_root):
    """Stub the rebase helper's non-git boundaries so the argv log is exact."""
    monkeypatch.setattr(p, "REPO_ROOT", repo_root)
    monkeypatch.setattr(p, "_default_branch", lambda: "master")
    monkeypatch.setattr(
        "pipeline.rebase._ensure_on_branch", lambda *a, **k: {"ok": True}
    )
    monkeypatch.setattr("pipeline.git_ops._commit_wip", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# prepare_merge owns the rebase + force-push
# ---------------------------------------------------------------------------
def test_prepare_merge_rebases_and_force_pushes(tmp_path, monkeypatch):
    """``prepare_merge`` must emit the rebase AND the force-push, in order."""
    rec = _Recorder()
    monkeypatch.setattr(subprocess, "run", rec)
    _patch_rebase_boundaries(monkeypatch, tmp_path)

    GitHubForge().prepare_merge(str(tmp_path), BRANCH)

    rebases = rec.argv_starting("git", "rebase")
    assert rebases == [["git", "rebase", "origin/master"]], rec.calls
    pushes = rec.argv_starting("git", "push")
    assert pushes == [
        ["git", "push", "--force-with-lease", "origin", BRANCH]
    ], rec.calls
    assert rec.calls.index(rebases[0]) < rec.calls.index(pushes[0]), (
        f"the rebase must precede the force-push; got {rec.calls!r}"
    )


def test_prepare_merge_conflict_short_circuits_before_push(tmp_path, monkeypatch):
    """A conflicted rebase must NOT push, and must return the bare
    ``rebase: <git stderr>`` text RC-02's ``rebase: .*conflict`` route matches.

    Wrapping the text (e.g. ``prepare_merge failed: rebase: ...``) or pushing
    anyway both break the conflict-rework route.
    """
    rec = _Recorder(fail_on=["git", "rebase"], stderr=CONFLICT_STDERR)
    monkeypatch.setattr(subprocess, "run", rec)
    _patch_rebase_boundaries(monkeypatch, tmp_path)

    prepared = GitHubForge().prepare_merge(str(tmp_path), BRANCH)

    assert rec.argv_starting("git", "push") == [], (
        f"a conflicted rebase must not force-push; got {rec.calls!r}"
    )
    assert prepared["ok"] is False, prepared
    assert prepared["error"] == f"rebase: {CONFLICT_STDERR}", prepared["error"]
    assert prepared["pushed_sha"] == ""


# ---------------------------------------------------------------------------
# merge_pr means only "land it"
# ---------------------------------------------------------------------------
def test_merge_pr_does_not_rebase_or_push(tmp_path, monkeypatch):
    """``merge_pr`` must emit no ``git rebase`` and no ``git push`` argv."""
    rec = _Recorder(stdout="merged")
    monkeypatch.setattr(subprocess, "run", rec)

    GitHubForge().merge_pr(str(tmp_path), BRANCH, PR_TITLE)

    assert rec.argv_starting("git", "rebase") == [], rec.calls
    assert rec.argv_starting("git", "push") == [], rec.calls
    assert rec.calls == [
        ["gh", "pr", "merge", BRANCH, "--squash", "--subject", PR_TITLE]
    ], rec.calls


def test_merge_pr_passes_pr_title_as_squash_subject(tmp_path, monkeypatch):
    """The squash subject is the PR title passed in, never a re-read subject."""
    rec = _Recorder(stdout="merged")
    monkeypatch.setattr(subprocess, "run", rec)

    GitHubForge().merge_pr(str(tmp_path), BRANCH, PR_TITLE)

    (call,) = rec.calls
    assert "--subject" in call, call
    assert call[call.index("--subject") + 1] == PR_TITLE, call
    # No `git log --format=%s` fallback: the branch's own "wip" subject must
    # never become the permanent squash subject.
    assert rec.argv_starting("git", "log") == [], rec.calls


# ---------------------------------------------------------------------------
# The seam: the helper delegates, and the conflict text survives
# ---------------------------------------------------------------------------
def test_rebase_helper_delegates_to_forge_prepare_merge(tmp_path, monkeypatch):
    """``pipeline.advance._rebase_and_push_for_merge`` routes through the forge.

    Driven through the ``pipeline.advance`` proxy on purpose: that is the
    binding ``advance_merge``'s ``_ModuleRef`` resolves and the six protected
    tests patch, so this proves the whole chain still lands on the helper.
    """
    fake = _FakeForge()
    monkeypatch.setattr(p, "REPO_ROOT", tmp_path)
    monkeypatch.setattr("pipeline.forge.get_forge", lambda repo_root: fake)

    # Seam invariant: the function keeps its name and its home module.
    assert callable(merge._rebase_and_push_for_merge)

    gate_error, pushed_sha = adv._rebase_and_push_for_merge(
        "plan", "s1", BRANCH, WORKTREE_MISSING
    )

    assert fake.calls == [(WORKTREE_MISSING, BRANCH)], (
        f"the helper must delegate to forge.prepare_merge; got {fake.calls!r}"
    )
    assert (gate_error, pushed_sha) == ("", "deadbeef")


def test_conflict_route_still_matches_rebase_error(tmp_path, monkeypatch):
    """The ``rebase: ... conflict ...`` text still reaches the rework route.

    ``pipeline/advance_merge.py`` gates RC-02's conflict-rework route on
    ``gate_error.startswith("rebase:") and "conflict" in gate_error.lower()``;
    the helper must pass the forge's error through unchanged.
    """
    fake = _FakeForge(result={
        "ok": False,
        "error": f"rebase: {CONFLICT_STDERR}",
        "pushed_sha": "",
        "auto_resolved": False,
    })
    monkeypatch.setattr(p, "REPO_ROOT", tmp_path)
    monkeypatch.setattr("pipeline.forge.get_forge", lambda repo_root: fake)

    gate_error, pushed_sha = adv._rebase_and_push_for_merge(
        "plan", "s1", BRANCH, WORKTREE_MISSING
    )

    assert gate_error == f"rebase: {CONFLICT_STDERR}", gate_error
    assert pushed_sha == ""
    # The exact RC-02 predicate from pipeline/advance_merge.py:395-397.
    assert gate_error.startswith("rebase:"), gate_error
    assert "conflict" in gate_error.lower(), gate_error
