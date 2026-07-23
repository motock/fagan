"""Tests for the two mechanical pieces of the real-repo benchmark driver
(`tests/benchmark/run_real_repo_task.py`): `setup_real_repo_workspace` and
`run_groundtruth_in_place`.

This is step 1 of REAL_REPO_INTEGRATION_TEST_PLAN.md: the purely-mechanical,
independently-testable helpers that a later follow-up story wires into a
full task spec + CLI. These tests build a tiny throwaway git fixture repo
inline (NOT the real pipeline repo) so they stay fast, hermetic, and
independent of Ollama / any live model.

The functions under test import and reuse `harness.py`'s working pieces
(PIPELINE_REPO, VENV_PY) the same way `compound_harness.py` does, rather
than editing the 1054-line harness.py.
"""
import os
import subprocess
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
if str(BENCH) not in sys.path:
    sys.path.insert(0, str(BENCH))

import harness  # noqa: E402
import run_real_repo_task as rrt  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers: build a tiny throwaway git repo inside tmp_path.
# ---------------------------------------------------------------------------

def _make_fixture_repo(root: Path) -> str:
    """Create a minimal git repo with two commits and return the HEAD sha.

    This stands in for the real pipeline repo (PIPELINE_REPO) that the real
    driver clones -- but it's tiny and hermetic, so the tests don't depend
    on the actual repo's contents or any live model.
    """
    repo = root / "fixture"
    repo.mkdir(parents=True)
    env = {
        "GIT_AUTHOR_NAME": "bench",
        "GIT_AUTHOR_EMAIL": "bench@local",
        "GIT_COMMITTER_NAME": "bench",
        "GIT_COMMITTER_EMAIL": "bench@local",
    }
    subprocess.run(["git", "init", "-q", "-b", "master", "."], cwd=repo, check=True, env={**os.environ, **env})
    (repo / "README.md").write_text("# fixture\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "first"], cwd=repo, check=True, env={**os.environ, **env})
    (repo / "file.txt").write_text("hello\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "second"], cwd=repo, check=True, env={**os.environ, **env})
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    return sha


def _git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True,
        capture_output=True, text=True,
    )


# ---------------------------------------------------------------------------
# setup_real_repo_workspace
# ---------------------------------------------------------------------------

def test_setup_clones_fixture_and_pins_commit(tmp_path, monkeypatch):
    """setup_real_repo_workspace clones the fixture repo and checks out the
    exact requested commit; all four returned paths exist as directories."""
    sha = _make_fixture_repo(tmp_path)
    fixture = tmp_path / "fixture"

    # Point PIPELINE_REPO at the fixture so the clone source is the tiny
    # throwaway repo, not the real pipeline repo.
    monkeypatch.setattr(harness, "PIPELINE_REPO", fixture)
    monkeypatch.setattr(rrt, "PIPELINE_REPO", fixture)

    cell = tmp_path / "cell"
    result = rrt.setup_real_repo_workspace(cell, sha)

    assert set(result) == {"repo", "origin", "plans", "worktrees"}
    for key in ("repo", "origin", "plans", "worktrees"):
        assert result[key].is_dir(), f"{key} should be a directory"

    head = _git(["rev-parse", "HEAD"], result["repo"]).stdout.strip()
    assert head == sha, "cloned repo HEAD must equal the requested base_commit"


def test_setup_origin_remote_is_pushable(tmp_path, monkeypatch):
    """The origin bare remote is genuinely wired up: a new commit in the
    clone can be pushed to origin, proving a real master branch + bare
    remote exist (not just an empty origin.git dir)."""
    sha = _make_fixture_repo(tmp_path)
    fixture = tmp_path / "fixture"
    monkeypatch.setattr(harness, "PIPELINE_REPO", fixture)
    monkeypatch.setattr(rrt, "PIPELINE_REPO", fixture)

    cell = tmp_path / "cell"
    result = rrt.setup_real_repo_workspace(cell, sha)
    repo = result["repo"]

    # The checked-out base must be on a branch named master (not detached),
    # so a push to origin master works.
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo).stdout.strip()
    assert branch == "master", f"expected master branch, got {branch!r}"

    # Make a new commit and push it to origin.
    (repo / "new.txt").write_text("new\n")
    env = {
        "GIT_AUTHOR_NAME": "bench",
        "GIT_AUTHOR_EMAIL": "bench@local",
        "GIT_COMMITTER_NAME": "bench",
        "GIT_COMMITTER_EMAIL": "bench@local",
    }
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "third"], cwd=repo, check=True, env={**os.environ, **env})
    # This push must succeed (no CalledProcessError).
    _git(["push", "origin", "master"], repo)


def test_setup_venv_symlink_points_at_pipeline_venv(tmp_path, monkeypatch):
    """The .venv symlink inside the clone points at the real pipeline .venv."""
    sha = _make_fixture_repo(tmp_path)
    fixture = tmp_path / "fixture"
    monkeypatch.setattr(harness, "PIPELINE_REPO", fixture)
    monkeypatch.setattr(rrt, "PIPELINE_REPO", fixture)

    # The fixture repo has no .venv; create a fake one so the symlink target
    # exists and resolves (mirrors the real PIPELINE_REPO/.venv).
    fake_venv = fixture / ".venv"
    fake_venv.mkdir(exist_ok=True)

    cell = tmp_path / "cell"
    result = rrt.setup_real_repo_workspace(cell, sha)
    venv_link = result["repo"] / ".venv"
    assert venv_link.is_symlink(), ".venv inside clone should be a symlink"
    target = os.readlink(str(venv_link))
    assert Path(target) == fake_venv or Path(target).resolve() == fake_venv.resolve(), (
        f".venv symlink target {target!r} should point at the pipeline .venv"
    )


def test_setup_nonexistent_commit_raises(tmp_path, monkeypatch):
    """A nonexistent base_commit SHA must raise (CalledProcessError), not
    silently succeed or return a partial result."""
    _make_fixture_repo(tmp_path)
    fixture = tmp_path / "fixture"
    monkeypatch.setattr(harness, "PIPELINE_REPO", fixture)
    monkeypatch.setattr(rrt, "PIPELINE_REPO", fixture)

    cell = tmp_path / "cell"
    bogus = "0" * 40
    try:
        rrt.setup_real_repo_workspace(cell, bogus)
    except subprocess.CalledProcessError:
        return  # expected
    raise AssertionError(
        "setup_real_repo_workspace should raise CalledProcessError for a "
        "nonexistent base_commit, but it returned without error"
    )


def test_setup_clears_existing_cell(tmp_path, monkeypatch):
    """If the cell dir already exists, it is cleared first (idempotent)."""
    sha = _make_fixture_repo(tmp_path)
    fixture = tmp_path / "fixture"
    monkeypatch.setattr(harness, "PIPELINE_REPO", fixture)
    monkeypatch.setattr(rrt, "PIPELINE_REPO", fixture)

    cell = tmp_path / "cell"
    cell.mkdir(parents=True)
    (cell / "stale.txt").write_text("stale\n")

    result = rrt.setup_real_repo_workspace(cell, sha)
    assert not (cell / "stale.txt").exists(), "pre-existing cell should be cleared"
    head = _git(["rev-parse", "HEAD"], result["repo"]).stdout.strip()
    assert head == sha


# ---------------------------------------------------------------------------
# run_groundtruth_in_place
# ---------------------------------------------------------------------------

def test_run_groundtruth_in_place_passing(tmp_path):
    """A trivially-passing groundtruth source returns passed=True and cleans
    up the throwaway test file afterward."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    name = "test_groundtruth_review_story_lock_guard.py"
    src = "def test_x():\n    assert True\n"

    result = rrt.run_groundtruth_in_place(repo, src, name)

    assert result["ran"] is True
    assert result["passed"] is True
    assert not (repo / name).exists(), "throwaway groundtruth file must be removed"


def test_run_groundtruth_in_place_failing(tmp_path):
    """A failing groundtruth source returns passed=False with a non-empty
    tail, and STILL cleans up the throwaway file (proves cleanup on the
    failure path, not only on success)."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    name = "test_groundtruth_review_story_lock_guard.py"
    src = "def test_x():\n    assert False, 'intentional failure'\n"

    result = rrt.run_groundtruth_in_place(repo, src, name)

    assert result["ran"] is True
    assert result["passed"] is False
    assert result["tail"], "tail should be non-empty for a failing run"
    assert not (repo / name).exists(), "throwaway file must be removed even on failure"


def test_run_groundtruth_in_place_default_name(tmp_path):
    """The default groundtruth_name is
    'test_groundtruth_review_story_lock_guard.py' when omitted."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    src = "def test_x():\n    assert True\n"

    result = rrt.run_groundtruth_in_place(repo, src)

    assert result["ran"] is True
    assert result["passed"] is True
    default = "test_groundtruth_review_story_lock_guard.py"
    assert not (repo / default).exists(), "default-named throwaway file must be removed"


def test_run_groundtruth_in_place_result_shape(tmp_path):
    """The returned dict has exactly the keys ran/passed/tail with the right
    types, matching run_groundtruth's shape so a later scorecard can consume
    it identically."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    result = rrt.run_groundtruth_in_place(repo, "def test_x():\n    assert True\n")
    assert set(result) == {"ran", "passed", "tail"}
    assert isinstance(result["ran"], bool)
    assert isinstance(result["passed"], bool)
    assert isinstance(result["tail"], str)


# ===========================================================================
# Step 2: the full task spec + CLI driver (main()).
#
# These tests cover the real-repo benchmark driver's main() entry point and
# the review_story_lock_guard task fixture (spec.json / groundtruth.py). They
# do NOT invoke a live model or run drive() to real completion - drive() is
# dependency-injected (imported as a module-level name the tests monkeypatch
# to a no-op) so the plan-building path can be exercised hermetically.
# ===========================================================================

import ast  # noqa: E402
import json  # noqa: E402

import pytest  # noqa: E402

TASK_DIR = BENCH / "tasks" / "review_story_lock_guard"
SPEC_PATH = TASK_DIR / "spec.json"
GROUNDTRUTH_PATH = TASK_DIR / "groundtruth.py"

REQUIRED_SPEC_KEYS = {
    "name", "summary", "agent_instructions", "persona",
    "model", "risk", "impl_file", "base_commit",
}


# ---------------------------------------------------------------------------
# spec.json / groundtruth.py validity
# ---------------------------------------------------------------------------

def test_spec_json_parses_and_has_all_required_keys():
    """spec.json must parse as valid JSON and contain every required key."""
    spec = json.loads(SPEC_PATH.read_text())
    missing = REQUIRED_SPEC_KEYS - set(spec)
    assert not missing, f"spec.json missing required keys: {missing!r}"


def test_spec_json_name_field():
    """The name field must be exactly 'review_story_lock_guard'."""
    spec = json.loads(SPEC_PATH.read_text())
    assert spec["name"] == "review_story_lock_guard"


def test_spec_json_base_commit_is_present_in_repo():
    """The base_commit must be a real commit in this repo's history."""
    spec = json.loads(SPEC_PATH.read_text())
    sha = spec["base_commit"]
    # Validate the commit exists without printing anything.
    r = subprocess.run(
        ["git", "rev-parse", "--verify", f"{sha}^{{commit}}"],
        cwd=str(Path(__file__).resolve().parent.parent.parent),
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"base_commit {sha!r} not found in repo history"


def test_spec_json_impl_file_points_at_pipeline_server():
    """impl_file must be pipeline/server.py (the file the fix touches)."""
    spec = json.loads(SPEC_PATH.read_text())
    assert spec["impl_file"] == "pipeline/server.py"


def test_spec_json_persona_model_risk_fields():
    """persona/model/risk must match the documented values."""
    spec = json.loads(SPEC_PATH.read_text())
    assert spec["persona"] == "software-engineer"
    assert spec["model"] == "sonnet"
    assert spec["risk"] == "low"


def test_spec_json_agent_instructions_nonempty_and_mentions_lock():
    """agent_instructions must be non-empty and reference the lock fix."""
    spec = json.loads(SPEC_PATH.read_text())
    instr = spec["agent_instructions"]
    assert isinstance(instr, str) and instr.strip(), (
        "agent_instructions must be a non-empty string"
    )
    assert "_plan_lock" in instr, (
        "agent_instructions must reference _plan_lock (the fix mechanism)"
    )


def test_spec_json_has_no_acceptance_key():
    """This task deliberately has acceptance == [] (model authors its own
    tests); spec.json must NOT declare an acceptance fixture path."""
    spec = json.loads(SPEC_PATH.read_text())
    assert "acceptance" not in spec, (
        "spec.json must not declare an acceptance fixture for this task"
    )


def test_no_acceptance_file_exists_for_task():
    """No acceptance.py / acceptance.<ext> file may exist for this task -
    load_task() would raise FileNotFoundError, which is the whole point."""
    for p in TASK_DIR.iterdir():
        assert not p.name.startswith("acceptance"), (
            f"no acceptance.* file may exist for this task; found {p.name!r}"
        )


def test_groundtruth_source_nonempty_and_valid_python():
    """groundtruth.py's source must be non-empty and syntactically valid."""
    src = GROUNDTRUTH_PATH.read_text()
    assert src.strip(), "groundtruth.py source must be non-empty"
    # Must parse without raising (syntax validation only).
    ast.parse(src)


def test_groundtruth_imports_pipeline_mcp_server():
    """groundtruth.py must import pipeline_mcp_server as p (the calling
    convention the driver sets up before running it)."""
    src = GROUNDTRUTH_PATH.read_text()
    tree = ast.parse(src)
    found_alias = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name == "pipeline_mcp_server" and alias.asname == "p"
            for alias in node.names
        ):
            found_alias = True
            break
    assert found_alias, (
        "groundtruth.py must contain 'import pipeline_mcp_server as p'"
    )


def test_groundtruth_asserts_registered_tool_is_real_review_story():
    """groundtruth.py must assert the registered MCP tool delegates to the
    real p.review_story (the rename-and-delegate failure shape)."""
    src = GROUNDTRUTH_PATH.read_text()
    assert "_tool_manager" in src and "review_story" in src, (
        "groundtruth.py must assert on the registered MCP tool path"
    )


def test_groundtruth_asserts_advance_pipeline_still_registered():
    """groundtruth.py must assert advance_pipeline is still registered
    (the exact regression the real model introduced on attempt 2)."""
    src = GROUNDTRUTH_PATH.read_text()
    assert "advance_pipeline" in src, (
        "groundtruth.py must check advance_pipeline is still registered"
    )


def test_groundtruth_covers_path_traversal_and_concurrency():
    """groundtruth.py must cover both the path-traversal ordering check and
    the genuine concurrency (lock-held) check."""
    src = GROUNDTRUTH_PATH.read_text()
    assert "fcntl" in src, (
        "groundtruth.py must use fcntl for the concurrency check"
    )
    assert "ValueError" in src, (
        "groundtruth.py must assert ValueError on invalid plan_name"
    )
    assert "skipped" in src and "locked" in src, (
        "groundtruth.py must assert the skip-as-locked behavior"
    )


# ---------------------------------------------------------------------------
# main() argument validation: unknown model exits non-zero, no FS side effects
# ---------------------------------------------------------------------------

def test_main_unknown_model_exits_nonzero(tmp_path, monkeypatch):
    """Invoking main() with --model not-a-real-model must exit non-zero
    (mirroring harness.py's exit-code-2 style) without touching the
    filesystem beyond argument parsing."""
    workdir = tmp_path / "runs"
    monkeypatch.setattr(
        sys, "argv",
        ["run_real_repo_task.py",
         "--task", "review_story_lock_guard",
         "--model", "not-a-real-model",
         "--workdir", str(workdir)],
    )
    # The workdir must NOT be created by a failed model-validation.
    rc = rrt.main()
    assert rc != 0, "unknown model must produce a non-zero exit code"
    assert not workdir.exists(), (
        "an unknown model must not create any workdir / touch the filesystem"
    )


def test_main_unknown_model_exit_code_matches_harness_style(tmp_path, monkeypatch):
    """The exit code for an unknown model must be 2 (same as harness.py)."""
    monkeypatch.setattr(
        sys, "argv",
        ["run_real_repo_task.py",
         "--task", "review_story_lock_guard",
         "--model", "zzz-does-not-exist",
         "--workdir", str(tmp_path / "runs")],
    )
    rc = rrt.main()
    assert rc == 2, f"unknown model should exit with code 2, got {rc}"


def test_main_model_is_required(tmp_path, monkeypatch):
    """--model is required; omitting it must raise (SystemExit from argparse)."""
    monkeypatch.setattr(
        sys, "argv",
        ["run_real_repo_task.py",
         "--task", "review_story_lock_guard",
         "--workdir", str(tmp_path / "runs")],
    )
    with pytest.raises(SystemExit):
        rrt.main()


# ---------------------------------------------------------------------------
# Plan-building path through ingest_plan (drive() stubbed to a no-op).
#
# main() must import drive as a module-level name so a test can monkeypatch
# rrt.drive to a no-op, then assert the saved/ingested plan has a single story
# with acceptance == [] and the exact summary/agent_instructions from spec.json.
# This uses a tiny fixture repo (same pattern as the prior story's tests)
# rather than a real clone of the pipeline repo, keeping it hermetic and fast.
# ---------------------------------------------------------------------------

def _make_fixture_repo_with_venv(root: Path) -> str:
    """A tiny git fixture repo with a fake .venv, standing in for PIPELINE_REPO.

    Mirrors _make_fixture_repo above but also creates a .venv dir so the
    symlink target exists (the real driver symlinks .venv into the clone).
    """
    sha = _make_fixture_repo(root)
    fixture = root / "fixture"
    (fixture / ".venv").mkdir(exist_ok=True)
    return sha


def test_main_builds_plan_with_single_story_and_empty_acceptance(
    tmp_path, monkeypatch,
):
    """main() must build a plan with a single story whose acceptance == [] and
    whose summary/agent_instructions match spec.json exactly. drive() is
    stubbed to a no-op so no live model is invoked."""
    _make_fixture_repo_with_venv(tmp_path)
    fixture = tmp_path / "fixture"
    monkeypatch.setattr(harness, "PIPELINE_REPO", fixture)
    monkeypatch.setattr(rrt, "PIPELINE_REPO", fixture)

    # Stub drive() to a no-op so main() never runs a live dispatch.
    monkeypatch.setattr(rrt, "drive", lambda *a, **k: [])

    # Stub run_groundtruth_in_place so we don't actually run pytest in the
    # clone (the fixture repo has no pipeline_mcp_server module).
    monkeypatch.setattr(
        rrt, "run_groundtruth_in_place",
        lambda *a, **k: {"ran": True, "passed": True, "tail": "stubbed"},
    )

    workdir = tmp_path / "runs"
    monkeypatch.setattr(
        sys, "argv",
        ["run_real_repo_task.py",
         "--task", "review_story_lock_guard",
         "--model", "gptoss",
         "--workdir", str(workdir)],
    )

    rc = rrt.main()
    assert rc == 0, f"main() should succeed with drive stubbed, got rc={rc}"

    # Locate the cell and the ingested manifest.
    cell = workdir.resolve() / "review_story_lock_guard__gptoss__t0"
    assert cell.is_dir(), f"cell dir must exist at {cell!r}"

    # The plan name follows the bench_<task>_<model>_t<trial> convention.
    plan_name = "bench_review_story_lock_guard_gptoss_t0"
    plans_dir = cell / "plans"
    manifest_path = plans_dir / f"{plan_name}.manifest.json"
    assert manifest_path.is_file(), (
        f"ingested manifest must exist at {manifest_path!r}"
    )

    manifest = json.loads(manifest_path.read_text())
    story_key = "REVIEW-STORY-LOCK-GUARD"
    assert story_key in manifest["stories"], (
        f"manifest must contain story {story_key!r}; "
        f"got {list(manifest['stories'])!r}"
    )
    story = manifest["stories"][story_key]

    # acceptance must be [] (this task deliberately has no acceptance fixture).
    assert story["acceptance"] == [], (
        f"story acceptance must be [], got {story['acceptance']!r}"
    )

    # summary / agent_instructions must match spec.json verbatim.
    spec = json.loads(SPEC_PATH.read_text())
    assert story["summary"] == spec["summary"], (
        "story summary must match spec.json exactly"
    )
    assert story["agent_instructions"] == spec["agent_instructions"], (
        "story agent_instructions must match spec.json exactly"
    )
    assert story["persona"] == spec["persona"]
    assert story["model"] == spec["model"]
    assert story["risk"] == spec["risk"]


def test_main_writes_result_json_with_required_fields(tmp_path, monkeypatch):
    """main() must write cell/result.json with the scorecard fields
    (final_status, review_verdict, merged, dispatched_model, elapsed_s,
    ticks, groundtruth_passed, groundtruth_tail) plus a rework-cycle count
    and an infra-failure count parsed from the journal."""
    _make_fixture_repo_with_venv(tmp_path)
    fixture = tmp_path / "fixture"
    monkeypatch.setattr(harness, "PIPELINE_REPO", fixture)
    monkeypatch.setattr(rrt, "PIPELINE_REPO", fixture)

    monkeypatch.setattr(rrt, "drive", lambda *a, **k: [])
    monkeypatch.setattr(
        rrt, "run_groundtruth_in_place",
        lambda *a, **k: {"ran": True, "passed": True, "tail": "stubbed"},
    )

    workdir = tmp_path / "runs"
    monkeypatch.setattr(
        sys, "argv",
        ["run_real_repo_task.py",
         "--task", "review_story_lock_guard",
         "--model", "gptoss",
         "--workdir", str(workdir)],
    )

    rc = rrt.main()
    assert rc == 0

    cell = workdir.resolve() / "review_story_lock_guard__gptoss__t0"
    result_path = cell / "result.json"
    assert result_path.is_file(), f"result.json must exist at {result_path!r}"
    result = json.loads(result_path.read_text())

    # Core scorecard fields (mirrors harness.py main()'s scorecard).
    for field in ("final_status", "review_verdict", "merged",
                  "dispatched_model", "elapsed_s", "ticks",
                  "groundtruth_passed", "groundtruth_tail"):
        assert field in result, f"result.json missing field {field!r}"

    # The two additions specific to this driver.
    assert "rework_cycles" in result, (
        "result.json must include a rework/review-cycle count "
        "(rework_cycles) parsed from the journal"
    )
    assert "infra_failures" in result, (
        "result.json must include an infra-failure count "
        "(infra_failures) parsed from the journal"
    )


def test_main_default_task_is_review_story_lock_guard(tmp_path, monkeypatch):
    """--task defaults to 'review_story_lock_guard' when omitted."""
    _make_fixture_repo_with_venv(tmp_path)
    fixture = tmp_path / "fixture"
    monkeypatch.setattr(harness, "PIPELINE_REPO", fixture)
    monkeypatch.setattr(rrt, "PIPELINE_REPO", fixture)

    monkeypatch.setattr(rrt, "drive", lambda *a, **k: [])
    monkeypatch.setattr(
        rrt, "run_groundtruth_in_place",
        lambda *a, **k: {"ran": True, "passed": True, "tail": "stubbed"},
    )

    # NOTE: no --task argument provided.
    monkeypatch.setattr(
        sys, "argv",
        ["run_real_repo_task.py",
         "--model", "gptoss",
         "--workdir", str(tmp_path / "runs")],
    )

    rc = rrt.main()
    assert rc == 0
    cell = (tmp_path / "runs").resolve() / "review_story_lock_guard__gptoss__t0"
    assert cell.is_dir(), (
        "default --task must be review_story_lock_guard (cell dir name)"
    )


def test_main_timeout_default_is_at_least_10800(tmp_path, monkeypatch):
    """The --timeout default must be at least 10800 (3h), per the plan doc
    (the real incident took ~2.5 hours). We verify by invoking --help and
    reading the documented default from the help text."""
    monkeypatch.setattr(
        sys, "argv",
        ["run_real_repo_task.py", "--help"],
    )
    import io
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    with pytest.raises(SystemExit):
        rrt.main()
    help_text = buf.getvalue()
    # The help text must mention a timeout default of at least 10800.
    assert "10800" in help_text, (
        "--timeout default must be at least 10800 (3h); help text: "
        f"{help_text!r}"
    )