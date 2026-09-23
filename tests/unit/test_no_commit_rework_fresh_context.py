"""LDC-9: a rework redispatch that ends with HEAD still at the last reviewed
commit must start the next rework cold.

Mode 27's guard (``pipeline/story_status.py``) already routes a no-new-commit
round back to ``changes_requested`` instead of stranding the story at
``tests_passed``. But the *next* rework redispatch resumed the same
``.agent_transcript.json``, replaying the confusion that produced the empty
round. This story makes that path:

  * delete the worktree's ``.agent_transcript.json`` (so the next rework
    starts from the brief at the last commit), and
  * fold a root-cause diagnosis into ``agent_instructions`` via the same
    fail-open ``_rebrief_step_cap_struggle`` helper the step-cap path uses.

The cap branch above it (attempts at ``REWORK_MAX_ATTEMPTS_NO_COMMIT``) is
deliberately unchanged: it still parks/escalates and must not consult the
diagnosis boundary or touch the transcript.

These tests drive the real ``check_story_status`` through the no-new-commit
path the way the existing Mode 27 tests do (dead pid, passing test command,
HEAD == ``last_reviewed_sha``) and stub only the diagnosis boundary
(``pipeline.server.diagnose_failure``, which ``_rebrief_step_cap_struggle``
reaches through its late-bound ``_ServerRef``).
"""
import json
from pathlib import Path

from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import rebrief
from pipeline import server as p
from pipeline import story_status as pstory_status
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _clear_caches,
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _plane_configured,
    _read_manifest,
    _write_manifest,
)

TRANSCRIPT_NAME = ".agent_transcript.json"
DIAGNOSIS_TEXT = "The agent re-ran the same failing test; fix the fixture."
ORIGINAL_BRIEF = "GOAL: build the thing."
HEAD_SHA = "abc123"


def _make_plan_dir(tmp_path, monkeypatch):
    """A hermetic PLAN_DIR, mirroring the shared ``plan_dir`` fixture.

    Defined locally (rather than importing the fixture) so this module's test
    signatures don't shadow an imported name - the repo's other split test
    files need a per-file F811 ignore for exactly that reason.
    """
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    # pipeline_persistence and pipeline_concurrency import PLAN_DIR from
    # pipeline_paths at module load and read it as a free var, so patches
    # must land on their own bindings too.
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


def _no_new_commit_setup(tmp_path, monkeypatch, *, rework_attempts=0,
                         transcript=True, diagnosis=DIAGNOSIS_TEXT,
                         auto_escalate=False):
    """Scaffolding for the Mode 27 no-new-commit path.

    Builds a worktree + manifest, mocks the pid dead (so check_story_status
    runs the tests), mocks test detection + the new-commits guard, routes
    ``git rev-parse HEAD`` to the story's ``last_reviewed_sha`` while every
    other subprocess call (the test command) succeeds, and stubs the
    diagnosis boundary. Returns ``(plan_dir, worktree, diagnosis_calls)``.
    """
    plan_dir = _make_plan_dir(tmp_path, monkeypatch)
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(
        "[step 1] bash: pytest -q\n"
        "[step 2] bash: pytest -q\n"
        "stuck re-running the same failing test\n"
    )
    if transcript:
        (worktree / TRANSCRIPT_NAME).write_text(
            json.dumps([{"role": "assistant", "content": "done"}])
        )
    story = {
        "summary": "thing",
        "status": "in_progress",
        "pid": 4242,
        "worktree": str(worktree),
        "rework_attempts": rework_attempts,
        "last_reviewed_sha": HEAD_SHA,
        "agent_instructions": ORIGINAL_BRIEF,
    }
    _write_manifest(plan_dir, "plan", {"S1": story})

    monkeypatch.setattr(
        p.os, "kill",
        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(p, "_escalate_review_to_claude", lambda *a, **k: None)
    monkeypatch.setattr(p, "_auto_escalation_enabled", lambda: auto_escalate)

    class Result:
        def __init__(self, stdout="", returncode=0):
            self.stdout = stdout
            self.returncode = returncode

    def run_mock(*args, **kwargs):
        if "rev-parse" in args[0]:
            return Result(stdout=HEAD_SHA + "\n")
        return Result()

    monkeypatch.setattr(p.subprocess, "run", run_mock)

    calls = []

    def _diagnose(*args, **kwargs):
        calls.append((args, kwargs))
        return diagnosis

    monkeypatch.setattr(p, "diagnose_failure", _diagnose)
    return plan_dir, worktree, calls


# ---------- happy path: transcript dropped + diagnosis folded ----------

def test_no_new_commit_rework_deletes_transcript_and_folds_diagnosis(
    tmp_path, monkeypatch,
):
    """The no-new-commit round must (a) delete the worktree transcript so the
    next rework starts cold from the brief and (b) fold the diagnosis into the
    persisted manifest's agent_instructions."""
    plan_dir, worktree, calls = _no_new_commit_setup(tmp_path, monkeypatch)

    result = p.check_story_status("plan", "S1")

    assert result["status"] == "changes_requested"
    assert result["reason"] == "no_new_commit_since_last_review"

    # (a) the transcript is gone, so the next rework cannot resume it.
    assert not (worktree / TRANSCRIPT_NAME).exists(), (
        "the no-new-commit rework path must delete .agent_transcript.json so "
        "the next rework starts from the brief instead of replaying the "
        "transcript that produced the empty round")

    story = _read_manifest(plan_dir, "plan")["stories"]["S1"]
    assert story["status"] == "changes_requested"
    assert story["rework_attempts"] == 1

    # (b) the diagnosis boundary was consulted and its text landed in the brief.
    assert calls, "the diagnosis boundary was never consulted"
    instructions = story["agent_instructions"]
    assert rebrief.DIAGNOSIS_HEADER in instructions, (
        "the no-new-commit rework must fold a PRIOR-ATTEMPT DIAGNOSIS block "
        f"into agent_instructions, got: {instructions!r}")
    assert DIAGNOSIS_TEXT in instructions
    # The original brief survives alongside the diagnosis.
    assert ORIGINAL_BRIEF in instructions


def test_no_new_commit_rework_rebrief_matches_step_cap_call_shape(
    tmp_path, monkeypatch,
):
    """The rebrief call must mirror the step-cap branch's call exactly: the
    story dict, the worktree as a string, the manifest's role_config, and the
    plan/story keys - so the diagnosis role sees the same inputs on both
    paths."""
    _plan_dir, worktree, calls = _no_new_commit_setup(tmp_path, monkeypatch)

    p.check_story_status("plan", "S1")

    assert calls, "the diagnosis boundary was never consulted"
    args, _kwargs = calls[0]
    # diagnose_failure(evidence, story, plan_role_config)
    assert len(args) == 3, f"expected (evidence, story, plan_role_config), got {args!r}"
    evidence, story_arg, role_config = args
    # diagnose_failure's evidence is the collect_failure_evidence STRING the
    # step-cap branch passes (rebrief.diagnose_failure annotates it `str` and
    # calls .strip() on it), not a dict.
    assert isinstance(evidence, str)
    assert "STORY: thing" in evidence
    assert "stuck re-running the same failing test" in evidence
    assert story_arg["pid"] == 4242
    assert story_arg["worktree"] == str(worktree)
    # No role_config in this manifest -> None, matching the step-cap branch's
    # manifest.get("role_config").
    assert role_config is None


# ---------- negative: a None diagnosis still drops the transcript ----------

def test_no_new_commit_rework_diagnosis_none_still_deletes_transcript(
    tmp_path, monkeypatch,
):
    """Fail-open: when the diagnosis role returns None (its documented
    failure mode), the transcript is still deleted and the reason is
    unchanged - only the diagnosis block is absent."""
    plan_dir, worktree, calls = _no_new_commit_setup(
        tmp_path, monkeypatch, diagnosis=None)

    result = p.check_story_status("plan", "S1")

    assert result["status"] == "changes_requested"
    assert result["reason"] == "no_new_commit_since_last_review"
    assert not (worktree / TRANSCRIPT_NAME).exists(), (
        "a None diagnosis must not suppress the transcript deletion")

    story = _read_manifest(plan_dir, "plan")["stories"]["S1"]
    assert story["status"] == "changes_requested"
    assert story["rework_attempts"] == 1
    assert calls, "the diagnosis boundary was never consulted"
    instructions = story["agent_instructions"]
    assert rebrief.DIAGNOSIS_HEADER not in instructions, (
        "a None diagnosis must leave no PRIOR-ATTEMPT DIAGNOSIS block, "
        f"got: {instructions!r}")
    assert DIAGNOSIS_TEXT not in instructions
    # The brief itself is untouched by a None diagnosis.
    assert ORIGINAL_BRIEF in instructions


# ---------- boundary: no transcript file at all ----------

def test_no_new_commit_rework_without_transcript_does_not_error(
    tmp_path, monkeypatch,
):
    """A worktree that never wrote a transcript (or already had it removed)
    must not raise - the deletion is fail-open - and the diagnosis is still
    folded in."""
    plan_dir, worktree, calls = _no_new_commit_setup(
        tmp_path, monkeypatch, transcript=False)
    assert not (worktree / TRANSCRIPT_NAME).exists()

    result = p.check_story_status("plan", "S1")

    assert result["status"] == "changes_requested"
    assert result["reason"] == "no_new_commit_since_last_review"
    assert not (worktree / TRANSCRIPT_NAME).exists()
    story = _read_manifest(plan_dir, "plan")["stories"]["S1"]
    assert calls, "the diagnosis boundary was never consulted"
    assert rebrief.DIAGNOSIS_HEADER in story["agent_instructions"]


# ---------- the cap branch above is unchanged ----------

def test_no_new_commit_cap_branch_parks_without_diagnosis_or_transcript_delete(
    tmp_path, monkeypatch,
):
    """At the no-commit cap the story still parks with the pre-existing reason;
    the new behavior must not leak into this branch: the diagnosis boundary is
    not consulted and the transcript is left alone."""
    plan_dir, worktree, calls = _no_new_commit_setup(
        tmp_path, monkeypatch, rework_attempts=1)

    result = p.check_story_status("plan", "S1")

    assert result["status"] == "parked"
    assert result["reason"] == "no_new_commit_rework_budget_exhausted"
    assert calls == [], (
        "the cap branch must not consult the diagnosis boundary; it parks for "
        "a human instead")
    assert (worktree / TRANSCRIPT_NAME).exists(), (
        "the cap branch is unchanged and must not delete the transcript")
    story = _read_manifest(plan_dir, "plan")["stories"]["S1"]
    assert story["status"] == "parked"
    assert story["rework_attempts"] == 2
    assert "no new commit after 2" in story["parked_reason"]


def test_no_new_commit_cap_branch_escalates_without_diagnosis(
    tmp_path, monkeypatch,
):
    """The cap branch's auto-escalation variant (PIPELINE_BACKEND_DISPATCH=auto)
    is likewise untouched: it routes to changes_requested with its own reason
    and never consults the diagnosis boundary."""
    _plan_dir, worktree, calls = _no_new_commit_setup(
        tmp_path, monkeypatch, rework_attempts=1, auto_escalate=True)

    result = p.check_story_status("plan", "S1")

    assert result["status"] == "changes_requested"
    assert result["reason"] == "no_new_commit_escalated_to_claude"
    assert calls == [], (
        "the cap branch's escalation path must not consult the diagnosis "
        "boundary")
    assert (worktree / TRANSCRIPT_NAME).exists()


# ---------- mechanically-checkable source requirements ----------

def test_story_status_source_deletes_transcript_in_no_new_commit_branch():
    """The edit must live in the non-cap no-new-commit branch: the transcript
    unlink and the rebrief call both sit between the cap branch's park reason
    and the branch's own return reason, and the rebrief call carries the same
    role_config kwarg the step-cap branch passes."""
    src = Path(pstory_status.__file__).read_text()

    cap_idx = src.index("no_new_commit_rework_budget_exhausted")
    reason_idx = src.index('"no_new_commit_since_last_review"')
    assert cap_idx < reason_idx, "unexpected source layout"

    assert TRANSCRIPT_NAME in src, (
        "the no-new-commit branch must reference .agent_transcript.json")
    unlink_idx = src.index(TRANSCRIPT_NAME)
    assert cap_idx < unlink_idx < reason_idx, (
        "the transcript deletion must be in the non-cap no-new-commit branch "
        "(after the cap branch's park reason, before its own return reason)")
    assert "unlink(missing_ok=True)" in src, (
        "the transcript deletion must be fail-open (missing_ok=True)")

    rebrief_idx = src.index("_rebrief_step_cap_struggle(", cap_idx)
    assert rebrief_idx < reason_idx, (
        "the no-new-commit branch must call _rebrief_step_cap_struggle before "
        "returning")
    assert unlink_idx < rebrief_idx, (
        "the transcript must be dropped before the rebrief runs, so the "
        "diagnosis is collected while the worktree is still intact")
    assert 'plan_role_config=manifest.get("role_config")' in src[cap_idx:reason_idx], (
        "the new rebrief call must pass the manifest's role_config, matching "
        "the step-cap branch")


def test_reference_documents_no_new_commit_rework_fresh_context():
    """REFERENCE.md must document the new behavior, placed between the
    step-cap auto-done bullet and the repeated-step-cap-interrupts bullet."""
    ref_path = Path(pstory_status.__file__).resolve().parents[1] / "REFERENCE.md"
    ref = ref_path.read_text()

    auto_done_idx = ref.index("**Step-cap auto-done:**")
    interrupts_idx = ref.index("**On repeated step-cap interrupts:**")
    bullet_idx = ref.index("**No-new-commit rework:**")
    assert auto_done_idx < bullet_idx < interrupts_idx, (
        "the no-new-commit rework bullet must sit between the step-cap "
        "auto-done bullet and the repeated-step-cap-interrupts bullet")

    bullet = ref[bullet_idx:interrupts_idx]
    assert TRANSCRIPT_NAME in bullet
    assert "agent_instructions" in bullet
    assert "starts from the brief" in bullet
    assert "prior-attempt diagnosis" in bullet
