"""Tests for the pipeline MCP server: give-up classification.

Split out of test_pipeline_mcp_server.py to keep it under the project's line-count target; shared fixtures/helpers moved to tests.unit._pipeline_mcp_server_test_helpers.
"""
import json
import subprocess

from app import backend
from pipeline import checkpoint as pcheckpoint
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

    assert killed == [(4242, pcheckpoint.signal.SIGTERM)]
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

    # Pin the review resource gate open so the test exercises the orphan-retry
    # logic, not the configured review backend's runtime availability (which
    # depends on the live model_registry.json review provider and free memory).
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (True, ""))

    reviewed = []
    monkeypatch.setattr(
        p, "review_story",
        lambda plan, key: reviewed.append(key) or {"status": "pr_open"},
    )

    result = p.advance_pipeline("orphan")
    assert reviewed == ["S1"]
    assert {"S1": "pr_open"} in result["advanced"]


# ---------- Fix #1: harness-owned acceptance oracle ----------

def test_ingest_plan_round_trips_acceptance_field(
    plan_dir, monkeypatch, tmp_path,
):
    """`acceptance` is optional; when present on a source story it must be
    carried verbatim onto the manifest entry so dispatch_story can later
    forward it to the local driver."""
    monkeypatch.setattr(pt, "PLANE_API_KEY", "")
    monkeypatch.setattr(pt, "PLANE_WORKSPACE", "")
    monkeypatch.setattr(pt, "PLANE_PROJECT", "")
    (plan_dir / "p.json").write_text(json.dumps({
        "epics": [{"summary": "Epic", "stories": [
            {"key": "S1", "summary": "Do thing",
             "agent_instructions": "Build.",
             # OPSA-8: the ingest gate lints .py fixture sources with ruff's
             # defaults, so this placeholder must be lint-clean (an unused
             # `import pytest` would be rejected as F401 before the manifest
             # is ever written). The assertion below round-trips what the plan
             # declared, verbatim.
             "acceptance": [
                 {"path": "tests/test_x.py",
                  "source": "def test_x():\n    assert True\n"},
             ]},
        ]}],
        "repo_root": str(tmp_path),
    }))

    p.ingest_plan("p")

    manifest = json.loads((plan_dir / "p.manifest.json").read_text())
    story = manifest["stories"]["S1"]
    assert story["acceptance"] == [
        {"path": "tests/test_x.py",
         "source": "def test_x():\n    assert True\n"},
    ]


def test_ingest_plan_omits_acceptance_when_source_story_has_none(
    plan_dir, monkeypatch, tmp_path,
):
    """Backwards compat: stories without an acceptance block still work and
    end up with an empty acceptance list on the manifest."""
    monkeypatch.setattr(pt, "PLANE_API_KEY", "")
    monkeypatch.setattr(pt, "PLANE_WORKSPACE", "")
    monkeypatch.setattr(pt, "PLANE_PROJECT", "")
    (plan_dir / "p.json").write_text(json.dumps({
        "epics": [{"summary": "Epic", "stories": [
            {"key": "S1", "summary": "Do thing"},
        ]}],
        "repo_root": str(tmp_path),
    }))

    p.ingest_plan("p")

    story = json.loads((plan_dir / "p.manifest.json").read_text())["stories"]["S1"]
    assert story["acceptance"] == []


def test_ingest_plan_round_trips_tdd_split_opt_in(plan_dir, monkeypatch, tmp_path):
    """§2.4's story-level eligibility gate: an explicit story["tdd_split"]
    opt-in must survive ingest onto the manifest, since dispatch_story reads
    it from there, not from the plan JSON."""
    monkeypatch.setattr(pt, "PLANE_API_KEY", "")
    monkeypatch.setattr(pt, "PLANE_WORKSPACE", "")
    monkeypatch.setattr(pt, "PLANE_PROJECT", "")
    (plan_dir / "p.json").write_text(json.dumps({
        "epics": [{"summary": "Epic", "stories": [
            {"key": "S1", "summary": "Do thing", "agent_instructions": "Build.",
             "tdd_split": True},
        ]}],
        "repo_root": str(tmp_path),
    }))

    p.ingest_plan("p")

    story = json.loads((plan_dir / "p.manifest.json").read_text())["stories"]["S1"]
    assert story["tdd_split"] is True


def test_ingest_plan_defaults_tdd_split_to_false(plan_dir, monkeypatch, tmp_path):
    """Absent opt-in must default False - Secure Defaults, and matches
    dispatch_story's `story.get("tdd_split")` truthiness check."""
    monkeypatch.setattr(pt, "PLANE_API_KEY", "")
    monkeypatch.setattr(pt, "PLANE_WORKSPACE", "")
    monkeypatch.setattr(pt, "PLANE_PROJECT", "")
    (plan_dir / "p.json").write_text(json.dumps({
        "epics": [{"summary": "Epic", "stories": [
            {"key": "S1", "summary": "Do thing"},
        ]}],
        "repo_root": str(tmp_path),
    }))

    p.ingest_plan("p")

    story = json.loads((plan_dir / "p.manifest.json").read_text())["stories"]["S1"]
    assert story["tdd_split"] is False


# ---------- role_config propagation bug: ingest_plan silently drops it ----------

def test_ingest_plan_writes_role_config_into_manifest(plan_dir, monkeypatch, tmp_path):
    """A plan-level role_config block (documented alongside epics/stories -
    see save_plan's docstring and the README's "Per-role provider/model
    configuration" section) must land on the manifest, since
    _plan_role_config() only ever reads it from there, never from the plan
    JSON."""
    monkeypatch.setattr(pt, "PLANE_API_KEY", "")
    monkeypatch.setattr(pt, "PLANE_WORKSPACE", "")
    monkeypatch.setattr(pt, "PLANE_PROJECT", "")
    (plan_dir / "p.json").write_text(json.dumps({
        "epics": [{"summary": "Epic", "stories": [
            {"key": "S1", "summary": "Do thing", "agent_instructions": "Build."},
        ]}],
        "repo_root": str(tmp_path),
        "role_config": {"review": {"provider": "claude", "model": "sonnet"}},
    }))

    p.ingest_plan("p")

    manifest = json.loads((plan_dir / "p.manifest.json").read_text())
    assert manifest["role_config"] == {
        "review": {"provider": "claude", "model": "sonnet"},
    }


def test_ingest_plan_updates_role_config_on_reingest(plan_dir, monkeypatch, tmp_path):
    """Re-ingesting (merge mode, default overwrite=False) with a DIFFERENT
    role_config must fully replace the old one - role_config is an authored
    field refreshed on ingest, same as epics/stories, not deep-merged."""
    monkeypatch.setattr(pt, "PLANE_API_KEY", "")
    monkeypatch.setattr(pt, "PLANE_WORKSPACE", "")
    monkeypatch.setattr(pt, "PLANE_PROJECT", "")
    plan_path = plan_dir / "p.json"
    plan_path.write_text(json.dumps({
        "epics": [{"summary": "Epic", "stories": [
            {"key": "S1", "summary": "Do thing", "agent_instructions": "Build."},
        ]}],
        "repo_root": str(tmp_path),
        "role_config": {"review": {"provider": "claude", "model": "sonnet"}},
    }))
    p.ingest_plan("p")

    plan_path.write_text(json.dumps({
        "epics": [{"summary": "Epic", "stories": [
            {"key": "S1", "summary": "Do thing", "agent_instructions": "Build."},
        ]}],
        "repo_root": str(tmp_path),
        "role_config": {"overlord": {"provider": "ollama", "model": "devstral"}},
    }))
    p.ingest_plan("p")

    manifest = json.loads((plan_dir / "p.manifest.json").read_text())
    assert manifest["role_config"] == {
        "overlord": {"provider": "ollama", "model": "devstral"},
    }


def test_ingest_plan_preserves_role_config_when_absent_on_reingest(
    plan_dir, monkeypatch, tmp_path,
):
    """A re-ingest whose plan JSON has NO role_config key at all (e.g. a
    caller that only re-authors epics/stories) must leave the manifest's
    existing role_config untouched, not wipe it to {} - matching the
    documented contract that "top-level manifest keys outside
    epics/stories/repo_root ... carry over untouched"."""
    monkeypatch.setattr(pt, "PLANE_API_KEY", "")
    monkeypatch.setattr(pt, "PLANE_WORKSPACE", "")
    monkeypatch.setattr(pt, "PLANE_PROJECT", "")
    plan_path = plan_dir / "p.json"
    plan_path.write_text(json.dumps({
        "epics": [{"summary": "Epic", "stories": [
            {"key": "S1", "summary": "Do thing", "agent_instructions": "Build."},
        ]}],
        "repo_root": str(tmp_path),
        "role_config": {"review": {"provider": "claude", "model": "sonnet"}},
    }))
    p.ingest_plan("p")

    plan_path.write_text(json.dumps({
        "epics": [{"summary": "Epic", "stories": [
            {"key": "S1", "summary": "Do thing", "agent_instructions": "Build. v2"},
        ]}],
        "repo_root": str(tmp_path),
    }))
    p.ingest_plan("p")

    manifest = json.loads((plan_dir / "p.manifest.json").read_text())
    assert manifest["role_config"] == {
        "review": {"provider": "claude", "model": "sonnet"},
    }


def test_ingest_plan_overwrite_true_drops_role_config_when_absent(
    plan_dir, monkeypatch, tmp_path,
):
    """overwrite=True restores the old wholesale-replace behavior (drops
    anything not produced by this call) - so a re-ingest with overwrite=True
    and no role_config key must reset the manifest's role_config to {},
    matching how it already drops un-repeated stories/epics."""
    monkeypatch.setattr(pt, "PLANE_API_KEY", "")
    monkeypatch.setattr(pt, "PLANE_WORKSPACE", "")
    monkeypatch.setattr(pt, "PLANE_PROJECT", "")
    plan_path = plan_dir / "p.json"
    plan_path.write_text(json.dumps({
        "epics": [{"summary": "Epic", "stories": [
            {"key": "S1", "summary": "Do thing", "agent_instructions": "Build."},
        ]}],
        "repo_root": str(tmp_path),
        "role_config": {"review": {"provider": "claude", "model": "sonnet"}},
    }))
    p.ingest_plan("p")

    plan_path.write_text(json.dumps({
        "epics": [{"summary": "Epic", "stories": [
            {"key": "S1", "summary": "Do thing", "agent_instructions": "Build."},
        ]}],
        "repo_root": str(tmp_path),
    }))
    p.ingest_plan("p", overwrite=True)

    manifest = json.loads((plan_dir / "p.manifest.json").read_text())
    assert manifest["role_config"] == {}


def test_get_role_config_reports_ingested_role_config_override(
    plan_dir, agents_dir, monkeypatch, tmp_path,
):
    """The actual user-visible symptom: get_role_config(plan_name=...) must
    report the role_config authored in the plan and carried onto the
    manifest by ingest_plan, not the env/registry/persona default - verified
    via the tool function directly, not just the raw manifest dict.

    The plan pins review's model to "opus", deliberately different from the
    agents_dir fixture's code-reviewer.md ("sonnet", the fallback that wins
    when no role_config reaches the manifest), so a pre-fix run reports the
    fallback and mismatches this assertion instead of passing by
    coincidence."""
    monkeypatch.setattr(pt, "PLANE_API_KEY", "")
    monkeypatch.setattr(pt, "PLANE_WORKSPACE", "")
    monkeypatch.setattr(pt, "PLANE_PROJECT", "")
    monkeypatch.delenv("PIPELINE_BACKEND_REVIEW", raising=False)
    (plan_dir / "p.json").write_text(json.dumps({
        "epics": [{"summary": "Epic", "stories": [
            {"key": "S1", "summary": "Do thing", "agent_instructions": "Build."},
        ]}],
        "repo_root": str(tmp_path),
        "role_config": {"review": {"provider": "claude", "model": "opus"}},
    }))

    p.ingest_plan("p")

    result = p.get_role_config(plan_name="p")
    assert result["roles"]["review"] == {"provider": "claude", "model": "opus"}


def test_dispatch_story_writes_oracle_files_into_worktree(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """When a story carries `acceptance`, dispatch_story materializes each
    oracle file at its declared path inside the worktree BEFORE invoking the
    backend, so the local oracle-harness can grade against it on launch."""
    _write_manifest(plan_dir, "oracle_fresh", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": [],
               "acceptance": [
                   {"path": "tests/test_x.py", "source": "import pytest\n"},
               ]},
    })
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen",
                        lambda cmd, **kw: _FakeProc(4242))
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("oracle_fresh", "S1")

    wt = worktree_root / "S1"
    assert (wt / "tests/test_x.py").exists()
    assert (wt / "tests/test_x.py").read_text() == "import pytest\n"


def test_dispatch_story_forwards_acceptance_paths_to_local_driver(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """With `dispatch=local`, the local driver receives the acceptance paths
    as JSON via LOCAL_AGENT_ACCEPTANCE and is launched in oracle mode."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "oracle_env", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": [],
               "acceptance": [
                   {"path": "tests/test_x.py", "source": "import pytest\n"},
                   {"path": "tests/test_y.py", "source": "import pytest\n"},
               ]},
    })
    # TDD-split is unconditional for local-family dispatch now; neutralize it
    # here so this test's popen_calls captures only the main executor's
    # dispatch, not an incidental test-author sub-dispatch.
    monkeypatch.setattr(p, "_run_test_author_phase", lambda *a, **k: False)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(7777)

    # This story carries an `acceptance` block, so the pre-dispatch oracle
    # gate needs a real CompletedProcess from subprocess.run, not None.
    monkeypatch.setattr(
        p.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "assert 0", ""),
    )
    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("oracle_env", "S1")

    assert len(popen_calls) == 1
    env = popen_calls[0]["env"]
    assert env["LOCAL_AGENT_MODE"] == "oracle"
    assert json.loads(env["LOCAL_AGENT_ACCEPTANCE"]) == [
        "tests/test_x.py", "tests/test_y.py",
    ]


def test_dispatch_story_forwards_acceptance_paths_under_explicit_provider_name(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """T16: an explicitly-pinned provider name (e.g. "lmstudio"), not just
    the "local" alias, must also count as local-family for the acceptance
    passthrough gate."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "lmstudio")
    _write_manifest(plan_dir, "oracle_env_lmstudio", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": [],
               "acceptance": [
                   {"path": "tests/test_x.py", "source": "import pytest\n"},
               ]},
    })
    # TDD-split is unconditional for local-family dispatch now; neutralize it
    # here so this test's popen_calls captures only the main executor's
    # dispatch, not an incidental test-author sub-dispatch.
    monkeypatch.setattr(p, "_run_test_author_phase", lambda *a, **k: False)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(7778)

    # This story carries an `acceptance` block, so the pre-dispatch oracle
    # gate needs a real CompletedProcess from subprocess.run, not None.
    monkeypatch.setattr(
        p.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "assert 0", ""),
    )
    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("oracle_env_lmstudio", "S1")

    assert len(popen_calls) == 1
    env = popen_calls[0]["env"]
    assert env["LOCAL_AGENT_MODE"] == "oracle"
    assert json.loads(env["LOCAL_AGENT_ACCEPTANCE"]) == ["tests/test_x.py"]


def test_dispatch_story_omits_oracle_env_when_no_acceptance(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Regression guard: stories without an `acceptance` block must keep
    exactly the same env-var surface as before — no LOCAL_AGENT_ACCEPTANCE,
    no LOCAL_AGENT_MODE, no oracle script. Otherwise every existing plan
    silently switches harnesses."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "no_oracle", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": []},
    })

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(8888)

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("no_oracle", "S1")

    # test_author (claude/sonnet) issues its own leading Popen call first;
    # the executor's (local, oracle-relevant) call is the last one.
    env = popen_calls[-1]["env"]
    assert "LOCAL_AGENT_ACCEPTANCE" not in env
    assert "LOCAL_AGENT_MODE" not in env
    # base script (not the oracle variant)
    assert popen_calls[-1]["cmd"][1].endswith("scripts/local_agent.py")


def test_dispatch_story_skips_oracle_write_when_resumed(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Resumed/interrupted stories must NOT have their oracle files
    re-overwritten — the agent may have committed an evolved oracle in a WIP
    and we don't want to silently revert it."""
    wt = worktree_root / "S1"
    wt.mkdir()
    (wt / "tests").mkdir()
    (wt / "tests/test_x.py").write_text("# evolved by the agent\n")
    _write_manifest(plan_dir, "oracle_resume", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build.",
               "status": "interrupted",
               "dependencies": [],
               "acceptance": [
                   {"path": "tests/test_x.py", "source": "import pytest\n"},
               ]},
    })

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen",
                        lambda cmd, **kw: _FakeProc(1234))
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("oracle_resume", "S1")

    assert (wt / "tests/test_x.py").read_text() == "# evolved by the agent\n"


def test_dispatch_story_local_rework_resumes_transcript_when_present(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A rework redispatch on the local Ollama driver whose worktree already
    holds a transcript from the prior attempt must resume that transcript
    (LOCAL_AGENT_RESUME_TRANSCRIPT_PATH + LOCAL_AGENT_RESUME_APPEND_CONTENT)
    instead of rebuilding the from-scratch rework_instruction prompt."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    transcript_path = worktree_path / ".agent_transcript.json"
    transcript_path.write_text(json.dumps([
        {"role": "system", "content": "sys"}, {"role": "user", "content": "task"},
    ]))
    _write_manifest(plan_dir, "localrw", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "changes_requested", "worktree": str(worktree_path),
               "review_feedback": "The SQL is injectable; parameterize it."},
    })

    # Always-on: the rework planner now runs for local-family dispatch. This
    # test pins the transcript-resume path and the raw-feedback append format
    # (the planner fail-open case), so stub the planner to None rather than
    # reaching a live backend.
    monkeypatch.setattr(p, "_run_rework_planner", lambda *a, **k: None)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(6001)

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("localrw", "S1")

    assert result["resumed"] is True
    env = popen_calls[0]["env"]
    assert env["LOCAL_AGENT_RESUME_TRANSCRIPT_PATH"] == str(transcript_path)
    assert env["LOCAL_AGENT_RESUME_APPEND_CONTENT"] == (
        "The code reviewer REQUESTED CHANGES on your previous attempt. "
        "Address this feedback:\nThe SQL is injectable; parameterize it."
    )
    # The old-style rework_instruction must not be baked into the cold-start
    # task prompt in this path - the transcript already carries prior context
    # and the append content carries the new feedback.
    assert "REQUESTED CHANGES" not in env["LOCAL_AGENT_TASK"]
    assert "The SQL is injectable" not in env["LOCAL_AGENT_TASK"]


def test_dispatch_story_explicit_provider_rework_resumes_transcript_when_present(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """T16: an explicitly-pinned provider name (e.g. "lmstudio"), not just
    the "local" alias, must also count as local-family for the
    transcript-resume-on-rework gate."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "lmstudio")
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    transcript_path = worktree_path / ".agent_transcript.json"
    transcript_path.write_text(json.dumps([
        {"role": "system", "content": "sys"}, {"role": "user", "content": "task"},
    ]))
    _write_manifest(plan_dir, "lmstudiorw", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "changes_requested", "worktree": str(worktree_path),
               "review_feedback": "The SQL is injectable; parameterize it."},
    })

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(6005)

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("lmstudiorw", "S1")

    assert result["resumed"] is True
    env = popen_calls[0]["env"]
    assert env["LOCAL_AGENT_RESUME_TRANSCRIPT_PATH"] == str(transcript_path)


def test_dispatch_story_local_rework_surfaces_revised_agent_instructions(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A transcript-resume rework redispatch must surface an operator's
    patch_story edit to agent_instructions since the story's last dispatch -
    otherwise the resumed agent only ever sees the reviewer's raw feedback
    and a corrected instruction (e.g. "delete the redundant retry wrapper
    instead of adding a new one") is silently dropped, and the agent
    re-derives its own (possibly wrong) fix instead of the one it was given.
    Detected via `_dispatched_agent_instructions`, a snapshot this same
    function records on every dispatch (see the sibling
    test_dispatch_story_local_rework_records_dispatched_instructions_snapshot)."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    transcript_path = worktree_path / ".agent_transcript.json"
    transcript_path.write_text(json.dumps([
        {"role": "system", "content": "sys"}, {"role": "user", "content": "task"},
    ]))
    _write_manifest(plan_dir, "localrwrevised", {
        "S1": {"summary": "Do thing",
               "agent_instructions": "Delete the redundant retry wrapper in foo().",
               "_dispatched_agent_instructions": "Build it.",
               "status": "changes_requested", "worktree": str(worktree_path),
               "review_feedback": "The SQL is injectable; parameterize it."},
    })
    monkeypatch.setattr(p, "_run_rework_planner", lambda *a, **k: None)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(6004)

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("localrwrevised", "S1")

    env = popen_calls[0]["env"]
    append = env["LOCAL_AGENT_RESUME_APPEND_CONTENT"]
    assert "Delete the redundant retry wrapper in foo()." in append
    assert "revised" in append.lower() or "updated" in append.lower()
    # The reviewer's raw feedback must still be present too.
    assert "The SQL is injectable; parameterize it." in append


def test_dispatch_story_local_rework_omits_note_when_instructions_unchanged(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """When agent_instructions is identical to the snapshot recorded at the
    story's last dispatch, no "revised instructions" note is injected - the
    append content is exactly the existing feedback-only format. Guards
    against re-surfacing the same instructions (as noise) on every rework
    round when nothing was actually patched."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    transcript_path = worktree_path / ".agent_transcript.json"
    transcript_path.write_text(json.dumps([
        {"role": "system", "content": "sys"}, {"role": "user", "content": "task"},
    ]))
    _write_manifest(plan_dir, "localrwsame", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "_dispatched_agent_instructions": "Build it.",
               "status": "changes_requested", "worktree": str(worktree_path),
               "review_feedback": "The SQL is injectable; parameterize it."},
    })
    monkeypatch.setattr(p, "_run_rework_planner", lambda *a, **k: None)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(6006)

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("localrwsame", "S1")

    env = popen_calls[0]["env"]
    assert env["LOCAL_AGENT_RESUME_APPEND_CONTENT"] == (
        "The code reviewer REQUESTED CHANGES on your previous attempt. "
        "Address this feedback:\nThe SQL is injectable; parameterize it."
    )


def test_dispatch_story_records_dispatched_instructions_snapshot(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Every dispatch_story call (cold or resumed) must record the
    agent_instructions it just handed the agent as
    story["_dispatched_agent_instructions"], persisted to the manifest - the
    baseline the next rework redispatch diffs against to detect an
    operator's patch_story edit."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    transcript_path = worktree_path / ".agent_transcript.json"
    transcript_path.write_text(json.dumps([
        {"role": "system", "content": "sys"}, {"role": "user", "content": "task"},
    ]))
    manifest_path = plan_dir / "localrwsnap.manifest.json"
    _write_manifest(plan_dir, "localrwsnap", {
        "S1": {"summary": "Do thing",
               "agent_instructions": "Delete the redundant retry wrapper in foo().",
               "_dispatched_agent_instructions": "Build it.",
               "status": "changes_requested", "worktree": str(worktree_path),
               "review_feedback": "The SQL is injectable; parameterize it."},
    })
    monkeypatch.setattr(p, "_run_rework_planner", lambda *a, **k: None)
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen",
                        lambda cmd, env, **kw: _FakeProc(6007))
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("localrwsnap", "S1")

    saved = json.loads(manifest_path.read_text())
    assert saved["stories"]["S1"]["_dispatched_agent_instructions"] == (
        "Delete the redundant retry wrapper in foo()."
    )


def test_dispatch_story_local_rework_falls_back_when_transcript_missing(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """If the worktree has no transcript file (first dispatch predated this
    feature, ran on a different backend, or the file was cleaned up), the
    local-driver rework redispatch must fall back to the existing
    from-scratch rework_instruction prompt rather than crash."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    _write_manifest(plan_dir, "localrwmiss", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "changes_requested", "worktree": str(worktree_path),
               "review_feedback": "The SQL is injectable; parameterize it."},
    })

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(6002)

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("localrwmiss", "S1")

    assert result["resumed"] is True
    env = popen_calls[0]["env"]
    assert "LOCAL_AGENT_RESUME_TRANSCRIPT_PATH" not in env
    assert "LOCAL_AGENT_RESUME_APPEND_CONTENT" not in env
    assert "The SQL is injectable; parameterize it." in env["LOCAL_AGENT_TASK"]


def test_dispatch_story_local_rework_empty_review_feedback_skips_resume_path(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """An empty review_feedback string is falsy, matching the existing
    rework_instruction guard (`if review_feedback:`) - it must not trigger
    the transcript-resume path even when a transcript file exists."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / ".agent_transcript.json").write_text(json.dumps([
        {"role": "system", "content": "sys"}, {"role": "user", "content": "task"},
    ]))
    _write_manifest(plan_dir, "localrwempty", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "changes_requested", "worktree": str(worktree_path),
               "review_feedback": ""},
    })

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(6003)

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("localrwempty", "S1")

    env = popen_calls[0]["env"]
    assert "LOCAL_AGENT_RESUME_TRANSCRIPT_PATH" not in env
    assert "LOCAL_AGENT_RESUME_APPEND_CONTENT" not in env


def test_dispatch_story_claude_rework_unaffected_by_transcript_resume(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """The claude backend's rework redispatch must be completely unchanged by
    this feature: it never sees LOCAL_AGENT_* env vars and still builds the
    full from-scratch rework prompt via _build_dispatch_command, even when a
    transcript file happens to exist in the worktree (e.g. left over from a
    prior local-backend attempt before an escalation flip)."""
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / ".agent_transcript.json").write_text(json.dumps([
        {"role": "system", "content": "sys"}, {"role": "user", "content": "task"},
    ]))
    _write_manifest(plan_dir, "clauderw", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "changes_requested", "worktree": str(worktree_path),
               "review_feedback": "The SQL is injectable; parameterize it."},
    })

    popen_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen",
                        lambda cmd, **kw: popen_calls.append(cmd) or _FakeProc(6004))
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("clauderw", "S1")

    assert result["resumed"] is True
    prompt = popen_calls[0][popen_calls[0].index("-p") + 1]
    assert "The SQL is injectable; parameterize it." in prompt
    assert "REQUESTED CHANGES" in prompt


