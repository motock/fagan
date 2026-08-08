"""The step-cap rebrief used to be "LLM diagnosis or nothing": the diagnosis
role saw a stale/absent last_test_check plus 4000 characters of agent.log, and
when it failed open the resumed attempt got no help at all.

These tests grade the mechanical evidence layer that fills that gap. Facts are
MEASURED from the worktree (git diff, agent.log guard markers, a bounded re-run
of the previously-failing tests), so they are available even when no diagnosis
model is configured, and they ground the diagnosis model when one is - a model
cannot hallucinate a code defect into a branch that has no diff.

The scenarios covered mirror the ways a real dispatch reaches the step cap:
no edit ever landed, a whole-file rewrite clobbered existing code, the agent
looped on nudges/parking, context kept getting trimmed, and tests are failing
with a traceback nobody captured.
"""
import subprocess

import pytest

from pipeline import rebrief


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    """A git repo on `master` with one committed file, plus a branch off it."""
    _git(tmp_path, "init", "-q", "-b", "master")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "mod.py").write_text("\n".join(f"line {i}" for i in range(100)) + "\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "checkout", "-q", "-b", "agent/story")
    return tmp_path


# ---------------------------------------------------------------------------
# Scenario: the attempt never landed a single edit
# ---------------------------------------------------------------------------

def test_no_diff_against_base_is_reported_as_no_code_change(repo):
    facts = rebrief.collect_attempt_facts(repo, {"summary": "s"})
    assert "NO CODE CHANGE" in facts


def test_no_code_change_tells_the_next_attempt_to_edit_not_read(repo):
    facts = rebrief.collect_attempt_facts(repo, {"summary": "s"})
    assert "edit" in facts.lower()


def test_a_landed_edit_is_not_reported_as_no_code_change(repo):
    (repo / "mod.py").write_text("changed\n")
    _git(repo, "commit", "-qam", "work")
    facts = rebrief.collect_attempt_facts(repo, {"summary": "s"})
    assert "NO CODE CHANGE" not in facts


def test_changed_files_are_listed_with_line_counts(repo):
    (repo / "mod.py").write_text("line 0\nline 1\nextra\n")
    _git(repo, "commit", "-qam", "work")
    facts = rebrief.collect_attempt_facts(repo, {"summary": "s"})
    assert "mod.py" in facts


# ---------------------------------------------------------------------------
# Scenario: a whole-file rewrite clobbered existing code
# ---------------------------------------------------------------------------

def test_sharp_shrinkage_is_flagged_as_a_possible_clobber(repo):
    (repo / "mod.py").write_text("line 0\nline 1\n")  # 100 lines -> 2
    _git(repo, "commit", "-qam", "rewrite")
    facts = rebrief.collect_attempt_facts(repo, {"summary": "s"})
    assert "POSSIBLE CLOBBER" in facts
    assert "mod.py" in facts


def test_a_normal_additive_change_is_not_flagged_as_a_clobber(repo):
    with (repo / "mod.py").open("a") as fh:
        fh.write("\n".join(f"new {i}" for i in range(60)) + "\n")
    _git(repo, "commit", "-qam", "append")
    facts = rebrief.collect_attempt_facts(repo, {"summary": "s"})
    assert "POSSIBLE CLOBBER" not in facts


def test_a_small_deletion_is_not_flagged_as_a_clobber(repo):
    lines = (repo / "mod.py").read_text().splitlines()
    (repo / "mod.py").write_text("\n".join(lines[:95]) + "\n")
    _git(repo, "commit", "-qam", "trim")
    facts = rebrief.collect_attempt_facts(repo, {"summary": "s"})
    assert "POSSIBLE CLOBBER" not in facts


# ---------------------------------------------------------------------------
# Scenario: the agent thrashed (nudges, parking, repeated reads, no edits)
# ---------------------------------------------------------------------------

_THRASH_LOG = """\
[boot] pid=1 model=m endpoint=e provider=ollama steps=60 timeout=5400.0s
[step 0] view_file: mod.py
[step 1] view_file: mod.py
[step 2] search: resolve_role
   [read-heavy nudge: 6 reads in a row]
[step 3] view_file: mod.py
   [parking: read-heavy after nudge]
[ended without done - step cap reached]
"""


def test_zero_edits_in_the_attempt_is_reported(repo):
    (repo / "agent.log").write_text(_THRASH_LOG)
    facts = rebrief.collect_attempt_facts(repo, {"summary": "s"})
    assert "NO EDIT TOOL" in facts


def test_guard_markers_are_surfaced_verbatim(repo):
    (repo / "agent.log").write_text(_THRASH_LOG)
    facts = rebrief.collect_attempt_facts(repo, {"summary": "s"})
    assert "read-heavy nudge" in facts
    assert "parking" in facts


def test_step_count_of_the_attempt_is_reported(repo):
    (repo / "agent.log").write_text(_THRASH_LOG)
    facts = rebrief.collect_attempt_facts(repo, {"summary": "s"})
    assert "4 step" in facts


def test_never_running_the_tests_is_reported(repo):
    (repo / "agent.log").write_text(_THRASH_LOG)
    facts = rebrief.collect_attempt_facts(repo, {"summary": "s"})
    assert "NEVER RAN THE TESTS" in facts


def test_a_run_that_edited_and_tested_reports_neither_warning(repo):
    (repo / "agent.log").write_text(
        "[boot] pid=1 model=m\n"
        "[step 0] str_replace: mod.py\n"
        "[step 1] bash: pytest -q tests/unit/test_mod.py\n"
    )
    facts = rebrief.collect_attempt_facts(repo, {"summary": "s"})
    assert "NO EDIT TOOL" not in facts
    assert "NEVER RAN THE TESTS" not in facts


def test_only_the_current_attempt_is_counted_not_earlier_ones(repo):
    """agent.log is appended across resumes; counting all of it would report
    33 attempts' worth of nudges as if they happened in the last 60 steps."""
    (repo / "agent.log").write_text(
        "[boot] pid=1 model=m\n"
        "[step 0] view_file: old.py\n"
        "   [read-heavy nudge: from the OLD attempt]\n"
        "[ended without done - step cap reached]\n"
        "[boot] pid=2 model=m\n"
        "[step 0] str_replace: mod.py\n"
    )
    facts = rebrief.collect_attempt_facts(repo, {"summary": "s"})
    assert "OLD attempt" not in facts


def test_context_trimming_pressure_is_reported(repo):
    (repo / "agent.log").write_text(
        "[boot] pid=1 model=m\n"
        + "[local_agent] RESUME TRIMMED: dropped 3 block(s) (1 -> 2 chars)\n" * 5
        + "[local_agent] CONTEXT EVICTED: truncated older tool output\n" * 3
        + "[step 0] str_replace: mod.py\n"
    )
    facts = rebrief.collect_attempt_facts(repo, {"summary": "s"})
    assert "CONTEXT" in facts
    assert "8" in facts


# ---------------------------------------------------------------------------
# Scenario: tests are failing and nobody captured the traceback
# ---------------------------------------------------------------------------

def test_previously_failing_tests_are_rerun_with_a_real_traceback(repo):
    (repo / "test_target.py").write_text(
        "def test_boom():\n    x = {'a': 1},\n    assert x['a'] == 1\n"
    )
    story = {
        "summary": "s",
        "last_test_check": {
            "cmd": ["pytest", "-q"],
            "cwd": str(repo),
            "returncode": 1,
            "stdout_tail": "FAILED test_target.py::test_boom - TypeError\n1 failed",
            "stderr_tail": "",
        },
    }
    facts = rebrief.collect_attempt_facts(repo, story)
    assert "RE-RAN" in facts
    # The real failure detail the 2000-char summary tail never carried.
    assert "TypeError" in facts


def test_rerun_reports_when_the_failing_tests_now_pass(repo):
    (repo / "test_target.py").write_text("def test_ok():\n    assert True\n")
    story = {
        "summary": "s",
        "last_test_check": {
            "cmd": ["pytest", "-q"],
            "cwd": str(repo),
            "returncode": 1,
            "stdout_tail": "FAILED test_target.py::test_ok - AssertionError\n1 failed",
            "stderr_tail": "",
        },
    }
    facts = rebrief.collect_attempt_facts(repo, story)
    assert "now PASS" in facts


def test_rerun_is_skipped_for_a_non_pytest_runner(repo):
    story = {
        "summary": "s",
        "last_test_check": {
            "cmd": ["cargo", "test"],
            "cwd": str(repo),
            "returncode": 1,
            "stdout_tail": "FAILED some::thing",
            "stderr_tail": "",
        },
    }
    assert "RE-RAN" not in rebrief.collect_attempt_facts(repo, story)


def test_rerun_is_skipped_when_no_node_ids_can_be_parsed(repo):
    story = {
        "summary": "s",
        "last_test_check": {
            "cmd": ["pytest", "-q"],
            "cwd": str(repo),
            "returncode": 1,
            "stdout_tail": "collection error: no tests ran",
            "stderr_tail": "",
        },
    }
    assert "RE-RAN" not in rebrief.collect_attempt_facts(repo, story)


def test_rerun_can_be_disabled_by_env(repo, monkeypatch):
    monkeypatch.setenv("PIPELINE_REBRIEF_TEST_RERUN", "0")
    (repo / "test_target.py").write_text("def test_ok():\n    assert True\n")
    story = {
        "summary": "s",
        "last_test_check": {
            "cmd": ["pytest", "-q"],
            "cwd": str(repo),
            "returncode": 1,
            "stdout_tail": "FAILED test_target.py::test_ok - AssertionError",
            "stderr_tail": "",
        },
    }
    assert "RE-RAN" not in rebrief.collect_attempt_facts(repo, story)


# ---------------------------------------------------------------------------
# Fail-open and bounding: facts are an optimization, never a gate
# ---------------------------------------------------------------------------

def test_a_non_git_worktree_yields_no_facts_and_does_not_raise(tmp_path):
    assert rebrief.collect_attempt_facts(tmp_path, {"summary": "s"}) == ""


def test_a_missing_worktree_yields_no_facts_and_does_not_raise(tmp_path):
    assert rebrief.collect_attempt_facts(tmp_path / "gone", {"summary": "s"}) == ""


def test_facts_are_bounded(repo):
    (repo / "agent.log").write_text(
        "[boot] pid=1 model=m\n"
        + "".join(f"[step {i}] view_file: f{i}.py\n   [x{i} nudge: {'y' * 200}]\n"
                 for i in range(200))
    )
    facts = rebrief.collect_attempt_facts(repo, {"summary": "s"})
    assert len(facts) <= 3000


def test_a_git_failure_does_not_raise(repo, monkeypatch):
    def boom(*a, **k):
        raise OSError("no git")
    monkeypatch.setattr(rebrief.subprocess, "run", boom)
    assert isinstance(rebrief.collect_attempt_facts(repo, {"summary": "s"}), str)


# ---------------------------------------------------------------------------
# The facts reach the next attempt whether or not a diagnosis model exists
# ---------------------------------------------------------------------------

def test_facts_are_folded_into_agent_instructions(repo):
    out = rebrief.compose_attempt_facts("GOAL: x", "- NO CODE CHANGE: nothing landed")
    assert "GOAL: x" in out
    assert rebrief.FACTS_HEADER in out
    assert "NO CODE CHANGE" in out


def test_empty_facts_are_a_no_op(repo):
    assert rebrief.compose_attempt_facts("GOAL: x", "") == "GOAL: x"
    assert rebrief.compose_attempt_facts("GOAL: x", None) == "GOAL: x"


def test_facts_replace_rather_than_stack(repo):
    once = rebrief.compose_attempt_facts("GOAL: x", "- first")
    twice = rebrief.compose_attempt_facts(once, "- second")
    assert twice.count(rebrief.FACTS_HEADER) == 1
    assert "first" not in twice
    assert "second" in twice


def test_stale_facts_are_dropped_when_the_new_attempt_has_none(repo):
    """A stale FACTS block outliving the attempt it described would be worse
    than no facts at all - the next attempt would act on the wrong evidence."""
    once = rebrief.compose_attempt_facts("GOAL: x", "- stale finding")
    assert rebrief.compose_attempt_facts(once, "") == "GOAL: x"


def test_facts_are_included_in_the_evidence_given_to_the_diagnosis_model(repo):
    (repo / "agent.log").write_text("[boot] pid=1\n[step 0] view_file: mod.py\n")
    evidence = rebrief.collect_failure_evidence(repo, {"summary": "s"})
    assert "NO CODE CHANGE" in evidence


def test_explicitly_passed_facts_are_not_recomputed(repo):
    evidence = rebrief.collect_failure_evidence(
        repo, {"summary": "s"}, facts="- PRECOMPUTED FACT")
    assert "PRECOMPUTED FACT" in evidence
    assert "NO CODE CHANGE" not in evidence


def test_facts_survive_trimming_of_an_oversized_log(repo):
    """Trimming must sacrifice the raw log tail, never the measured facts."""
    (repo / "agent.log").write_text("[boot] pid=1\n" + "x" * 500_000)
    evidence = rebrief.collect_failure_evidence(repo, {"summary": "s"}, limit=4000)
    assert len(evidence) <= 4000
    assert "NO CODE CHANGE" in evidence


# ---------------------------------------------------------------------------
# The diagnosis prompt must be grounded in the facts, not free to invent
# ---------------------------------------------------------------------------

class _RecordingDriver:
    def __init__(self):
        self.complete_kwargs = {}

    def complete(self, *, prompt, system, model):
        self.complete_kwargs = {"prompt": prompt, "system": system, "model": model}
        return "diagnosis"


def test_step_cap_rebrief_folds_facts_in_even_without_a_diagnosis(monkeypatch, repo):
    """The whole point of the mechanical layer: no diagnosis model configured
    (diagnose_failure fails open) must no longer mean no help at all."""
    from pipeline import server as p

    monkeypatch.setattr(p, "diagnose_failure", lambda *a, **k: None)
    story = {"agent_instructions": "GOAL: x"}
    p._rebrief_step_cap_struggle(story, str(repo))
    assert "NO CODE CHANGE" in story["agent_instructions"]
    assert "GOAL: x" in story["agent_instructions"]


def test_step_cap_rebrief_keeps_facts_and_diagnosis_together(monkeypatch, repo):
    from pipeline import server as p

    monkeypatch.setattr(p, "diagnose_failure", lambda *a, **k: "root cause here")
    story = {"agent_instructions": "GOAL: x"}
    p._rebrief_step_cap_struggle(story, str(repo))
    assert "root cause here" in story["agent_instructions"]
    assert "NO CODE CHANGE" in story["agent_instructions"]


def test_repeated_step_caps_do_not_stack_facts_blocks(monkeypatch, repo):
    from pipeline import server as p

    monkeypatch.setattr(p, "diagnose_failure", lambda *a, **k: "root cause here")
    story = {"agent_instructions": "GOAL: x"}
    p._rebrief_step_cap_struggle(story, str(repo))
    p._rebrief_step_cap_struggle(story, str(repo))
    assert story["agent_instructions"].count(rebrief.FACTS_HEADER) == 1
    assert story["agent_instructions"].count(rebrief.DIAGNOSIS_HEADER) == 1


def test_escalation_folds_facts_in_even_without_a_diagnosis(monkeypatch, tmp_path):
    from pipeline import escalation

    monkeypatch.setattr(escalation, "collect_attempt_facts", lambda *a, **k: "- MEASURED")
    monkeypatch.setattr(escalation, "diagnose_failure", lambda *a, **k: None)
    monkeypatch.setattr(escalation.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(escalation, "_atomic_write_json", lambda *a, **k: None)
    manifest = {"stories": {"S1": {"agent_instructions": "GOAL: x",
                                   "worktree": str(tmp_path)}}}
    escalation._escalate_to_claude(manifest, "plan", "S1", tmp_path / "m.json")
    assert "MEASURED" in manifest["stories"]["S1"]["agent_instructions"]


def test_local_fallback_escalation_folds_facts_in(monkeypatch, tmp_path):
    from pipeline import escalation

    monkeypatch.setattr(escalation, "collect_attempt_facts", lambda *a, **k: "- MEASURED")
    monkeypatch.setattr(escalation, "diagnose_failure", lambda *a, **k: None)
    monkeypatch.setattr(escalation.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(escalation, "_atomic_write_json", lambda *a, **k: None)
    manifest = {"stories": {"S1": {"agent_instructions": "GOAL: x",
                                   "worktree": str(tmp_path)}}}
    escalation._escalate_to_local_fallback_model(
        manifest, "plan", "S1", tmp_path / "m.json", "other-model")
    assert "MEASURED" in manifest["stories"]["S1"]["agent_instructions"]


def test_facts_are_measured_before_the_worktree_is_wiped():
    """The evidence lives in the worktree; collecting it after the teardown
    would measure a directory that no longer exists."""
    import inspect

    from pipeline import escalation

    src = inspect.getsource(escalation._escalate_to_claude)
    assert src.index("collect_attempt_facts") < src.index('"worktree", "remove"')


def test_diagnosis_prompt_tells_the_model_to_ground_claims_in_the_facts(monkeypatch):
    monkeypatch.delenv("PIPELINE_BACKEND_DIAGNOSIS", raising=False)
    monkeypatch.setattr(rebrief.role_registry, "load_registry", lambda: {"roles": {}})
    driver = _RecordingDriver()
    monkeypatch.setattr(rebrief.backend, "get_backend", lambda role, name=None: driver)
    rebrief._run_diagnosis_role(
        "evidence", {"summary": "s", "backend": "ollama", "dispatched_model": "m"})
    p = driver.complete_kwargs["prompt"].lower()
    assert "facts" in p
    assert "no edit" in p or "no code change" in p
