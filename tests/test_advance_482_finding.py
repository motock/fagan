"""Regression test for the rework-round-2 Blocking finding at
pipeline/advance.py's merge gate (originally line 482).

Recovered finding (verbatim from the earlier review thread):

    "Blocking: pipeline/advance.py:482: merge gate passes the hardcoded
    convention branch into ``_rebase_and_push_for_merge``, mismatching the
    alias branch ``_merge_pr`` merges."

Round 1 moved the resolution into advance.py's gate block but LEFT the
hardcoded convention-branch computation in place as a "fallback":

    branch = f"agent/{key.lower()}"
    if worktree and Path(worktree).is_dir():
        branch = _resolve_story_branch(worktree, key)

That fallback is the specific mistake the finding names: the gate call still
receives a branch computed from the hardcoded convention name, so any path
where the resolver degrades to the convention name (probe failure on an
existing worktree, a future refactor dropping the guard) pushes the stale
convention branch again - the exact live-incident failure ("src refspec
agent/s10 does not match any" after a prior _merge_pr deleted it, or a stale
twin pushed while CI polls a never-pushed SHA). The single source of truth
must be the gate itself: advance.py should not compute or pass a branch at
all; ``_rebase_and_push_for_merge`` already resolves the worktree HEAD
internally (pipeline/merge.py), and the CI poll must poll the branch the gate
actually pushed - the post-rebase worktree HEAD - not a locally computed name.

This test pins both halves of that contract:

1. Structural: advance.py's merge-adjudication block must not compute the
   convention branch name at all (no ``f"agent/{key.lower()}"`` reaching the
   gate), and must not pass a branch argument into ``_rebase_and_push_for_merge``.
2. Behavioural (follow-up-call shape, per the stateful-finding rule): with the
   worktree HEAD on the alias and the local convention branch already deleted
   by a prior ``_merge_pr`` (the live-incident state), the advance tick must
   clear the merge gate on call 1 AND on the follow-up call 2 - call 1 must
   leave origin/agent/<key>-2 at the pushed SHA, and call 2 (master advanced,
   rebase rewrites HEAD again) must still push the alias with a matching
   force-with-lease and poll CI on the freshly pushed SHA. A fix that resolves
   on call 1 but falls back to the hardcoded convention name on call 2 (the
   "correct return value, wrong persisted state" mistake) fails call 2.
"""

import json
import subprocess
from pathlib import Path

import pytest

import pipeline.server as p
from pipeline.server import _advance_pipeline_locked


# ---------------------------------------------------------------------------
# Real-git fixture (same topology as the round-1 regression tests).
# ---------------------------------------------------------------------------
def _git(*args, cwd, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=check,
        capture_output=True, text=True,
    )


def _init_story_repo(tmp_path):
    """origin (bare) + story repo whose HEAD is the alias agent/s10-2, with
    the local convention branch agent/s10 ALREADY DELETED (the state a prior
    _merge_pr `git branch -D` leaves behind)."""
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

    _git("checkout", "-q", "-b", "agent/s10", cwd=repo)
    (repo / "conv.txt").write_text("conv\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "conv", cwd=repo)
    _git("push", "-q", "origin", "agent/s10", cwd=repo)

    _git("checkout", "-q", "master", cwd=repo)
    _git("checkout", "-q", "-b", "agent/s10-2", cwd=repo)
    (repo / "alias.txt").write_text("alias\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "alias", cwd=repo)
    alias_sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    _git("push", "-q", "origin", "agent/s10-2", cwd=repo)

    # The incident state: prior _merge_pr deleted the local convention branch.
    _git("branch", "-q", "-D", "agent/s10", cwd=repo)

    return repo, alias_sha


def _fake_rebase_onto_master(worktree, branch):
    # Simulate a successful Mode 9 rebase: rewrite the worktree HEAD (new
    # SHA) without changing which branch is checked out.
    _git("commit", "-q", "--allow-empty", "-m", "rebased onto master",
         cwd=worktree)
    return {"ok": True}


def _spy_on_git(monkeypatch):
    recorded = []
    real_run = subprocess.run

    def spy(cmd, *args, **kwargs):
        recorded.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(p.subprocess, "run", spy)
    return recorded


def _pushed_refspecs(push_call):
    idx = push_call.index("origin")
    return [a.lstrip("+").split(":")[0] for a in push_call[idx + 1:]]


def _write_manifest(plan_dir, plan_name, stories):
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}, indent=2)
    )


def _read_manifest(plan_dir, plan_name):
    return json.loads(
        (plan_dir / f"{plan_name}.manifest.json").read_text()
    )


def _story(worktree):
    return {
        "summary": "alias-head story ready to merge",
        "status": "pr_open",
        "review_verdict": "APPROVE",
        "risk": "low",
        "worktree": worktree,
    }


# ---------------------------------------------------------------------------
# 1. Structural: no hardcoded convention branch may reach the gate.
# ---------------------------------------------------------------------------
def test_advance_merge_gate_does_not_compute_or_pass_convention_branch():
    import inspect

    import pipeline.advance as adv

    src = inspect.getsource(adv)
    # The merge-adjudication block must not compute the convention branch
    # name at all - the resolver inside the gate is the single source of
    # truth, and a locally computed fallback is the exact mistake the
    # finding names.
    assert 'branch = f"agent/{key.lower()}"' not in src, (
        "advance.py still computes the hardcoded convention branch "
        "f\"agent/{key.lower()}\" in its merge-adjudication block; the gate "
        "must resolve the worktree HEAD itself (single source of truth)"
    )
    # The gate call must not receive a branch argument.
    gate_calls = [
        line.strip() for line in src.splitlines()
        if "_rebase_and_push_for_merge(" in line and "def " not in line
    ]
    assert gate_calls, "advance.py must call _rebase_and_push_for_merge"
    for line in gate_calls:
        assert not line.startswith("branch ="), line
        assert "branch," not in line.replace(
            "_rebase_and_push_for_merge(plan_name, key, branch, worktree)", ""
        ) or "branch" not in line.split("(", 1)[1].split(")", 1)[0], (
            f"gate call still passes a branch argument: {line!r}"
        )


# ---------------------------------------------------------------------------
# 2. Behavioural, follow-up-call shape: the tick must clear the gate on the
#    first call AND on the follow-up call, with the alias pushed both times.
# ---------------------------------------------------------------------------
def test_advance_tick_clears_merge_gate_on_alias_head_across_calls(
    plan_dir, tmp_path, monkeypatch,
):
    repo, alias_sha = _init_story_repo(tmp_path)
    recorded = _spy_on_git(monkeypatch)
    ci_calls = []

    def ci_once(branch, *, sha):
        ci_calls.append((branch, sha))
        return {"state": "success", "error": ""}

    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "_ci_status_once", ci_once)
    monkeypatch.setattr(
        p, "_ci_status",
        lambda *a, **k: pytest.fail(
            "_ci_status (blocking) called by advance tick"
        ),
    )
    monkeypatch.setattr(p, "_ci_rerun", lambda sha: None)
    monkeypatch.setattr(p, "REPO_ROOT", repo)
    monkeypatch.setattr(p, "_rebase_onto_master", _fake_rebase_onto_master)
    monkeypatch.setattr(p, "_default_branch", lambda: "master")
    monkeypatch.setattr(p, "_mcp_self_source_touched", lambda wt, base: "")
    monkeypatch.setattr(
        p, "_reverify_acceptance",
        lambda story, wt, key: {"state": "pass"},
    )
    monkeypatch.setattr(p, "_reverify_build", lambda wt: {"state": "pass"})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda *a, **k: None)
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
    if hasattr(p, "_maybe_record_retro"):
        monkeypatch.setattr(p, "_maybe_record_retro", lambda *a, **k: None)

    _write_manifest(plan_dir, "go", {"S10": _story(str(repo))})

    # --- Call 1: the tick must clear the gate (the incident parked here). ---
    _advance_pipeline_locked("go")
    man = _read_manifest(plan_dir, "go")
    story = man["stories"]["S10"]
    assert story["status"] == "done", (
        f"call 1 did not clear the merge gate: status={story['status']!r} "
        f"parked_reason={story.get('parked_reason')!r}"
    )
    pushed_sha_1 = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    assert pushed_sha_1 != alias_sha, (
        "fake rebase must rewrite the worktree HEAD for this test to be "
        "meaningful"
    )
    push_calls_1 = [c for c in recorded if c[:2] == ["git", "push"]]
    assert push_calls_1, "call 1 must push the rebased branch"
    assert _pushed_refspecs(push_calls_1[0]) == ["agent/s10-2"], (
        f"call 1 pushed {_pushed_refspecs(push_calls_1[0])}: the gate must "
        f"push the resolved alias agent/s10-2, not the hardcoded convention "
        f"branch agent/s10"
    )
    assert ci_calls, "call 1 must poll CI after pushing"
    assert ci_calls[-1][0] == "agent/s10-2", (
        f"call 1 polled CI on {ci_calls[-1][0]!r}: must poll the resolved "
        f"alias branch that was actually pushed"
    )
    assert ci_calls[-1][1] == pushed_sha_1
    origin_alias_1 = _git(
        "rev-parse", "origin/agent/s10-2", cwd=repo
    ).stdout.strip()
    assert origin_alias_1 == pushed_sha_1, (
        "(a) call 1's expected stored state: origin/agent/s10-2 must point "
        "at the SHA the gate pushed"
    )

    # --- Call 2: follow-up tick against the state call 1 left behind. ---
    recorded.clear()
    ci_calls.clear()
    # Re-open the story exactly as a rework round would leave it: pr_open,
    # approved, worktree still on the alias (now at the rebased SHA).
    man["stories"]["S10"].update({
        "status": "pr_open",
        "review_verdict": "APPROVE",
        "risk": "low",
        "worktree": str(repo),
    })
    man["stories"]["S10"].pop("parked_reason", None)
    man["stories"]["S10"].pop("ci_pending_sha", None)
    man["stories"]["S10"].pop("ci_rerun_attempted", None)
    _write_manifest(plan_dir, "go", man["stories"])

    _advance_pipeline_locked("go")
    man = _read_manifest(plan_dir, "go")
    story = man["stories"]["S10"]
    assert story["status"] == "done", (
        f"call 2 did not clear the merge gate: status={story['status']!r} "
        f"parked_reason={story.get('parked_reason')!r}"
    )
    pushed_sha_2 = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    assert pushed_sha_2 != pushed_sha_1, (
        "call 2's rebase must rewrite the worktree HEAD again"
    )
    push_calls_2 = [c for c in recorded if c[:2] == ["git", "push"]]
    assert len(push_calls_2) >= 2, (
        "call 2 must push the rebased branch again (follow-up call)"
    )
    assert _pushed_refspecs(push_calls_2[-1]) == ["agent/s10-2"], (
        f"call 2 pushed {_pushed_refspecs(push_calls_2[-1])}: the follow-up "
        f"call must still push the resolved alias, not a locally computed "
        f"convention fallback"
    )
    assert ci_calls[-1][0] == "agent/s10-2"
    assert ci_calls[-1][1] == pushed_sha_2, (
        "(c) call 2's expected return: CI must be polled on the freshly "
        "pushed post-rebase SHA"
    )
    origin_alias_2 = _git(
        "rev-parse", "origin/agent/s10-2", cwd=repo
    ).stdout.strip()
    assert origin_alias_2 == pushed_sha_2, (
        "origin/agent/s10-2 must track the follow-up call's pushed SHA"
    )