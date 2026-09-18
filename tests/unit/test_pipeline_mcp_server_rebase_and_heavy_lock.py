"""Tests for the pipeline MCP server: resume auto-rebase onto master and the heavy-build lock.

Split out of test_pipeline_mcp_server.py to keep it under the project's line-count target; shared fixtures/helpers moved to tests.unit._pipeline_mcp_server_test_helpers.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline import git_ops
from pipeline import server as p
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _STEP_CAP_MARKER_LOCAL,
    _clear_caches,
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _make_fake_git_run,
    _plane_configured,
    _read_manifest,
    _write_manifest,
    plan_dir,
)

# ---------- reset_false_positive_tests_passed.py unit tests ----------

_RESET_SCRIPT_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "reset_false_positive_tests_passed.py"
_reset_spec = importlib.util.spec_from_file_location(
    "reset_false_positive_tests_passed", _RESET_SCRIPT_PATH,
)
reset_script = importlib.util.module_from_spec(_reset_spec)
_reset_spec.loader.exec_module(reset_script)
sys.modules["reset_false_positive_tests_passed"] = reset_script


def _make_story(**overrides) -> dict:
    """A canonical tests_passed story with full review/dispatch bookkeeping.

    Used as the starting point for reset-script tests; override fields
    to construct variations (e.g. cleared fields, status='interrupted')."""
    base = {
        "summary": "thing",
        "agent_instructions": "",
        "dependencies": [],
        "persona": None,
        "model": None,
        "risk": "low",
        "status": "tests_passed",
        "backend": "local",
        "pid": 4242,
        "worktree": "/tmp/wt",
        "log": "/tmp/wt/agent.log",
        "review_verdict": "APPROVE",
        "review_feedback": "looks good",
        "rework_attempts": 2,
        "failure_reason": "old failure",
        "dispatch_attempts": 1,
    }
    base.update(overrides)
    return base


def test_reset_script_resets_empty_branch_false_positive(monkeypatch):
    """A story at tests_passed with no commits on its agent branch is
    the false-positive signature -> reset to interrupted and clear the
    review/dispatch bookkeeping."""
    manifest = {"stories": {"S1": _make_story()}}
    monkeypatch.setattr(reset_script.p, "_default_branch", lambda: "main")
    monkeypatch.setattr(reset_script.p, "_worktree_has_new_commits",
                        lambda *a, **k: False)

    action = reset_script.reset_story(manifest, "S1")

    assert action.startswith("RESET")
    story = manifest["stories"]["S1"]
    assert story["status"] == "interrupted"
    # Cleared fields:
    for field in ("review_verdict", "review_feedback", "rework_attempts",
                  "pid", "dispatch_attempts", "failure_reason"):
        assert field not in story, f"{field} should have been cleared"
    # Preserved fields (reused by dispatch_story's resume logic):
    assert story["worktree"] == "/tmp/wt"
    assert story["log"] == "/tmp/wt/agent.log"
    assert story["backend"] == "local"


def test_reset_script_skips_story_with_real_commits(monkeypatch):
    """A tests_passed story whose agent branch has real commits is a
    genuine success, not a false positive. Skip it; don't touch the
    manifest entry."""
    manifest = {"stories": {"S1": _make_story()}}
    monkeypatch.setattr(reset_script.p, "_default_branch", lambda: "main")
    monkeypatch.setattr(reset_script.p, "_worktree_has_new_commits",
                        lambda *a, **k: True)

    action = reset_script.reset_story(manifest, "S1")

    assert action.startswith("SKIP")
    story = manifest["stories"]["S1"]
    # Untouched:
    assert story["status"] == "tests_passed"
    assert story["review_verdict"] == "APPROVE"
    assert story["rework_attempts"] == 2
    assert story["pid"] == 4242


def test_reset_script_skips_non_tests_passed_stories(monkeypatch):
    """The script's signature check is `status == tests_passed`. Any
    other status (todo, in_progress, failed, parked, done, interrupted)
    is skipped without inspecting the worktree. This makes the script
    idempotent and safe to re-run after the gate marks a story failed.
    """
    manifest = {
        "stories": {
            "INTERRUPTED": _make_story(status="interrupted"),
            "FAILED":      _make_story(status="failed"),
            "TODO":        _make_story(status="todo"),
            "IN_PROGRESS": _make_story(status="in_progress", pid=9999),
        }
    }
    monkeypatch.setattr(reset_script.p, "_default_branch", lambda: "main")
    monkeypatch.setattr(reset_script.p, "_worktree_has_new_commits",
                        lambda *a, **k: False)

    for key in ("INTERRUPTED", "FAILED", "TODO", "IN_PROGRESS"):
        action = reset_script.reset_story(manifest, key)
        assert action.startswith("SKIP"), f"{key}: {action}"
        assert manifest["stories"][key]["status"] != "interrupted" or key == "INTERRUPTED"
    # In particular, IN_PROGRESS still has its pid (would have been
    # cleared if the script had wrongly fired).
    assert manifest["stories"]["IN_PROGRESS"]["pid"] == 9999


def test_reset_script_skips_missing_or_worktree_less_story():
    """A target UUID that isn't in the manifest, or has no recorded
    worktree path, is skipped without raising."""
    manifest_empty = {"stories": {}}
    assert reset_script.reset_story(manifest_empty, "GHOST").startswith("SKIP")

    manifest_no_wt = {"stories": {"S1": _make_story()}}
    manifest_no_wt["stories"]["S1"].pop("worktree")
    assert reset_script.reset_story(manifest_no_wt, "S1").startswith("SKIP")


def test_reset_script_main_writes_manifest_and_reports(monkeypatch, tmp_path):
    """End-to-end of `main()`: builds a tmp manifest with one
    false-positive and one legit tests_passed, runs main() against it,
    asserts only the false-positive was reset and the file was
    written atomically (tmp + rename). The PLAN_DIR/MANIFEST_PATH and
    TARGETS are redirected to test-local values so we don't touch the
    real manifest or depend on the production UUIDs."""
    manifest_path = tmp_path / "e2e-decentralized-messaging-roadmap.manifest.json"
    manifest_path.write_text(json.dumps({
        "stories": {
            "FP":  _make_story(summary="false positive"),
            "REAL": _make_story(summary="real success"),
        }
    }))
    monkeypatch.setattr(reset_script, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(reset_script, "TARGETS", ["FP", "REAL"])
    monkeypatch.setattr(reset_script.p, "_default_branch", lambda: "main")
    # FP has empty branch; REAL has real commits.
    _has_commits = {"FP": False, "REAL": True}

    def _stub(worktree, story_key, base_branch):
        return _has_commits.get(story_key, False)
    monkeypatch.setattr(reset_script.p, "_worktree_has_new_commits", _stub)

    rc = reset_script.main()

    assert rc == 0
    written = json.loads(manifest_path.read_text())
    assert written["stories"]["FP"]["status"] == "interrupted"
    assert written["stories"]["FP"]["worktree"] == "/tmp/wt"
    assert "review_verdict" not in written["stories"]["FP"]
    # REAL untouched.
    assert written["stories"]["REAL"]["status"] == "tests_passed"
    assert written["stories"]["REAL"]["review_verdict"] == "APPROVE"


def test_reset_script_main_is_noop_when_nothing_matches(monkeypatch, tmp_path):
    """If no target UUID matches the false-positive signature (e.g.
    all have been reset or never were false-positives), main() must
    NOT write the manifest — that would be a needless disk churn and
    would also bump the manifest's mtime, which the orchestrator
    relies on for change detection."""
    manifest_path = tmp_path / "e2e-decentralized-messaging-roadmap.manifest.json"
    original = {"stories": {"S1": _make_story()}}
    manifest_path.write_text(json.dumps(original))
    monkeypatch.setattr(reset_script, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(reset_script, "TARGETS", ["S1"])
    monkeypatch.setattr(reset_script.p, "_default_branch", lambda: "main")
    monkeypatch.setattr(reset_script.p, "_worktree_has_new_commits",
                        lambda *a, **k: True)  # everything is "real"

    rc = reset_script.main()

    assert rc == 0
    # File untouched: same content, no temp file left behind.
    assert json.loads(manifest_path.read_text()) == original
    assert not manifest_path.with_suffix(".json.tmp").exists()

def test_check_story_status_treats_empty_agent_log_as_infra_failure(
    plan_dir, tmp_path, monkeypatch,
):
    """A 0-byte agent.log past the startup grace window — after the process
    has exited — means the headless agent never produced any output, almost
    certainly a failed launch, not a real attempt at the story. Running the
    test suite against the untouched worktree in that case just records a
    misleading "failed" for work that was never tried, and (unlike
    "failed") nothing ever retries it. Treat it like "interrupted" instead,
    which advance_pipeline already redispatches automatically."""
    monkeypatch.setattr(p, "DISPATCH_STARTUP_GRACE_SECONDS", 0)
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


# ---------- Step-cap exit routing (regression guard for PR #49) ----------
#
# When the headless agent hits its step cap it prints a terminal marker on
# its last log line, exits with code 2, and has already WIP-committed. The
# bug fixed by this block: check_story_status used to ignore the marker and
# fall straight through to running the test suite against the WIP commit,
# marking the story `tests_passed` and making the incomplete work merge-
# eligible (which is how PR #49 / commit 90a3cf1 landed in master). These
# tests pin the new routing: marker -> interrupted, no test run, journal
# entry written so dispatch_story can resume.



def test_check_story_status_routes_step_cap_to_interrupted(
    plan_dir, tmp_path, monkeypatch,
):
    """Regression guard for PR #49: when the agent's last log line is the
    step-cap marker, the story must be marked interrupted (NOT tests_passed),
    no test suite is run, and a journal entry is appended so a subsequent
    dispatch_story call can resume."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(
        "Working on it...\n"
        "[step 12] bash: pytest -q\n"
        f"{_STEP_CAP_MARKER_LOCAL}\n"
    )
    _write_manifest(plan_dir, "cap1", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    # The test suite MUST NOT be invoked. detect_test_command is the gate
    # in front of subprocess.run for the test runner; if it gets called the
    # routing is broken and we'd silently re-introduce PR #49.
    def _fail_detect(*a, **k):
        raise AssertionError("detect_test_command must not run on a step-cap exit")
    monkeypatch.setattr(p, "detect_test_command", _fail_detect)
    # _commit_wip WILL be called (mirror interrupt_story); stub git so the
    # checkpoint path returns a clean sha without touching real git.
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    result = p.check_story_status("cap1", "S1")

    assert result["status"] == "interrupted"
    assert result["reason"] == "step_cap_reached"
    manifest = _read_manifest(plan_dir, "cap1")
    assert manifest["stories"]["S1"]["status"] == "interrupted"
    # Journal entry must exist so the resume path has context.
    journal = p._read_journal("cap1", "S1")
    assert any(e.get("step") == "step_cap_reached" for e in journal), journal


def test_check_story_status_step_cap_adds_cleanup_guidance_even_when_diagnosis_fails(
    plan_dir, tmp_path, monkeypatch,
):
    """Worktree-hygiene guidance must be applied UNCONDITIONALLY on every
    step-cap resume, independent of whether the diagnosis role succeeds -
    it is wired as a separate, unconditional call, not folded into
    _rebrief_step_cap_struggle's fail-open diagnosis path. Simulate the
    diagnosis role failing open (returns None, per diagnose_failure's
    documented contract) and confirm the cleanup guidance still lands in
    agent_instructions."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(
        "Working on it...\n"
        f"{_STEP_CAP_MARKER_LOCAL}\n"
    )
    _write_manifest(plan_dir, "cap2", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4243, "worktree": str(worktree),
               "agent_instructions": "GOAL: build the thing."},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command",
                         lambda *a, **k: (_ for _ in ()).throw(
                             AssertionError("must not run on a step-cap exit")))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))
    # Force the diagnosis role to fail open, exactly as diagnose_failure
    # does on an unconfigured/erroring role.
    monkeypatch.setattr(p, "diagnose_failure", lambda *a, **k: None)

    p.check_story_status("cap2", "S1")

    manifest = _read_manifest(plan_dir, "cap2")
    instructions = manifest["stories"]["S1"]["agent_instructions"]
    from pipeline import rebrief
    assert rebrief.CLEANUP_HEADER in instructions
    assert "GOAL: build the thing." in instructions
    # No diagnosis block, since diagnose_failure returned None.
    assert rebrief.DIAGNOSIS_HEADER not in instructions


def test_check_story_status_routes_infra_failure_to_interrupted_without_burning_rework(
    plan_dir, tmp_path, monkeypatch,
):
    """Found live 2026-07-22 (MODE-29-REVIEW-STORY-LOCK-GUARD): a dispatch
    that died on an Ollama 500 (or timeout) after chat()'s own retries and
    the 5xx trim-retry are exhausted got treated exactly like a real review
    cycle - the test suite ran against its incomplete WIP and, worse, the
    infra death counted against rework_attempts, parking a story partly on
    infrastructure flakiness the model had no way to avoid. The last line
    must route to interrupted (no test run, dispatch-eligible for a clean
    resume) with rework_attempts UNCHANGED - distinct from the STEP_CAP_MARKERS
    routing, which shares the interrupted/no-test-run behavior but is a
    capability signal, not an infra one, so it's allowed to feed the
    model-fallback-switching logic that this path must NOT trigger."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(
        "[boot] pid=123 model=gpt-oss:20b endpoint=http://localhost:11434 provider=ollama steps=60 timeout=5400.0s\n"
        "[step 17] LLM call failed: Server error '500 Internal Server Error' for url 'http://localhost:11434/api/chat'\n"
    )
    _write_manifest(plan_dir, "infra1", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree), "rework_attempts": 1},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))

    def _fail_detect(*a, **k):
        raise AssertionError("detect_test_command must not run on an infra-failure exit")
    monkeypatch.setattr(p, "detect_test_command", _fail_detect)
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    result = p.check_story_status("infra1", "S1")

    assert result["status"] == "interrupted"
    assert result["reason"] == "infra_failure"
    manifest = _read_manifest(plan_dir, "infra1")
    story = manifest["stories"]["S1"]
    assert story["status"] == "interrupted"
    assert story["rework_attempts"] == 1, (
        f"an infra death must not burn a rework attempt, got {story['rework_attempts']!r}"
    )
    journal = p._read_journal("infra1", "S1")
    assert any(e.get("step") == "infra_failure" for e in journal), journal


def test_check_story_status_infra_failure_does_not_trigger_model_fallback(
    plan_dir, tmp_path, monkeypatch,
):
    """An infra death is not evidence the MODEL is struggling - it must not
    feed the STEP_CAP_MARKERS branch's consecutive-failure model-fallback
    switch, even when the plan has opted in to local_model_fallback."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(
        "[step 5] LLM call failed after trim-retry: Server error '500'\n"
    )
    _write_manifest(plan_dir, "infra2", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "model": "gpt-oss:20b",
               "dispatched_model": "gpt-oss:20b", "step_cap_streak": 2,
               "step_cap_streak_model": "gpt-oss:20b"},
    })
    manifest_path = plan_dir / "infra2.manifest.json"
    m = json.loads(manifest_path.read_text())
    m["local_model_fallback"] = "devstral:24b"
    manifest_path.write_text(json.dumps(m))
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run tests")))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    p.check_story_status("infra2", "S1")

    manifest = _read_manifest(plan_dir, "infra2")
    story = manifest["stories"]["S1"]
    assert story["model"] == "gpt-oss:20b", "infra death must not switch the model"


# ---------- infra-failure streak: visibility + bound on a persistent
# condition (2026-07-29) ----------
# Unlike STEP_CAP_MARKERS, the infra-failure branch had no streak counter, no
# threshold, no fallback, and no _notify_user - a persistent infra condition
# (a wedged Ollama server, a model too large for available memory) looped
# silently forever: dispatch, die, interrupted, redispatch, die again, with
# nothing to show and no notification. These mirror the step-cap streak
# tests above but use SEPARATE fields (infra_failure_streak /
# infra_failure_streak_model) so an infra death still never feeds the
# step-cap model-switch logic (see the does_not_trigger_model_fallback test
# above, which stays valid unmodified: its streak of 1 is below threshold).

def test_check_story_status_infra_failure_streak_notifies_on_first_occurrence(
    plan_dir, tmp_path, monkeypatch,
):
    """A single infra death is worth surfacing immediately - unlike a
    step-cap hit (routine for a local model), a transport failure after
    chat()'s own retries AND the 5xx trim-retry are exhausted is unusual
    enough to be worth a notification on the very first occurrence, not just
    after a streak."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(
        "[step 5] LLM call failed after trim-retry: Server error '500'\n"
    )
    _write_manifest(plan_dir, "infra3", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "model": "gpt-oss:20b",
               "backend": "local", "dispatched_model": "gpt-oss:20b"},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run tests")))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    p.check_story_status("infra3", "S1")

    story = _read_manifest(plan_dir, "infra3")["stories"]["S1"]
    assert story["infra_failure_streak"] == 1
    assert story["infra_failure_streak_model"] == "gpt-oss:20b"
    notif = (plan_dir / "infra3.notifications.log").read_text()
    assert "infrastructure failure" in notif
    assert "gpt-oss:20b" in notif


def test_check_story_status_infra_failure_streak_switches_model_at_threshold(
    plan_dir, tmp_path, monkeypatch,
):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(
        "[step 5] LLM call failed after trim-retry: Server error '500'\n"
    )
    _write_manifest(plan_dir, "infra4", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "model": "gpt-oss:20b",
               "backend": "local", "dispatched_model": "gpt-oss:20b",
               "infra_failure_streak": p.INFRA_FAILURE_FALLBACK_THRESHOLD - 1,
               "infra_failure_streak_model": "gpt-oss:20b"},
    })
    manifest_path = plan_dir / "infra4.manifest.json"
    m = json.loads(manifest_path.read_text())
    m["local_model_fallback"] = "glm-5.2:cloud"
    manifest_path.write_text(json.dumps(m))
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run tests")))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    p.check_story_status("infra4", "S1")

    story = _read_manifest(plan_dir, "infra4")["stories"]["S1"]
    assert story["model"] == "glm-5.2:cloud"
    assert story["backend"] == "local"  # never claude
    assert "infra_failure_streak" not in story
    assert "infra_failure_streak_model" not in story
    notif = (plan_dir / "infra4.notifications.log").read_text()
    assert "switching to fallback model glm-5.2:cloud" in notif


def test_check_story_status_infra_failure_streak_escalates_to_claude_at_threshold(
    plan_dir, tmp_path, monkeypatch,
):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(
        "[step 5] LLM call failed after trim-retry: Server error '500'\n"
    )
    _write_manifest(plan_dir, "infra5", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "model": "gpt-oss:20b",
               "backend": "local", "dispatched_model": "gpt-oss:20b",
               "infra_failure_streak": p.INFRA_FAILURE_FALLBACK_THRESHOLD - 1,
               "infra_failure_streak_model": "gpt-oss:20b"},
    })
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run tests")))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    result = p.check_story_status("infra5", "S1")

    assert result == {"status": "todo", "reason": "infra_failure_escalated_to_claude", "pid": 4242}
    story = _read_manifest(plan_dir, "infra5")["stories"]["S1"]
    assert story["backend"] == "claude"
    assert story["escalated"] is True
    assert story["status"] == "todo"
    assert "infra_failure_streak" not in story
    assert "infra_failure_streak_model" not in story
    notif = (plan_dir / "infra5.notifications.log").read_text()
    assert "Claude" in notif
    assert "infrastructure failure" in notif


def test_escalate_to_claude_pops_infra_failure_streak_fields(
    plan_dir, tmp_path, monkeypatch,
):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    manifest_path = plan_dir / "escih.manifest.json"
    manifest = {
        "epics": {},
        "stories": {
            "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
                   "worktree": str(worktree), "backend": "local",
                   "infra_failure_streak": 3, "infra_failure_streak_model": "gpt-oss:20b",
                   "dispatch_attempts": 1, "dispatch_error": "boom"},
        },
    }
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    p._escalate_to_claude(manifest, "escih", "S1", manifest_path)

    story = manifest["stories"]["S1"]
    assert "infra_failure_streak" not in story
    assert "infra_failure_streak_model" not in story
    assert story["backend"] == "claude"


# ---------- _rebase_onto_master: pre-rebase WIP checkpoint ----------

def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def origin_clone_and_worktree(tmp_path):
    """Real origin + clone + linked worktree on branch ``agent/s1``.

    origin/main is advanced one commit past the branch's base using a
    brand-new file, so the rebase has real work to replay and can never
    conflict with the branch's own commits. Returns ``(repo, wt)`` where
    ``repo`` is the clone to use as REPO_ROOT.
    """
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-q", "-b", "main", cwd=seed)
    _git("config", "user.email", "t@e", cwd=seed)
    _git("config", "user.name", "t", cwd=seed)
    (seed / "README.md").write_text("seed\n")
    _git("add", "-A", cwd=seed)
    _git("commit", "-q", "-m", "seed", cwd=seed)

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(origin)],
                   check=True, capture_output=True, text=True)

    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)],
                   check=True, capture_output=True, text=True)
    _git("config", "user.email", "t@e", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    _git("checkout", "-q", "-b", "agent/s1", cwd=repo)
    (repo / "story.txt").write_text("story\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "story work", cwd=repo)
    _git("checkout", "-q", "main", cwd=repo)

    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", str(wt), "agent/s1", cwd=repo)

    (seed / "main_only.txt").write_text("main\n")
    _git("add", "-A", cwd=seed)
    _git("commit", "-q", "-m", "advance main", cwd=seed)
    _git("push", "-q", str(origin), "main", cwd=seed)
    return repo, wt


def _patch_rebase_env(monkeypatch, repo):
    monkeypatch.setattr(p, "REPO_ROOT", str(repo))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")


def test_rebase_commits_dirty_worktree_before_rebasing(
    origin_clone_and_worktree, monkeypatch,
):
    """Regression: a worktree with unstaged changes used to make every
    merge-gate rebase fail identically ("cannot rebase: You have unstaged
    changes") and the retry loop gave up after PIPELINE_MERGE_MAX_ATTEMPTS.
    The dirty state must be committed as a WIP checkpoint first, then the
    rebase must succeed."""
    repo, wt = origin_clone_and_worktree
    (wt / "story.txt").write_text("story\ndirty\n")  # tracked, unstaged

    calls = []
    real_commit_wip = git_ops._commit_wip

    def _spy_commit_wip(worktree, story_key, step, guard_against_deletion=False):
        calls.append(("commit_wip", story_key, step))
        return real_commit_wip(worktree, story_key, step, guard_against_deletion)

    monkeypatch.setattr(git_ops, "_commit_wip", _spy_commit_wip)
    real_run = subprocess.run

    def _spy_run(argv, **kwargs):
        calls.append(("run", argv[1]))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(p.subprocess, "run", _spy_run)
    _patch_rebase_env(monkeypatch, repo)

    rb = p._rebase_onto_master(str(wt), "agent/s1")

    assert rb["ok"] is True, rb
    # The checkpoint runs first, before any git subprocess the rebase issues.
    assert calls[0] == ("commit_wip", "merge-gate", "pre-rebase-checkpoint")
    assert ("run", "rebase") in calls
    # The dirty edit survived as a commit rather than being discarded.
    assert (wt / "story.txt").read_text() == "story\ndirty\n"
    subjects = subprocess.run(["git", "log", "--format=%s", "agent/s1"], cwd=wt,
                              capture_output=True, text=True, check=True).stdout
    assert "wip(merge-gate): pre-rebase-checkpoint" in subjects


def test_rebase_calls_commit_wip_even_when_worktree_is_clean(
    origin_clone_and_worktree, monkeypatch,
):
    """No uncommitted changes: _commit_wip is still called (it is a no-op
    then) and creates no extra commit, so the plain-success return shape and
    the branch's own commits are unchanged."""
    repo, wt = origin_clone_and_worktree

    calls = []
    real_commit_wip = git_ops._commit_wip

    def _spy_commit_wip(worktree, story_key, step, guard_against_deletion=False):
        calls.append((story_key, step))
        return real_commit_wip(worktree, story_key, step, guard_against_deletion)

    monkeypatch.setattr(git_ops, "_commit_wip", _spy_commit_wip)
    _patch_rebase_env(monkeypatch, repo)

    rb = p._rebase_onto_master(str(wt), "agent/s1")

    assert calls == [("merge-gate", "pre-rebase-checkpoint")]
    assert rb == {"ok": True, "conflict": False, "error": ""}
    subjects = subprocess.run(["git", "log", "--format=%s", "agent/s1"], cwd=wt,
                              capture_output=True, text=True, check=True).stdout
    assert "wip(merge-gate)" not in subjects
    assert "story work" in subjects


def test_rebase_survives_commit_wip_failure_and_still_attempts_rebase(
    origin_clone_and_worktree, monkeypatch,
):
    """Negative: if the WIP checkpoint itself fails (e.g. `git` missing), the
    failure must not escape - the rebase is still attempted and reports its
    own clear error, strictly no worse than before the checkpoint existed."""
    repo, wt = origin_clone_and_worktree
    (wt / "story.txt").write_text("story\ndirty\n")

    def _raise_commit_wip(*a, **k):
        raise FileNotFoundError("[Errno 2] No such file or directory: 'git'")

    monkeypatch.setattr(git_ops, "_commit_wip", _raise_commit_wip)
    ran = []
    real_run = subprocess.run

    def _spy_run(argv, **kwargs):
        ran.append(argv[1])
        return real_run(argv, **kwargs)

    monkeypatch.setattr(p.subprocess, "run", _spy_run)
    _patch_rebase_env(monkeypatch, repo)

    rb = p._rebase_onto_master(str(wt), "agent/s1")

    assert "rebase" in ran  # the rebase was still attempted
    assert rb["ok"] is False
    assert rb["conflict"] is False
    assert "unstaged" in rb["error"].lower()


def test_rebase_survives_non_oserror_commit_wip_failure(
    origin_clone_and_worktree, monkeypatch,
):
    """Negative: _commit_wip can also fail with a non-OSError (it raises
    RuntimeError on a genuine `git commit` failure and CalledProcessError when
    its own `git add -A` fails, e.g. a stale index.lock). Those must not escape
    either - _rebase_onto_master's contract is to never raise, so a clean-tree
    rebase still succeeds."""
    repo, wt = origin_clone_and_worktree

    def _raise_commit_wip(*a, **k):
        raise RuntimeError("git commit failed (exit 1): boom")

    monkeypatch.setattr(git_ops, "_commit_wip", _raise_commit_wip)
    _patch_rebase_env(monkeypatch, repo)

    rb = p._rebase_onto_master(str(wt), "agent/s1")

    assert rb == {"ok": True, "conflict": False, "error": ""}


