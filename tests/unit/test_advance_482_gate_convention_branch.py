"""Regression tests for the rework-round Blocking finding at
pipeline/advance.py:482 (line number from the earlier review's snapshot; the
merge-adjudication block has since shifted to roughly lines 555-593).

Recovered finding (verbatim from the earlier review thread):

    "Blocking: pipeline/advance.py:482: merge gate passes the hardcoded
    convention branch into ``_rebase_and_push_for_merge``, mismatching the
    alias branch ``_merge_pr`` merges."

The specific mistake: advance.py's merge-adjudication block computes the
hardcoded convention branch name ``agent/<key>`` and hands it to the merge
gate as the branch to rebase/push/CI-poll. A rework round leaves the story
worktree checked out on an alias branch ``agent/<key>-<suffix>`` (e.g.
``agent/s10-2``), and a prior ``_merge_pr`` squash-merges and
``git branch -D``-deletes the convention name -- so a gate fed the
convention name either fails with "src refspec agent/s10 does not match
any", or pushes a stale local twin while the CI poll queries a SHA that was
never pushed to the polled branch, and the branch the gate operated on is
not the branch ``_merge_pr`` merges.

The single source of truth must be the gate itself:
``_rebase_and_push_for_merge`` (pipeline/merge.py) already resolves the
worktree's ACTUAL HEAD branch internally and only degrades to the
caller-passed branch when there is no worktree to probe. So advance.py must
not compute or pass a convention branch at all. The current code evades the
existing literal check by laundering the same hardcoded name through the
``_convention_branch`` helper (advance.py:581, imported at advance.py:571)
and passes the locally computed ``branch`` into the gate (advance.py:593) --
the exact pass-through the finding names. advance.py's own comment
(advance.py:569-570) already states the contract this test pins: "none may
be added: a locally computed convention branch is the exact mistake the
round-2 review finding names."

Worked example (story S10, worktree HEAD on alias ``agent/s10-2``, local
``agent/s10`` already deleted by a prior ``_merge_pr``):

- Wrong (current code): a tick whose worktree probe degrades (missing or
  anomalous worktree, resolver fail-open) passes branch ``"agent/s10"``
  into ``_rebase_and_push_for_merge`` -> the gate pushes the deleted
  convention branch ("src refspec agent/s10 does not match any") or a
  stale twin, and the CI poll queries a SHA never pushed to it; the story
  loops in pr_open forever (the live LA-VERIFY incident, ~5h of ticks).
- Correct: the gate receives no locally computed branch (empty), resolves
  ``agent/s10-2`` from the worktree HEAD itself, pushes THAT branch, and
  the CI poll queries (agent/s10-2, pushed_sha).

Both tests follow the follow-up-call rule for stateful findings: the
worktree HEAD branch identity (and the deleted convention branch) persists
across ticks, so each test drives at least two advance ticks and asserts
the contract on the FOLLOW-UP call's arguments and the state it leaves
behind, not just the first call's return value.
"""

import inspect
import json
import subprocess
from pathlib import Path

import pytest

import pipeline.merge as merge_mod
import pipeline.server as p
from pipeline.server import _advance_pipeline_locked


# ---------------------------------------------------------------------------
# Real-git fixture (same topology as the round-1 regression tests): bare
# origin + story repo whose HEAD is the alias agent/s10-2, with the local
# convention branch agent/s10 already deleted by a prior _merge_pr.
# ---------------------------------------------------------------------------
def _git(*args, cwd, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=check,
        capture_output=True, text=True,
    )


def _init_story_repo(tmp_path):
    """origin with master + agent/s10 + agent/s10-2; local agent/s10 deleted."""
    repo = tmp_path / "story-repo"
    repo.mkdir()
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(origin)], check=True
    )
    _git("init", "-q", "-b", "master", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "f.txt").write_text("base\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "base", cwd=repo)
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

    # p.subprocess IS the global subprocess module, so this records every
    # git invocation in the process, including merge.py's push.
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


def _make_gate_spy(monkeypatch):
    """Record every branch argument handed to the merge gate.

    Delegates to the REAL pipeline/merge.py gate when the worktree exists
    (so push/CI identity is exercised end to end); with a missing worktree
    there is nothing to rebase or push, so it reports a successful no-op
    with a sentinel SHA, mirroring the existing test fakes' contract.
    """
    calls = []
    real_gate = merge_mod._rebase_and_push_for_merge

    def spy(plan_name, key, branch, worktree):
        calls.append({"key": key, "branch": branch, "worktree": worktree})
        if worktree and Path(worktree).is_dir():
            return real_gate(plan_name, key, branch, worktree)
        return ("", "deadbeefcafe")

    monkeypatch.setattr(p, "_rebase_and_push_for_merge", spy)
    return calls


def _stub_tick_env(plan_dir, repo, monkeypatch):
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
    return recorded, ci_calls


# ---------------------------------------------------------------------------
# 1. Structural: advance.py must not compute or pass the hardcoded
#    convention branch into the merge gate (the finding, made checkable).
#    The pre-existing structural test's gate-call clause is vacuous (its
#    `or` short-circuits), which is how the laundered _convention_branch
#    fallback slipped through; this one has no escape hatch.
# ---------------------------------------------------------------------------
def _gate_function_sources():
    adv = inspect.getmodule(_advance_pipeline_locked)
    import pipeline.advance as adv_mod

    out = []
    for name, fn in inspect.getmembers(adv_mod, inspect.isfunction):
        try:
            fn_src = inspect.getsource(fn)
        except (OSError, TypeError):
            continue
        if "_rebase_and_push_for_merge(" in fn_src:
            out.append((name, fn_src))
    return out


def test_advance_merge_gate_receives_no_hardcoded_convention_branch():
    import pipeline.advance as adv_mod

    src = inspect.getsource(adv_mod)
    # (a) The original hardcoded form is banned outright.
    assert 'f"agent/{key.lower()}"' not in src, (
        "advance.py still contains the hardcoded convention branch literal "
        'f"agent/{key.lower()}"; the merge gate must resolve the worktree '
        "HEAD itself (single source of truth)"
    )
    # (b) The laundered form: the convention-name helper must not be
    #     computed anywhere in the function housing the gate call.
    gate_fns = _gate_function_sources()
    assert gate_fns, (
        "advance.py must call _rebase_and_push_for_merge; the gate call "
        "moved or was renamed - re-point this test at it"
    )
    for name, fn_src in gate_fns:
        assert "_convention_branch" not in fn_src, (
            f"advance.py:{name} still computes the hardcoded convention "
            "branch via _convention_branch() and passes it into "
            "_rebase_and_push_for_merge - the exact pass-through the "
            "Blocking finding at advance.py:482 names; the gate (merge.py) "
            "must resolve the worktree HEAD itself"
        )
        # (c) No inline convention-name expression may reach the gate call
        #     either (catches f-strings/concatenation laundered inline).
        for line in fn_src.splitlines():
            if "_rebase_and_push_for_merge(" in line and "def " not in line:
                args = line.split("(", 1)[1].rsplit(")", 1)[0]
                assert "agent/" not in args, (
                    f"gate call passes an inline convention branch: {line!r}"
                )


# ---------------------------------------------------------------------------
# 2. Behavioural, follow-up-call shape: across repeated advance ticks the
#    gate must never be handed the hardcoded convention branch, the alias
#    must be pushed, and the CI poll must query the freshly pushed SHA.
# ---------------------------------------------------------------------------
def test_advance_tick_never_feeds_convention_branch_to_gate_across_calls(
    plan_dir, tmp_path, monkeypatch,
):
    repo, alias_sha = _init_story_repo(tmp_path)
    recorded, ci_calls = _stub_tick_env(plan_dir, repo, monkeypatch)
    gate_calls = _make_gate_spy(monkeypatch)
    convention = "agent/s10"
    alias = "agent/s10-2"

    _write_manifest(plan_dir, "go", {"S10": _story(str(repo))})

    # --- Call 1: the tick must clear the merge gate on the alias head. ---
    _advance_pipeline_locked("go")
    man = _read_manifest(plan_dir, "go")
    story = man["stories"]["S10"]
    assert story["status"] == "done", (
        f"call 1 did not clear the merge gate: status={story['status']!r} "
        f"parked_reason={story.get('parked_reason')!r}"
    )
    assert gate_calls, "call 1 must invoke the merge gate"
    assert gate_calls[0]["branch"] != convention, (
        f"call 1 passed branch {gate_calls[0]['branch']!r} into "
        f"_rebase_and_push_for_merge: the gate must never receive the "
        f"hardcoded convention branch {convention!r} (the finding's "
        f"pass-through); it gets no locally computed branch at all"
    )
    pushed_sha_1 = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    assert pushed_sha_1 != alias_sha, (
        "fake rebase must rewrite the worktree HEAD for this test to be "
        "meaningful"
    )
    push_calls_1 = [c for c in recorded if c[:2] == ["git", "push"]]
    assert push_calls_1, "call 1 must push the rebased branch"
    assert _pushed_refspecs(push_calls_1[0]) == [alias], (
        f"call 1 pushed {_pushed_refspecs(push_calls_1[0])}: the gate must "
        f"push the resolved alias {alias}, not the convention branch"
    )
    assert ci_calls and ci_calls[-1] == (alias, pushed_sha_1), (
        f"call 1 polled CI as {ci_calls[-1] if ci_calls else None!r}: must "
        f"poll ({alias!r}, pushed_sha)"
    )
    origin_alias_1 = _git("rev-parse", f"origin/{alias}", cwd=repo).stdout.strip()
    assert origin_alias_1 == pushed_sha_1, (
        "call 1's expected stored state: origin/agent/s10-2 must point at "
        "the SHA the gate pushed"
    )

    # --- Call 2: follow-up tick against the state call 1 left behind. ---
    recorded.clear()
    ci_calls.clear()
    n_gate_calls_1 = len(gate_calls)
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
    assert len(gate_calls) > n_gate_calls_1, (
        "call 2 must invoke the merge gate again (follow-up call)"
    )
    assert gate_calls[-1]["branch"] != convention, (
        f"follow-up call passed branch {gate_calls[-1]['branch']!r} into "
        f"the gate: a fix that resolves on call 1 but degrades to the "
        f"hardcoded convention branch {convention!r} on the follow-up call "
        f"is the exact stateful mistake the finding describes"
    )
    pushed_sha_2 = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    assert pushed_sha_2 != pushed_sha_1, (
        "call 2's rebase must rewrite the worktree HEAD again"
    )
    push_calls_2 = [c for c in recorded if c[:2] == ["git", "push"]]
    assert push_calls_2, "call 2 must push the rebased branch again"
    assert _pushed_refspecs(push_calls_2[-1]) == [alias], (
        f"call 2 pushed {_pushed_refspecs(push_calls_2[-1])}: the follow-up "
        f"call must still push the resolved alias {alias}"
    )
    assert ci_calls and ci_calls[-1] == (alias, pushed_sha_2), (
        f"call 2 polled CI as {ci_calls[-1] if ci_calls else None!r}: must "
        f"poll the freshly pushed post-rebase SHA on {alias!r}"
    )
    origin_alias_2 = _git("rev-parse", f"origin/{alias}", cwd=repo).stdout.strip()
    assert origin_alias_2 == pushed_sha_2, (
        "origin/agent/s10-2 must track the follow-up call's pushed SHA"
    )

    # --- Call 3: the degraded-worktree tick (the incident's fail-open
    #     path). Even with no worktree to probe, the gate must NEVER be
    #     handed the hardcoded convention branch. RED today: advance.py's
    #     fallback passes "agent/s10" into the gate.
    n_gate_calls_2 = len(gate_calls)
    man["stories"]["S10"].update({
        "status": "pr_open",
        "review_verdict": "APPROVE",
        "risk": "low",
        "worktree": str(tmp_path / "gone-worktree"),
    })
    man["stories"]["S10"].pop("parked_reason", None)
    man["stories"]["S10"].pop("ci_pending_sha", None)
    man["stories"]["S10"].pop("ci_rerun_attempted", None)
    _write_manifest(plan_dir, "go", man["stories"])

    _advance_pipeline_locked("go")
    new_calls = gate_calls[n_gate_calls_2:]
    for c in new_calls:
        assert c["branch"] != convention, (
            f"degraded-worktree tick passed the hardcoded convention branch "
            f"{convention!r} into _rebase_and_push_for_merge (branch arg "
            f"{c['branch']!r}): the exact pass-through the Blocking finding "
            f"at advance.py:482 names - with the convention branch deleted "
            f"by a prior _merge_pr this push fails with 'src refspec "
            f"agent/s10 does not match any' or pushes a stale twin while CI "
            f"polls a never-pushed SHA"
        )