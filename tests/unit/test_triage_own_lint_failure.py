"""TDD tests for the ``own_lint_failure`` park class (story coa-02).

A story whose suite is green but whose lint gate is red, where every file the
linter names was added or changed by the story's own branch (or is in its
``files``), is the story's own defect. Before this class it fell into
``unknown``, whose allowed actions exclude ``rebrief``, so the overlord parked
it for a human.

The branch-diff read is a git call, so ``subprocess.run`` is patched inside
``pipeline.triage_lint_evidence`` and git never runs for real.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pipeline.server
import pipeline.triage
from pipeline import triage_rebrief
from pipeline.triage_lint_evidence import (
    branch_changed_paths,
    lint_paths,
    with_lint_evidence,
)
from pipeline.triage_park_classes import (
    ALLOWED_ACTIONS,
    check_ruling_allowed,
    classify_park,
)

_GIT_RUN = "pipeline.triage_lint_evidence.subprocess.run"
_LINT_RED_OUTPUT = "tests/unit/test_new.py:12:5: F821 Undefined name `missing`\nFound 1 error.\n"


def _git(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def _story(worktree: Path, **overrides) -> dict:
    story = {
        "status": "failed",
        "worktree": str(worktree),
        "files": [],
        "last_test_check": {"returncode": 0, "stdout_tail": "1 passed", "stderr_tail": ""},
        "last_lint_check": {"returncode": 1, "stdout_tail": _LINT_RED_OUTPUT, "stderr_tail": ""},
    }
    story.update(overrides)
    return story


def _park_with_git(story: dict, git_stdout: str = "", git_returncode: int = 0):
    """Classify ``story`` as the ruling path does, with the branch diff faked."""
    with patch(_GIT_RUN, return_value=_git(git_stdout, git_returncode)):
        evidence = with_lint_evidence(story, {"test_output": None})
    return classify_park(story, evidence)


# ---------------------------------------------------------------------------
# lint_paths: the files a linter names
# ---------------------------------------------------------------------------


def test_lint_paths_reads_concise_ruff_format():
    assert lint_paths("tests/unit/test_new.py:12:5: F821 Undefined name `x`") == ["tests/unit/test_new.py"]


def test_lint_paths_reads_full_ruff_arrow_format():
    output = "F821 Undefined name `x`\n  --> tests/unit/test_new.py:12:5\n   |\n"
    assert lint_paths(output) == ["tests/unit/test_new.py"]


def test_lint_paths_collapses_duplicates_in_first_seen_order():
    output = "b/two.py:1:1: E501 long\na/one.py:3:1: F401 unused\nb/two.py:9:1: E501 long\n"
    assert lint_paths(output) == ["b/two.py", "a/one.py"]


def test_lint_paths_is_empty_when_output_names_no_file():
    assert lint_paths("All checks passed!\nFound 0 errors.") == []


def test_lint_paths_is_empty_for_empty_output():
    assert lint_paths("") == []


# ---------------------------------------------------------------------------
# branch_changed_paths: the live git read, fail closed
# ---------------------------------------------------------------------------


def test_branch_changed_paths_returns_the_diff_names(tmp_path):
    with patch(_GIT_RUN, return_value=_git("tests/unit/test_new.py\nsrc/a.py\n")) as run:
        paths = branch_changed_paths(_story(tmp_path))
    assert paths == frozenset({"tests/unit/test_new.py", "src/a.py"})
    args, kwargs = run.call_args
    assert args[0] == ["git", "diff", "--name-only", "--no-renames", "origin/HEAD...HEAD"]
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["timeout"] == 30


def test_branch_changed_paths_is_empty_for_a_missing_worktree(tmp_path):
    with patch(_GIT_RUN) as run:
        paths = branch_changed_paths(_story(tmp_path / "gone"))
    assert paths == frozenset()
    run.assert_not_called()


def test_branch_changed_paths_is_empty_when_git_exits_non_zero(tmp_path):
    with patch(_GIT_RUN, return_value=_git("", returncode=128)):
        assert branch_changed_paths(_story(tmp_path)) == frozenset()


def test_branch_changed_paths_is_empty_when_git_times_out(tmp_path):
    with patch(_GIT_RUN, side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30)):
        assert branch_changed_paths(_story(tmp_path)) == frozenset()


def test_branch_changed_paths_is_empty_when_git_cannot_start(tmp_path):
    with patch(_GIT_RUN, side_effect=OSError("no git")):
        assert branch_changed_paths(_story(tmp_path)) == frozenset()


# ---------------------------------------------------------------------------
# with_lint_evidence: git runs only when lint is red
# ---------------------------------------------------------------------------


def test_with_lint_evidence_adds_sorted_branch_paths_when_lint_is_red(tmp_path):
    with patch(_GIT_RUN, return_value=_git("tests/unit/test_new.py\nsrc/a.py\n")):
        evidence = with_lint_evidence(_story(tmp_path), {"test_output": "x"})
    assert evidence == {"test_output": "x", "branch_paths": ["src/a.py", "tests/unit/test_new.py"]}


def test_with_lint_evidence_runs_no_git_when_lint_is_green(tmp_path):
    story = _story(tmp_path, last_lint_check={"returncode": 0, "stdout_tail": "", "stderr_tail": ""})
    evidence = {"test_output": "x"}
    with patch(_GIT_RUN) as run:
        result = with_lint_evidence(story, evidence)
    run.assert_not_called()
    assert result == {"test_output": "x"}
    assert result is not evidence


# ---------------------------------------------------------------------------
# classify_park: the own_lint_failure class
# ---------------------------------------------------------------------------


def test_lint_red_suite_green_on_branch_path_is_own_lint_failure(tmp_path):
    park = _park_with_git(_story(tmp_path), git_stdout="tests/unit/test_new.py\n")
    assert park.name == "own_lint_failure"
    assert park.facts["paths"] == ["tests/unit/test_new.py"]
    assert park.facts["lint_excerpt"] == _LINT_RED_OUTPUT
    assert check_ruling_allowed(park, "rebrief")


def test_lint_excerpt_is_the_last_1500_characters_of_lint_output(tmp_path):
    long_output = "noise\n" * 400 + _LINT_RED_OUTPUT
    story = _story(tmp_path, last_lint_check={"returncode": 1, "stdout_tail": long_output, "stderr_tail": ""})
    park = _park_with_git(story, git_stdout="tests/unit/test_new.py\n")
    assert park.name == "own_lint_failure"
    assert park.facts["lint_excerpt"] == long_output[-1500:]


def test_lint_red_suite_green_on_files_path_is_own_lint_failure(tmp_path):
    story = _story(tmp_path, files=["tests/unit/test_new.py"])
    park = _park_with_git(story, git_stdout="", git_returncode=128)
    assert park.name == "own_lint_failure"
    assert park.facts["paths"] == ["tests/unit/test_new.py"]


def test_own_lint_failure_allows_rebrief_and_park_for_human_only():
    assert ALLOWED_ACTIONS["own_lint_failure"] == frozenset({"rebrief", "park_for_human"})


def test_red_suite_is_not_own_lint_failure(tmp_path):
    story = _story(tmp_path, last_test_check={"returncode": 1, "stdout_tail": "1 failed", "stderr_tail": ""})
    park = _park_with_git(story, git_stdout="tests/unit/test_new.py\n")
    assert park.name == "unknown"


def test_lint_path_outside_branch_and_files_is_not_own_lint_failure(tmp_path):
    story = _story(tmp_path, files=["src/a.py"])
    park = _park_with_git(story, git_stdout="src/other.py\n")
    assert park.name == "unknown"


def test_green_lint_is_not_own_lint_failure(tmp_path):
    story = _story(tmp_path, last_lint_check={"returncode": 0, "stdout_tail": "", "stderr_tail": ""})
    park = _park_with_git(story, git_stdout="tests/unit/test_new.py\n")
    assert park.name != "own_lint_failure"


def test_red_lint_naming_no_file_is_not_own_lint_failure(tmp_path):
    story = _story(tmp_path, last_lint_check={"returncode": 1, "stdout_tail": "Internal error", "stderr_tail": ""})
    park = _park_with_git(story, git_stdout="tests/unit/test_new.py\n")
    assert park.name == "unknown"


def test_failed_git_read_leaves_branch_paths_empty_so_class_does_not_match(tmp_path):
    park = _park_with_git(_story(tmp_path), git_stdout="", git_returncode=128)
    assert park.name == "unknown"


def test_existing_class_wins_over_own_lint_failure(tmp_path):
    story = _story(
        tmp_path,
        parked_reason="rebase conflict: CONFLICT (content): Merge conflict in tests/unit/test_new.py",
    )
    park = _park_with_git(story, git_stdout="tests/unit/test_new.py\n")
    assert park.name == "rebase_conflict"


# ---------------------------------------------------------------------------
# Call-site wiring: the ruling and the rebrief executor see the same class
# ---------------------------------------------------------------------------


def test_rebrief_park_class_is_own_lint_failure(tmp_path):
    story = _story(tmp_path)
    with patch(_GIT_RUN, return_value=_git("tests/unit/test_new.py\n")):
        park = triage_rebrief._park_class(story)
    assert park.name == "own_lint_failure"
    assert check_ruling_allowed(park, "rebrief")


def test_ruling_prompt_names_own_lint_failure_as_observed_class(tmp_path):
    story = _story(tmp_path, status="failed")
    with (
        patch(_GIT_RUN, return_value=_git("tests/unit/test_new.py\n")),
        patch.object(pipeline.triage, "_load_policy", return_value="POLICY"),
        patch.object(pipeline.triage, "_plan_role_config", return_value={}),
        patch.object(pipeline.triage, "_invoke_overlord", return_value="") as invoke,
    ):
        pipeline.triage.rule_on_story("plan", "coa-02", story, "EVID")
    prompt = invoke.call_args.args[0]
    assert "OBSERVED PARK CLASS: own_lint_failure" in prompt
    assert '"paths": ["tests/unit/test_new.py"]' in prompt


# ---------------------------------------------------------------------------
# Documentation: the policy table and the environment reference name the class
# ---------------------------------------------------------------------------

_REPO = Path(__file__).resolve().parents[2]


def test_overlord_policy_table_has_own_lint_failure_row_allowing_rebrief():
    rows = [line for line in (_REPO / "overlord-policy.md").read_text(encoding="utf-8").splitlines()
            if line.startswith("| `own_lint_failure`")]
    assert len(rows) == 1
    assert "`rebrief`" in rows[0]
    assert "`park_for_human`" in rows[0]


def test_reference_names_own_lint_failure_park_class():
    assert "own_lint_failure" in (_REPO / "REFERENCE.md").read_text(encoding="utf-8")
