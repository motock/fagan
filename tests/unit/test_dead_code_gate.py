"""Dead-code gate tests: check_story_status's guard against a newly-added,
never-called module-level function.

Root-caused live 2026-07-25 on MODE40-CI-REWORK-FEEDBACK-V2: the story
correctly implemented and unit-tested a helper function
(`_ci_rework_feedback`), but never actually wired it into the production
call path it was meant to replace - the old inline template stayed live.
No test caught this because the story's own tests called the helper
directly, never through the code path that was supposed to route to it.
The real LLM reviewer caught it, but that spends a whole review cycle on
something a cheap, static, pre-review check can catch for free - the same
philosophy as the lint gate (see test_check_story_status_lint_gate.py).

Two layers tested here:
  1. `_find_dead_new_functions` / `_module_level_function_names`: the
     detection logic itself, exercised against real git repos (the check
     is git-diff/git-grep-based, not mockable at a useful granularity).
  2. `check_story_status`'s wiring: a story whose tests+lint pass but whose
     diff introduces a dead function must NOT land on `tests_passed`.
"""

import json
import subprocess

import pytest

from pipeline import server as p

# ---------------------------------------------------------------------------
# Layer 1: _find_dead_new_functions / _module_level_function_names
# ---------------------------------------------------------------------------

def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo_with_feature_branch(tmp_path, base_content, feature_content,
                                    base_filename="mod.py",
                                    extra_feature_files=None):
    """A repo with `base_content` committed on master, then a `feature`
    branch (checked out from master) adding `feature_content` as a new
    commit - the shape check_story_status's real callers operate on: a
    story branch that diverged from the default branch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "master", cwd=repo)
    _git("config", "user.email", "t@t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / base_filename).write_text(base_content)
    _git("add", base_filename, cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)

    _git("checkout", "-q", "-b", "feature", cwd=repo)
    (repo / base_filename).write_text(feature_content)
    files_to_add = [base_filename]
    for name, content in (extra_feature_files or {}).items():
        (repo / name).write_text(content)
        files_to_add.append(name)
    _git("add", *files_to_add, cwd=repo)
    _git("commit", "-q", "-m", "feature work", cwd=repo)
    return repo


def test_module_level_function_names_ignores_nested_and_methods():
    source = (
        "def top_level():\n"
        "    def nested():\n"
        "        pass\n"
        "    return nested\n\n"
        "class C:\n"
        "    def a_method(self):\n"
        "        pass\n\n"
        "async def top_level_async():\n"
        "    pass\n"
    )
    names = p._module_level_function_names(source)
    assert names == {"top_level", "top_level_async"}


def test_module_level_function_names_handles_syntax_error():
    assert p._module_level_function_names("def broken(:\n") == set()


def test_finds_a_new_helper_never_called_anywhere(tmp_path):
    """The exact MODE40 shape: a correctly-written helper, added but never
    wired into any call site, anywhere in the repo."""
    repo = _init_repo_with_feature_branch(
        tmp_path,
        base_content="def existing_helper():\n    return 'hi'\n",
        feature_content=(
            "def existing_helper():\n"
            "    return 'hi'\n\n\n"
            "def _ci_rework_feedback(gate_error):\n"
            "    return f'failed: {gate_error}'\n"
        ),
    )
    result = p._find_dead_new_functions(repo, "master")
    assert result == ["mod.py:_ci_rework_feedback"]


def test_does_not_flag_a_helper_wired_in_same_file(tmp_path):
    repo = _init_repo_with_feature_branch(
        tmp_path,
        base_content="def existing_helper():\n    return 'hi'\n",
        feature_content=(
            "def existing_helper():\n"
            "    return 'hi'\n\n\n"
            "def _ci_rework_feedback(gate_error):\n"
            "    return f'failed: {gate_error}'\n\n\n"
            "def caller():\n"
            "    return _ci_rework_feedback('x')\n"
        ),
    )
    result = p._find_dead_new_functions(repo, "master")
    assert "mod.py:_ci_rework_feedback" not in result


def test_does_not_flag_a_function_called_only_from_a_different_file(tmp_path):
    """A new public entry point called from another file in the same repo
    must not false-positive - the check is repo-wide, not per-file."""
    repo = _init_repo_with_feature_branch(
        tmp_path,
        base_content="def existing_helper():\n    return 'hi'\n",
        feature_content=(
            "def existing_helper():\n"
            "    return 'hi'\n\n\n"
            "def new_public_entry_point():\n"
            "    return 'called from elsewhere'\n"
        ),
        extra_feature_files={
            "other.py": (
                "from mod import new_public_entry_point\n\n"
                "def run():\n"
                "    return new_public_entry_point()\n"
            ),
        },
    )
    result = p._find_dead_new_functions(repo, "master")
    assert "mod.py:new_public_entry_point" not in result


def test_does_not_flag_a_preexisting_function_untouched_by_this_story(tmp_path):
    """A function that already existed on master (not newly added by this
    story) must never be flagged, even if it happens to be unreferenced -
    this gate is about NEW dead code, not a general unused-code audit."""
    repo = _init_repo_with_feature_branch(
        tmp_path,
        base_content=(
            "def existing_helper():\n    return 'hi'\n\n\n"
            "def already_dead_before_this_story():\n    return 'orphan'\n"
        ),
        feature_content=(
            "def existing_helper():\n    return 'hi, updated'\n\n\n"
            "def already_dead_before_this_story():\n    return 'orphan'\n"
        ),
    )
    result = p._find_dead_new_functions(repo, "master")
    assert result == []


def test_module_level_function_names_includes_dunders():
    # _module_level_function_names itself does not filter dunders - the
    # skip lives in _find_dead_new_functions, exercised below with a real
    # repo (it needs the git-grep occurrence path).
    assert p._module_level_function_names("def __getattr__(name):\n    pass\n") == {
        "__getattr__"
    }


def test_ignores_dunder_functions_in_full_gate(tmp_path):
    repo = _init_repo_with_feature_branch(
        tmp_path,
        base_content="def existing_helper():\n    return 'hi'\n",
        feature_content=(
            "def existing_helper():\n"
            "    return 'hi'\n\n\n"
            "def __getattr__(name):\n"
            "    raise AttributeError(name)\n"
        ),
    )
    result = p._find_dead_new_functions(repo, "master")
    assert result == []


def test_ignores_new_test_files_in_full_gate(tmp_path):
    """A new file matching the test-file naming convention is never a
    candidate for this check, even if it defines a helper nothing calls -
    test-file structure (fixtures, parametrize targets) is out of scope."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "master", cwd=repo)
    _git("config", "user.email", "t@t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "mod.py").write_text("def existing_helper():\n    return 'hi'\n")
    _git("add", "mod.py", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)

    _git("checkout", "-q", "-b", "feature", cwd=repo)
    (repo / "test_mod.py").write_text(
        "def _unused_fixture_helper():\n    return object()\n\n\n"
        "def test_something():\n    assert True\n"
    )
    _git("add", "test_mod.py", cwd=repo)
    _git("commit", "-q", "-m", "add tests", cwd=repo)

    result = p._find_dead_new_functions(repo, "master")
    assert result == []


def test_fails_open_on_git_error(tmp_path):
    """A directory that isn't a git repo at all must not raise - this is a
    quality signal, never a gate-crashing hazard."""
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()
    assert p._find_dead_new_functions(not_a_repo, "master") == []


# ---------------------------------------------------------------------------
# Layer 2: check_story_status wiring
# ---------------------------------------------------------------------------

@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    return d


def _write_manifest(plan_dir, plan_name, stories):
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}, indent=2)
    )


def _read_manifest(plan_dir, plan_name):
    return json.loads((plan_dir / f"{plan_name}.manifest.json").read_text())


def _base_setup(plan_dir, monkeypatch, *, plan_name="dc", story_key="S1",
                dead_functions=None):
    """A worktree + manifest with tests passing and no lint signal (isolates
    this gate from the lint gate), routing _find_dead_new_functions to a
    stub returning `dead_functions` (default: none found)."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("[step 0] bash: pwd\n")
    _write_manifest(plan_dir, plan_name, {
        story_key: {"summary": "thing", "status": "in_progress",
                    "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(
        p.os, "kill",
        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()),
    )
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["pytest", "-q"]))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)
    monkeypatch.setattr(p, "detect_lint_command", lambda wt: None)  # no lint signal
    monkeypatch.setattr(p.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(
                            cmd, 0, stdout="1 passed", stderr=""))
    monkeypatch.setattr(
        p, "_find_dead_new_functions", lambda wt, base: (dead_functions or [])
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "master")
    return worktree


def test_gate_clean_keeps_tests_passed(plan_dir, monkeypatch):
    _base_setup(plan_dir, monkeypatch, dead_functions=[])

    result = p.check_story_status("dc", "S1")

    assert result["status"] == "tests_passed"
    story = _read_manifest(plan_dir, "dc")["stories"]["S1"]
    assert story["status"] == "tests_passed"
    assert story["last_dead_code_check"] == []


def test_gate_finds_dead_function_routes_to_failed(plan_dir, monkeypatch):
    """Tests and lint pass, but a dead function is found: the story is
    routed to `failed` (NOT `tests_passed`), and the finding is recorded
    on the story for the downstream reviewer to see."""
    _base_setup(
        plan_dir, monkeypatch,
        dead_functions=["pipeline/server.py:_ci_rework_feedback"],
    )

    result = p.check_story_status("dc", "S1")

    assert result["status"] == "failed", (
        "a newly-added, never-called function after passing tests must "
        "route to failed, not tests_passed"
    )
    story = _read_manifest(plan_dir, "dc")["stories"]["S1"]
    assert story["status"] == "failed"
    assert story["last_dead_code_check"] == [
        "pipeline/server.py:_ci_rework_feedback"
    ]


def test_gate_skipped_when_tests_already_failed(plan_dir, monkeypatch):
    """The dead-code gate must only run once the baseline (tests, lint)
    passes - a story already failing on those doesn't need this check to
    also run (mirrors the lint gate's own `if passed:` scoping)."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("[step 0] bash: pwd\n")
    _write_manifest(plan_dir, "dcfail", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(
        p.os, "kill",
        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()),
    )
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["pytest", "-q"]))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)
    monkeypatch.setattr(p.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(
                            cmd, 1, stdout="1 failed", stderr=""))

    dead_code_calls = []
    monkeypatch.setattr(
        p, "_find_dead_new_functions",
        lambda wt, base: dead_code_calls.append(True) or [],
    )

    result = p.check_story_status("dcfail", "S1")

    assert result["status"] == "failed"
    assert dead_code_calls == [], "dead-code gate must not run when tests already failed"
