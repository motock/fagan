"""Tests for the pipeline MCP server.

Run with the project venv:
    cd ~/.claude/mcp-servers/pipeline && .venv/bin/python -m pytest -q

External boundaries (the `claude` subprocess, git, gh, Plane HTTP) are mocked;
internal logic is exercised directly. Tools are plain callables after the
@mcp.tool() decorator, so they are imported and called as functions.
"""

import fcntl
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

import backend
import pipeline_mcp_server as p


# ---------- Fixtures ----------
@pytest.fixture(autouse=True)
def _clear_caches():
    p._state_cache.clear()
    p._label_cache.clear()
    yield


@pytest.fixture(autouse=True)
def _plane_configured(monkeypatch):
    """Default the test world to "Plane is wired up", which is what the
    existing tests assume (they mock plane_request and expect calls to
    happen). The Plane-optional path is exercised by the handful of tests
    that explicitly clear these to "" via _plane_disabled."""
    monkeypatch.setattr(p, "PLANE_API_KEY", "test-key")
    monkeypatch.setattr(p, "PLANE_WORKSPACE", "test-ws")
    monkeypatch.setattr(p, "PLANE_PROJECT", "test-proj")


@pytest.fixture
def _plane_disabled(monkeypatch):
    """Simulate an unconfigured Plane (no API key / workspace / project), so
    Plane calls must be skipped rather than fired at a dead endpoint."""
    monkeypatch.setattr(p, "PLANE_API_KEY", "")
    monkeypatch.setattr(p, "PLANE_WORKSPACE", "")
    monkeypatch.setattr(p, "PLANE_PROJECT", "")


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    d = tmp_path / "agents"
    d.mkdir()
    (d / "overlord.md").write_text(
        '---\nname: "overlord"\nmodel: opus\nmemory: user\n---\n\n'
        "You are the Overlord body text.\n"
    )
    (d / "software-engineer.md").write_text(
        '---\nname: "software-engineer"\nmodel: sonnet\n---\n\nEngineer body.\n'
    )
    (d / "code-reviewer.md").write_text(
        '---\nname: "code-reviewer"\nmodel: sonnet\n---\n\nReviewer body.\n'
    )
    monkeypatch.setattr(p, "AGENTS_DIR", d)
    return d


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    return d


@pytest.fixture
def worktree_root(tmp_path, monkeypatch):
    d = tmp_path / "worktrees"
    d.mkdir()
    monkeypatch.setattr(p, "WORKTREE_ROOT", d)
    return d


@pytest.fixture(autouse=True)
def _isolate_usage_state(tmp_path, monkeypatch):
    # Point the usage gate at a non-existent tmp file for EVERY test so none of
    # them read the developer's live ~/.claude/usage_state.json. That file is
    # rewritten every ~60s by the real usage poller, so advance_pipeline tests
    # that don't otherwise stub the gate were flaky - passing or failing purely
    # on whether the live session/week usage happened to be over the pause
    # threshold when the suite ran. A missing file reads as "not paused".
    path = tmp_path / "usage_state.json"
    monkeypatch.setattr(p, "USAGE_STATE_PATH", path)
    return path


@pytest.fixture
def usage_state_path(_isolate_usage_state):
    # Same isolated path as the autouse fixture; tests that want a specific gate
    # state write to it.
    return _isolate_usage_state


SAMPLE_USAGE_TEXT = (
    "You are currently using your subscription to power your Claude Code usage\n\n"
    "Current session: 9% used · resets Jun 18 at 11:59am (America/Chicago)\n"
    "Current week (all models): 48% used · resets Jun 23 at 9am (America/Chicago)\n\n"
    "What's contributing to your limits usage?\n"
)


# ---------- Test runner detection ----------
def test_detect_test_command_finds_root_package_json(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    test_dir, cmd = p.detect_test_command(tmp_path)
    assert test_dir == tmp_path
    assert cmd == ["npm", "test"]


def test_detect_test_command_falls_back_to_subdirectory(tmp_path):
    # Project lives in a subdirectory (e.g. engine/) rather than repo root.
    sub = tmp_path / "engine"
    sub.mkdir()
    (sub / "package.json").write_text("{}")
    test_dir, cmd = p.detect_test_command(tmp_path)
    assert test_dir == sub
    assert cmd == ["npm", "test"]


def test_detect_test_command_prefers_root_over_subdirectory(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    sub = tmp_path / "engine"
    sub.mkdir()
    (sub / "package.json").write_text("{}")
    test_dir, cmd = p.detect_test_command(tmp_path)
    assert test_dir == tmp_path


def test_detect_test_command_no_marker_anywhere_falls_back_to_npm_test(tmp_path):
    test_dir, cmd = p.detect_test_command(tmp_path)
    assert test_dir == tmp_path
    assert cmd == ["npm", "test"]


# ---------- Persona helpers ----------
def test_persona_body_strips_frontmatter(agents_dir):
    body = p._persona_body("software-engineer")
    assert "Engineer body." in body
    assert "name:" not in body
    assert not body.startswith("---")


def test_persona_default_model_reads_frontmatter(agents_dir):
    assert p._persona_default_model("software-engineer") == "sonnet"
    assert p._persona_default_model("overlord") == "opus"


def test_persona_body_unknown_raises(agents_dir):
    with pytest.raises(FileNotFoundError):
        p._persona_body("does-not-exist")


def test_persona_default_model_unknown_returns_none(agents_dir):
    assert p._persona_default_model("does-not-exist") is None


# ---------- request_decision / list_decisions ----------
def test_request_decision_records_and_returns(plan_dir, agents_dir, monkeypatch):
    canned = (
        "RULING: Use the existing http client; do not add a new dependency.\n"
        "TIER: routine\n"
        "RISK: low\n"
        "RATIONALE: The stack already includes httpx; adding requests duplicates it.\n"
        "NOTIFY_USER: no\n"
    )
    monkeypatch.setattr(p, "_invoke_overlord", lambda prompt: canned)

    result = p.request_decision(
        "myplan", "PIPE-7",
        "Should I add the requests library?",
        ["add requests", "use existing httpx"],
        context="parser story",
    )
    assert result["ruling"].startswith("Use the existing http client")
    assert result["tier"] == "routine"
    assert result["risk"] == "low"
    assert result["notify_user"] is False
    assert result["decided_by"] == "overlord"

    log = json.loads((plan_dir / "myplan.decisions.json").read_text())
    assert len(log) == 1
    assert log[0]["story_key"] == "PIPE-7"
    assert "decided_at" in log[0]


def test_request_decision_notify_and_high_risk_parsed(plan_dir, agents_dir, monkeypatch):
    canned = (
        "RULING: Hold for human review.\n"
        "TIER: park-and-ping\n"
        "RISK: high\n"
        "RATIONALE: Touches auth.\n"
        "NOTIFY_USER: yes\n"
    )
    monkeypatch.setattr(p, "_invoke_overlord", lambda prompt: canned)
    result = p.request_decision("myplan", "PIPE-9", "q", ["a", "b"])
    assert result["notify_user"] is True
    assert result["risk"] == "high"
    assert result["tier"] == "park-and-ping"


def test_list_decisions_empty_then_populated(plan_dir, agents_dir, monkeypatch):
    assert p.list_decisions("emptyplan") == []
    monkeypatch.setattr(
        p, "_invoke_overlord",
        lambda prompt: "RULING: x\nTIER: routine\nRISK: low\nRATIONALE: y\nNOTIFY_USER: no\n",
    )
    p.request_decision("myplan", "S1", "q", ["a"])
    p.request_decision("myplan", "S2", "q", ["a"])
    items = p.list_decisions("myplan")
    assert len(items) == 2
    assert {i["story_key"] for i in items} == {"S1", "S2"}


# ---------- Persona/model-aware dispatch ----------
def _story(**over):
    base = {"summary": "Do the thing", "agent_instructions": "Build it with tests."}
    base.update(over)
    return base


def test_dispatch_command_uses_persona_body_and_model(agents_dir):
    spec = p._build_dispatch_command(_story(persona="software-engineer", model="opus"), "PIPE-1")
    assert "Engineer body." in spec["system"]
    assert spec["model"] == "opus"


def test_dispatch_command_falls_back_to_persona_default_model(agents_dir):
    spec = p._build_dispatch_command(_story(persona="software-engineer"), "PIPE-2")
    assert spec["model"] == "sonnet"


def test_dispatch_command_no_persona_uses_default_model_and_no_system_prompt(agents_dir):
    spec = p._build_dispatch_command(_story(), "PIPE-3")
    assert spec["model"] == p.DEFAULT_MODEL
    assert spec["system"] is None


def test_dispatch_command_unknown_persona_raises(agents_dir):
    with pytest.raises(FileNotFoundError):
        p._build_dispatch_command(_story(persona="no-such-persona"), "PIPE-4")


def test_dispatch_command_reviewer_tools_are_read_only(agents_dir):
    spec = p._build_dispatch_command(_story(persona="code-reviewer"), "PIPE-5")
    assert spec["allowed_tools"] == "Bash,Read"


def test_dispatch_command_resume_includes_completed_steps_and_hint(agents_dir):
    journal = [
        {"step": "step-1", "summary": "Wrote the parser",
         "next_hint": "add validation", "commit": "sha-1", "ts": "x"},
    ]
    spec = p._build_dispatch_command(_story(), "PIPE-1", resume_journal=journal)
    prompt = spec["prompt"]
    assert "RESUMING" in prompt
    assert "Wrote the parser" in prompt
    assert "add validation" in prompt
    assert "do not redo" in prompt.lower()


def test_dispatch_command_no_resume_journal_uses_original_prompt(agents_dir):
    spec = p._build_dispatch_command(_story(), "PIPE-1")
    prompt = spec["prompt"]
    assert "RESUMING" not in prompt
    assert "completing issue" in prompt


def test_dispatch_command_includes_checkpoint_instruction_when_plan_name_given(agents_dir):
    spec = p._build_dispatch_command(_story(), "PIPE-1", plan_name="myplan")
    prompt = spec["prompt"]
    assert "checkpoint" in prompt.lower()
    assert "myplan" in prompt
    assert "PIPE-1" in prompt


def test_dispatch_command_omits_checkpoint_instruction_without_plan_name(agents_dir):
    spec = p._build_dispatch_command(_story(), "PIPE-1")
    assert "checkpoint tool" not in spec["prompt"].lower()


def test_dispatch_command_resume_also_includes_checkpoint_instruction(agents_dir):
    journal = [
        {"step": "step-1", "summary": "Wrote the parser",
         "next_hint": "add validation", "commit": "sha-1", "ts": "x"},
    ]
    spec = p._build_dispatch_command(
        _story(), "PIPE-1", plan_name="myplan", resume_journal=journal,
    )
    prompt = spec["prompt"]
    assert "checkpoint" in prompt.lower()
    assert "myplan" in prompt


def test_dispatch_command_default_tools(agents_dir):
    spec = p._build_dispatch_command(_story(persona="software-engineer"), "PIPE-6")
    assert spec["allowed_tools"] == "Bash,Edit,Write,Read"


def test_dispatch_command_includes_review_feedback(agents_dir):
    # A redispatched changes_requested story must carry the reviewer's feedback
    # into the prompt so the agent knows what to fix.
    feedback = "The error path is untested and the SQL is injectable."
    spec = p._build_dispatch_command(_story(), "PIPE-1", review_feedback=feedback)
    assert feedback in spec["prompt"]
    assert "REQUESTED CHANGES" in spec["prompt"].upper()


# ---------- Plan schema carry-through ----------
def test_save_plan_preserves_persona_model_risk(plan_dir):
    plan = {
        "epics": [{
            "summary": "E1",
            "stories": [_story(persona="security-engineer", model="opus", risk="high")],
        }]
    }
    p.save_plan("carry", json.dumps(plan))
    saved = json.loads((plan_dir / "carry.json").read_text())
    story = saved["epics"][0]["stories"][0]
    assert story["persona"] == "security-engineer"
    assert story["model"] == "opus"
    assert story["risk"] == "high"


def _fake_plane(method, path, **kwargs):
    if path.endswith("/states/"):
        return {"results": [
            {"group": "backlog", "id": "st-backlog"},
            {"group": "started", "id": "st-started"},
            {"group": "completed", "id": "st-done"},
        ]}
    if path.endswith("/labels/") and method == "GET":
        return {"results": []}
    if path.endswith("/labels/") and method == "POST":
        return {"id": "label-1"}
    if path.endswith("/epics/") and method == "POST":
        return {"id": "epic-1"}
    if path.endswith("/work-items/") and method == "POST":
        return {"id": "issue-1"}
    return {}


def test_ingest_plan_carries_persona_model_risk_into_manifest(plan_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(p, "plane_request", _fake_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{
            "summary": "E1",
            "stories": [_story(persona="security-engineer", model="opus", risk="high")],
        }]
    }
    (plan_dir / "ing.json").write_text(json.dumps(plan))
    result = p.ingest_plan("ing")
    assert result["ok"] is True
    manifest = json.loads((plan_dir / "ing.manifest.json").read_text())
    story = manifest["stories"]["issue-1"]
    assert story["persona"] == "security-engineer"
    assert story["model"] == "opus"
    assert story["risk"] == "high"


def test_ingest_plan_remaps_local_keys_to_issue_ids_in_dependencies(plan_dir, monkeypatch, tmp_path):
    issue_ids = iter(["issue-1", "issue-2", "issue-3"])
    monkeypatch.setattr(
        p, "plane_request",
        lambda method, path, **kw: (
            _fake_plane(method, path, **kw) if not path.endswith("/work-items/")
            else {"id": next(issue_ids)}
        ),
    )
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{
            "summary": "E1",
            "stories": [
                _story(key="S1"),
                _story(key="S2", dependencies=["S1"]),
                _story(key="S3", dependencies=["S1", "S2"]),
            ],
        }]
    }
    (plan_dir / "deps.json").write_text(json.dumps(plan))
    result = p.ingest_plan("deps")
    assert result["ok"] is True
    manifest = json.loads((plan_dir / "deps.manifest.json").read_text())
    stories = manifest["stories"]
    assert stories["issue-1"]["dependencies"] == []
    assert stories["issue-2"]["dependencies"] == ["issue-1"]
    assert stories["issue-3"]["dependencies"] == ["issue-1", "issue-2"]


def test_ingest_plan_leaves_unresolvable_dependency_keys_unchanged(plan_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(p, "plane_request", _fake_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{
            "summary": "E1",
            "stories": [_story(key="S1", dependencies=["no-such-key"])],
        }]
    }
    (plan_dir / "dangling.json").write_text(json.dumps(plan))
    result = p.ingest_plan("dangling")
    assert result["ok"] is True
    manifest = json.loads((plan_dir / "dangling.manifest.json").read_text())
    assert manifest["stories"]["issue-1"]["dependencies"] == ["no-such-key"]


# ---------- Logging hygiene ----------
def test_http_loggers_are_quieted():
    """Importing the server caps httpx/httpcore at WARNING so the per-tick
    HTTP probes don't flood the unattended launchd logs at INFO."""
    import logging
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


# ---------- Plane optional (unconfigured) ----------
def test_plane_enabled_reflects_config(monkeypatch):
    assert p._plane_enabled() is True  # set by _plane_configured fixture
    monkeypatch.setattr(p, "PLANE_PROJECT", "")
    assert p._plane_enabled() is False


def _explode_plane(*a, **kw):
    raise AssertionError("plane_request must not be called when Plane is unconfigured")


def test_ingest_plan_without_plane_skips_calls_and_keys_by_story_key(
    _plane_disabled, plan_dir, monkeypatch, tmp_path,
):
    monkeypatch.setattr(p, "plane_request", _explode_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{
            "summary": "E1",
            "stories": [
                _story(key="S1"),
                _story(key="S2", dependencies=["S1"]),
            ],
        }]
    }
    (plan_dir / "noplane.json").write_text(json.dumps(plan))
    result = p.ingest_plan("noplane")
    assert result["ok"] is True
    manifest = json.loads((plan_dir / "noplane.manifest.json").read_text())
    # Stories are keyed by their plan key (no Plane UUID to key on), and
    # dependencies still resolve to those keys.
    assert set(manifest["stories"]) == {"S1", "S2"}
    assert manifest["stories"]["S2"]["dependencies"] == ["S1"]


def test_ingest_plan_without_plane_synthesizes_keys_when_absent(
    _plane_disabled, plan_dir, monkeypatch, tmp_path,
):
    monkeypatch.setattr(p, "plane_request", _explode_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{"summary": "E1", "stories": [_story(), _story()]}],
    }
    (plan_dir / "nokeys.json").write_text(json.dumps(plan))
    result = p.ingest_plan("nokeys")
    assert result["ok"] is True
    manifest = json.loads((plan_dir / "nokeys.manifest.json").read_text())
    assert len(manifest["stories"]) == 2  # two distinct synthetic keys


def test_plane_set_state_noop_when_plane_disabled(_plane_disabled, monkeypatch):
    monkeypatch.setattr(p, "plane_request", _explode_plane)
    assert p._plane_set_state("S1", "started") is True


def test_mark_story_done_without_plane_skips_patch(_plane_disabled, plan_dir, monkeypatch):
    monkeypatch.setattr(p, "plane_request", _explode_plane)
    (plan_dir / "md.manifest.json").write_text(json.dumps(
        {"stories": {"S1": {"status": "pr_open"}}}))
    result = p.mark_story_done("md", "S1")
    assert result["ok"] is True
    manifest = json.loads((plan_dir / "md.manifest.json").read_text())
    assert manifest["stories"]["S1"]["status"] == "done"


def test_mark_story_in_progress_without_plane_skips_patch(_plane_disabled, plan_dir, monkeypatch):
    monkeypatch.setattr(p, "plane_request", _explode_plane)
    (plan_dir / "mip.manifest.json").write_text(json.dumps(
        {"stories": {"S1": {"status": "todo"}}}))
    result = p.mark_story_in_progress("mip", "S1")
    assert result["ok"] is True
    manifest = json.loads((plan_dir / "mip.manifest.json").read_text())
    assert manifest["stories"]["S1"]["status"] == "in_progress"


def test_ingest_plan_rejects_missing_repo_root(plan_dir, monkeypatch):
    """Without repo_root, advance_all_plans() falls back to the global
    REPO_ROOT for this plan - almost certainly the wrong repo (or a
    deliberately-broken sentinel, if one's configured to fail loudly rather
    than silently operate on the wrong repo). Catch it at ingest, not three
    silent merge-attempt failures later."""
    called = []
    monkeypatch.setattr(p, "plane_request", lambda *a, **kw: called.append(1) or _fake_plane(*a, **kw))
    plan = {"epics": [{"summary": "E1", "stories": [_story()]}]}
    (plan_dir / "norepo.json").write_text(json.dumps(plan))

    result = p.ingest_plan("norepo")

    assert result["ok"] is False
    assert "repo_root" in result["error"]
    assert not (plan_dir / "norepo.manifest.json").exists()
    assert not called  # must fail before any Plane side effects


def test_ingest_plan_rejects_nonexistent_repo_root_directory(plan_dir, monkeypatch):
    monkeypatch.setattr(p, "plane_request", _fake_plane)
    plan = {
        "repo_root": "/nonexistent-repo-root-set-per-plan-only",
        "epics": [{"summary": "E1", "stories": [_story()]}],
    }
    (plan_dir / "badrepo.json").write_text(json.dumps(plan))

    result = p.ingest_plan("badrepo")

    assert result["ok"] is False
    assert "repo_root" in result["error"]
    assert not (plan_dir / "badrepo.manifest.json").exists()


# ---------- Review gate + auto-PR ----------
def _write_manifest(plan_dir, plan_name, stories):
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}, indent=2)
    )


def _read_manifest(plan_dir, plan_name):
    return json.loads((plan_dir / f"{plan_name}.manifest.json").read_text())


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


def test_parse_verdict_variants():
    assert p._parse_verdict("...\nVERDICT: APPROVE\n") == "APPROVE"
    assert p._parse_verdict("VERDICT: REQUEST_CHANGES") == "REQUEST_CHANGES"
    assert p._parse_verdict("no verdict here") == "UNKNOWN"


def test_review_story_approve_opens_pr(plan_dir, agents_dir, monkeypatch):
    _write_manifest(plan_dir, "rv", {
        "S1": {"summary": "Add thing", "status": "in_progress",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br: "VERDICT: APPROVE")
    monkeypatch.setattr(p, "_open_pr", lambda wt, key, story: "https://gh/pr/1")

    result = p.review_story("rv", "S1")
    assert result["verdict"] == "APPROVE"
    assert result["status"] == "pr_open"
    assert result["pr_url"] == "https://gh/pr/1"
    story = _read_manifest(plan_dir, "rv")["stories"]["S1"]
    assert story["status"] == "pr_open"
    assert story["pr_url"] == "https://gh/pr/1"


def test_review_story_request_changes_opens_no_pr(plan_dir, agents_dir, monkeypatch):
    _write_manifest(plan_dir, "rv", {
        "S1": {"summary": "Add thing", "status": "in_progress",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br: "VERDICT: REQUEST_CHANGES")

    def _boom(*a, **k):
        raise AssertionError("PR must not be opened on REQUEST_CHANGES")

    monkeypatch.setattr(p, "_open_pr", _boom)

    result = p.review_story("rv", "S1")
    assert result["verdict"] == "REQUEST_CHANGES"
    assert result["status"] == "changes_requested"
    assert result.get("pr_url") is None
    story = _read_manifest(plan_dir, "rv")["stories"]["S1"]
    assert "pr_url" not in story


def test_review_story_persists_feedback_on_request_changes(plan_dir, agents_dir, monkeypatch):
    # The reviewer's reasoning must be stored, not just the verdict, so a
    # redispatched agent knows what to fix.
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "rvfb", {
        "S1": {"summary": "Add thing", "status": "in_progress",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    reviewer_output = ("The error path is untested and the SQL is injectable.\n"
                       "VERDICT: REQUEST_CHANGES")
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br: reviewer_output)

    p.review_story("rvfb", "S1")

    story = _read_manifest(plan_dir, "rvfb")["stories"]["S1"]
    assert story["status"] == "changes_requested"
    assert story["review_feedback"] == reviewer_output
    assert story["rework_attempts"] == 1


def test_review_story_clears_feedback_and_rework_on_approve(plan_dir, agents_dir, monkeypatch):
    # An approval after prior rework cycles must wipe the stale feedback/counter
    # so the story records a clean approval.
    _write_manifest(plan_dir, "rvclear", {
        "S1": {"summary": "Add thing", "status": "in_progress",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "review_feedback": "old gripes", "rework_attempts": 2},
    })
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br: "VERDICT: APPROVE")
    monkeypatch.setattr(p, "_open_pr", lambda wt, key, story: "https://gh/pr/1")

    p.review_story("rvclear", "S1")

    story = _read_manifest(plan_dir, "rvclear")["stories"]["S1"]
    assert story["status"] == "pr_open"
    assert "review_feedback" not in story
    assert "rework_attempts" not in story


def test_review_story_parks_after_rework_budget_exhausted(plan_dir, agents_dir, monkeypatch):
    # A story the reviewer keeps rejecting must eventually park for human review
    # rather than looping through redispatch forever.
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "rvpark", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "rework_attempts": 2},
    })
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br: "still bad\nVERDICT: REQUEST_CHANGES")
    notes = []
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))

    result = p.review_story("rvpark", "S1")

    story = _read_manifest(plan_dir, "rvpark")["stories"]["S1"]
    assert story["status"] == "parked"
    assert story["rework_attempts"] == 3
    assert result["status"] == "parked"
    assert len(notes) == 1 and "S1" in notes[0]


def test_merge_pr_does_not_pass_delete_branch_to_gh(monkeypatch, tmp_path):
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        class Result:
            stdout = "merged\n"
            returncode = 0
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "REPO_ROOT", tmp_path)
    result = p._merge_pr(str(tmp_path / "wt"), "S1")

    assert result == "merged"
    gh_calls = [c for c in calls if c[:2] == ["gh", "pr"]]
    assert "--delete-branch" not in gh_calls[0]


def test_merge_pr_cleans_up_worktree_and_branches_after_merge(monkeypatch, tmp_path):
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("cwd")))
        class Result:
            stdout = "merged\n"
            returncode = 0
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "REPO_ROOT", tmp_path)
    worktree = str(tmp_path / "wt")
    p._merge_pr(worktree, "S1")

    cmds = [c for c, _cwd in calls]
    assert ["git", "worktree", "remove", "--force", worktree] in cmds
    assert ["git", "branch", "-D", "agent/s1"] in cmds
    assert ["git", "push", "origin", "--delete", "agent/s1"] in cmds
    # Cleanup must run from REPO_ROOT, not the worktree being removed.
    cleanup_cwds = [cwd for cmd, cwd in calls if cmd[:2] == ["git", "worktree"]]
    assert all(cwd == tmp_path for cwd in cleanup_cwds)


def test_commit_wip_does_not_track_real_agent_log_file(tmp_path):
    p.subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    p.subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=tmp_path, check=True)
    p.subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "feature.ts").write_text("export const x = 1;\n")
    p.subprocess.run(["git", "add", "feature.ts"], cwd=tmp_path, check=True)
    p.subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    (tmp_path / "feature.ts").write_text("export const x = 2;\n")
    (tmp_path / "agent.log").write_text("session narration, not project code\n")

    p._commit_wip(str(tmp_path), "S1", "interrupted")

    tracked = p.subprocess.run(
        ["git", "ls-files"], cwd=tmp_path, check=True, capture_output=True, text=True,
    ).stdout
    assert "agent.log" not in tracked
    assert "feature.ts" in tracked


def test_commit_wip_checkpoints_when_agent_log_is_git_ignored(tmp_path):
    """Regression: a worktree may have agent.log locally git-ignored (via
    .git/info/exclude or .gitignore). The old `git add -A -- . :!agent.log`
    named agent.log in the pathspec, so git rejected the whole add ("paths are
    ignored... use -f", exit 1) and _commit_wip raised before committing - the
    checkpoint was lost even though real work was staged. The commit must
    succeed and exclude agent.log regardless of whether it is git-ignored."""
    p.subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    p.subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=tmp_path, check=True)
    p.subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "feature.ts").write_text("export const x = 1;\n")
    p.subprocess.run(["git", "add", "feature.ts"], cwd=tmp_path, check=True)
    p.subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    # Locally git-ignore agent.log, as a reviewer might to keep it out of diffs.
    (tmp_path / ".git" / "info" / "exclude").write_text("agent.log\n")
    (tmp_path / "feature.ts").write_text("export const x = 2;\n")
    (tmp_path / "agent.log").write_text("session narration, not project code\n")

    sha = p._commit_wip(str(tmp_path), "S1", "interrupted")

    assert sha  # a real commit sha, not a raised RuntimeError
    committed = p.subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=tmp_path, check=True, capture_output=True, text=True,
    ).stdout
    assert "feature.ts" in committed
    assert "agent.log" not in committed


# ---------- Per-plan repo_root ----------
def test_repo_root_for_returns_manifest_value_when_present(plan_dir):
    _write_manifest(plan_dir, "rr1", {})
    manifest_path = plan_dir / "rr1.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["repo_root"] = "/some/specific/repo"
    manifest_path.write_text(json.dumps(manifest))

    assert p._repo_root_for("rr1") == p.Path("/some/specific/repo")


def test_repo_root_for_falls_back_to_global_when_absent_in_manifest(plan_dir, monkeypatch, tmp_path):
    _write_manifest(plan_dir, "rr2", {})
    monkeypatch.setattr(p, "REPO_ROOT", tmp_path)
    assert p._repo_root_for("rr2") == tmp_path


def test_repo_root_for_falls_back_when_no_manifest_exists(plan_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(p, "REPO_ROOT", tmp_path)
    assert p._repo_root_for("does-not-exist") == tmp_path


def test_scoped_repo_root_sets_and_restores(plan_dir, monkeypatch, tmp_path):
    _write_manifest(plan_dir, "sr1", {})
    manifest_path = plan_dir / "sr1.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["repo_root"] = str(tmp_path / "the-repo")
    manifest_path.write_text(json.dumps(manifest))

    original = p.Path("/original/repo")
    monkeypatch.setattr(p, "REPO_ROOT", original)

    with p._scoped_repo_root("sr1") as scoped:
        assert scoped == tmp_path / "the-repo"
        assert p.REPO_ROOT == tmp_path / "the-repo"
    assert p.REPO_ROOT == original


def test_scoped_repo_root_restores_on_exception(plan_dir, monkeypatch, tmp_path):
    _write_manifest(plan_dir, "sr2", {})
    manifest_path = plan_dir / "sr2.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["repo_root"] = str(tmp_path / "the-repo")
    manifest_path.write_text(json.dumps(manifest))

    original = p.Path("/original/repo")
    monkeypatch.setattr(p, "REPO_ROOT", original)

    with pytest.raises(RuntimeError):
        with p._scoped_repo_root("sr2"):
            raise RuntimeError("boom")
    assert p.REPO_ROOT == original


def test_default_branch_does_not_leak_cache_across_repos(monkeypatch, tmp_path):
    monkeypatch.setattr(p, "_default_branch_cache", {})
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"

    def _fake_run(cmd, cwd=None, **kwargs):
        class Result:
            returncode = 0
            stdout = ("origin/feature-a\n" if cwd == repo_a
                       else "origin/feature-b\n")
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    monkeypatch.setattr(p, "REPO_ROOT", repo_a)
    branch_a = p._default_branch()
    monkeypatch.setattr(p, "REPO_ROOT", repo_b)
    branch_b = p._default_branch()

    assert branch_a == "feature-a"
    assert branch_b == "feature-b"


def test_ingest_plan_carries_repo_root_into_manifest(plan_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(p, "plane_request", _fake_plane)
    plan = {
        "epics": [{"summary": "E1", "stories": [_story()]}],
        "repo_root": str(tmp_path),
    }
    (plan_dir / "rrplan.json").write_text(json.dumps(plan))
    result = p.ingest_plan("rrplan")
    assert result["ok"] is True
    manifest = json.loads((plan_dir / "rrplan.manifest.json").read_text())
    assert manifest["repo_root"] == str(tmp_path)


def test_dispatch_story_uses_manifest_repo_root_for_git_commands(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    real_repo = tmp_path / "real-repo"
    _write_manifest(plan_dir, "rrds", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })
    manifest_path = plan_dir / "rrds.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["repo_root"] = str(real_repo)
    manifest_path.write_text(json.dumps(manifest))

    monkeypatch.setattr(p, "REPO_ROOT", p.Path("/wrong/default/repo"))

    cwds_used = []

    def _fake_run(cmd, cwd=None, **kwargs):
        cwds_used.append(cwd)
        class Result:
            returncode = 0
            stdout = ""
            stderr = ""
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: _FakeProc(123))
    monkeypatch.setattr(
        p, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )

    p.dispatch_story("rrds", "S1")

    assert any(c == real_repo for c in cwds_used), cwds_used
    assert all(c != p.Path("/wrong/default/repo") for c in cwds_used)
    assert p.REPO_ROOT == p.Path("/wrong/default/repo")


def test_advance_pipeline_merge_uses_plan_repo_root(plan_dir, monkeypatch, tmp_path):
    real_repo = tmp_path / "real-repo"
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "REPO_ROOT", p.Path("/wrong/default/repo"))
    _write_manifest(plan_dir, "rrmerge", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": str(tmp_path / "wt")},
    })
    manifest_path = plan_dir / "rrmerge.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["repo_root"] = str(real_repo)
    manifest_path.write_text(json.dumps(manifest))

    cleanup_cwds = []

    def _fake_run(cmd, cwd=None, **kwargs):
        if cmd[:2] == ["git", "worktree"] or cmd[:2] == ["git", "branch"] or cmd[:2] == ["git", "push"]:
            cleanup_cwds.append(cwd)
        class Result:
            returncode = 0
            stdout = "merged\n"
            stderr = ""
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    p.advance_pipeline("rrmerge")

    assert cleanup_cwds, "expected cleanup git commands to run"
    assert all(c == real_repo for c in cleanup_cwds)
    assert p.REPO_ROOT == p.Path("/wrong/default/repo")


def test_request_decision_loads_policy_override_from_plan_repo_root(
    plan_dir, agents_dir, monkeypatch, tmp_path,
):
    real_repo = tmp_path / "real-repo"
    real_repo.mkdir()
    (real_repo / ".overlord-policy.md").write_text("Per-repo override text.")
    monkeypatch.setattr(p, "REPO_ROOT", p.Path("/wrong/default/repo"))
    monkeypatch.setattr(p, "POLICY_PATH", tmp_path / "nonexistent-global-policy.md")
    _write_manifest(plan_dir, "rrdec", {})
    manifest_path = plan_dir / "rrdec.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["repo_root"] = str(real_repo)
    manifest_path.write_text(json.dumps(manifest))

    captured_prompt = {}

    def _fake_invoke(prompt):
        captured_prompt["text"] = prompt
        return "RULING: x\nTIER: routine\nRISK: low\nRATIONALE: y\nNOTIFY_USER: no\n"

    monkeypatch.setattr(p, "_invoke_overlord", _fake_invoke)

    p.request_decision("rrdec", "S1", "q", ["a"])

    assert "Per-repo override text." in captured_prompt["text"]
    assert p.REPO_ROOT == p.Path("/wrong/default/repo")


# ---------- Usage probe ----------
def test_parse_usage_output_extracts_session_and_week():
    result = p._parse_usage_output(SAMPLE_USAGE_TEXT)
    assert result["session_pct"] == 9
    assert result["session_reset"] == "Jun 18 at 11:59am (America/Chicago)"
    assert result["week_pct"] == 48
    assert result["week_reset"] == "Jun 23 at 9am (America/Chicago)"


def test_parse_usage_output_handles_100_percent():
    text = (
        "Current session: 100% used · resets Jun 18 at 11:59am (America/Chicago)\n"
        "Current week (all models): 100% used · resets Jun 23 at 9am (America/Chicago)\n"
    )
    result = p._parse_usage_output(text)
    assert result["session_pct"] == 100
    assert result["week_pct"] == 100


def test_parse_usage_output_raises_on_unparseable_text():
    with pytest.raises(ValueError):
        p._parse_usage_output("some unexpected format with no usage lines")


def test_run_usage_probe_parses_and_stamps_checked_at(monkeypatch):
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        class Result:
            returncode = 0
            stdout = json.dumps({"type": "result", "result": SAMPLE_USAGE_TEXT})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)

    result = p._run_usage_probe()

    assert calls == [["claude", "-p", "/cost", "--output-format", "json"]]
    assert result["session_pct"] == 9
    assert result["week_pct"] == 48
    assert "checked_at" in result


def test_run_usage_probe_raises_on_invalid_json(monkeypatch):
    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = "not json"
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)

    with pytest.raises(RuntimeError):
        p._run_usage_probe()


def test_write_and_read_usage_state_roundtrip(usage_state_path):
    p._write_usage_state({"session_pct": 9, "week_pct": 48, "checked_at": "x"})
    assert p._read_usage_state() == {"session_pct": 9, "week_pct": 48, "checked_at": "x"}


def test_read_usage_state_missing_file_returns_empty_dict(usage_state_path):
    assert p._read_usage_state() == {}


@pytest.mark.parametrize("prev_paused,session_pct,week_pct,expected", [
    (False, 50, 10, False),
    (False, 90, 10, True),
    (False, 10, 90, True),
    (False, 89, 10, False),
    (True, 80, 10, True),
    (True, 70, 10, True),
    (True, 69, 10, False),
    (True, 10, 75, True),
])
def test_usage_gate(prev_paused, session_pct, week_pct, expected):
    assert p._usage_gate(prev_paused, session_pct, week_pct) is expected


def test_usage_gate_session_and_week_have_independent_pause_thresholds(monkeypatch):
    monkeypatch.setattr(p, "SESSION_PAUSE_THRESHOLD", 80)
    monkeypatch.setattr(p, "WEEK_PAUSE_THRESHOLD", 95)

    # Week at 85% would have tripped the old shared 80% threshold, but
    # week's own threshold (95) is not yet reached, and session is low.
    assert p._usage_gate(False, session_pct=10, week_pct=85) is False
    # Session alone crossing its own (lower) threshold still trips it.
    assert p._usage_gate(False, session_pct=80, week_pct=10) is True
    # Week crossing its own (higher) threshold also trips it.
    assert p._usage_gate(False, session_pct=10, week_pct=95) is True


def test_usage_gate_session_and_week_have_independent_resume_thresholds(monkeypatch):
    monkeypatch.setattr(p, "SESSION_RESUME_THRESHOLD", 60)
    monkeypatch.setattr(p, "WEEK_RESUME_THRESHOLD", 75)

    # Already paused; week is still above its own resume threshold even
    # though it's below the (lower) session resume threshold - stays paused.
    assert p._usage_gate(True, session_pct=10, week_pct=80) is True
    # Both windows have dropped below their own resume thresholds - resumes.
    assert p._usage_gate(True, session_pct=10, week_pct=70) is False


def test_check_usage_carries_paused_hysteresis_from_previous_state(usage_state_path, monkeypatch):
    usage_state_path.write_text(json.dumps(
        {"session_pct": 95, "week_pct": 10, "paused": True, "checked_at": "x"}
    ))

    def _fake_run(cmd, **kwargs):
        text = (
            "Current session: 80% used · resets Jun 18 at 11:59am (America/Chicago)\n"
            "Current week (all models): 10% used · resets Jun 23 at 9am (America/Chicago)\n"
        )
        class Result:
            returncode = 0
            stdout = json.dumps({"result": text})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)

    result = p.check_usage()
    assert result["paused"] is True


def test_check_usage_clears_paused_once_below_resume_threshold(usage_state_path, monkeypatch):
    usage_state_path.write_text(json.dumps(
        {"session_pct": 95, "week_pct": 10, "paused": True, "checked_at": "x"}
    ))

    def _fake_run(cmd, **kwargs):
        text = (
            "Current session: 50% used · resets Jun 18 at 11:59am (America/Chicago)\n"
            "Current week (all models): 10% used · resets Jun 23 at 9am (America/Chicago)\n"
        )
        class Result:
            returncode = 0
            stdout = json.dumps({"result": text})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)

    result = p.check_usage()
    assert result["paused"] is False


def test_check_usage_tool_probes_and_persists_state(usage_state_path, monkeypatch):
    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps({"type": "result", "result": SAMPLE_USAGE_TEXT})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)

    result = p.check_usage()

    assert result["session_pct"] == 9
    assert result["week_pct"] == 48
    persisted = json.loads(usage_state_path.read_text())
    assert persisted["session_pct"] == 9
    assert persisted["week_pct"] == 48


def test_check_usage_falls_back_to_last_known_state_when_cli_omits_percentages(
    usage_state_path, monkeypatch,
):
    usage_state_path.write_text(json.dumps(
        {"session_pct": 91, "week_pct": 60, "paused": True, "checked_at": "old"}
    ))
    blackout_text = (
        "You are currently using your subscription to power your Claude Code usage\n\n"
        "What's contributing to your limits usage?\n"
        "Last 24h · 540 requests · 9 sessions\n"
    )

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps({"type": "result", "result": blackout_text})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)

    result = p.check_usage()

    assert result["session_pct"] == 91
    assert result["week_pct"] == 60
    assert result["paused"] is True
    assert result["checked_at"] != "old"
    persisted = json.loads(usage_state_path.read_text())
    assert persisted["session_pct"] == 91


def test_check_usage_raises_on_cli_omitting_percentages_with_no_prior_state(
    usage_state_path, monkeypatch,
):
    blackout_text = (
        "You are currently using your subscription to power your Claude Code usage\n\n"
        "What's contributing to your limits usage?\n"
    )

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps({"type": "result", "result": blackout_text})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)

    with pytest.raises(ValueError):
        p.check_usage()


def test_usage_state_age_seconds_returns_none_when_checked_at_missing():
    assert p._usage_state_age_seconds({}) is None


def test_usage_state_age_seconds_returns_none_when_checked_at_unparseable():
    assert p._usage_state_age_seconds({"checked_at": "old"}) is None


def test_usage_state_age_seconds_returns_elapsed_seconds_for_valid_timestamp():
    checked_at = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    age = p._usage_state_age_seconds({"checked_at": checked_at})
    assert age is not None
    assert 110 <= age <= 130


def test_check_usage_keeps_paused_when_blackout_is_within_staleness_window(
    usage_state_path, monkeypatch,
):
    recent = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    usage_state_path.write_text(json.dumps(
        {"session_pct": 91, "week_pct": 60, "paused": True, "checked_at": recent}
    ))
    blackout_text = (
        "You are currently using your subscription to power your Claude Code usage\n\n"
        "What's contributing to your limits usage?\n"
    )

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps({"type": "result", "result": blackout_text})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "USAGE_STALE_AFTER_SECONDS", 1800)

    result = p.check_usage()

    assert result["paused"] is True
    assert result.get("stale") is not True


def test_check_usage_clears_pause_when_blackout_outlasts_staleness_window(
    usage_state_path, monkeypatch,
):
    long_ago = (datetime.now(timezone.utc) - timedelta(seconds=3600)).isoformat()
    usage_state_path.write_text(json.dumps(
        {"session_pct": 91, "week_pct": 60, "paused": True, "checked_at": long_ago}
    ))
    blackout_text = (
        "You are currently using your subscription to power your Claude Code usage\n\n"
        "What's contributing to your limits usage?\n"
    )

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps({"type": "result", "result": blackout_text})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "USAGE_STALE_AFTER_SECONDS", 1800)

    result = p.check_usage()

    assert result["paused"] is False
    assert result["stale"] is True
    persisted = json.loads(usage_state_path.read_text())
    assert persisted["paused"] is False


def test_check_usage_repeated_blackouts_do_not_reset_the_staleness_clock(
    usage_state_path, monkeypatch,
):
    """Each fallback call bumps checked_at to "now" (it's still useful as
    "last time we tried"), so checked_at alone can't be the staleness clock -
    a poller calling check_usage every 60s would perpetually look "fresh" by
    that measure even though the actual session_pct/week_pct have not been
    re-measured in hours. The real measurement time (measured_at) must be
    carried forward unchanged across fallback calls instead."""
    long_ago = (datetime.now(timezone.utc) - timedelta(seconds=7200)).isoformat()
    recent = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    usage_state_path.write_text(json.dumps({
        "session_pct": 91, "week_pct": 60, "paused": True,
        "checked_at": recent, "measured_at": long_ago,
    }))
    blackout_text = (
        "You are currently using your subscription to power your Claude Code usage\n\n"
        "What's contributing to your limits usage?\n"
    )

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps({"type": "result", "result": blackout_text})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "USAGE_STALE_AFTER_SECONDS", 1800)

    result = p.check_usage()

    assert result["paused"] is False
    assert result["stale"] is True


def test_check_usage_fallback_preserves_measured_at_for_the_next_call(
    usage_state_path, monkeypatch,
):
    """measured_at must itself be persisted on every fallback call, not just
    read - otherwise it silently disappears after one call and the next
    call falls back to the (just-bumped) checked_at, recreating the exact
    bug this guards against one call later."""
    long_ago = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    usage_state_path.write_text(json.dumps({
        "session_pct": 91, "week_pct": 60, "paused": True,
        "checked_at": long_ago, "measured_at": long_ago,
    }))
    blackout_text = (
        "You are currently using your subscription to power your Claude Code usage\n\n"
        "What's contributing to your limits usage?\n"
    )

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps({"type": "result", "result": blackout_text})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "USAGE_STALE_AFTER_SECONDS", 1800)

    p.check_usage()
    persisted = json.loads(usage_state_path.read_text())

    assert persisted["measured_at"] == long_ago


def test_check_usage_success_path_sets_measured_at(usage_state_path, monkeypatch):
    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps({"type": "result", "result": SAMPLE_USAGE_TEXT})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)

    result = p.check_usage()

    assert result["measured_at"] == result["checked_at"]


# ---------- Usage gate blind-state visibility ----------
_BLACKOUT_TEXT = (
    "You are currently using your subscription to power your Claude Code usage\n\n"
    "What's contributing to your limits usage?\n"
)


def _blackout_run(cmd, **kwargs):
    class Result:
        returncode = 0
        stdout = json.dumps({"type": "result", "result": _BLACKOUT_TEXT})
        stderr = ""
    return Result()


def test_check_usage_marks_gate_blind_when_failing_open(usage_state_path, monkeypatch):
    """When the probe has been dark past the staleness window, failing the gate
    open must be recorded visibly (gate_blind + blind_since), not just printed."""
    long_ago = (datetime.now(timezone.utc) - timedelta(seconds=3600)).isoformat()
    usage_state_path.write_text(json.dumps({
        "session_pct": 91, "week_pct": 60, "paused": True,
        "checked_at": long_ago, "measured_at": long_ago,
    }))
    monkeypatch.setattr(backend.subprocess, "run", _blackout_run)
    monkeypatch.setattr(p, "USAGE_STALE_AFTER_SECONDS", 1800)

    result = p.check_usage()

    assert result["paused"] is False
    assert result["gate_blind"] is True
    assert result["blind_since"]  # a timestamp was stamped
    assert result["consecutive_parse_failures"] == 1


def test_check_usage_counts_parse_failures_before_going_blind(usage_state_path, monkeypatch):
    """A parse failure still inside the staleness window bumps the counter but
    does not (yet) blind the gate — the last measurement is still trusted."""
    recent = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    usage_state_path.write_text(json.dumps({
        "session_pct": 91, "week_pct": 60, "paused": True,
        "checked_at": recent, "measured_at": recent,
        "consecutive_parse_failures": 2,
    }))
    monkeypatch.setattr(backend.subprocess, "run", _blackout_run)
    monkeypatch.setattr(p, "USAGE_STALE_AFTER_SECONDS", 1800)

    result = p.check_usage()

    assert result["consecutive_parse_failures"] == 3
    assert result.get("gate_blind") is not True
    assert result["paused"] is True  # last measurement still trusted


def test_check_usage_preserves_blind_since_across_consecutive_blind_polls(
    usage_state_path, monkeypatch,
):
    long_ago = (datetime.now(timezone.utc) - timedelta(seconds=3600)).isoformat()
    blind_since = (datetime.now(timezone.utc) - timedelta(seconds=900)).isoformat()
    usage_state_path.write_text(json.dumps({
        "session_pct": 91, "week_pct": 60, "paused": False,
        "checked_at": long_ago, "measured_at": long_ago,
        "gate_blind": True, "blind_since": blind_since,
        "consecutive_parse_failures": 5,
    }))
    monkeypatch.setattr(backend.subprocess, "run", _blackout_run)
    monkeypatch.setattr(p, "USAGE_STALE_AFTER_SECONDS", 1800)

    result = p.check_usage()

    assert result["gate_blind"] is True
    assert result["blind_since"] == blind_since  # not reset
    assert result["consecutive_parse_failures"] == 6


def test_check_usage_clears_blind_state_on_successful_probe(usage_state_path, monkeypatch):
    """A real measurement clears the blind flags so the dashboard stops alerting."""
    long_ago = (datetime.now(timezone.utc) - timedelta(seconds=3600)).isoformat()
    usage_state_path.write_text(json.dumps({
        "session_pct": 50, "week_pct": 50, "paused": False,
        "checked_at": long_ago, "measured_at": long_ago,
        "gate_blind": True, "blind_since": long_ago,
        "consecutive_parse_failures": 9,
    }))

    def _ok_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps({"type": "result", "result": SAMPLE_USAGE_TEXT})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _ok_run)

    result = p.check_usage()

    assert result["gate_blind"] is False
    assert result["consecutive_parse_failures"] == 0
    assert result.get("blind_since") is None


# ---------- Merge adjudication (pure decision) ----------
@pytest.mark.parametrize("autonomy,threshold,verdict,risk,expected", [
    ("gated", "low", "APPROVE", "low", "merge"),
    ("gated", "low", "APPROVE", "medium", "park"),
    ("gated", "medium", "APPROVE", "medium", "merge"),
    ("gated", "low", "REQUEST_CHANGES", "low", "park"),
    ("full", "low", "APPROVE", "medium", "merge"),
    ("full", "low", "APPROVE", "high", "park"),
    ("dry-run", "high", "APPROVE", "low", "park"),
])
def test_merge_decision(monkeypatch, autonomy, threshold, verdict, risk, expected):
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", autonomy)
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", threshold)
    story = {"review_verdict": verdict, "risk": risk}
    assert p._merge_decision(story)["action"] == expected


# ---------- advance_pipeline concurrency lock ----------
# Overlapping advance_pipeline ticks for the same plan (e.g. launchd firing a
# burst of missed StartIntervals after the machine wakes from sleep) must not
# both see the same ready story and dispatch duplicate, colliding agents into
# the same worktree - that's what actually caused repeated zero-output agent
# deaths in production, not per-story flakiness.
def test_advance_pipeline_skips_when_another_tick_holds_the_lock(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "lk", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
    })

    def _boom(*a, **k):
        raise AssertionError("a locked-out tick must not dispatch anything")
    monkeypatch.setattr(p, "dispatch_story", _boom)

    lock_path = plan_dir / "lk.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = p.advance_pipeline("lk")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert result["ok"] is True
    assert result.get("skipped") == "locked"
    manifest = _read_manifest(plan_dir, "lk")
    assert manifest["stories"]["T1"]["status"] == "todo"


def test_advance_pipeline_proceeds_when_lock_is_free(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "lk2", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
    })
    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))

    result = p.advance_pipeline("lk2")

    assert result.get("skipped") is None
    assert dispatched == ["T1"]


def test_advance_pipeline_releases_lock_after_each_call(plan_dir, monkeypatch):
    # A held-then-released lock (the normal case: one tick finishes before
    # the next starts) must not leak into a permanent skip.
    _write_manifest(plan_dir, "lk3", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
    })
    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))

    p.advance_pipeline("lk3")
    result = p.advance_pipeline("lk3")

    assert result.get("skipped") is None


def test_advance_pipeline_lock_is_independent_per_plan(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "lkA", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
    })
    _write_manifest(plan_dir, "lkB", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
    })
    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append((plan, key)))

    lock_path = plan_dir / "lkA.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = p.advance_pipeline("lkB")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert result.get("skipped") is None
    assert ("lkB", "T1") in dispatched


# ---------- advance_pipeline orchestration ----------
# advance_pipeline is a coordinator; the per-story operations (dispatch_story,
# check_story_status, review_story, gh merge) are exercised by their own tests
# above, so here we substitute test doubles to verify routing and gating.
def test_advance_pipeline_dry_run_has_no_side_effects(plan_dir, monkeypatch):
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "dry-run")
    _write_manifest(plan_dir, "dr", {
        "T1": {"summary": "todo one", "status": "todo", "dependencies": []},
        "P1": {"summary": "ready pr", "status": "pr_open",
               "review_verdict": "APPROVE", "risk": "low", "worktree": "/x"},
    })

    def _boom(*a, **k):
        raise AssertionError("dry-run must not take actions")

    monkeypatch.setattr(p, "dispatch_story", _boom)
    monkeypatch.setattr(p, "_merge_pr", _boom)

    result = p.advance_pipeline("dr")
    assert result["dry_run"] is True
    assert "T1" in result["would_dispatch"]
    assert "P1" in result["would_merge_decisions"]


def test_advance_pipeline_gated_dispatches_merges_and_parks(plan_dir, monkeypatch):
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    _write_manifest(plan_dir, "go", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
        "P1": {"summary": "low approved", "status": "pr_open",
               "review_verdict": "APPROVE", "risk": "low", "worktree": "/x"},
        "P2": {"summary": "high approved", "status": "pr_open",
               "review_verdict": "APPROVE", "risk": "high", "worktree": "/y"},
    })

    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))
    merged = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged.append(key) or "merged")
    notes = []
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))

    result = p.advance_pipeline("go")
    assert dispatched == ["T1"]
    assert merged == ["P1"]
    assert "P1" in result["merged"]
    assert "P2" in result["parked"]
    assert "P2" in result["notify"]

    manifest = _read_manifest(plan_dir, "go")
    assert manifest["stories"]["P1"]["status"] == "done"
    assert manifest["stories"]["P2"]["status"] == "parked"


def test_advance_pipeline_merge_transitions_plane_issue_to_done(plan_dir, monkeypatch):
    # A story merged via advance_pipeline's automatic path must move the
    # Plane issue to Done too - otherwise it stays "In Progress" forever,
    # since dispatch_story is the only other place that touches Plane state.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    story_key = "11111111-1111-1111-1111-111111111111"
    _write_manifest(plan_dir, "planedone", {
        story_key: {"summary": "approved", "status": "pr_open",
                     "review_verdict": "APPROVE", "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_get_state", lambda group: f"state-{group}")

    patches = []
    monkeypatch.setattr(
        p, "plane_request",
        lambda method, path, **kw: patches.append((method, path, kw)),
    )

    p.advance_pipeline("planedone")

    assert ("PATCH", f"/projects/{p.PLANE_PROJECT}/work-items/{story_key}/",
            {"json": {"state": "state-completed"}}) in patches


def test_advance_pipeline_merge_tolerates_plane_failure(plan_dir, monkeypatch):
    # Mirrors dispatch_story's resilience: not every plan is Plane-backed, so
    # a Plane error must not block the local merge from completing.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    _write_manifest(plan_dir, "planefail", {
        "P1": {"summary": "approved", "status": "pr_open",
               "review_verdict": "APPROVE", "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(
        p, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )

    result = p.advance_pipeline("planefail")

    assert result["merged"] == ["P1"]
    assert _read_manifest(plan_dir, "planefail")["stories"]["P1"]["status"] == "done"


def test_approve_merge_merges_a_parked_approved_story(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "am", {
        "P1": {"summary": "approved but medium risk", "status": "parked",
               "review_verdict": "APPROVE", "risk": "medium", "worktree": "/x"},
    })
    merged = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged.append(key) or "merged")
    plane_calls = []
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: plane_calls.append(key))

    result = p.approve_merge("am", "P1")

    assert result["ok"] is True
    assert result["status"] == "done"
    assert merged == ["P1"]
    assert plane_calls == ["P1"]
    assert _read_manifest(plan_dir, "am")["stories"]["P1"]["status"] == "done"


def test_approve_merge_merges_a_pr_open_approved_story(plan_dir, monkeypatch):
    # A human may approve before the gate even runs (status still pr_open),
    # not only after it's been parked.
    _write_manifest(plan_dir, "am2", {
        "P1": {"summary": "approved, not yet adjudicated", "status": "pr_open",
               "review_verdict": "APPROVE", "risk": "high", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    result = p.approve_merge("am2", "P1")

    assert result["ok"] is True
    assert _read_manifest(plan_dir, "am2")["stories"]["P1"]["status"] == "done"


def test_approve_merge_rejects_story_without_approve_verdict(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "am3", {
        "P1": {"summary": "changes requested", "status": "parked",
               "review_verdict": "REQUEST_CHANGES", "risk": "medium", "worktree": "/x"},
    })
    merge_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merge_calls.append(key))

    result = p.approve_merge("am3", "P1")

    assert result["ok"] is False
    assert merge_calls == []
    assert _read_manifest(plan_dir, "am3")["stories"]["P1"]["status"] == "parked"


@pytest.mark.parametrize("status", ["todo", "in_progress", "done", "failed", "interrupted"])
def test_approve_merge_rejects_story_in_non_mergeable_status(plan_dir, monkeypatch, status):
    _write_manifest(plan_dir, "am4", {
        "P1": {"summary": "not ready", "status": status,
               "review_verdict": "APPROVE", "risk": "low", "worktree": "/x"},
    })
    merge_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merge_calls.append(key))

    result = p.approve_merge("am4", "P1")

    assert result["ok"] is False
    assert merge_calls == []


def test_approve_merge_unknown_story_returns_error(plan_dir):
    _write_manifest(plan_dir, "am5", {})
    result = p.approve_merge("am5", "nope")
    assert result["ok"] is False


def test_approve_merge_uses_plan_repo_root(plan_dir, monkeypatch, tmp_path):
    real_repo = tmp_path / "real-repo"
    monkeypatch.setattr(p, "REPO_ROOT", p.Path("/wrong/default/repo"))
    _write_manifest(plan_dir, "am6", {
        "P1": {"summary": "approved", "status": "parked", "review_verdict": "APPROVE",
               "risk": "medium", "worktree": str(tmp_path / "wt")},
    })
    manifest_path = plan_dir / "am6.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["repo_root"] = str(real_repo)
    manifest_path.write_text(json.dumps(manifest))

    seen_repo_roots = []

    def _fake_merge_pr(wt, key):
        seen_repo_roots.append(p.REPO_ROOT)
        return "merged"

    monkeypatch.setattr(p, "_merge_pr", _fake_merge_pr)
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    p.approve_merge("am6", "P1")

    assert seen_repo_roots == [real_repo]
    assert p.REPO_ROOT == p.Path("/wrong/default/repo")


def test_advance_pipeline_paused_interrupts_running_and_skips_new_work(
    plan_dir, usage_state_path, monkeypatch,
):
    usage_state_path.write_text(json.dumps({"session_pct": 95, "week_pct": 10, "paused": True}))
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    _write_manifest(plan_dir, "pause", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
        "R1": {"summary": "running", "status": "in_progress", "pid": 111, "worktree": "/x"},
        "TP1": {"summary": "awaiting review", "status": "tests_passed",
                "worktree": "/y", "risk": "low"},
    })

    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))
    reviewed = []
    monkeypatch.setattr(
        p, "review_story",
        lambda plan, key: reviewed.append(key) or {"status": "pr_open"},
    )
    interrupted = []
    monkeypatch.setattr(
        p, "interrupt_story",
        lambda plan, key: interrupted.append(key) or {"ok": True},
    )

    result = p.advance_pipeline("pause")

    assert result["paused"] is True
    assert dispatched == []
    assert reviewed == []
    assert interrupted == ["R1"]
    assert "R1" in result["interrupted"]


def test_role_resource_ok_auto_does_not_crash_and_is_ok_when_local_available(
    usage_state_path, monkeypatch,
):
    """PIPELINE_BACKEND_<ROLE>=auto must not reach get_backend with the literal
    'auto' (which raises ValueError). Under auto the role can take work whenever
    the local backend is healthy, even with Claude's usage gate tripped."""
    usage_state_path.write_text(json.dumps({"session_pct": 95, "week_pct": 95, "paused": True}))
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setattr(backend.OllamaDriver, "resource_status",
                        lambda self: {"ok": True, "reason": ""})

    ok, reason = p._role_resource_ok("dispatch")

    assert ok is True
    assert reason == ""


def test_role_resource_ok_auto_gated_only_when_both_backends_unavailable(
    usage_state_path, monkeypatch,
):
    """Under auto, the role is gated only when BOTH local and Claude are down;
    it then surfaces Claude's gate reason."""
    usage_state_path.write_text(json.dumps({"session_pct": 95, "week_pct": 95, "paused": True}))
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setattr(backend.OllamaDriver, "resource_status",
                        lambda self: {"ok": False, "reason": "Ollama unreachable"})

    ok, reason = p._role_resource_ok("dispatch")

    assert ok is False
    assert reason == "Claude usage gate tripped"


def test_list_ready_stories_resolves_summary_dependencies(plan_dir):
    """Dependencies expressed as a prerequisite's summary string (the documented
    save_plan schema) must resolve against done stories even when the manifest is
    keyed by UUID rather than by summary."""
    _write_manifest(plan_dir, "sdep", {
        "uuid-a": {"summary": "Foundation", "status": "done", "dependencies": []},
        "uuid-b": {"summary": "Builds on foundation", "status": "todo",
                   "dependencies": ["Foundation"]},
        "uuid-c": {"summary": "Blocked", "status": "todo",
                   "dependencies": ["Builds on foundation"]},
    })

    ready = p.list_ready_stories("sdep")

    assert [r["summary"] for r in ready] == ["Builds on foundation"]


def test_advance_pipeline_dispatches_story_with_summary_dependency(plan_dir, monkeypatch):
    """The dispatch tick must treat a satisfied summary-string dependency as met
    and dispatch the dependent story, not silently skip it forever."""
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "_role_resource_ok", lambda role: (True, ""))
    _write_manifest(plan_dir, "adep", {
        "uuid-a": {"summary": "Foundation", "status": "done", "dependencies": []},
        "uuid-b": {"summary": "Next", "status": "todo", "dependencies": ["Foundation"]},
    })
    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))
    monkeypatch.setattr(p, "check_story_status", lambda plan, key: {"status": "in_progress"})

    p.advance_pipeline("adep")

    assert dispatched == ["uuid-b"]


def test_advance_pipeline_local_dispatch_runs_while_claude_review_gated(
    plan_dir, usage_state_path, monkeypatch,
):
    """Step 5: dispatch on local + review on Claude. Claude usage is maxed,
    but local dispatch must still proceed (and not interrupt running local
    agents); only the Claude-backed review is deferred."""
    usage_state_path.write_text(json.dumps({"session_pct": 95, "week_pct": 10, "paused": True}))
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.delenv("PIPELINE_BACKEND_REVIEW", raising=False)  # -> claude
    # Local backend reports healthy without hitting a real Ollama.
    monkeypatch.setattr(backend.OllamaDriver, "resource_status", lambda self: {"ok": True, "reason": ""})

    _write_manifest(plan_dir, "split", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
        "R1": {"summary": "running", "status": "in_progress", "pid": 111, "worktree": "/x"},
        "TP1": {"summary": "awaiting review", "status": "tests_passed", "worktree": "/y", "risk": "low"},
    })
    dispatched, reviewed, interrupted = [], [], []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))
    monkeypatch.setattr(p, "review_story", lambda plan, key: reviewed.append(key) or {"status": "pr_open"})
    monkeypatch.setattr(p, "interrupt_story", lambda plan, key: interrupted.append(key) or {"ok": True})
    monkeypatch.setattr(p, "check_story_status", lambda plan, key: {"status": "in_progress"})

    result = p.advance_pipeline("split")

    assert result["dispatch_paused"] is False     # local dispatch not gated by Claude
    assert result["review_paused"] is True         # Claude review deferred
    assert dispatched == ["T1"]                     # dispatch proceeded
    assert interrupted == []                         # running local agent left alone
    assert reviewed == []                            # review deferred


def test_advance_pipeline_paused_still_processes_merges(plan_dir, usage_state_path, monkeypatch):
    usage_state_path.write_text(json.dumps({"session_pct": 95, "week_pct": 10, "paused": True}))
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    _write_manifest(plan_dir, "pausemerge", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    merged = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged.append(key) or "merged")

    result = p.advance_pipeline("pausemerge")
    assert merged == ["P1"]
    assert "P1" in result["merged"]


def test_advance_pipeline_merge_failure_retries_within_budget(plan_dir, monkeypatch):
    # A transient _merge_pr failure (e.g. gh hiccup) must not crash the tick or
    # burn the story: it stays pr_open with a bumped attempt counter so the next
    # tick retries, and the user is notified.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "mergeretry", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_merge_pr",
                        lambda wt, key: (_ for _ in ()).throw(RuntimeError("gh hiccup")))

    result = p.advance_pipeline("mergeretry")

    story = _read_manifest(plan_dir, "mergeretry")["stories"]["P1"]
    assert story["status"] == "pr_open"
    assert story["merge_attempts"] == 1
    assert result["merged"] == []
    assert result["failed"] == []
    assert "P1" in result["notify"]


def test_advance_pipeline_merge_failure_exhausts_budget(plan_dir, monkeypatch):
    # Once the attempt budget is spent, a persistently failing merge becomes a
    # hard failure that needs human intervention rather than retrying forever.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "mergegiveup", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x", "merge_attempts": 2},
    })
    monkeypatch.setattr(p, "_merge_pr",
                        lambda wt, key: (_ for _ in ()).throw(RuntimeError("still broken")))

    result = p.advance_pipeline("mergegiveup")

    story = _read_manifest(plan_dir, "mergegiveup")["stories"]["P1"]
    assert story["status"] == "failed"
    assert story["merge_attempts"] == 3
    assert "still broken" in story.get("merge_error", "")
    assert result["merged"] == []
    assert "P1" in result["failed"]
    assert "P1" in result["notify"]


def test_advance_pipeline_merge_success_clears_attempt_counter(plan_dir, monkeypatch):
    # A merge that finally succeeds after earlier failures must clear the
    # attempt counter so the story records a clean done.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    _write_manifest(plan_dir, "mergerecover", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x", "merge_attempts": 1},
    })
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    result = p.advance_pipeline("mergerecover")

    story = _read_manifest(plan_dir, "mergerecover")["stories"]["P1"]
    assert story["status"] == "done"
    assert "merge_attempts" not in story
    assert result["merged"] == ["P1"]


def test_approve_merge_returns_error_on_merge_failure(plan_dir, monkeypatch):
    # The manual override surfaces a merge failure as a structured error to the
    # human invoking it rather than raising an unhandled exception.
    _write_manifest(plan_dir, "ammergefail", {
        "P1": {"summary": "approved", "status": "parked", "review_verdict": "APPROVE",
               "risk": "medium", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_merge_pr",
                        lambda wt, key: (_ for _ in ()).throw(RuntimeError("gh down")))

    result = p.approve_merge("ammergefail", "P1")

    assert result["ok"] is False
    assert "gh down" in result["error"]
    assert _read_manifest(plan_dir, "ammergefail")["stories"]["P1"]["status"] == "parked"


def test_advance_pipeline_redispatches_changes_requested(plan_dir, monkeypatch):
    # A story the reviewer sent back must be dispatch-eligible so the next tick
    # picks it up and reworks it - otherwise it freezes forever.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    _write_manifest(plan_dir, "cr", {
        "S1": {"summary": "rework me", "status": "changes_requested",
               "dependencies": [], "worktree": "/x",
               "review_feedback": "fix the bug", "rework_attempts": 1},
    })
    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))

    p.advance_pipeline("cr")

    assert dispatched == ["S1"]


def test_advance_pipeline_dispatch_failure_retries_within_budget(plan_dir, monkeypatch):
    # A raising dispatch_story (bad git pull, backend hiccup) must not crash the
    # tick: the story keeps its dispatch-eligible status, its attempt counter is
    # bumped, and the user is notified so the next tick retries.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "DISPATCH_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "dispretry", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
    })
    monkeypatch.setattr(p, "dispatch_story",
                        lambda plan, key: (_ for _ in ()).throw(RuntimeError("git pull failed")))

    result = p.advance_pipeline("dispretry")

    story = _read_manifest(plan_dir, "dispretry")["stories"]["T1"]
    assert story["status"] == "todo"
    assert story["dispatch_attempts"] == 1
    assert "T1" not in result["failed"]
    assert "T1" in result["notify"]


def test_advance_pipeline_dispatch_failure_exhausts_budget(plan_dir, monkeypatch):
    # A persistently failing launch becomes a hard failure (terminal: failed is
    # not dispatch-eligible) rather than retrying every tick forever.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "DISPATCH_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "dispgiveup", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": [],
               "dispatch_attempts": 2},
    })
    monkeypatch.setattr(p, "dispatch_story",
                        lambda plan, key: (_ for _ in ()).throw(RuntimeError("still broken")))

    result = p.advance_pipeline("dispgiveup")

    story = _read_manifest(plan_dir, "dispgiveup")["stories"]["T1"]
    assert story["status"] == "failed"
    assert story["dispatch_attempts"] == 3
    assert "still broken" in story.get("dispatch_error", "")
    assert "T1" in result["failed"]
    assert "T1" in result["notify"]


def test_check_story_status_failed_launch_exhausts_budget(plan_dir, tmp_path, monkeypatch):
    # An empty agent.log is a failed launch. Within budget it stays interrupted
    # (redispatched); once the budget is spent it becomes a terminal failure.
    monkeypatch.setattr(p, "DISPATCH_MAX_ATTEMPTS", 3)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("")
    _write_manifest(plan_dir, "launchgiveup", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "dispatch_attempts": 2},
    })
    monkeypatch.setattr(p.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command",
                        lambda wt: (_ for _ in ()).throw(AssertionError("must not run tests")))

    result = p.check_story_status("launchgiveup", "S1")

    assert result["status"] == "failed"
    story = _read_manifest(plan_dir, "launchgiveup")["stories"]["S1"]
    assert story["status"] == "failed"
    assert story["dispatch_attempts"] == 3


def test_check_story_status_successful_run_clears_dispatch_attempts(plan_dir, tmp_path, monkeypatch):
    # Once a launch actually produces output and the tests run, the failed-launch
    # counter is cleared so earlier infra blips don't count against a clean run.
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("the agent did real work\n")
    _write_manifest(plan_dir, "launchclear", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "dispatch_attempts": 2},
    })
    monkeypatch.setattr(p.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))

    class _Done:
        returncode = 0
        stdout = "ok"
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (str(worktree), ["true"]))
    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: _Done())

    result = p.check_story_status("launchclear", "S1")

    assert result["status"] == "tests_passed"
    assert "dispatch_attempts" not in _read_manifest(plan_dir, "launchclear")["stories"]["S1"]


def test_plane_set_state_retries_then_succeeds(monkeypatch):
    # A transient Plane failure is retried within budget rather than dropped.
    monkeypatch.setattr(p, "PLANE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(p, "_resolve_issue_uuid", lambda key: "uuid-1")
    monkeypatch.setattr(p, "_get_state", lambda group: f"state-{group}")
    calls = []

    def _flaky(method, path, **kw):
        calls.append(path)
        if len(calls) < 2:
            raise RuntimeError("502")
        return {}
    monkeypatch.setattr(p, "plane_request", _flaky)

    assert p._plane_set_state("S1", "started") is True
    assert len(calls) == 2


def test_plane_set_state_gives_up_after_budget_and_notifies(plan_dir, monkeypatch):
    # A persistent Plane outage gives up after the budget WITHOUT raising (Plane
    # is best-effort) and records the drop durably instead of a silent print.
    monkeypatch.setattr(p, "PLANE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(p, "_resolve_issue_uuid", lambda key: "uuid-1")
    monkeypatch.setattr(p, "_get_state", lambda group: f"state-{group}")
    monkeypatch.setattr(p, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("plane down")))
    notes = []
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))

    result = p._plane_set_state("S1", "completed", plan_name="pl")

    assert result is False
    assert len(notes) == 1
    assert "plane down" in notes[0]


def test_count_in_progress_agents_counts_across_plans(plan_dir, monkeypatch):
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: None)
    _write_manifest(plan_dir, "cnt1", {
        "A1": {"summary": "a", "status": "in_progress", "pid": 1},
        "A2": {"summary": "b", "status": "in_progress", "pid": 2},
        "A3": {"summary": "c", "status": "todo"},
    })
    _write_manifest(plan_dir, "cnt2", {
        "B1": {"summary": "d", "status": "in_progress", "pid": 3},
        "B2": {"summary": "e", "status": "done"},
    })
    assert p._count_in_progress_agents() == 3


def test_count_in_progress_agents_ignores_status_without_pid(plan_dir):
    # A story can be marked in_progress by mark_story_in_progress without
    # ever having been dispatched (no pid) - must not count as a running agent.
    _write_manifest(plan_dir, "cnt3", {
        "A1": {"summary": "a", "status": "in_progress"},
    })
    assert p._count_in_progress_agents() == 0


def test_count_in_progress_agents_skips_dead_pids(plan_dir, monkeypatch):
    # A story can be stuck at in_progress with a pid whose process already
    # exited (e.g. another plan whose own advance_pipeline tick never ran
    # again to notice) - it must not permanently consume a concurrency slot.
    _write_manifest(plan_dir, "cnt4", {
        "A1": {"summary": "alive", "status": "in_progress", "pid": 111},
        "A2": {"summary": "dead", "status": "in_progress", "pid": 222},
    })

    def _fake_kill(pid, sig):
        if pid == 222:
            raise ProcessLookupError
        return None

    monkeypatch.setattr(p.os, "kill", _fake_kill)

    assert p._count_in_progress_agents() == 1


def test_advance_pipeline_caps_dispatch_at_max_concurrent_agents(plan_dir, monkeypatch):
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "MAX_CONCURRENT_AGENTS", 2)
    _write_manifest(plan_dir, "cap1", {
        "T1": {"summary": "one", "status": "todo", "dependencies": []},
        "T2": {"summary": "two", "status": "todo", "dependencies": []},
        "T3": {"summary": "three", "status": "todo", "dependencies": []},
    })

    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))

    result = p.advance_pipeline("cap1")
    assert dispatched == ["T1", "T2"]
    assert result["dispatched"] == ["T1", "T2"]


def test_advance_pipeline_cap_accounts_for_already_running_agents(plan_dir, monkeypatch):
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "MAX_CONCURRENT_AGENTS", 2)
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: None)
    _write_manifest(plan_dir, "cap2", {
        "R1": {"summary": "running", "status": "in_progress", "pid": 111, "worktree": "/x"},
        "T1": {"summary": "one", "status": "todo", "dependencies": []},
        "T2": {"summary": "two", "status": "todo", "dependencies": []},
    })

    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))
    monkeypatch.setattr(
        p, "check_story_status", lambda plan, key: {"status": "running"},
    )

    result = p.advance_pipeline("cap2")
    assert dispatched == ["T1"]
    assert result["dispatched"] == ["T1"]


def test_advance_pipeline_zero_max_concurrent_agents_means_unlimited(plan_dir, monkeypatch):
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "MAX_CONCURRENT_AGENTS", 0)
    _write_manifest(plan_dir, "cap3", {
        "T1": {"summary": "one", "status": "todo", "dependencies": []},
        "T2": {"summary": "two", "status": "todo", "dependencies": []},
        "T3": {"summary": "three", "status": "todo", "dependencies": []},
    })

    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))

    p.advance_pipeline("cap3")
    assert dispatched == ["T1", "T2", "T3"]


def test_advance_pipeline_not_paused_redispatches_interrupted_stories(
    plan_dir, usage_state_path, monkeypatch,
):
    usage_state_path.write_text(json.dumps({"session_pct": 20, "week_pct": 10, "paused": False}))
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    _write_manifest(plan_dir, "resume", {
        "S1": {"summary": "interrupted one", "status": "interrupted",
               "worktree": "/x", "dependencies": []},
    })
    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))

    p.advance_pipeline("resume")
    assert dispatched == ["S1"]


def test_advance_pipeline_plan_paused_skips_dispatch_review_and_merge(plan_dir, monkeypatch):
    # A plan-level pause (pause_plan) must stop a plan from being advanced
    # at all -- unlike the usage gate, it does not even adjudicate merges,
    # since the human asked for this specific plan to stop moving.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    (plan_dir / "halted.manifest.json").write_text(json.dumps({
        "epics": {},
        "paused": True,
        "stories": {
            "T1": {"summary": "todo", "status": "todo", "dependencies": []},
            "R1": {"summary": "running", "status": "in_progress", "pid": 111, "worktree": "/x"},
            "TP1": {"summary": "awaiting review", "status": "tests_passed",
                    "worktree": "/y", "risk": "low"},
            "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
                   "risk": "low", "worktree": "/z"},
        },
    }))

    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))
    reviewed = []
    monkeypatch.setattr(
        p, "review_story",
        lambda plan, key: reviewed.append(key) or {"status": "pr_open"},
    )
    merged = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged.append(key) or "merged")
    interrupted = []
    monkeypatch.setattr(
        p, "interrupt_story",
        lambda plan, key: interrupted.append(key) or {"ok": True},
    )

    result = p.advance_pipeline("halted")

    assert result == {"ok": True, "skipped": "plan_paused"}
    assert dispatched == []
    assert reviewed == []
    assert merged == []
    assert interrupted == ["R1"]


def test_advance_pipeline_plan_paused_with_no_running_story_is_a_pure_noop(plan_dir, monkeypatch):
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    (plan_dir / "halted2.manifest.json").write_text(json.dumps({
        "epics": {},
        "paused": True,
        "stories": {"T1": {"summary": "todo", "status": "todo", "dependencies": []}},
    }))
    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))

    result = p.advance_pipeline("halted2")

    assert result == {"ok": True, "skipped": "plan_paused"}
    assert dispatched == []


def test_pause_plan_sets_manifest_flag(plan_dir):
    _write_manifest(plan_dir, "tobehalted", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
    })

    result = p.pause_plan("tobehalted")

    assert result == {"ok": True, "plan_name": "tobehalted", "paused": True}
    assert _read_manifest(plan_dir, "tobehalted")["paused"] is True


def test_resume_plan_clears_manifest_flag(plan_dir):
    (plan_dir / "halted3.manifest.json").write_text(json.dumps({
        "epics": {}, "paused": True,
        "stories": {"T1": {"summary": "todo", "status": "todo", "dependencies": []}},
    }))

    result = p.resume_plan("halted3")

    assert result == {"ok": True, "plan_name": "halted3", "paused": False}
    assert _read_manifest(plan_dir, "halted3")["paused"] is False


def test_pause_plan_no_such_manifest_returns_error(plan_dir):
    result = p.pause_plan("never-ingested")
    assert result == {"ok": False, "error": "No manifest for never-ingested"}


def test_resume_plan_no_such_manifest_returns_error(plan_dir):
    result = p.resume_plan("never-ingested")
    assert result == {"ok": False, "error": "No manifest for never-ingested"}


def test_resume_plan_when_not_paused_is_a_noop(plan_dir):
    _write_manifest(plan_dir, "neverhalted", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
    })

    result = p.resume_plan("neverhalted")

    assert result == {"ok": True, "plan_name": "neverhalted", "paused": False}
    assert _read_manifest(plan_dir, "neverhalted")["paused"] is False


def test_advance_all_plans_runs_every_manifest(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "p1", {})
    _write_manifest(plan_dir, "p2", {})

    calls = []
    monkeypatch.setattr(
        p, "advance_pipeline",
        lambda plan_name: calls.append(plan_name) or {"ok": True, "plan": plan_name},
    )

    result = p.advance_all_plans()

    assert result["ok"] is True
    assert sorted(calls) == ["p1", "p2"]
    assert result["plans"]["p1"]["ok"] is True
    assert result["plans"]["p2"]["ok"] is True


def test_advance_all_plans_isolates_failures_and_continues(plan_dir, monkeypatch):
    """One plan crashing (e.g. a bad repo_root, a missing dependency tool)
    must not abort the whole batch -- other plans still need their tick."""
    _write_manifest(plan_dir, "p1", {})
    _write_manifest(plan_dir, "p2", {})

    def _fake_advance(plan_name):
        if plan_name == "p1":
            raise RuntimeError("boom")
        return {"ok": True, "plan": plan_name}

    monkeypatch.setattr(p, "advance_pipeline", _fake_advance)

    result = p.advance_all_plans()

    assert result["ok"] is True
    assert result["plans"]["p1"]["ok"] is False
    assert "boom" in result["plans"]["p1"]["error"]
    assert result["plans"]["p2"]["ok"] is True


def test_advance_all_plans_with_no_manifests_returns_empty(plan_dir):
    result = p.advance_all_plans()
    assert result == {"ok": True, "plans": {}}


def test_advance_all_plans_ignores_unignested_plan_json(plan_dir, monkeypatch):
    (plan_dir / "p3.json").write_text(json.dumps({"epics": []}))

    calls = []
    monkeypatch.setattr(
        p, "advance_pipeline",
        lambda plan_name: calls.append(plan_name) or {"ok": True},
    )

    result = p.advance_all_plans()
    assert calls == []
    assert result["plans"] == {}


def test_check_story_status_passing_tests_is_not_done(plan_dir, monkeypatch):
    """"done" must mean merged. A story whose tests just passed is only
    ready for review — conflating the two lets it both skip review (never
    retried, since advance_pipeline only re-checks "in_progress" stories)
    and falsely satisfy other stories' dependency gate before it merges."""
    _write_manifest(plan_dir, "cs", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(plan_dir / "wt")},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))

    class Result:
        stdout = ""
        returncode = 0

    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: Result())

    result = p.check_story_status("cs", "S1")
    assert result["status"] == "tests_passed"
    manifest = _read_manifest(plan_dir, "cs")
    assert manifest["stories"]["S1"]["status"] == "tests_passed"


def test_check_story_status_treats_empty_agent_log_as_infra_failure(
    plan_dir, tmp_path, monkeypatch,
):
    """A 0-byte agent.log after the process has exited means the headless
    agent never produced any output - almost certainly a failed launch, not
    a real attempt at the story. Running the test suite against the
    untouched worktree in that case just records a misleading "failed" for
    work that was never tried, and (unlike "failed") nothing ever retries
    it. Treat it like "interrupted" instead, which advance_pipeline already
    redispatches automatically."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("")
    _write_manifest(plan_dir, "es", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))

    def _fail_if_called(*a, **k):
        raise AssertionError("test command should not run against an untouched worktree")
    monkeypatch.setattr(p, "detect_test_command", _fail_if_called)

    result = p.check_story_status("es", "S1")

    assert result["status"] == "interrupted"
    manifest = _read_manifest(plan_dir, "es")
    assert manifest["stories"]["S1"]["status"] == "interrupted"


def test_check_story_status_runs_tests_normally_when_agent_log_has_content(
    plan_dir, tmp_path, monkeypatch,
):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("Implemented the thing.\nCommitted.\n")
    _write_manifest(plan_dir, "ns", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["false"]))

    class Result:
        stdout = "1 test failed"
        returncode = 1

    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: Result())

    result = p.check_story_status("ns", "S1")

    assert result["status"] == "failed"
    manifest = _read_manifest(plan_dir, "ns")
    assert manifest["stories"]["S1"]["status"] == "failed"


def test_checkpoint_commits_and_records_journal_entry(plan_dir, tmp_path, monkeypatch):
    worktree = str(tmp_path / "wt")
    _write_manifest(plan_dir, "ck", {
        "S1": {"summary": "thing", "status": "in_progress", "worktree": worktree},
    })

    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        class Result:
            returncode = 0
            stdout = "abc123\n" if cmd[:2] == ["git", "rev-parse"] else ""
            stderr = ""
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    result = p.checkpoint(
        "ck", "S1", "step-1", "Implemented the parser",
        next_hint="write tests for edge cases",
    )

    assert result["ok"] is True
    assert result["commit"] == "abc123"
    assert result["step"] == "step-1"

    assert ["git", "add", "-A"] in calls
    assert ["git", "reset", "-q", "--", "agent.log"] in calls
    commit_calls = [c for c in calls if c[:2] == ["git", "commit"]]
    assert commit_calls and commit_calls[0][-1] == "wip(S1): step-1"

    journal = json.loads((plan_dir / "ck.S1.journal.json").read_text())
    assert len(journal) == 1
    assert journal[0]["step"] == "step-1"
    assert journal[0]["summary"] == "Implemented the parser"
    assert journal[0]["next_hint"] == "write tests for edge cases"
    assert journal[0]["commit"] == "abc123"
    assert "ts" in journal[0]


def test_checkpoint_appends_multiple_entries_in_order(plan_dir, tmp_path, monkeypatch):
    worktree = str(tmp_path / "wt")
    _write_manifest(plan_dir, "ck2", {
        "S1": {"summary": "thing", "status": "in_progress", "worktree": worktree},
    })

    shas = iter(["sha-1", "sha-2"])

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = (next(shas) + "\n") if cmd[:2] == ["git", "rev-parse"] else ""
            stderr = ""
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    p.checkpoint("ck2", "S1", "step-1", "first")
    p.checkpoint("ck2", "S1", "step-2", "second")

    journal = json.loads((plan_dir / "ck2.S1.journal.json").read_text())
    assert [e["step"] for e in journal] == ["step-1", "step-2"]
    assert [e["commit"] for e in journal] == ["sha-1", "sha-2"]


def test_checkpoint_unknown_story_returns_error(plan_dir):
    _write_manifest(plan_dir, "ck3", {})
    result = p.checkpoint("ck3", "NOPE", "step-1", "summary")
    assert result["ok"] is False
    assert "NOPE" in result["error"]


def test_checkpoint_nothing_to_commit_still_records_journal(plan_dir, tmp_path, monkeypatch):
    """If the agent already committed its own work (e.g. via Bash), git commit
    finds nothing staged. The checkpoint must still succeed and record the
    current HEAD sha rather than failing the whole call."""
    worktree = str(tmp_path / "wt")
    _write_manifest(plan_dir, "ck4", {
        "S1": {"summary": "thing", "status": "in_progress", "worktree": worktree},
    })

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = ""
            stderr = ""
        if cmd[:2] == ["git", "commit"]:
            Result.returncode = 1
            Result.stdout = "nothing to commit, working tree clean\n"
        elif cmd[:2] == ["git", "rev-parse"]:
            Result.stdout = "existing-sha\n"
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    result = p.checkpoint("ck4", "S1", "step-1", "no new changes")
    assert result["ok"] is True
    assert result["commit"] == "existing-sha"


def test_checkpoint_nothing_to_commit_due_to_excluded_agent_log_still_records_journal(
    plan_dir, tmp_path, monkeypatch,
):
    """When the only untracked file is the excluded agent.log, git's "clean"
    message is "nothing added to commit but untracked files present" rather
    than "nothing to commit, working tree clean" - this must also count as
    a successful no-op, not an error."""
    worktree = str(tmp_path / "wt")
    _write_manifest(plan_dir, "ck5", {
        "S1": {"summary": "thing", "status": "in_progress", "worktree": worktree},
    })

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = ""
            stderr = ""
        if cmd[:2] == ["git", "commit"]:
            Result.returncode = 1
            Result.stdout = (
                "On branch agent/x\n\nUntracked files:\n"
                '  (use "git add <file>..." to include in what will be committed)\n'
                "\tagent.log\n\n"
                "nothing added to commit but untracked files present "
                '(use "git add" to track)\n'
            )
        elif cmd[:2] == ["git", "rev-parse"]:
            Result.stdout = "existing-sha\n"
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    result = p.checkpoint("ck5", "S1", "step-1", "no new changes")
    assert result["ok"] is True
    assert result["commit"] == "existing-sha"


def test_checkpoint_raises_on_real_commit_failure(plan_dir, tmp_path, monkeypatch):
    worktree = str(tmp_path / "wt")
    _write_manifest(plan_dir, "ck5", {
        "S1": {"summary": "thing", "status": "in_progress", "worktree": worktree},
    })

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = ""
            stderr = ""
        if cmd[:2] == ["git", "commit"]:
            Result.returncode = 1
            Result.stderr = "fatal: unable to write new index file"
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    with pytest.raises(RuntimeError):
        p.checkpoint("ck5", "S1", "step-1", "summary")


def test_check_story_status_skips_test_run_for_interrupted_story(plan_dir):
    """An interrupted story is incomplete by definition — running its test
    suite would just record a spurious failure instead of staying resumable."""
    _write_manifest(plan_dir, "intr", {
        "S1": {"summary": "thing", "status": "interrupted", "pid": 999,
               "worktree": str(plan_dir / "wt"), "last_commit": "abc123"},
    })
    result = p.check_story_status("intr", "S1")
    assert result == {"status": "interrupted", "pid": 999}


class _FakeProc:
    def __init__(self, pid):
        self.pid = pid


def test_dispatch_story_fresh_creates_worktree_and_dispatches(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    _write_manifest(plan_dir, "ds", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })

    run_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: run_calls.append(cmd))
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: _FakeProc(1234))
    monkeypatch.setattr(
        p, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("ds", "S1")

    assert result["ok"] is True
    assert result["pid"] == 1234
    assert result["resumed"] is False
    assert ["git", "worktree", "add", "-b", "agent/s1", str(worktree_root / "S1")] in run_calls
    assert any(c[:2] == ["git", "pull"] for c in run_calls)

    manifest = _read_manifest(plan_dir, "ds")
    story = manifest["stories"]["S1"]
    assert story["status"] == "in_progress"
    assert story["pid"] == 1234
    assert story["worktree"] == str(worktree_root / "S1")


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
    monkeypatch.setattr(
        p, "plane_request",
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
    monkeypatch.setattr(
        p, "plane_request",
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
    monkeypatch.setattr(p, "plane_request",
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
    monkeypatch.setattr(
        p, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("ds3", "S1")

    assert result["resumed"] is True
    assert not any(c[:3] == ["git", "worktree", "add"] for c in run_calls)


# ---------- Local-first routing ----------

def test_route_dispatch_backend_low_risk_goes_local(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_RISK", "low")
    assert p._route_dispatch_backend({"risk": "low", "persona": "software-engineer"}) == "local"


def test_route_dispatch_backend_medium_risk_above_low_ceiling_goes_claude(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_RISK", "low")
    assert p._route_dispatch_backend({"risk": "medium", "persona": "software-engineer"}) == "claude"


def test_route_dispatch_backend_medium_risk_within_medium_ceiling_goes_local(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_RISK", "medium")
    assert p._route_dispatch_backend({"risk": "medium", "persona": "software-engineer"}) == "local"


def test_route_dispatch_backend_high_risk_above_medium_ceiling_goes_claude(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_RISK", "medium")
    assert p._route_dispatch_backend({"risk": "high", "persona": "software-engineer"}) == "claude"


def test_route_dispatch_backend_high_ceiling_allows_high_risk_local(monkeypatch):
    """Explicitly setting the ceiling to 'high' lets even high-risk go local."""
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_RISK", "high")
    assert p._route_dispatch_backend({"risk": "high", "persona": "software-engineer"}) == "local"


def test_route_dispatch_backend_security_persona_always_claude(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_RISK", "high")
    assert p._route_dispatch_backend({"risk": "low", "persona": "security-engineer"}) == "claude"


def test_route_dispatch_backend_missing_risk_defaults_low(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_RISK", "low")
    assert p._route_dispatch_backend({"persona": "software-engineer"}) == "local"


def test_dispatch_story_auto_routes_low_risk_local(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """PIPELINE_BACKEND_DISPATCH=auto sends a low-risk story to OllamaDriver."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_RISK", "low")
    _write_manifest(plan_dir, "auto1", {
        "S1": {"summary": "Thing", "agent_instructions": "Build it.",
               "status": "todo", "risk": "low", "dependencies": []},
    })
    popen_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: (popen_calls.append(cmd), _FakeProc(11))[1])
    monkeypatch.setattr(p, "plane_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("auto1", "S1")

    assert result["ok"] is True
    manifest = _read_manifest(plan_dir, "auto1")
    assert manifest["stories"]["S1"]["backend"] == "local"
    # OllamaDriver uses venv python, not "claude"
    assert popen_calls[0][0] != "claude"


def test_dispatch_story_auto_routes_high_risk_claude(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """PIPELINE_BACKEND_DISPATCH=auto sends a high-risk story to ClaudeCliDriver."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_RISK", "low")
    _write_manifest(plan_dir, "auto2", {
        "S1": {"summary": "Thing", "agent_instructions": "Build it.",
               "status": "todo", "risk": "high", "dependencies": []},
    })
    popen_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: (popen_calls.append(cmd), _FakeProc(22))[1])
    monkeypatch.setattr(p, "plane_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("auto2", "S1")

    manifest = _read_manifest(plan_dir, "auto2")
    assert manifest["stories"]["S1"]["backend"] == "claude"
    assert popen_calls[0][0] == "claude"


def test_dispatch_story_persists_backend_to_manifest(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """The resolved backend is written to story['backend'] so escalation sees it."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "pb1", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": []},
    })
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: _FakeProc(33))
    monkeypatch.setattr(p, "plane_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("pb1", "S1")

    assert _read_manifest(plan_dir, "pb1")["stories"]["S1"]["backend"] == "local"


def test_dispatch_story_honors_stored_backend_override(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """story['backend'] takes precedence over env, used by escalation to lock to Claude."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "pb2", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": [], "backend": "claude"},
    })
    popen_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: (popen_calls.append(cmd), _FakeProc(44))[1])
    monkeypatch.setattr(p, "plane_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("pb2", "S1")

    # Even though env says local, the stored override wins → ClaudeCliDriver
    assert popen_calls[0][0] == "claude"


def test_advance_pipeline_escalates_local_failure_to_claude(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A local agent failure triggers clean Claude escalation (not terminal failure)."""
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / "agent.log").write_text("some output\n")
    _write_manifest(plan_dir, "esc1", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "in_progress", "pid": 9001,
               "worktree": str(worktree_path),
               "log": str(worktree_path / "agent.log"),
               "backend": "local", "dependencies": []},
    })

    class _FailResult:
        stdout = "test failed"; stderr = ""; returncode = 1

    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: _FailResult())
    monkeypatch.setattr(p, "_role_resource_ok", lambda role: (True, ""))

    result = p.advance_pipeline("esc1")

    manifest = _read_manifest(plan_dir, "esc1")
    story = manifest["stories"]["S1"]
    assert story["backend"] == "claude"
    assert story["status"] == "todo"
    assert story["escalated"] is True
    assert "pid" not in story
    assert "S1" not in result.get("failed", [])
    notif = (plan_dir / "esc1.notifications.log").read_text()
    assert "escalating to Claude" in notif


def test_advance_pipeline_does_not_escalate_claude_failure(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A Claude-run failure is terminal (not escalated again)."""
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / "agent.log").write_text("some output\n")
    _write_manifest(plan_dir, "esc2", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "in_progress", "pid": 9002,
               "worktree": str(worktree_path),
               "log": str(worktree_path / "agent.log"),
               "backend": "claude", "dependencies": []},
    })

    class _FailResult:
        stdout = "test failed"; stderr = ""; returncode = 1

    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: _FailResult())
    monkeypatch.setattr(p, "_role_resource_ok", lambda role: (True, ""))

    result = p.advance_pipeline("esc2")

    manifest = _read_manifest(plan_dir, "esc2")
    story = manifest["stories"]["S1"]
    assert story["status"] == "failed"
    assert "S1" in result.get("failed", [])


def test_advance_pipeline_does_not_escalate_already_escalated(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A story that already has escalated=True is not escalated a second time."""
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / "agent.log").write_text("some output\n")
    _write_manifest(plan_dir, "esc3", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "in_progress", "pid": 9003,
               "worktree": str(worktree_path),
               "log": str(worktree_path / "agent.log"),
               "backend": "local", "escalated": True, "dependencies": []},
    })

    class _FailResult:
        stdout = "test failed"; stderr = ""; returncode = 1

    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: _FailResult())
    monkeypatch.setattr(p, "_role_resource_ok", lambda role: (True, ""))

    result = p.advance_pipeline("esc3")

    manifest = _read_manifest(plan_dir, "esc3")
    story = manifest["stories"]["S1"]
    assert story["status"] == "failed"
    assert "S1" in result.get("failed", [])


# ---------- Interrupt path ----------
def test_interrupt_story_sends_sigterm_and_checkpoints(plan_dir, tmp_path, monkeypatch):
    worktree = str(tmp_path / "wt")
    _write_manifest(plan_dir, "it", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": worktree},
    })

    killed = []
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = "sha-int\n" if cmd[:2] == ["git", "rev-parse"] else ""
            stderr = ""
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    result = p.interrupt_story("it", "S1")

    assert killed == [(4242, p.signal.SIGTERM)]
    assert result["ok"] is True
    assert result["status"] == "interrupted"
    assert result["commit"] == "sha-int"

    manifest = _read_manifest(plan_dir, "it")
    story = manifest["stories"]["S1"]
    assert story["status"] == "interrupted"
    assert story["last_commit"] == "sha-int"
    assert "interrupted_at" in story

    journal = json.loads((plan_dir / "it.S1.journal.json").read_text())
    assert journal[-1]["step"] == "interrupted"


def test_interrupt_story_handles_already_dead_process(plan_dir, tmp_path, monkeypatch):
    worktree = str(tmp_path / "wt")
    _write_manifest(plan_dir, "it2", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": worktree},
    })

    def _raise_kill(pid, sig):
        raise ProcessLookupError()

    monkeypatch.setattr(p.os, "kill", _raise_kill)

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = "sha-dead\n" if cmd[:2] == ["git", "rev-parse"] else ""
            stderr = ""
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    result = p.interrupt_story("it2", "S1")
    assert result["ok"] is True
    assert result["status"] == "interrupted"
    manifest = _read_manifest(plan_dir, "it2")
    assert manifest["stories"]["S1"]["status"] == "interrupted"


def test_interrupt_story_unknown_story_returns_error(plan_dir):
    _write_manifest(plan_dir, "it3", {})
    result = p.interrupt_story("it3", "NOPE")
    assert result["ok"] is False
    assert "NOPE" in result["error"]


def test_interrupt_story_not_dispatched_returns_error(plan_dir):
    _write_manifest(plan_dir, "it4", {
        "S1": {"summary": "thing", "status": "todo"},
    })
    result = p.interrupt_story("it4", "S1")
    assert result["ok"] is False
    assert "not dispatched" in result["error"].lower()


def test_advance_pipeline_retries_review_for_orphaned_tests_passed_story(plan_dir, monkeypatch):
    """A story stuck at tests_passed (e.g. review_story crashed mid-tick on
    a prior run) must be retried on the next tick, not silently ignored."""
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    _write_manifest(plan_dir, "orphan", {
        "S1": {"summary": "thing", "status": "tests_passed",
               "worktree": "/x", "risk": "low"},
    })

    reviewed = []
    monkeypatch.setattr(
        p, "review_story",
        lambda plan, key: reviewed.append(key) or {"status": "pr_open"},
    )

    result = p.advance_pipeline("orphan")
    assert reviewed == ["S1"]
    assert {"S1": "pr_open"} in result["advanced"]
