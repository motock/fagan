"""Regression test: the manual merge path must push from the PLAN's scoped
repo, not the pipeline server's own default REPO_ROOT.

Live incident (2026-09-17, anagram-service plan, story cf843d3e): a plan
with its own ``repo_root`` (a repo other than the pipeline's own) hit
``approve_merge`` failing every time with
``push failed: error: src refspec agent/<key> does not match any`` /
``failed to push some refs to '<the pipeline's OWN origin>'`` - even though
the branch had already been correctly rebased and pushed by hand moments
earlier.

Root cause: ``pipeline/merge.py::_approve_merge_impl`` does
``from .server import (REPO_ROOT, ...)`` at the TOP of the function - before
it enters its own ``with _scoped_repo_root(plan_name):`` block a few lines
later. ``_scoped_repo_root`` reassigns the *module-level* global
``pipeline.server.REPO_ROOT`` for the duration of the block, but the local
name ``REPO_ROOT`` already bound in ``_approve_merge_impl``'s frame was
captured at the OLD value and never sees the reassignment. The subsequent
``git push --force-with-lease origin <branch>`` then runs with
``cwd=REPO_ROOT`` - the pipeline's own repo directory, not the plan's repo -
so it either pushes nothing meaningful there or fails outright with
"src refspec ... does not match any" against the wrong remote.

This is unrelated to the alias-branch bug covered by
test_merge_gate_alias_branch.py: that suite always monkeypatches
``_scoped_repo_root`` down to a ``contextlib.nullcontext()`` no-op and
separately forces ``p.REPO_ROOT`` to already equal the story repo, which
sidesteps the exact timing bug this test exercises. Here ``_scoped_repo_root``
runs for REAL, starting from a DIFFERENT default REPO_ROOT (a decoy
directory standing in for the pipeline's own repo), so the reassignment
must actually take effect for the push to land in the right place.
"""

import json
import subprocess

import pipeline.server as p
from pipeline import merge


def _git(*args, cwd, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=check,
        capture_output=True, text=True,
    )


def _init_plan_repo(tmp_path):
    """A bare ``origin`` plus a story repo on branch ``agent/s1``."""
    origin = tmp_path / "origin.git"
    _git("-c", "init.defaultBranch=master", "init", "--bare", "-q",
         str(origin), cwd=tmp_path)
    repo = tmp_path / "repo"
    _git("-c", "init.defaultBranch=master", "init", "-q", str(repo),
         cwd=tmp_path)
    _git("config", "user.email", "agent@local", cwd=repo)
    _git("config", "user.name", "agent", cwd=repo)

    (repo / "master.txt").write_text("m\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "m0", cwd=repo)
    _git("remote", "add", "origin", str(origin), cwd=repo)
    _git("push", "-q", "origin", "master", cwd=repo)

    _git("checkout", "-q", "-b", "agent/s1", cwd=repo)
    (repo / "story.txt").write_text("story\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "story work", cwd=repo)
    _git("push", "-q", "origin", "agent/s1", cwd=repo)

    return repo


def _fake_rebase_onto_master(worktree, branch):
    _git("commit", "-q", "--allow-empty", "-m", "rebased onto master",
         cwd=worktree)
    return {"ok": True}


def _spy_on_git_cwd(monkeypatch):
    """Record (cmd, cwd) for every subprocess.run call the gate makes."""
    recorded = []
    real_run = subprocess.run

    def spy(cmd, *args, **kwargs):
        recorded.append((list(cmd), kwargs.get("cwd")))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(merge.subprocess, "run", spy)
    return recorded


def _write_manifest(plan_dir, plan_name, stories, repo_root):
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps(
            {"epics": {}, "stories": stories, "repo_root": str(repo_root)},
            indent=2,
        )
    )


def _story(**overrides):
    base = {
        "summary": "story on a plan-scoped repo",
        "status": "parked",
        "review_verdict": "APPROVE",
        "risk": "low",
        "worktree": "/nonexistent-worktree",
    }
    base.update(overrides)
    return base


class TestApproveMergePushesToScopedRepoNotDefaultRepoRoot:
    def test_manual_path_pushes_from_plan_repo_not_pipelines_own_repo_root(
        self, plan_dir, tmp_path, monkeypatch,
    ):
        repo = _init_plan_repo(tmp_path)
        # Stand-in for the pipeline server's OWN repo: a directory that is
        # NOT the plan's repo at all, matching the live incident where
        # REPO_ROOT defaulted to the pipeline's own checkout.
        decoy_repo_root = tmp_path / "pipeline-servers-own-repo"
        decoy_repo_root.mkdir()

        recorded = _spy_on_git_cwd(monkeypatch)
        ci_calls = []

        def ci_status(branch, *, sha):
            ci_calls.append((branch, sha))
            return {"state": "success", "error": ""}

        monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
        # The DEFAULT global REPO_ROOT is the decoy - exactly like the
        # pipeline server's own REPO_ROOT env var pointing at its own repo
        # while this plan's manifest names a different repo_root.
        monkeypatch.setattr(p, "REPO_ROOT", decoy_repo_root)
        monkeypatch.setattr(p, "_rebase_onto_master", _fake_rebase_onto_master)
        monkeypatch.setattr(p, "_default_branch", lambda: "master")
        monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
        # _scoped_repo_root runs FOR REAL here (not stubbed to a no-op) so
        # the reassignment timing bug is actually exercised.
        monkeypatch.setattr(p, "_ci_status", ci_status)
        monkeypatch.setattr(p, "_ci_rerun", lambda sha: None)
        monkeypatch.setattr(
            p, "_reverify_acceptance",
            lambda story, wt, key: {"state": "pass"},
        )
        monkeypatch.setattr(p, "_reverify_build", lambda wt: {"state": "pass"})
        monkeypatch.setattr(p, "_mcp_self_source_touched", lambda wt, base: "")
        monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
        monkeypatch.setattr(p, "_mark_plane_done", lambda *a, **k: None)
        if hasattr(p, "_maybe_record_retro"):
            monkeypatch.setattr(p, "_maybe_record_retro", lambda *a, **k: None)

        _write_manifest(
            plan_dir, "go",
            {"S1": _story(worktree=str(repo))},
            repo_root=repo,
        )

        result = merge._approve_merge_impl("go", "S1")

        assert result.get("ok") is True, (
            f"manual merge failed: {result!r} - push_calls seen so far: "
            f"{[c for c in recorded if c[0][:2] == ['git', 'push']]!r}"
        )

        push_calls = [c for c in recorded if c[0][:2] == ["git", "push"]]
        assert push_calls, "the manual merge path must push the rebased branch"
        _push_cmd, push_cwd = push_calls[0]
        assert str(push_cwd) == str(repo), (
            f"push ran with cwd={push_cwd!r}, but it must run from the "
            f"plan's scoped repo {repo!r} (resolved via _scoped_repo_root), "
            f"not the pipeline server's own default REPO_ROOT "
            f"{decoy_repo_root!r} - pushing from the wrong repo either "
            f"fails outright (\"src refspec ... does not match any\" "
            f"against the wrong origin) or silently no-ops"
        )

        # The default global must be restored after the scoped block exits.
        assert p.REPO_ROOT == decoy_repo_root, (
            "_scoped_repo_root must restore the previous REPO_ROOT on exit"
        )

        assert ci_calls, "the manual merge path must CI-check the pushed branch"
