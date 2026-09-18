"""Tests for the pipeline MCP server: logging hygiene, Plane-unconfigured behavior, and the TicketProvider abstraction.

Split out of test_pipeline_mcp_server.py to keep it under the project's line-count target; shared fixtures/helpers moved to tests.unit._pipeline_mcp_server_test_helpers.
"""
import fcntl
import inspect
import json
import os

import pytest

from pipeline import pr as pr_mod
from pipeline import server as p
from pipeline import ticketing as pt
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _clear_caches,
    _explode_plane,
    _fake_plane,
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _plane_configured,
    _plane_disabled,
    _read_manifest,
    _story,
    agents_dir,
    plan_dir,
)

# ---------- ingest_plan re-ingest merges instead of clobbering (T1) ----------
# Re-running ingest_plan with only_epics scoped to a newly-added epic used to
# replace the whole manifest, silently deleting every previously-ingested
# story's status/pr_url/history (2026-07-07 web-client-epic retro, incident
# #2: 43 tracked stories -> 10 after one only_epics call).

def test_ingest_plan_reingest_preserves_stories_from_untouched_epics(
    _plane_disabled, plan_dir, monkeypatch, tmp_path,
):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [
            {"summary": "E1", "stories": [_story(key="S1")]},
            {"summary": "E2", "stories": [_story(key="S2")]},
        ],
    }
    (plan_dir / "reingest.json").write_text(json.dumps(plan))

    first = p.ingest_plan("reingest")
    assert first["ok"] is True
    manifest = _read_manifest(plan_dir, "reingest")
    # Simulate real progress recorded against S1 by later pipeline activity.
    manifest["stories"]["S1"]["status"] = "done"
    manifest["stories"]["S1"]["pr_url"] = "https://example.com/pr/48"
    (plan_dir / "reingest.manifest.json").write_text(json.dumps(manifest))

    result = p.ingest_plan("reingest", only_epics=["E2"])

    assert result["ok"] is True
    merged = _read_manifest(plan_dir, "reingest")
    assert merged["stories"]["S1"]["status"] == "done"
    assert merged["stories"]["S1"]["pr_url"] == "https://example.com/pr/48"
    assert "S2" in merged["stories"]


def test_ingest_plan_reingest_refreshes_authored_fields_preserves_runtime_status(
    _plane_disabled, plan_dir, monkeypatch, tmp_path,
):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{"summary": "E1", "stories": [
            _story(key="S1", agent_instructions="Build v1."),
        ]}],
    }
    (plan_dir / "refresh.json").write_text(json.dumps(plan))
    p.ingest_plan("refresh")
    manifest = _read_manifest(plan_dir, "refresh")
    manifest["stories"]["S1"]["status"] = "done"
    manifest["stories"]["S1"]["pr_url"] = "https://example.com/pr/1"
    (plan_dir / "refresh.manifest.json").write_text(json.dumps(manifest))

    plan["epics"][0]["stories"][0]["agent_instructions"] = "Build v2, with edge cases."
    (plan_dir / "refresh.json").write_text(json.dumps(plan))
    result = p.ingest_plan("refresh")

    assert result["ok"] is True
    merged = _read_manifest(plan_dir, "refresh")
    assert merged["stories"]["S1"]["agent_instructions"] == "Build v2, with edge cases."
    assert merged["stories"]["S1"]["status"] == "done"
    assert merged["stories"]["S1"]["pr_url"] == "https://example.com/pr/1"


def test_ingest_plan_overwrite_true_drops_untouched_epics(
    _plane_disabled, plan_dir, monkeypatch, tmp_path,
):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [
            {"summary": "E1", "stories": [_story(key="S1")]},
            {"summary": "E2", "stories": [_story(key="S2")]},
        ],
    }
    (plan_dir / "ovr.json").write_text(json.dumps(plan))
    p.ingest_plan("ovr")

    result = p.ingest_plan("ovr", only_epics=["E2"], overwrite=True)

    assert result["ok"] is True
    manifest = _read_manifest(plan_dir, "ovr")
    assert "S1" not in manifest["stories"]
    assert "S2" in manifest["stories"]


def test_ingest_plan_reingest_preserves_top_level_paused_and_fallback_fields(
    plan_dir, monkeypatch, tmp_path,
):
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{"summary": "E1", "stories": [_story(key="S1")]}],
    }
    (plan_dir / "topkeys.json").write_text(json.dumps(plan))
    p.ingest_plan("topkeys")
    manifest = _read_manifest(plan_dir, "topkeys")
    manifest["paused"] = True
    manifest["local_model_fallback"] = "glm-5.2:cloud"
    (plan_dir / "topkeys.manifest.json").write_text(json.dumps(manifest))

    result = p.ingest_plan("topkeys")

    assert result["ok"] is True
    merged = _read_manifest(plan_dir, "topkeys")
    assert merged["paused"] is True
    assert merged["local_model_fallback"] == "glm-5.2:cloud"


def test_ingest_plan_skips_when_lock_held(plan_dir, monkeypatch, tmp_path):
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{"summary": "E1", "stories": [_story(key="S1")]}],
    }
    (plan_dir / "ilk2.json").write_text(json.dumps(plan))

    def _boom(*a, **kw):
        raise AssertionError("a locked-out ingest_plan must not touch Plane or the manifest")
    monkeypatch.setattr(pt, "plane_request", _boom)

    lock_path = plan_dir / "ilk2.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = p.ingest_plan("ilk2")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert result["ok"] is True
    assert result.get("skipped") == "locked"
    assert not (plan_dir / "ilk2.manifest.json").exists()


# ---------- Review gate + auto-PR ----------


def test_open_pr_pushes_branch_before_creating_pr(monkeypatch, tmp_path):
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        class Result:
            stdout = "https://gh/pr/1\n"
            returncode = 0
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    url = p._open_pr(str(tmp_path), "S1", {"summary": "Add thing"})

    assert url == "https://gh/pr/1"
    push_calls = [c for c in calls if c[:2] == ["git", "push"]]
    pr_calls = [c for c in calls if c[:2] == ["gh", "pr"]]
    assert push_calls, "expected the branch to be pushed before opening a PR"
    assert calls.index(push_calls[0]) < calls.index(pr_calls[0])
    assert "agent/s1" in push_calls[0]


def test_open_pr_force_pushes_with_lease_to_survive_a_prior_rebase(monkeypatch, tmp_path):
    """A story's agent/<key> branch is rebased onto origin/master before every
    resumed dispatch (see _rebase_onto_master / the "worktree base predates
    origin/master" notification path). If review_story already pushed once
    for an earlier PR (e.g. review APPROVEd, PR opened, then a later rework
    round rebased the branch again), the rebase rewrites local commit SHAs so
    they diverge from what's already on the remote. A plain (non-force) `git
    push` is then rejected as non-fast-forward every single time review_story
    retries - reproduced live 2026-08-21 on story 24cfe47f: 100+ identical
    "review APPROVEd but could not open PR (CalledProcessError)" notifications
    over 2+ hours, the story permanently stuck at tests_passed. Since this
    branch is exclusively owned by the pipeline's own dispatched agent (no
    external pusher to race), a force-with-lease push - the same pattern
    already used by _rebase_and_push_for_merge - is safe and must be used
    here too so a legitimately-rebased branch can still be pushed."""
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        class Result:
            stdout = "https://gh/pr/1\n"
            returncode = 0
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    p._open_pr(str(tmp_path), "S1", {"summary": "Add thing"})

    push_calls = [c for c in calls if c[:2] == ["git", "push"]]
    assert push_calls, "expected the branch to be pushed before opening a PR"
    assert "--force-with-lease" in push_calls[0]


def test_open_pr_reuses_existing_pr_when_one_already_exists(monkeypatch, tmp_path):
    """A dispatched agent may have already run `gh pr create` itself before
    review_story gets to it. _open_pr must recover the existing PR's URL
    instead of bubbling up gh's "already exists" failure."""
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "create"]:
            raise p.subprocess.CalledProcessError(
                1, cmd,
                output="",
                stderr=(
                    'a pull request for branch "agent/s1" into branch "main" '
                    "already exists:\nhttps://github.com/org/repo/pull/4\n"
                ),
            )
        class Result:
            stdout = "https://github.com/org/repo/pull/4\n"
            returncode = 0
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    url = p._open_pr(str(tmp_path), "S1", {"summary": "Add thing"})

    assert url == "https://github.com/org/repo/pull/4"
    view_calls = [c for c in calls if c[:2] == ["gh", "pr"] and "view" in c]
    assert view_calls, "expected a fallback `gh pr view` lookup"


def test_open_pr_reraises_other_gh_pr_create_failures(monkeypatch, tmp_path):
    def _fake_run(cmd, **kwargs):
        if cmd[:3] == ["gh", "pr", "create"]:
            raise p.subprocess.CalledProcessError(
                1, cmd, output="", stderr="some other failure",
            )
        class Result:
            stdout = ""
            returncode = 0
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    with pytest.raises(p.subprocess.CalledProcessError):
        p._open_pr(str(tmp_path), "S1", {"summary": "Add thing"})


# ---------- Story branch alias resolution (rework suffix branches) ----------
# When a story's worktree is checked out on an alias branch created during
# rework (agent/<key>-<suffix>, e.g. agent/la-verify-followup), _open_pr must
# push and open the PR from THAT branch instead of the convention name
# agent/<key> - otherwise it pushes a stale/already-merged branch, `gh pr
# create` fails with "No commits between master and agent/<key>", and the
# story silently loops in tests_passed forever (live failure 2026-08-31,
# LA-VERIFY, ~5h of APPROVE-with-no-PR ticks). _merge_pr must resolve the
# same branch so the squash-merge, the local `git branch -D`, and the
# `git push origin --delete` all operate on the branch _open_pr pushed;
# pushing one branch and merging another would strand the PR.


def _stub_run_recording_head(head, *, revparse_returncode=0):
    """subprocess.run stub in the style of the _open_pr tests above: records
    every non-rev-parse command (and succeeds); answers
    `git rev-parse --abbrev-ref HEAD` with `head`, or with a failing
    returncode when revparse_returncode != 0."""
    calls = []

    def _fake_run(cmd, **kwargs):
        if cmd[:2] == ["git", "rev-parse"]:
            if revparse_returncode != 0:
                class Failed:
                    stdout = ""
                    stderr = "fatal: not a git repository"
                    returncode = revparse_returncode
                return Failed()
            class Result:
                stdout = head
                returncode = 0
            return Result()
        calls.append(cmd)

        class Ok:
            stdout = "https://gh/pr/1\n"
            returncode = 0
        return Ok()

    return calls, _fake_run


def test_open_pr_pushes_and_creates_pr_from_rework_alias_branch(
    monkeypatch, tmp_path,
):
    calls, _fake_run = _stub_run_recording_head("agent/s1-followup\n")
    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    p._open_pr(str(tmp_path), "S1", {"summary": "Add thing"})

    push_calls = [c for c in calls if c[:2] == ["git", "push"]]
    create_calls = [c for c in calls if c[:3] == ["gh", "pr", "create"]]
    assert push_calls, "expected the branch to be pushed before opening a PR"
    assert create_calls, "expected `gh pr create` to run"
    assert push_calls[0][-1] == "agent/s1-followup", (
        "push must target the worktree's actual HEAD branch (the rework "
        "alias), not the stale convention branch agent/s1"
    )
    head_idx = create_calls[0].index("--head")
    assert create_calls[0][head_idx + 1] == "agent/s1-followup", (
        "`gh pr create --head` must name the rework alias branch"
    )


def test_open_pr_keeps_convention_branch_when_head_is_already_on_it(
    monkeypatch, tmp_path,
):
    calls, _fake_run = _stub_run_recording_head("agent/s1\n")
    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    p._open_pr(str(tmp_path), "S1", {"summary": "Add thing"})

    push_calls = [c for c in calls if c[:2] == ["git", "push"]]
    create_calls = [c for c in calls if c[:3] == ["gh", "pr", "create"]]
    assert push_calls, "expected the branch to be pushed before opening a PR"
    assert push_calls[0][-1] == "agent/s1"
    head_idx = create_calls[0].index("--head")
    assert create_calls[0][head_idx + 1] == "agent/s1"


def test_open_pr_falls_back_to_convention_branch_when_rev_parse_fails(
    monkeypatch, tmp_path,
):
    calls, _fake_run = _stub_run_recording_head("", revparse_returncode=128)
    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    p._open_pr(str(tmp_path), "S1", {"summary": "Add thing"})

    push_calls = [c for c in calls if c[:2] == ["git", "push"]]
    create_calls = [c for c in calls if c[:3] == ["gh", "pr", "create"]]
    assert push_calls, "expected the fallback branch to still be pushed"
    assert push_calls[0][-1] == "agent/s1"
    head_idx = create_calls[0].index("--head")
    assert create_calls[0][head_idx + 1] == "agent/s1"


def test_open_pr_falls_back_to_convention_branch_when_head_is_detached(
    monkeypatch, tmp_path,
):
    calls, _fake_run = _stub_run_recording_head("HEAD\n")
    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    p._open_pr(str(tmp_path), "S1", {"summary": "Add thing"})

    push_calls = [c for c in calls if c[:2] == ["git", "push"]]
    create_calls = [c for c in calls if c[:3] == ["gh", "pr", "create"]]
    assert push_calls, "expected the fallback branch to still be pushed"
    assert push_calls[0][-1] == "agent/s1"
    head_idx = create_calls[0].index("--head")
    assert create_calls[0][head_idx + 1] == "agent/s1"


def test_merge_pr_merges_and_cleans_up_the_same_rework_alias_branch(
    monkeypatch, tmp_path,
):
    calls, _fake_run = _stub_run_recording_head("agent/s1-followup\n")
    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "REPO_ROOT", str(tmp_path / "repo"))
    result = p._merge_pr(str(tmp_path / "wt"), "S1")

    assert result == "https://gh/pr/1"
    merge_calls = [c for c in calls if c[:3] == ["gh", "pr", "merge"]]
    assert merge_calls, "expected a `gh pr merge` call"
    assert merge_calls[0][:4] == ["gh", "pr", "merge", "agent/s1-followup"]
    assert "--squash" in merge_calls[0]

    branch_d = [c for c in calls if c[:2] == ["git", "branch"]]
    assert branch_d, "expected the local branch cleanup `git branch -D`"
    assert branch_d[0] == ["git", "branch", "-D", "agent/s1-followup"]

    remote_del = [c for c in calls if c[:2] == ["git", "push"] and "--delete" in c]
    assert remote_del, "expected `git push origin --delete` cleanup"
    assert remote_del[0][-1] == "agent/s1-followup"

    worktree_rm = [c for c in calls if c[:2] == ["git", "worktree"]]
    assert worktree_rm, "worktree cleanup must still happen"

    # One branch end-to-end: pushing agent/s1 but merging agent/s1-followup
    # (or vice versa) would strand the PR and skip its cleanup.
    assert (
        merge_calls[0][3] == branch_d[0][3] == remote_del[0][-1]
        == "agent/s1-followup"
    )


def _stub_run_with_pr_title(title, *, title_returncode=0, head="agent/s1\n"):
    """subprocess.run stub answering `git rev-parse --abbrev-ref HEAD` with
    `head` and `gh pr view ... --json title` with `title`/`title_returncode`,
    recording every non-rev-parse command."""
    calls = []

    def _fake_run(cmd, **kwargs):
        if cmd[:2] == ["git", "rev-parse"]:
            class Head:
                stdout = head
                returncode = 0
            return Head()
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "view"]:
            class Title:
                stdout = title
                stderr = "" if title_returncode == 0 else "no pull requests found"
                returncode = title_returncode
            return Title()

        class Ok:
            stdout = "https://gh/pr/1\n"
            returncode = 0
        return Ok()

    return calls, _fake_run


def test_merge_pr_passes_pr_title_as_explicit_squash_subject(
    monkeypatch, tmp_path,
):
    """Without --subject, `gh pr merge --squash` can fall back to the branch's
    own last commit message (observed live 2026-09-17, PR #821: a step-cap
    "WIP (step cap reached)" checkpoint became master's permanent title). The
    PR's own title must be read and passed explicitly."""
    calls, _fake_run = _stub_run_with_pr_title("S1: Add thing\n")
    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "REPO_ROOT", str(tmp_path / "repo"))
    result = p._merge_pr(str(tmp_path / "wt"), "S1")

    view_calls = [c for c in calls if c[:3] == ["gh", "pr", "view"]]
    assert view_calls == [
        ["gh", "pr", "view", "agent/s1", "--json", "title", "-q", ".title"],
    ], "the PR's own title must be read before merging"
    merge_idx = next(
        i for i, c in enumerate(calls) if c[:3] == ["gh", "pr", "merge"]
    )
    assert calls.index(view_calls[0]) < merge_idx, (
        "the title lookup must run before the merge subprocess call"
    )

    merge_calls = [c for c in calls if c[:3] == ["gh", "pr", "merge"]]
    assert merge_calls == [[
        "gh", "pr", "merge", "agent/s1", "--squash",
        "--subject", "S1: Add thing",
    ]]

    # Return value and cleanup side effects are unaffected by the new flag.
    assert result == "https://gh/pr/1"
    assert [c for c in calls if c[:2] == ["git", "worktree"]], (
        "worktree cleanup must still happen"
    )
    assert [c for c in calls if c[:2] == ["git", "branch"]] == [
        ["git", "branch", "-D", "agent/s1"],
    ]


def test_merge_pr_omits_subject_when_title_lookup_fails(monkeypatch, tmp_path):
    """A failed `gh pr view` must never block a merge that would otherwise
    have succeeded: fall back to today's exact argv (no --subject at all)."""
    calls, _fake_run = _stub_run_with_pr_title("", title_returncode=1)
    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "REPO_ROOT", str(tmp_path / "repo"))
    result = p._merge_pr(str(tmp_path / "wt"), "S1")

    merge_calls = [c for c in calls if c[:3] == ["gh", "pr", "merge"]]
    assert merge_calls == [["gh", "pr", "merge", "agent/s1", "--squash"]], (
        "a failed title lookup must not block the merge nor invent a subject"
    )
    assert result == "https://gh/pr/1"


@pytest.mark.parametrize("blank", ["", "   \n"])
def test_merge_pr_omits_subject_when_title_is_blank(
    monkeypatch, tmp_path, blank,
):
    """An empty/whitespace-only title must degrade to no --subject, never to
    `--subject ""` (which would blank out master's commit title)."""
    calls, _fake_run = _stub_run_with_pr_title(blank)
    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "REPO_ROOT", str(tmp_path / "repo"))
    result = p._merge_pr(str(tmp_path / "wt"), "S1")

    merge_calls = [c for c in calls if c[:3] == ["gh", "pr", "merge"]]
    assert merge_calls == [["gh", "pr", "merge", "agent/s1", "--squash"]]
    assert "--subject" not in merge_calls[0]
    assert result == "https://gh/pr/1"


def test_resolve_story_branch_helper_contract_in_pipeline_pr_module():
    """The resolver must be a module-level helper in pipeline/pr.py, both
    _open_pr and _merge_pr must route through it (the old inline
    f"agent/{story_key.lower()}" computation must be gone from both), the
    'Tests mock this function' docstring contract stays intact, and server
    keeps re-exporting the seams so patch-via-p.<name> still lands."""
    assert callable(pr_mod._resolve_story_branch)
    module_src = inspect.getsource(pr_mod)
    assert "def _resolve_story_branch(" in module_src
    # Regression guard (LA-VERIFY, 2026-08-31): an earlier draft of the
    # resolver left a stray top-level `import logging` in pipeline/pr.py -
    # dead weight that also broke the module's "no logging" contract the
    # pipeline relies on for its quiet CLI surface. This assertion pins that
    # pr.py stays free of module-level logging imports.
    assert "import logging" not in module_src

    open_src = inspect.getsource(pr_mod._open_pr)
    merge_src = inspect.getsource(pr_mod._merge_pr)
    assert "_resolve_story_branch(" in open_src
    assert "_resolve_story_branch(" in merge_src
    assert 'f"agent/{story_key.lower()}"' not in open_src
    assert 'f"agent/{story_key.lower()}"' not in merge_src

    assert "Tests mock this function" in (pr_mod._open_pr.__doc__ or "")
    assert "Tests mock this function" in (pr_mod._merge_pr.__doc__ or "")

    assert p._open_pr is pr_mod._open_pr
    assert p._merge_pr is pr_mod._merge_pr


def test_resolve_story_branch_returns_alias_and_runs_rev_parse_in_worktree(
    monkeypatch, tmp_path,
):
    seen = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs

        class Result:
            stdout = "agent/s1-followup\n"
            returncode = 0
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    assert pr_mod._resolve_story_branch(str(tmp_path), "S1") == "agent/s1-followup"
    assert seen["cmd"] == ["git", "rev-parse", "--abbrev-ref", "HEAD"]
    assert str(seen["kwargs"].get("cwd")) == str(tmp_path)
    assert seen["kwargs"].get("capture_output") is True
    assert seen["kwargs"].get("text") is True


def test_resolve_story_branch_returns_convention_branch_when_head_is_on_it(
    monkeypatch, tmp_path,
):
    def _fake_run(cmd, **kwargs):
        class Result:
            stdout = "agent/s1\n"
            returncode = 0
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    assert pr_mod._resolve_story_branch(str(tmp_path), "S1") == "agent/s1"


def test_resolve_story_branch_falls_back_when_rev_parse_fails(
    monkeypatch, tmp_path,
):
    def _fake_run(cmd, **kwargs):
        class Failed:
            stdout = ""
            stderr = "fatal: not a git repository"
            returncode = 128
        return Failed()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    assert pr_mod._resolve_story_branch(str(tmp_path), "S1") == "agent/s1"


def test_resolve_story_branch_falls_back_when_head_is_detached(
    monkeypatch, tmp_path,
):
    def _fake_run(cmd, **kwargs):
        class Result:
            stdout = "HEAD\n"
            returncode = 0
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    assert pr_mod._resolve_story_branch(str(tmp_path), "S1") == "agent/s1"


def test_resolve_story_branch_falls_back_when_rev_parse_stdout_is_empty(
    monkeypatch, tmp_path,
):
    def _fake_run(cmd, **kwargs):
        class Result:
            stdout = ""
            returncode = 0
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    assert pr_mod._resolve_story_branch(str(tmp_path), "S1") == "agent/s1"


def test_resolve_story_branch_requires_dash_suffix_not_bare_prefix(
    monkeypatch, tmp_path,
):
    """'agent/s10' shares the 'agent/s1' prefix but is a different branch;
    only the convention name followed by '-' marks a rework alias."""
    def _fake_run(cmd, **kwargs):
        class Result:
            stdout = "agent/s10\n"
            returncode = 0
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    assert pr_mod._resolve_story_branch(str(tmp_path), "S1") == "agent/s1"


def test_resolve_story_branch_resolves_multiword_key_alias_from_live_incident(
    monkeypatch, tmp_path,
):
    """2026-08-31 LA-VERIFY: the worktree sat on agent/la-verify-followup
    while _open_pr kept pushing agent/la-verify."""
    def _fake_run(cmd, **kwargs):
        class Result:
            stdout = "agent/la-verify-followup\n"
            returncode = 0
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    assert (
        pr_mod._resolve_story_branch(str(tmp_path), "LA-VERIFY")
        == "agent/la-verify-followup"
    )


def test_open_pr_routes_branch_through_resolve_story_branch_helper(
    monkeypatch, tmp_path,
):
    seen = []
    calls, _fake_run = _stub_run_recording_head("agent/s1\n")

    def _fake_resolve(wt, key):
        seen.append((wt, key))
        return "agent/s1-alias"

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(pr_mod, "_resolve_story_branch", _fake_resolve)
    p._open_pr(str(tmp_path), "S1", {"summary": "Add thing"})

    assert seen == [(str(tmp_path), "S1")]
    push_calls = [c for c in calls if c[:2] == ["git", "push"]]
    assert push_calls, "expected the resolved branch to be pushed"
    assert push_calls[0][-1] == "agent/s1-alias"


def test_parse_verdict_variants():
    assert p._parse_verdict("...\nVERDICT: APPROVE\n") == "APPROVE"
    assert p._parse_verdict("VERDICT: REQUEST_CHANGES") == "REQUEST_CHANGES"
    assert p._parse_verdict("no verdict here") == "UNKNOWN"


def test_parse_verdict_recognizes_approve_with_fix():
    """APPROVE_WITH_FIX must parse as its own distinct verdict, not collapse
    into bare APPROVE via prefix-matching in the regex alternation (ordering
    matters: APPROVE_WITH_FIX must be tried before the bare APPROVE
    alternative, or a naive alternation matches "APPROVE" as a substring of
    "APPROVE_WITH_FIX" and silently drops the _WITH_FIX distinction)."""
    assert p._parse_verdict("...\nVERDICT: APPROVE_WITH_FIX\n") == "APPROVE_WITH_FIX"
    # Bare APPROVE must still parse as plain APPROVE, not get upgraded.
    assert p._parse_verdict("VERDICT: APPROVE") == "APPROVE"


def test_run_reviewer_prompt_asks_reviewer_to_flag_missing_documentation(
    agents_dir, monkeypatch,
):
    """The reviewer rubric must explicitly ask whether a user-visible change
    needs a documentation update, not just correctness/mutation/validation -
    otherwise a story can cleanly pass review and merge while silently
    missing the README update CLAUDE.md's Definition of Done requires
    (observed: REVIEW-LOG/MODEL-TUNING-TABLE/GPTOSS-TEMP03 merged without
    it; only REVIEW-UNKNOWN got documented, because that story's own
    agent_instructions happened to ask for it explicitly)."""
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, **kwargs):
            captured["prompt"] = prompt
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())

    p._run_reviewer("/tmp/some-worktree", "agent/some-branch")

    prompt = captured["prompt"]
    assert "documentation" in prompt.lower()
    assert "README" in prompt


def test_run_reviewer_prompt_does_not_block_on_docs_for_brand_new_code(
    agents_dir, monkeypatch,
):
    """The documentation check (see the test above) must not fire as a
    blocker for a brand-new addition nothing else in the repo calls yet --
    only for behavior EXISTING callers/users already depend on. Without this
    distinction, every new-module story (the common case for early-stage
    work) burns a full extra dispatch+rework+re-review cycle on a doc nit
    CLAUDE.md's own Blocking-vs-Suggestion guidance says should default to
    Suggestion, not REQUEST_CHANGES -- and each of those cycles is a full
    reviewer invocation, cloud or local, that a spurious block doesn't need
    to spend (observed: a throwaway benchmark task's rate limiter got
    REQUEST_CHANGES purely for missing README docs on a brand-new, not-yet-
    consumed class, 2026-07-04)."""
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, **kwargs):
            captured["prompt"] = prompt
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())

    p._run_reviewer("/tmp/some-worktree", "agent/some-branch")

    prompt = captured["prompt"].lower()
    assert "existing caller" in prompt or "existing consumer" in prompt or "already depend" in prompt
    assert "suggestion" in prompt
    assert "brand-new" in prompt or "brand new" in prompt


def test_run_reviewer_prompt_asks_for_every_blocking_finding_in_one_pass(
    agents_dir, monkeypatch,
):
    """The reviewer rubric must ask for ALL Blocking findings in a single
    review, not just the first one noticed - otherwise a weak local
    implementer burns a full rework cycle per finding, and each cycle is a
    fresh opportunity to regress already-correct code (observed live
    2026-07-22, MODE-29-REVIEW-STORY-LOCK-GUARD: review #1 flagged only the
    missing docstring; review #2, on otherwise-correct code, surfaced a
    SECOND pre-existing issue (validate-before-lock) that was visible in
    review #1's diff but never raised there; the extra rework cycle this
    forced is where the implementation broke)."""
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, **kwargs):
            captured["prompt"] = prompt
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())

    p._run_reviewer("/tmp/some-worktree", "agent/some-branch")

    prompt = captured["prompt"].lower()
    assert "every" in prompt and "blocking" in prompt
    assert "rework" in prompt


def test_run_reviewer_prompt_omits_approve_with_fix_by_default(agents_dir, monkeypatch):
    """The reviewer self-fix option (APPROVE_WITH_FIX) must be off by
    default (secure-by-default: this is a new capability that auto-commits
    reviewer-authored code) - an operator opts in explicitly via
    PIPELINE_REVIEWER_AUTO_FIX. Without the env var set, the prompt must
    never mention the option, even for a low-risk story."""
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, **kwargs):
            captured["prompt"] = prompt
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())
    monkeypatch.delenv("PIPELINE_REVIEWER_AUTO_FIX", raising=False)

    p._run_reviewer("/tmp/some-worktree", "agent/some-branch", risk="low")

    assert "APPROVE_WITH_FIX" not in captured["prompt"]


def test_run_reviewer_prompt_mentions_approve_with_fix_when_enabled_and_low_risk(
    agents_dir, monkeypatch,
):
    """Enabled + low risk is the only combination that offers the reviewer
    the self-fix option."""
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, **kwargs):
            captured["prompt"] = prompt
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())
    monkeypatch.setenv("PIPELINE_REVIEWER_AUTO_FIX", "1")

    p._run_reviewer("/tmp/some-worktree", "agent/some-branch", risk="low")

    assert "APPROVE_WITH_FIX" in captured["prompt"]


def test_run_reviewer_prompt_omits_approve_with_fix_for_high_risk_even_when_enabled(
    agents_dir, monkeypatch,
):
    """Defense in depth at the prompt-construction layer, not just the
    harness-side guardrail: a high-risk story never even gets offered the
    self-fix option, regardless of the operator's global opt-in."""
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, **kwargs):
            captured["prompt"] = prompt
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())
    monkeypatch.setenv("PIPELINE_REVIEWER_AUTO_FIX", "1")

    p._run_reviewer("/tmp/some-worktree", "agent/some-branch", risk="high")

    assert "APPROVE_WITH_FIX" not in captured["prompt"]


def test_run_reviewer_does_not_run_test_suite(agents_dir, tmp_path, monkeypatch):
    """Real-world PR review: the reviewer reviews the diff and trusts CI.
    review_story only runs when status == tests_passed, so the suite is
    already green; a reviewer-driven rerun is pure duplicate spend (a full
    agentic Bash tool-loop re-executing what check_story_status just ran).
    The prompt must NOT instruct the reviewer to run the test suite, and
    must not inject any resolved test command."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, **kwargs):
            captured["prompt"] = prompt
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())

    p._run_reviewer(str(tmp_path), "agent/some-branch")

    prompt = captured["prompt"]
    assert "Run the test suite" not in prompt
    assert "do not substitute" not in prompt.lower()
    assert "-m pytest" not in prompt


def test_run_reviewer_first_review_covers_full_branch_diff(agents_dir, tmp_path, monkeypatch):
    """First review (no prior REQUEST_CHANGES, since_sha unset): review the
    full branch diff -- no incremental scoping, no test-suite rerun."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, **kwargs):
            captured["prompt"] = prompt
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())

    p._run_reviewer(str(tmp_path), "agent/some-branch")

    prompt = captured["prompt"]
    assert "agent/some-branch" in prompt
    assert "Run the test suite" not in prompt
    # No incremental since-SHA scoping on a first review.
    assert "..HEAD" not in prompt


def test_run_reviewer_uses_review_model_override_when_backend_is_local(agents_dir, monkeypatch):
    """Asymmetric review: both software-engineer.md and code-reviewer.md
    declare `model: sonnet`, so without an override dispatch and review
    resolve to the identical concrete local model - a model reviewing its
    own work with identical weights. PIPELINE_LOCAL_REVIEW_MODEL lets
    review run on a different model, but only when the review backend is
    actually local (passing a bare Ollama tag like "devstral:24b" as the
    Claude CLI's --model would break cloud review)."""
    monkeypatch.setenv("PIPELINE_BACKEND_REVIEW", "local")
    monkeypatch.setenv("PIPELINE_LOCAL_REVIEW_MODEL", "devstral:24b")
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, *, model, **kwargs):
            captured["model"] = model
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())

    p._run_reviewer("/tmp/some-worktree", "agent/some-branch")

    assert captured["model"] == "devstral:24b"


def test_run_reviewer_ignores_review_model_override_when_backend_is_claude(agents_dir, monkeypatch):
    """Regression guard: PIPELINE_LOCAL_REVIEW_MODEL must NOT leak into a
    cloud (claude) review - it must keep using the persona's declared tier
    ("sonnet") so ClaudeCliDriver gets a real Claude model name, not an
    Ollama tag."""
    monkeypatch.setenv("PIPELINE_BACKEND_REVIEW", "claude")
    monkeypatch.setenv("PIPELINE_LOCAL_REVIEW_MODEL", "devstral:24b")
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, *, model, **kwargs):
            captured["model"] = model
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())

    p._run_reviewer("/tmp/some-worktree", "agent/some-branch")

    assert captured["model"] == "sonnet"


def test_run_reviewer_explicit_local_backend_name_honors_review_model_override(agents_dir, monkeypatch):
    """review_story's FM-B rate-limit fallback calls _run_reviewer with an
    explicit backend_name="local" override (not via the env var) - the
    review-model override must apply in that path too."""
    monkeypatch.delenv("PIPELINE_BACKEND_REVIEW", raising=False)
    monkeypatch.setenv("PIPELINE_LOCAL_REVIEW_MODEL", "devstral:24b")
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, *, model, **kwargs):
            captured["model"] = model
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())

    p._run_reviewer("/tmp/some-worktree", "agent/some-branch", backend_name="local")

    assert captured["model"] == "devstral:24b"


def test_run_reviewer_explicit_provider_name_honors_review_model_override(agents_dir, monkeypatch):
    """T16: an explicitly-pinned provider name (not just the "local" alias)
    must also count as local-family for the review-model override gate."""
    monkeypatch.delenv("PIPELINE_BACKEND_REVIEW", raising=False)
    monkeypatch.setenv("PIPELINE_LOCAL_REVIEW_MODEL", "devstral:24b")
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, *, model, **kwargs):
            captured["model"] = model
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())

    p._run_reviewer("/tmp/some-worktree", "agent/some-branch", backend_name="lmstudio")

    assert captured["model"] == "devstral:24b"


