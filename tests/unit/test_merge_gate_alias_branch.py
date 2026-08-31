"""Regression tests: the merge gate must operate on the worktree's RESOLVED
HEAD branch, not the hardcoded convention branch ``agent/<key>``.

Live incident (LA-VERIFY rework-alias follow-up, 2026-08-31): a rework round
leaves the story worktree checked out on an alias branch
``agent/<key>-<suffix>`` (e.g. ``agent/s10-2``). ``_open_pr``/``_merge_pr``
were already fixed to resolve that alias via
``pipeline/pr.py::_resolve_story_branch``, but the merge gate was not:

  - ``pipeline/merge.py::_rebase_and_push_for_merge`` pushes
    ``git push --force-with-lease origin <branch>`` with the convention
    branch passed in from ``pipeline/advance.py`` (``branch =
    f"agent/{key.lower()}"``), and advance.py CI-checks that same convention
    branch at the worktree-HEAD SHA - while ``_merge_pr`` merges the
    RESOLVED alias branch.
  - The manual merge path (``pipeline/merge.py::_approve_merge_impl``)
    repeats the same convention-branch push/CI-check.

In the incident state the local convention branch had already been
squash-merged and deleted by a prior ``_merge_pr`` (``git branch -D``), so
the gate's push failed with ``src refspec agent/s10 does not match any`` and
the story parked at the merge gate forever - the tests_passed loop became a
merge-gate loop. If a stale local convention branch survived instead, the
gate pushed that stale code while ``_ci_status_once`` polled
``commits/<worktree-HEAD-sha>/check-runs`` - a SHA that was never pushed to
the polled branch - so CI stayed pending until expiry; and with
``PIPELINE_MERGE_CI_GATE=0`` the manual path's ``gh pr merge`` landed the
un-rebased remote alias, silently defeating the Mode 9 rebase gate.

These tests drive the gate against a REAL git repo (a bare ``origin`` plus a
story repo whose HEAD is the alias branch) so the exact refspec and failure
modes reproduce. They fail against the current code because the gate
pushes/CI-checks the hardcoded convention branch; they pass once the gate
resolves the worktree's actual HEAD branch (``_resolve_story_branch``) so
rebase -> push -> CI -> merge all operate on one branch.
"""

import contextlib
import inspect
import json
import subprocess

from pipeline import merge
import pipeline.server as p


# ---------------------------------------------------------------------------
# Real-git fixture helpers (self-contained; no mocking of git itself).
# ---------------------------------------------------------------------------
def _git(*args, cwd, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=check,
        capture_output=True, text=True,
    )


def _init_story_repo(tmp_path, *, delete_local_convention):
    """Create ``origin`` (bare) + a story repo with the incident branch topology.

    Leaves the repo HEAD on the alias branch ``agent/s10-2`` @ ALIAS_SHA with
    ``origin/agent/s10-2`` @ ALIAS_SHA, and:

      - ``delete_local_convention=False``: a STALE local ``agent/s10`` @
        CONV_LOCAL_SHA that is AHEAD of ``origin/agent/s10`` @ CONV_ORIGIN_SHA
        (unpushed stale code - the "stale twin" state);
      - ``delete_local_convention=True``: the local ``agent/s10`` branch
        deleted - exactly the state a prior ``_merge_pr`` ``git branch -D``
        leaves behind (the live-incident merge-gate state).

    Returns ``(repo, conv_origin_sha, conv_local_sha, alias_sha)``.
    """
    origin = tmp_path / "origin.git"
    _git("-c", "init.defaultBranch=master", "init", "--bare", "-q", str(origin),
         cwd=tmp_path)
    repo = tmp_path / "repo"
    _git("-c", "init.defaultBranch=master", "init", "-q", str(repo), cwd=tmp_path)
    _git("config", "user.email", "agent@local", cwd=repo)
    _git("config", "user.name", "agent", cwd=repo)

    (repo / "master.txt").write_text("m\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "m0", cwd=repo)
    _git("remote", "add", "origin", str(origin), cwd=repo)
    _git("push", "-q", "origin", "master", cwd=repo)

    # Convention branch: origin/agent/s10 @ conv_origin, then a stale local
    # commit ahead of it (code that was never pushed).
    _git("checkout", "-q", "-b", "agent/s10", cwd=repo)
    (repo / "conv.txt").write_text("conv-origin\n")
    _git("commit", "-q", "-a", "-m", "conv origin", cwd=repo)
    conv_origin_sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    _git("push", "-q", "origin", "agent/s10", cwd=repo)
    (repo / "conv.txt").write_text("conv-stale-local\n")
    _git("commit", "-q", "-a", "-m", "conv stale local", cwd=repo)
    conv_local_sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    # Alias branch the rework round left checked out in the worktree.
    _git("checkout", "-q", "master", cwd=repo)
    _git("checkout", "-q", "-b", "agent/s10-2", cwd=repo)
    (repo / "alias.txt").write_text("alias\n")
    _git("commit", "-q", "-a", "-m", "alias", cwd=repo)
    alias_sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    _git("push", "-q", "origin", "agent/s10-2", cwd=repo)

    if delete_local_convention:
        _git("branch", "-q", "-D", "agent/s10", cwd=repo)

    return repo, conv_origin_sha, conv_local_sha, alias_sha


def _fake_rebase_onto_master(worktree, branch):
    """Simulate a successful Mode 9 rebase: rewrite the worktree HEAD commit
    (new SHA) without changing which branch is checked out."""
    _git("commit", "-q", "--allow-empty", "-m", "rebased onto master",
         cwd=worktree)
    return {"ok": True}


def _spy_on_git(monkeypatch):
    """Record every subprocess command while still really running it."""
    recorded = []
    real_run = subprocess.run

    def spy(cmd, *args, **kwargs):
        recorded.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(merge.subprocess, "run", spy)
    return recorded


def _push_refspecs(recorded):
    return [c for c in recorded if c[:2] == ["git", "push"]]


def _call_rebase_and_push_for_merge(plan_name, key, branch, worktree):
    """Call the merge gate tolerating both fix shapes for the branch arg.

    The blocking fix may either keep
    ``_rebase_and_push_for_merge(plan_name, key, branch, worktree)`` (resolving
    the worktree HEAD internally and ignoring the passed convention branch) or
    drop the branch parameter entirely (advance.py stops passing it). Both
    shapes must satisfy the behavioural contract below, so dispatch on the
    actual signature instead of hard-coding one.
    """
    fn = merge._rebase_and_push_for_merge
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        params = {}
    if "branch" in params:
        return fn(plan_name, key, branch, worktree)
    return fn(plan_name, key, worktree)


# ---------------------------------------------------------------------------
# Direct gate tests: pipeline.merge._rebase_and_push_for_merge
# ---------------------------------------------------------------------------
def test_gate_pushes_resolved_alias_branch_not_stale_convention_branch(
    tmp_path, monkeypatch,
):
    repo, conv_origin_sha, conv_local_sha, alias_sha = _init_story_repo(
        tmp_path, delete_local_convention=False
    )
    recorded = _spy_on_git(monkeypatch)
    monkeypatch.setattr(p, "REPO_ROOT", repo)
    monkeypatch.setattr(p, "_rebase_onto_master", _fake_rebase_onto_master)
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(p, "_default_branch", lambda: "master")

    gate_error, pushed_sha = _call_rebase_and_push_for_merge(
        "go", "s10", "agent/s10", str(repo)
    )

    assert gate_error == "", gate_error
    post_rebase_sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    assert post_rebase_sha != alias_sha, (
        "fake rebase must rewrite the worktree HEAD for this test to be "
        "meaningful"
    )

    push_calls = _push_refspecs(recorded)
    assert push_calls, "the merge gate must push the rebased branch"
    assert "--force-with-lease" in push_calls[0]
    assert push_calls[0][-1] == "agent/s10-2", (
        f"gate pushed {push_calls[0][-1]!r}: it must push the worktree's "
        f"resolved HEAD branch agent/s10-2, not the hardcoded convention "
        f"branch agent/s10"
    )

    # The SHA the gate reports (and CI will poll) is the post-rebase
    # worktree-HEAD SHA that was actually pushed to the alias branch.
    assert pushed_sha == post_rebase_sha
    origin_alias_sha = _git(
        "rev-parse", "origin/agent/s10-2", cwd=repo
    ).stdout.strip()
    assert origin_alias_sha == post_rebase_sha, (
        "origin/agent/s10-2 must point at the post-rebase worktree-HEAD SHA "
        "so the CI poll queries a SHA that was actually pushed"
    )

    # The stale local convention branch (agent/s10 @ conv_local_sha, ahead of
    # origin) must NOT be pushed by the gate - pushing it would land
    # un-rebased stale code on the convention remote branch.
    origin_conv_sha = _git(
        "rev-parse", "origin/agent/s10", cwd=repo
    ).stdout.strip()
    assert origin_conv_sha == conv_origin_sha, (
        "the gate must never push the stale convention branch agent/s10"
    )


def test_gate_completes_when_convention_branch_already_deleted(
    tmp_path, monkeypatch,
):
    """The live-incident state: a prior _merge_pr already squash-merged and
    `git branch -D`-ed the local convention branch, leaving the worktree on
    the alias. The gate must still complete (no `src refspec` failure)."""
    repo, conv_origin_sha, conv_local_sha, alias_sha = _init_story_repo(
        tmp_path, delete_local_convention=True
    )
    recorded = _spy_on_git(monkeypatch)
    monkeypatch.setattr(p, "REPO_ROOT", repo)
    monkeypatch.setattr(p, "_rebase_onto_master", _fake_rebase_onto_master)
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(p, "_default_branch", lambda: "master")

    gate_error, pushed_sha = _call_rebase_and_push_for_merge(
        "go", "s10", "agent/s10", str(repo)
    )

    assert "src refspec" not in gate_error, (
        f"live-incident merge-gate failure reproduced: {gate_error!r} - the "
        f"gate pushed the convention branch agent/s10 that a prior _merge_pr "
        f"had already deleted instead of the worktree's alias HEAD"
    )
    assert gate_error == "", gate_error

    push_calls = _push_refspecs(recorded)
    assert push_calls, "the merge gate must push the rebased branch"
    assert push_calls[0][-1] == "agent/s10-2", (
        f"gate pushed {push_calls[0][-1]!r}: it must push the resolved alias "
        f"branch agent/s10-2"
    )

    post_rebase_sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    assert pushed_sha == post_rebase_sha
    origin_alias_sha = _git(
        "rev-parse", "origin/agent/s10-2", cwd=repo
    ).stdout.strip()
    assert origin_alias_sha == post_rebase_sha, (
        "origin/agent/s10-2 must point at the post-rebase worktree-HEAD SHA "
        "so the CI poll queries a SHA that was actually pushed"
    )


# ---------------------------------------------------------------------------
# Advance-level merge gate wiring (pipeline/advance.py merge adjudication).
# ---------------------------------------------------------------------------
def _write_manifest(plan_dir, plan_name, stories):
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        __import__("json").dumps({"epics": {}, "stories": stories}, indent=2)
    )


def _read_manifest(plan_dir, plan_name):
    import json

    return json.loads(
        (plan_dir / f"{plan_name}.manifest.json").read_text()
    )


def _story(worktree, **overrides):
    base = {
        "summary": "alias-head story ready to merge",
        "status": "pr_open",
        "review_verdict": "APPROVE",
        "risk": "low",
        "worktree": worktree,
    }
    base.update(overrides)
    return base


def test_advance_merge_gate_pushes_and_polls_ci_on_resolved_alias_branch(
    plan_dir, tmp_path, monkeypatch,
):
    """End-to-end: with the worktree HEAD on the alias and the local
    convention branch already deleted, the merge gate must rebase, push and
    CI-poll the RESOLVED alias branch (agent/s10-2) at the post-rebase SHA -
    not park the story on a `src refspec` push failure (the incident), not
    poll a SHA that was never pushed."""
    import json

    repo, conv_origin_sha, conv_local_sha, alias_sha = _init_story_repo(
        tmp_path, delete_local_convention=True
    )
    recorded = _spy_on_git(monkeypatch)
    ci_calls = []
    notify_msgs = []

    monkeypatch.setattr(p, "REPO_ROOT", repo)
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "_rebase_onto_master", _fake_rebase_onto_master)
    monkeypatch.setattr(p, "_default_branch", lambda: "master")
    monkeypatch.setattr(
        p, "_notify_user",
        lambda *a, **k: notify_msgs.append(a[1] if len(a) > 1 else str(a)),
    )

    def ci_once(branch, *, sha):
        ci_calls.append((branch, sha))
        return {"state": "success", "error": ""}

    monkeypatch.setattr(p, "_ci_status_once", ci_once)
    monkeypatch.setattr(
        p, "_ci_status",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("_ci_status (blocking) called by advance tick")
        ),
    )
    monkeypatch.setattr(p, "_ci_rerun", lambda sha: None)
    monkeypatch.setattr(
        p, "_reverify_acceptance",
        lambda story, worktree, key: {"state": "pass"},
    )
    monkeypatch.setattr(p, "_reverify_build", lambda worktree: {"state": "pass"})
    monkeypatch.setattr(p, "_mcp_self_source_touched", lambda wt, base: "")
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda *a, **k: None)
    monkeypatch.setattr(p, "_maybe_record_retro", lambda *a, **k: None)

    (plan_dir / "go.manifest.json").write_text(
        json.dumps(
            {"epics": {}, "stories": {"S10": _story(str(repo))}}, indent=2
        )
    )

    p._advance_pipeline_locked("go")

    man = _read_manifest(plan_dir, "go")
    story = man["stories"]["S10"]
    assert story["status"] == "done", (
        f"story did not clear the merge gate: status={story['status']!r} "
        f"parked_reason={story.get('parked_reason')!r} "
        f"merge_error={story.get('merge_error')!r} notify={notify_msgs!r}"
    )

    post_rebase_sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    push_calls = _push_refspecs(recorded)
    assert push_calls, "the merge gate must push the rebased branch"
    assert push_calls[0][-1] == "agent/s10-2", (
        f"gate pushed {push_calls[0][-1]!r}: advance.py must not feed the "
        f"hardcoded convention branch agent/s10 into the gate"
    )

    assert ci_calls, "the gate must poll CI after pushing"
    assert ci_calls[0][0] == "agent/s10-2", (
        f"CI polled branch {ci_calls[0][0]!r}: the merge gate must CI-check "
        f"the resolved alias branch agent/s10-2 that was actually pushed"
    )
    assert ci_calls[0][1] == post_rebase_sha, (
        f"CI polled sha {ci_calls[0][1]!r}: must be the post-rebase "
        f"worktree-HEAD sha that was actually pushed"
    )
    origin_alias_sha = _git(
        "rev-parse", "origin/agent/s10-2", cwd=repo
    ).stdout.strip()
    assert origin_alias_sha == post_rebase_sha


# ---------------------------------------------------------------------------
# Manual merge path: pipeline.merge._approve_merge_impl
# ---------------------------------------------------------------------------
def test_approve_merge_manual_path_pushes_and_ci_checks_resolved_alias_branch(
    plan_dir, tmp_path, monkeypatch,
):
    """The human-approved merge path must resolve the worktree's alias HEAD
    for its --force-with-lease push and CI check too (same bug, second site)."""
    import json

    repo, conv_origin_sha, conv_local_sha, alias_sha = _init_story_repo(
        tmp_path, delete_local_convention=True
    )
    recorded = _spy_on_git(monkeypatch)
    ci_calls = []

    monkeypatch.setattr(p, "REPO_ROOT", repo)
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "_rebase_onto_master", _fake_rebase_onto_master)
    monkeypatch.setattr(p, "_default_branch", lambda: "master")
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(p, "_scoped_repo_root", lambda plan_name:
                        contextlib.nullcontext())
    monkeypatch.setattr(p, "_store", p._store)  # real store; PLAN_DIR patched

    def ci_status(branch, *, sha):
        ci_calls.append((branch, sha))
        return {"state": "success", "error": ""}

    monkeypatch.setattr(p, "_ci_status", ci_status)
    monkeypatch.setattr(p, "_ci_rerun", lambda sha: None)
    monkeypatch.setattr(
        p, "_reverify_acceptance",
        lambda story, worktree, key: {"state": "pass"},
    )
    monkeypatch.setattr(p, "_reverify_build", lambda worktree: {"state": "pass"})
    monkeypatch.setattr(p, "_mcp_self_source_touched", lambda wt, base: "")
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda *a, **k: None)
    monkeypatch.setattr(p, "_maybe_record_retro", lambda *a, **k: None)

    (plan_dir / "go.manifest.json").write_text(
        json.dumps(
            {"epics": {}, "stories": {"S10": _story(str(repo))}}, indent=2
        )
    )

    result = merge._approve_merge_impl("go", "S10")

    assert result.get("ok") is True, (
        f"manual merge failed through the gate: {result!r}"
    )

    post_rebase_sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    push_calls = _push_refspecs(recorded)
    assert push_calls, "the manual merge path must push the rebased branch"
    assert push_calls[0][-1] == "agent/s10-2", (
        f"gate pushed {push_calls[0][-1]!r}: the manual merge path must push "
        f"the resolved alias branch agent/s10-2, not agent/s10"
    )

    assert ci_calls, "the manual merge path must CI-check the pushed branch"
    assert ci_calls[0][0] == "agent/s10-2", (
        f"CI checked branch {ci_calls[0][0]!r}: must be the resolved alias "
        f"branch agent/s10-2"
    )
    assert ci_calls[0][1] == post_rebase_sha, (
        f"CI checked sha {ci_calls[0][1]!r}: must be the post-rebase "
        f"worktree-HEAD sha that was actually pushed"
    )
    origin_alias_sha = _git(
        "rev-parse", "origin/agent/s10-2", cwd=repo
    ).stdout.strip()
    assert origin_alias_sha == post_rebase_sha