"""Tests for WORKTREE_SCOPE_RULE (HARDEN-1).

A plan-authored brief once carried the line
``REPO: /absolute/path/to/the/shared/primary/checkout`` and the dispatched
agent ran every command as ``cd <that path> && ...``, committing straight
onto master. The harness was never at fault (``cwd=`` was already the
worktree); the gap was that no prompt ever told the agent the working
directory is authoritative.

The fix is prompt-level only: one shared constant
(``pipeline.config.WORKTREE_SCOPE_RULE``) referenced from BOTH prompt sites -
the test-author prompt builders (``pipeline/test_author.py``) and the
executor instructions built in ``pipeline/dispatch.py``.

Written TDD-first: every test below fails (``AttributeError`` on
``pconfig.WORKTREE_SCOPE_RULE``, or a membership failure) until the
implementation lands. Assertions on the constant's text are MEMBERSHIP-only -
never its exact wording, length or hash - so later stories may legitimately
extend the wording without breaking these tests.
"""

import json
import re
import subprocess

import pytest

from app import backend
from pipeline import concurrency as pcon
from pipeline import config as pconfig
from pipeline import persistence as ppers
from pipeline import persona as pper
from pipeline import server as p
from pipeline import test_author as pta
from pipeline import ticketing as pt


# ---------------------------------------------------------------------------
# Fixtures (mirror tests/unit/test_dispatch_staleness.py so this file is
# self-contained - this repo has no shared conftest.py for these).
# ---------------------------------------------------------------------------
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
    monkeypatch.setattr(pper, "AGENTS_DIR", d)
    return d


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


@pytest.fixture
def worktree_root(tmp_path, monkeypatch):
    d = tmp_path / "worktrees"
    d.mkdir()
    monkeypatch.setattr(p, "WORKTREE_ROOT", d)
    return d


# ---------------------------------------------------------------------------
# 1. The shared constant itself
# ---------------------------------------------------------------------------
def test_worktree_scope_rule_exists_and_is_a_nonempty_string():
    rule = pconfig.WORKTREE_SCOPE_RULE
    assert isinstance(rule, str), (
        "pipeline.config.WORKTREE_SCOPE_RULE must be a module-level str"
    )
    assert rule.strip(), "WORKTREE_SCOPE_RULE must not be empty/whitespace"


@pytest.mark.parametrize(
    "keyword",
    [
        # idea 1: the cwd IS the repository for this task
        "working directory",
        # idea 2: it is a git worktree on this story's own branch
        "git",
        "worktree",
        "branch",
        # idea 3: never `cd` to an absolute path / never hand a file tool one
        "cd",
        "absolute path",
        # idea 4: a shared checkout elsewhere on master; writing there
        # bypasses the branch, review and CI
        "master",
        "review",
    ],
)
def test_worktree_scope_rule_states_required_idea(keyword):
    rule = pconfig.WORKTREE_SCOPE_RULE
    assert keyword in rule.lower(), (
        f"WORKTREE_SCOPE_RULE must state the {keyword!r} idea; got: {rule!r}"
    )


def test_worktree_scope_rule_mentions_ci():
    rule = pconfig.WORKTREE_SCOPE_RULE
    assert re.search(r"\bci\b|continuous integration", rule, re.IGNORECASE), (
        "WORKTREE_SCOPE_RULE must say that writing to the shared checkout "
        f"bypasses CI; got: {rule!r}"
    )


# ---------------------------------------------------------------------------
# 2. Test-author prompt builders (pipeline/test_author.py)
# ---------------------------------------------------------------------------
_SCOPE_HEADER = "--- Test-authoring scope for THIS dispatch ---"


def test_test_author_prompt_includes_worktree_scope_rule():
    prompt = pta._test_author_prompt("Build the thing.")
    assert pconfig.WORKTREE_SCOPE_RULE in prompt, (
        "the test-author prompt must carry WORKTREE_SCOPE_RULE"
    )


def test_test_author_prompt_prepends_worktree_scope_rule():
    prompt = pta._test_author_prompt("Build the thing.")
    assert _SCOPE_HEADER in prompt
    assert prompt.index(pconfig.WORKTREE_SCOPE_RULE) < prompt.index(_SCOPE_HEADER), (
        "WORKTREE_SCOPE_RULE must be PREPENDED to the test-author prompt, "
        "not buried mid-prompt"
    )


def test_test_author_prompt_includes_rule_when_instructions_empty():
    prompt = pta._test_author_prompt("")
    assert pconfig.WORKTREE_SCOPE_RULE in prompt, (
        "the rule must not be conditional on a non-empty brief"
    )


def test_rework_test_author_prompt_includes_worktree_scope_rule():
    prompt = pta._rework_test_author_prompt("Please fix the bug.", None)
    assert pconfig.WORKTREE_SCOPE_RULE in prompt, (
        "the rework test-author prompt must carry WORKTREE_SCOPE_RULE"
    )


def test_rework_test_author_prompt_prepends_worktree_scope_rule():
    prompt = pta._rework_test_author_prompt("Please fix the bug.", None)
    assert _SCOPE_HEADER in prompt
    assert prompt.index(pconfig.WORKTREE_SCOPE_RULE) < prompt.index(_SCOPE_HEADER), (
        "WORKTREE_SCOPE_RULE must be PREPENDED to the rework test-author "
        "prompt, not buried mid-prompt"
    )


def test_test_author_module_references_worktree_scope_rule():
    """The constant must be referenced from pipeline/test_author.py itself
    (not only from some other module that happens to build the prompt)."""
    import inspect

    assert "WORKTREE_SCOPE_RULE" in inspect.getsource(pta), (
        "pipeline/test_author.py must reference WORKTREE_SCOPE_RULE"
    )


# ---------------------------------------------------------------------------
# 3. Executor instructions built in pipeline/dispatch.py
# ---------------------------------------------------------------------------
_BRIEF_MARKER = "BRIEF-MARKER-9f3a"


class _FakeHandle:
    def __init__(self, pid=4242):
        self.pid = pid


class _CapturingDispatchBackend:
    """Stand-in for whatever backend.get_backend(...) returns, capturing the
    exact kwargs _dispatch_story_impl handed to dispatch()."""

    def __init__(self):
        self.calls = []

    def dispatch(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeHandle()

    def complete(self, prompt, **kwargs):  # planner/diagnosis roles, if reached
        return ""


def _run(args, cwd, check=True):
    return subprocess.run(args, cwd=cwd, check=check, capture_output=True, text=True)


def _make_origin_and_repo(tmp_path, branch="main"):
    """Bare `origin` + a real local clone `repo`, both on `branch`, one commit
    deep. Returns (origin, repo, branch)."""
    origin = tmp_path / "origin.git"
    _run(["git", "init", "-q", "--bare", "-b", branch, str(origin)], tmp_path)

    repo = tmp_path / "repo"
    _run(["git", "init", "-q", "-b", branch, str(repo)], tmp_path)
    _run(["git", "config", "user.email", "t@e.com"], repo)
    _run(["git", "config", "user.name", "t"], repo)
    (repo / "README.md").write_text("seed\n")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-qm", "init"], repo)
    _run(["git", "remote", "add", "origin", str(origin)], repo)
    _run(["git", "push", "-q", "-u", "origin", branch], repo)
    return origin, repo, branch


def _capture_executor_prompt(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
    plan_name, agent_instructions,
):
    """Dispatch a RESUMED story against a REAL repo/worktree and return the
    exact prompt string handed to the dispatch backend - i.e. the executor
    instruction text built in pipeline/dispatch.py."""
    _origin, repo, branch = _make_origin_and_repo(tmp_path)
    worktree_path = worktree_root / "S1"
    _run(
        ["git", "worktree", "add", "-b", "agent/s1", str(worktree_path), "HEAD"],
        repo,
    )
    (plan_dir / f"{plan_name}.manifest.json").write_text(json.dumps({
        "epics": {},
        "repo_root": str(repo),
        "stories": {
            "S1": {
                "summary": "Do thing",
                "agent_instructions": agent_instructions,
                "status": "interrupted",
                "dependencies": [],
                "worktree": str(worktree_path),
            },
        },
    }))

    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    monkeypatch.setattr(p, "_default_branch", lambda: branch)
    monkeypatch.setattr(
        pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(p, "_provision_worktree_venv", lambda *a, **k: None)

    capturing = _CapturingDispatchBackend()
    monkeypatch.setattr(backend, "get_backend", lambda role, name=None: capturing)

    p.dispatch_story(plan_name, "S1")

    assert capturing.calls, (
        "the dispatch backend was never called - the executor prompt could "
        "not be captured"
    )
    return capturing.calls[-1]["prompt"]


def test_executor_prompt_includes_worktree_scope_rule(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    prompt = _capture_executor_prompt(
        plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
        "scoperule1", f"Build it. {_BRIEF_MARKER}",
    )
    assert pconfig.WORKTREE_SCOPE_RULE in prompt, (
        "the executor prompt built in pipeline/dispatch.py must carry "
        "WORKTREE_SCOPE_RULE"
    )


def test_executor_prompt_prepends_worktree_scope_rule(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    prompt = _capture_executor_prompt(
        plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
        "scoperule2", f"Build it. {_BRIEF_MARKER}",
    )
    assert _BRIEF_MARKER in prompt, "the story brief must still reach the agent"
    assert prompt.index(pconfig.WORKTREE_SCOPE_RULE) < prompt.index(_BRIEF_MARKER), (
        "WORKTREE_SCOPE_RULE must be PREPENDED to the executor instructions, "
        "not buried mid-prompt"
    )


def test_executor_prompt_includes_rule_when_agent_instructions_empty(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    prompt = _capture_executor_prompt(
        plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
        "scoperule3", "",
    )
    assert pconfig.WORKTREE_SCOPE_RULE in prompt, (
        "the rule must survive a story whose agent_instructions are empty"
    )


def test_executor_prompt_includes_rule_when_brief_contains_absolute_path(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    brief = (
        "REPO: /absolute/path/to/the/shared/primary/checkout  (Python, pytest)"
    )
    prompt = _capture_executor_prompt(
        plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
        "scoperule4", brief,
    )
    assert pconfig.WORKTREE_SCOPE_RULE in prompt, (
        "an absolute path in the brief must not drop or truncate the rule"
    )
    assert "/absolute/path/to/the/shared/primary/checkout" in prompt, (
        "the brief itself must still be delivered intact"
    )


def test_dispatch_module_references_worktree_scope_rule():
    """The constant must be referenced from pipeline/dispatch.py itself (not
    only from some other module that happens to build the prompt)."""
    import inspect

    from pipeline import dispatch as pdispatch

    assert "WORKTREE_SCOPE_RULE" in inspect.getsource(pdispatch), (
        "pipeline/dispatch.py must reference WORKTREE_SCOPE_RULE"
    )
