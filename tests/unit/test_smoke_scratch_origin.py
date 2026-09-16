"""TDD tests for the scratch-origin fix in scripts/smoke_getting_started.py.

The smoke's scratch target repo used to be created with a bare ``git init``
and NO remote, while pipeline/dispatch.py builds the agent worktree from
``origin/<default-branch>`` (``git fetch origin <branch>`` then
``git worktree add -b <branch> <path> origin/<branch>``). With no origin,
dispatch failed with exit 128 and the smoke exited 4.

These tests pin the fix: ``_prepare_scratch_env`` must give the scratch repo a
REAL origin - a local BARE repo under the scratch root - whose HEAD symref
names the scratch repo's actual current branch (git's init.defaultBranch
differs per machine, so the branch name is read, never assumed).

They are PURE GIT tests: they never shell out to the ``claude`` CLI.
"""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "smoke_getting_started.py"

# Pinned by tests/unit/test_smoke_getting_started.py; the origin setup must
# live INSIDE _prepare_scratch_env (a nested closure is fine).
ALLOWED_FUNCTIONS = {
    "_prepare_scratch_env",
    "_require_claude_backend",
    "run_smoke",
    "main",
}


def _load_script():
    if not SCRIPT_PATH.exists():
        pytest.fail(f"scripts/smoke_getting_started.py not found at {SCRIPT_PATH}")
    mod_name = "smoke_getting_started_origin_under_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@contextmanager
def _pipeline_import_guard():
    """Restore pipeline.* sys.modules entries after a scratch-env call."""
    prefix = "pipeline"
    before = {
        k: v
        for k, v in sys.modules.items()
        if k == prefix or k.startswith(prefix + ".")
    }
    try:
        yield
    finally:
        for name in list(sys.modules):
            is_pipeline = name == prefix or name.startswith(prefix + ".")
            if is_pipeline and name not in before:
                sys.modules.pop(name, None)
        for name, mod in before.items():
            sys.modules[name] = mod


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )


def _prepare(tmp_path, monkeypatch):
    mod = _load_script()
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    monkeypatch.setenv("PLAN_DIR", str(tmp_path / "pre-plans"))
    monkeypatch.setenv("WORKTREE_ROOT", str(tmp_path / "pre-worktrees"))
    with _pipeline_import_guard():
        # pipeline.paths reads PLAN_DIR/WORKTREE_ROOT at import time; drop any
        # cached copy so the scratch env is what it resolves against.
        sys.modules.pop("pipeline.paths", None)
        import pipeline.paths  # noqa: F401

        return mod._prepare_scratch_env(tmp_path)


def _repo(layout):
    return Path(layout["TARGET_REPO"])


def _current_branch(repo):
    res = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def _origin_path(repo):
    res = _git(repo, "remote", "get-url", "origin")
    assert res.returncode == 0, f"scratch repo has no 'origin' remote: {res.stderr}"
    return Path(res.stdout.strip())


# --------------------------------------------------------------------------
# 1. POSITIVE: a real, reachable origin remote
# --------------------------------------------------------------------------
def test_scratch_repo_has_origin_remote_that_ls_remote_reaches(tmp_path, monkeypatch):
    layout = _prepare(tmp_path, monkeypatch)
    repo = _repo(layout)

    remotes = _git(repo, "remote")
    assert remotes.returncode == 0, remotes.stderr
    assert "origin" in remotes.stdout.split(), (
        f"scratch repo must have an 'origin' remote; got {remotes.stdout!r}"
    )

    ls = _git(repo, "ls-remote", "origin")
    assert ls.returncode == 0, f"'git ls-remote origin' must exit 0; stderr: {ls.stderr}"
    assert ls.stdout.strip(), "origin must contain the pushed initial commit"


# --------------------------------------------------------------------------
# 2. origin HEAD symref names the scratch repo's ACTUAL current branch
# --------------------------------------------------------------------------
def test_bare_origin_head_symref_matches_scratch_current_branch(tmp_path, monkeypatch):
    layout = _prepare(tmp_path, monkeypatch)
    repo = _repo(layout)
    origin = _origin_path(repo)
    branch = _current_branch(repo)

    assert branch not in ("", "HEAD"), f"scratch repo has no current branch: {branch!r}"

    symref = _git(origin, "symbolic-ref", "HEAD")
    assert symref.returncode == 0, (
        f"bare origin HEAD must be a symref; stderr: {symref.stderr}"
    )
    assert symref.stdout.strip() == f"refs/heads/{branch}", (
        f"origin HEAD must point at the scratch repo's branch {branch!r} "
        f"(git's init.defaultBranch differs per machine - never assume "
        f"master/main); got {symref.stdout.strip()!r}"
    )


# --------------------------------------------------------------------------
# 3. the exact first command dispatch runs
# --------------------------------------------------------------------------
def test_fetch_origin_branch_succeeds(tmp_path, monkeypatch):
    layout = _prepare(tmp_path, monkeypatch)
    repo = _repo(layout)
    branch = _current_branch(repo)

    res = _git(repo, "fetch", "origin", branch)
    assert res.returncode == 0, (
        f"'git fetch origin {branch}' must exit 0 - this is the exact command "
        f"that failed in dispatch; stderr: {res.stderr}"
    )


# --------------------------------------------------------------------------
# 4. the exact second command dispatch runs
# --------------------------------------------------------------------------
def test_worktree_add_from_origin_branch_succeeds(tmp_path, monkeypatch):
    layout = _prepare(tmp_path, monkeypatch)
    repo = _repo(layout)
    branch = _current_branch(repo)

    fetch = _git(repo, "fetch", "origin", branch)
    assert fetch.returncode == 0, fetch.stderr

    wt = tmp_path / "wt-smoketest"
    res = _git(
        repo, "worktree", "add", "-b", "smoketest", str(wt), f"origin/{branch}"
    )
    assert res.returncode == 0, (
        f"'git worktree add -b smoketest <path> origin/{branch}' must exit 0 - "
        f"the exact second command dispatch runs; stderr: {res.stderr}"
    )
    assert (wt / "README.md").exists(), "worktree must be populated from origin"


# --------------------------------------------------------------------------
# 5. NEGATIVE/BOUNDARY: bare origin inside the scratch root; layout unchanged
# --------------------------------------------------------------------------
def test_origin_is_bare_inside_scratch_root_and_layout_keys_unchanged(
    tmp_path, monkeypatch
):
    layout = _prepare(tmp_path, monkeypatch)
    repo = _repo(layout)
    origin = _origin_path(repo)

    root = tmp_path.resolve()
    resolved = origin.resolve()
    assert resolved == root or root in resolved.parents, (
        f"bare origin {resolved} must live INSIDE the scratch root {root} - "
        f"nothing may be written outside it"
    )

    bare = _git(origin, "rev-parse", "--is-bare-repository")
    assert bare.returncode == 0, bare.stderr
    assert bare.stdout.strip() == "true", (
        f"origin must be a BARE repo (git init --bare); got {bare.stdout!r}"
    )

    assert set(layout) == {"PLAN_DIR", "WORKTREE_ROOT", "TARGET_REPO"}, (
        f"_prepare_scratch_env must keep returning exactly the same keys; "
        f"got {sorted(layout)}"
    )


# --------------------------------------------------------------------------
# source-level pins for the rest of the story
# --------------------------------------------------------------------------
def test_no_new_top_level_functions():
    tree = ast.parse(SCRIPT_PATH.read_text())
    defined = {
        n.name
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert defined == ALLOWED_FUNCTIONS, (
        "the origin setup must live INSIDE _prepare_scratch_env (a nested "
        f"closure is fine); top-level defs must stay {sorted(ALLOWED_FUNCTIONS)}, "
        f"got {sorted(defined)}"
    )


def test_poll_loop_succeeds_on_tests_passed_not_done():
    src = SCRIPT_PATH.read_text()
    assert 'last_status == "tests_passed"' in src, (
        "the poll loop must succeed on status 'tests_passed'"
    )
    assert 'last_status == "done"' not in src, (
        "'done' is unreachable by construction (a local bare origin cannot "
        "host a gh PR); the poll loop must not wait for it"
    )


def test_pass_output_names_story_key_and_final_status_without_pr_url():
    src = SCRIPT_PATH.read_text()
    tree = ast.parse(src)
    success_if = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            continue
        comp = node.test
        if not (isinstance(comp.left, ast.Name) and comp.left.id == "last_status"):
            continue
        if any(
            isinstance(c, ast.Constant) and c.value == "tests_passed"
            for c in comp.comparators
        ):
            success_if = node
    assert success_if is not None, (
        "no `if last_status == 'tests_passed'` success block found"
    )

    body_src = "\n".join(ast.unparse(stmt) for stmt in success_if.body)
    assert "story_key" in body_src, (
        f"PASS output must print the story key; got:\n{body_src}"
    )
    assert "last_status" in body_src, (
        f"PASS output must print the final status; got:\n{body_src}"
    )
    assert "PR URL" not in body_src and "pr_url" not in body_src, (
        f"the PASS output must no longer promise a PR URL; got:\n{body_src}"
    )


def test_terminal_failure_timeout_and_poll_interval_unchanged():
    src = SCRIPT_PATH.read_text()
    assert "POLL_INTERVAL_S = 15" in src, "poll interval must stay 15s"
    assert 'TERMINAL_FAILURE_STATUSES = ("failed", "parked")' in src, (
        "terminal-failure statuses must stay failed/parked"
    )
    assert "last_status in TERMINAL_FAILURE_STATUSES" in src, (
        "terminal-failure handling must stay as it is"
    )
    assert "return 4" in src, "terminal failure must still exit 4"
    assert "return 3" in src, "timeout must still exit 3"


def test_module_docstring_exit_code_table_and_local_origin_note():
    doc = _load_script().__doc__ or ""
    lowered = doc.lower()

    exit_zero_lines = [
        line.strip() for line in doc.splitlines() if line.strip().startswith("0 ")
    ]
    assert exit_zero_lines, "the docstring must keep its exit-code table"
    exit_zero = " ".join(exit_zero_lines)
    assert "tests_passed" in exit_zero, (
        "the exit-code table must say exit 0 means the story was implemented "
        f"and its tests passed (status 'tests_passed'); got {exit_zero!r}"
    )
    assert "merged" not in exit_zero, (
        f"exit 0 must no longer mean a merged story; got {exit_zero!r}"
    )

    assert "bare" in lowered, (
        "the docstring must explain that the scratch repo's origin is a LOCAL "
        "BARE repo"
    )
    assert "gh pr create" in doc, (
        "the docstring must name 'gh pr create' as the reason pr_open/done are "
        "unreachable"
    )
    assert "pr_open" in doc, "the docstring must name the pr_open status"
    assert "unreachable" in lowered, (
        "the docstring must state that pr_open/done are unreachable by "
        "construction"
    )
    assert "save_plan" in doc, (
        "the docstring must state what this smoke DOES validate "
        "(save_plan -> ingest -> dispatch -> implement -> test gate)"
    )
