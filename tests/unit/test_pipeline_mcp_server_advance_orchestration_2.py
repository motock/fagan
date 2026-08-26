"""Tests for the pipeline MCP server: advance_pipeline orchestration (part 2).

Split out of test_pipeline_mcp_server.py to keep it under the project's line-count target; shared fixtures/helpers moved to tests.unit._pipeline_mcp_server_test_helpers.
"""
import json
import subprocess

from app import backend
from pipeline import server as p
from pipeline import ticketing as pt
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _clear_caches,
    _FakeProc,
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _plane_configured,
    _read_manifest,
    _write_manifest,
    agents_dir,
    plan_dir,
    worktree_root,
)


def test_dispatch_story_passes_rework_full_suite_when_review_feedback_set(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """L1 gap: a story carrying reviewer `review_feedback` (a REQUEST_CHANGES
    rework redispatch, not a CI-fail rework) must ALSO reach the agent
    subprocess as LOCAL_AGENT_REWORK_FULL_SUITE=1. Without this, the
    reviewer-rework path lets the agent call `done` on a dirty/broken tree
    the reviewer never re-checked in full - the acceptance oracle stays
    green even when the agent's own edit broke the rest of the suite
    (observed live 2026-07-22, MODE-29-REVIEW-STORY-LOCK-GUARD cycle 3: a
    botched replace_lines orphaned a function definition, the agent called
    done with 79 tests failing, and nothing rejected it)."""
    captured: dict = {}
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(
        backend.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakeProc(4323),
    )
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    _write_manifest(plan_dir, "revfb", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "changes_requested", "dependencies": [],
               "review_feedback": "REQUEST_CHANGES: fix the docstring placement.",
               "acceptance": [{"path": "tests/test_a.py", "source": "def test_a(): pass"}]},
    })

    result = p.dispatch_story("revfb", "S1")
    assert result["ok"] is True
    assert captured["env"]["LOCAL_AGENT_REWORK_FULL_SUITE"] == "1"


def test_dispatch_story_excludes_review_and_agent_log_from_worktree_tracking(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    """Mode 17: review.log (written by the local review loop) gets committed
    by a rework cycle's auto-WIP-commit if it isn't excluded, so the next
    review cycle's append makes it a modified TRACKED file - the pre-merge
    rebase then refuses ("You have unstaged changes"), failing an already
    -APPROVED, ground-truth-correct story. Excluding review.log (and
    agent.log, for the same reason) via .git/info/exclude at worktree
    creation means `git add -A` can never track them in the first place.
    Uses a REAL git repo (not mocked subprocess) so the actual exclude file
    content is verified end-to-end, not just that some git command ran."""
    real_repo = tmp_path / "real-repo"
    real_repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master", "."], cwd=real_repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=real_repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=real_repo, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"],
                    cwd=real_repo, check=True)
    bare_origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(bare_origin)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare_origin)], cwd=real_repo, check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "master"], cwd=real_repo, check=True)

    _write_manifest(plan_dir, "excl", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })
    manifest_path = plan_dir / "excl.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["repo_root"] = str(real_repo)
    manifest_path.write_text(json.dumps(manifest))

    # backend.subprocess IS the real, global subprocess module (a singleton
    # import, not a copy) - patching its Popen would also break the REAL
    # git commands this test needs (git worktree add / pull / add / diff).
    # Discriminate: only fake the actual dispatch invocation (argv[0] ==
    # "claude"), delegate everything else to the real Popen.
    real_popen = backend.subprocess.Popen

    def _discriminating_popen(cmd, **kw):
        if cmd and cmd[0] == "claude":
            return _FakeProc(999)
        return real_popen(cmd, **kw)

    monkeypatch.setattr(backend.subprocess, "Popen", _discriminating_popen)
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "master")

    result = p.dispatch_story("excl", "S1")
    assert result["ok"] is True

    exclude_content = (real_repo / ".git" / "info" / "exclude").read_text()
    assert "review.log" in exclude_content
    assert "agent.log" in exclude_content

    # The exclusion must actually work, not just be present as text: a
    # rework-style `git add -A` inside the worktree must not stage either
    # file, even when both exist with real content.
    worktree_path = worktree_root / "S1"
    (worktree_path / "review.log").write_text("review cycle 1\n")
    (worktree_path / "agent.log").write_text("[step 0] bash: ls\n")
    subprocess.run(["git", "add", "-A"], cwd=worktree_path, check=True)
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"],
                             cwd=worktree_path, capture_output=True, text=True, check=True)
    assert "review.log" not in staged.stdout
    assert "agent.log" not in staged.stdout


def test_agent_plan_src_hash_is_excluded_from_worktree_tracking(tmp_path):
    """Regression guard for the .agent_plan_src_hash leak (2026-08-13): the
    checklist-reuse guard's companion hash file is an untracked runtime
    artifact (like .agent_plan.md) that must be in _WORKTREE_LOG_EXCLUDES so a
    rework WIP-commit's `git add -A` never tracks it - a tracked copy caused an
    add/add rebase conflict that terminal-failed P3-6's otherwise-green merge
    gate. Mirrors test_dispatch_story_excludes_review_and_agent_log_from_worktree_tracking's
    staging assertion, exercised directly against the exclude helper."""
    from pipeline.paths import (
        _WORKTREE_LOG_EXCLUDES,
        _exclude_worktree_logs_from_tracking,
    )

    # The artifact must be in the exclude list (the one-line fix).
    assert ".agent_plan_src_hash" in _WORKTREE_LOG_EXCLUDES

    # The exclude helper must write it to the repo's .git/info/exclude (the
    # shared, per-repo file that governs every worktree of the repo).
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master", str(repo)], check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"],
                   cwd=repo, check=True)
    _exclude_worktree_logs_from_tracking(repo)
    exclude_content = (repo / ".git" / "info" / "exclude").read_text()
    assert ".agent_plan_src_hash" in exclude_content

    # The exclusion must actually work: a `git add -A` in the working tree must
    # not stage the artifact even when it exists with real content.
    (repo / ".agent_plan_src_hash").write_text("deadbeef\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"],
                            cwd=repo, capture_output=True, text=True, check=True)
    assert ".agent_plan_src_hash" not in staged.stdout


def test_dispatch_story_fresh_seeds_checkpoint_instruction_with_plan_name(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    _write_manifest(plan_dir, "ds4", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })

    popen_calls = []

    def _fake_popen(cmd, **kw):
        popen_calls.append(cmd)
        return _FakeProc(9999)

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("ds4", "S1")

    prompt = popen_calls[0][popen_calls[0].index("-p") + 1]
    assert "checkpoint" in prompt.lower()
    assert "ds4" in prompt
    assert "S1" in prompt


def test_dispatch_story_resume_reuses_worktree_and_seeds_journal(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    _write_manifest(plan_dir, "ds2", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "interrupted", "worktree": str(worktree_path),
               "last_commit": "sha-1"},
    })
    (plan_dir / "ds2.S1.journal.json").write_text(json.dumps([
        {"step": "step-1", "summary": "Wrote the parser",
         "next_hint": "add validation", "commit": "sha-1", "ts": "x"},
    ]))

    run_calls = []
    popen_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: run_calls.append(cmd))

    def _fake_popen(cmd, **kw):
        popen_calls.append(cmd)
        return _FakeProc(5555)

    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("ds2", "S1")

    assert result["ok"] is True
    assert result["resumed"] is True
    assert not any(c[:3] == ["git", "worktree", "add"] for c in run_calls)
    assert not any(c[:2] == ["git", "pull"] for c in run_calls)

    prompt = popen_calls[0][popen_calls[0].index("-p") + 1]
    assert "RESUMING" in prompt
    assert "Wrote the parser" in prompt
    assert "add validation" in prompt

    manifest = _read_manifest(plan_dir, "ds2")
    story = manifest["stories"]["S1"]
    assert story["status"] == "in_progress"
    assert story["pid"] == 5555
    assert story["worktree"] == str(worktree_path)


def test_dispatch_story_changes_requested_seeds_review_feedback(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    # Redispatching a changes_requested story must feed the reviewer's stored
    # feedback into the agent's prompt so it reworks the right thing.
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    _write_manifest(plan_dir, "dscr", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "changes_requested", "worktree": str(worktree_path),
               "review_feedback": "The SQL is injectable; parameterize it."},
    })

    popen_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen",
                        lambda cmd, **kw: popen_calls.append(cmd) or _FakeProc(5556))
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("dscr", "S1")

    assert result["resumed"] is True
    prompt = popen_calls[0][popen_calls[0].index("-p") + 1]
    assert "The SQL is injectable; parameterize it." in prompt
    assert _read_manifest(plan_dir, "dscr")["stories"]["S1"]["status"] == "in_progress"


def test_dispatch_story_resumes_when_worktree_exists_even_without_interrupted_status(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A story can be re-dispatched manually after a kill that never reached
    interrupt_story. Detect the leftover worktree and resume rather than
    failing on `git worktree add` for a path that already exists."""
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    _write_manifest(plan_dir, "ds3", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "failed", "worktree": str(worktree_path)},
    })

    run_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: run_calls.append(cmd))
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: _FakeProc(7777))
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("ds3", "S1")

    assert result["resumed"] is True
    assert not any(c[:3] == ["git", "worktree", "add"] for c in run_calls)


