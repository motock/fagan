"""Post-agent scratchpad untrack: the harness cleans up, not the agent.

``pipeline/git_ops._commit_wip`` already untracks a scratchpad for the commit
the harness itself makes, but a scratchpad the dispatched agent tracked with
its OWN ``git add -f`` / ``git commit`` bypasses every harness commit path and
survives. This story adds ``pipeline.git_ops._untrack_scratchpad`` and calls it
from ``check_story_status`` at the point where the tick concludes the agent
process is gone -- immediately before the grade -- so the branch tip is clean
before anything grades, reviews or merges it.

A tracked scratchpad is not cosmetic: the repo's guard tests fail on it, so the
grade rejects a story that was otherwise fine; and the file travels into the
eventual merge, where a rebase against another story that tracked the same
shared path conflicts (2026-09-17).

Written FIRST (TDD): every test below is RED against the current
implementation -- ``AttributeError`` on ``pipeline.git_ops._untrack_scratchpad``
for the unit tests, and a missing ``scratchpad_untrack`` record for the
integration ones, which drive the REAL ``check_story_status`` path. The new
names are reached by attribute access at runtime (never a module-level
``from ... import``) so collection still succeeds before they exist.
"""

import json
import subprocess
from pathlib import Path

import pytest

from pipeline import git_ops

# pipeline.story_status imports pipeline.server at module level and
# pipeline.server imports check_story_status back from story_status, so
# pipeline.server must be imported FIRST or collection dies on the cycle.
from pipeline import server as p
from pipeline import story_status as ss

PLAN_NAME = "su1"
STORY_KEY = "S1"


# ---------------------------------------------------------------------------
# 1. The helper: pipeline.git_ops._untrack_scratchpad (REAL git)
# ---------------------------------------------------------------------------
def _git(repo: Path, *args: str) -> str:
    """Run a git command in `repo`, check it succeeded, return its stdout."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    ).stdout


def _init_repo(tmp_path: Path, *, ignore_scratchpad: bool = False) -> Path:
    """A real git repo with one committed code file. Configures the committer
    identity LOCALLY (CI runners may have no global git identity)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test Runner")
    (repo / "code.py").write_text("x = 1\n")
    if ignore_scratchpad:
        (repo / ".gitignore").write_text(".agent_scratchpad*.md\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


def _head_subject(repo: Path) -> str:
    return _git(repo, "log", "-1", "--format=%s").strip()


def _track(repo: Path, name: str) -> None:
    """Force-add `name` and commit it, exactly like a dispatched agent's own
    ``git add -f`` + ``git commit``."""
    (repo / name).write_text("agent notes\n")
    _git(repo, "add", "-f", "--", name)
    _git(repo, "commit", "-m", f"add {name}")


def test_should_untrack_a_committed_scratchpad_and_report_it(tmp_path):
    repo = _init_repo(tmp_path)
    _track(repo, ".agent_scratchpad.md")

    result = git_ops._untrack_scratchpad(str(repo), "SU-1")

    assert result["paths"] == [".agent_scratchpad.md"]
    assert result["sha"] == _git(repo, "rev-parse", "HEAD").strip()
    assert _git(repo, "ls-files", "--", ".agent_scratchpad.md") == ""
    assert _head_subject(repo) == "chore(SU-1): untrack agent scratchpad"
    # The agent's own notes stay on disk: only the git history is cleaned up.
    assert (repo / ".agent_scratchpad.md").exists()


def test_should_untrack_a_staged_scratchpad_and_make_no_commit(tmp_path):
    """A scratchpad that was `git add -f`-ed but never committed leaves no
    deletion to record once it is unstaged -- so the untrack makes no commit,
    and the story is still rescued (the guard tests read the INDEX)."""
    repo = _init_repo(tmp_path)
    (repo / ".agent_scratchpad.md").write_text("agent notes\n")
    _git(repo, "add", "-f", "--", ".agent_scratchpad.md")

    result = git_ops._untrack_scratchpad(str(repo), "SU-1")

    assert result["paths"] == [".agent_scratchpad.md"]
    assert result["sha"] == ""
    assert _git(repo, "ls-files", "--", ".agent_scratchpad.md") == ""
    assert _head_subject(repo) == "init"


def test_should_make_no_commit_when_nothing_is_tracked(tmp_path):
    repo = _init_repo(tmp_path)

    result = git_ops._untrack_scratchpad(str(repo), "SU-1")

    assert result == {"paths": [], "sha": ""}
    assert _head_subject(repo) == "init"


def test_should_leave_an_ignored_untracked_scratchpad_alone(tmp_path):
    repo = _init_repo(tmp_path, ignore_scratchpad=True)
    (repo / ".agent_scratchpad.md").write_text("agent notes\n")

    result = git_ops._untrack_scratchpad(str(repo), "SU-1")

    assert result == {"paths": [], "sha": ""}
    assert _head_subject(repo) == "init"
    assert _git(repo, "status", "--porcelain") == ""
    assert (repo / ".agent_scratchpad.md").exists()


def test_should_untrack_every_matching_variant(tmp_path):
    repo = _init_repo(tmp_path)
    names = [
        ".agent_scratchpad.md",
        ".agent_scratchpad_plan.md",
        "notes_agent_scratchpad_tmp.md",
    ]
    for name in names:
        _track(repo, name)

    result = git_ops._untrack_scratchpad(str(repo), "SU-1")

    assert result["paths"] == sorted(names)
    for name in names:
        assert _git(repo, "ls-files", "--", name) == ""
        assert (repo / name).exists()


def test_should_not_touch_other_tracked_files(tmp_path):
    repo = _init_repo(tmp_path)
    _track(repo, ".agent_scratchpad.md")
    (repo / "code.py").write_text("x = 2\n")

    git_ops._untrack_scratchpad(str(repo), "SU-1")

    assert _git(repo, "show", "--name-only", "--format=", "HEAD").split() == [
        ".agent_scratchpad.md"
    ]
    lines = _git(repo, "status", "--porcelain").splitlines()
    # code.py is still tracked with its edit uncommitted, and the untrack
    # staged nothing: the scratchpad is left on disk, untracked.
    assert " M code.py" in lines, lines
    assert "?? .agent_scratchpad.md" in lines, lines


def test_should_report_a_git_failure_without_raising(tmp_path):
    """Never raises: a caller inside the scheduler tick keeps grading."""
    missing = tmp_path / "not-a-repo"

    result = git_ops._untrack_scratchpad(str(missing), "SU-1")

    assert result["paths"] == []
    assert result["sha"] == ""
    assert result["error"]


# ---------------------------------------------------------------------------
# 2. Wiring: the REAL check_story_status path must call it, before the grade
# ---------------------------------------------------------------------------
@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    return d


def _write_manifest(plan_dir, stories):
    (plan_dir / f"{PLAN_NAME}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}, indent=2)
    )


def _read_story(plan_dir):
    manifest = json.loads((plan_dir / f"{PLAN_NAME}.manifest.json").read_text())
    return manifest["stories"][STORY_KEY]


def _dead_kill(pid, sig):
    raise ProcessLookupError(f"pid {pid} is gone")


def _install_grade_harness(plan_dir, monkeypatch):
    """A worktree + manifest story whose pid is dead, the test command
    detected, and the pass-path gates stubbed (each has its own test file).
    ``subprocess.run`` answers the test command with a green result."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("[step 0] bash: pwd\n")
    _write_manifest(plan_dir, {STORY_KEY: {
        "summary": "thing",
        "status": "in_progress",
        "pid": 4242,
        "worktree": str(worktree),
    }})
    monkeypatch.setattr(p.os, "kill", _dead_kill)
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["pytest", "-q"]))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)
    monkeypatch.setattr(p, "_default_branch", lambda: "master")
    monkeypatch.setattr(p, "_run_lint_gate", lambda wt, env: None)
    monkeypatch.setattr(p, "_find_dead_new_functions", lambda wt, branch: [])
    monkeypatch.setattr(p, "_acceptance_tampered", lambda story, wt: None)
    monkeypatch.setattr(p, "_added_pytest_test_paths", lambda wt, key, branch: [])

    def run_mock(cmd_, **kwargs):
        if list(cmd_)[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(cmd_, 0, stdout="deadbeef\n", stderr="")
        return subprocess.CompletedProcess(cmd_, 0, stdout="1 passed\n", stderr="")

    monkeypatch.setattr(p.subprocess, "run", run_mock)
    return {"worktree": worktree}


def _recorder(monkeypatch, *, result):
    """Replace the untrack helper with a recorder. The rebound grade body
    resolves the name through globals() against pipeline.server's namespace, so
    patching pipeline.server is what grades the real call site -- the helper is
    deliberately not exported there under pytest, hence raising=False. The
    story_status attribute is patched too, mirroring the detached-grade tests'
    two-surface pattern."""
    calls = []

    def fake(worktree, story_key):
        calls.append((worktree, story_key))
        return result

    monkeypatch.setattr(ss, "_untrack_scratchpad", fake)
    monkeypatch.setattr(p, "_untrack_scratchpad", fake, raising=False)
    return calls


def test_should_untrack_before_grading_and_record_it(plan_dir, monkeypatch):
    _install_grade_harness(plan_dir, monkeypatch)
    calls = _recorder(
        monkeypatch, result={"paths": [".agent_scratchpad.md"], "sha": "abc123"}
    )

    result = p.check_story_status(PLAN_NAME, STORY_KEY)

    assert result["status"] == "tests_passed"
    assert len(calls) == 1
    assert calls[0][0].endswith("wt")
    assert calls[0][1] == STORY_KEY
    story = _read_story(plan_dir)
    assert story["scratchpad_untrack"]["paths"] == [".agent_scratchpad.md"]
    assert story["scratchpad_untrack"]["sha"] == "abc123"
    assert story["scratchpad_untrack"]["ts"]


def test_should_record_nothing_when_nothing_was_tracked(plan_dir, monkeypatch):
    _install_grade_harness(plan_dir, monkeypatch)
    calls = _recorder(monkeypatch, result={"paths": [], "sha": ""})

    result = p.check_story_status(PLAN_NAME, STORY_KEY)

    assert result["status"] == "tests_passed"
    assert len(calls) == 1
    assert "scratchpad_untrack" not in _read_story(plan_dir)


def test_should_not_untrack_while_a_grade_is_already_in_flight(plan_dir, monkeypatch):
    """The untrack runs once per agent run, not on every poll tick: a story
    whose detached grade is still in flight is left alone."""
    _install_grade_harness(plan_dir, monkeypatch)
    story = _read_story(plan_dir)
    story["grading_pid"] = 999999
    story["grading_started_at"] = "2026-01-01T00:00:00+00:00"
    story["grading_result_path"] = str(
        plan_dir / "grading" / STORY_KEY / "result.json"
    )
    _write_manifest(plan_dir, {STORY_KEY: story})
    calls = _recorder(
        monkeypatch, result={"paths": [".agent_scratchpad.md"], "sha": "abc"}
    )

    def _kill(pid, sig):
        if pid == 999999:
            # The probe may not signal it: the grade is presumed in flight.
            raise PermissionError("not permitted")
        raise ProcessLookupError(f"pid {pid} is gone")

    monkeypatch.setattr(p.os, "kill", _kill)

    result = p.check_story_status(PLAN_NAME, STORY_KEY)

    assert result["status"] == "grading"
    assert calls == []


# ---------------------------------------------------------------------------
# 3. Structural: one implementation, reachable from the rebound grade body
# ---------------------------------------------------------------------------
def test_the_helper_lives_in_git_ops_and_is_not_exported_from_story_status():
    assert ss._untrack_scratchpad is git_ops._untrack_scratchpad
    assert "def _untrack_scratchpad(" not in Path(ss.__file__).read_text()


def test_the_helper_is_exported_only_outside_pytest():
    """The export must sit INSIDE the ``if "pytest" not in sys.modules:`` guard,
    beside the detached-grade primitives it mirrors. ``_untrack_scratchpad``
    shells out to git, so exporting the real function under pytest would run it
    inside every pre-existing test that drives the dead-pid grade path -- they
    stub subprocess.run and pin its call sequence. The wiring tests above patch
    ``p._untrack_scratchpad`` directly, which is the surface the rebound body
    reads via globals()."""
    src = Path(ss.__file__).read_text()
    export = "_server._untrack_scratchpad = _untrack_scratchpad"
    guard = 'if "pytest" not in sys.modules:'
    assert export in src
    assert guard in src
    assert src.index(guard) < src.index(export)


def test_the_call_site_precedes_the_detached_grade_spawn():
    """Edit order matters: the untrack must land BEFORE the grade runs, or a
    tracked scratchpad fails the repo's guard tests and rejects the story."""
    src = Path(ss.__file__).read_text()
    call = 'untrack = globals().get("_untrack_scratchpad")'
    spawn = 'starter = globals().get("start_detached_grade")'
    assert call in src
    assert spawn in src
    assert src.index(call) < src.index(spawn)


def test_the_call_site_tolerates_the_helper_being_absent():
    """Absent under pytest (the guard skipped the export), so the grade body
    resolves it the way it resolves ``start_detached_grade``: a ``globals()``
    lookup plus a ``None`` check, never a bare call that would raise."""
    src = Path(ss.__file__).read_text()
    lookup = 'untrack = globals().get("_untrack_scratchpad")'
    assert lookup in src
    assert "if untrack is not None:" in src
    assert src.index(lookup) < src.index("if untrack is not None:")
