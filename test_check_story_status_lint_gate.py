"""Server-side lint gate tests for `check_story_status`.

This story wires `detect_lint_command` (already on master in
`pipeline/build_detect.py`) into the SERVER-SIDE gate in
`check_story_status`: when a story passes its tests but a lint signal is
detected for the repo, a lint failure routes the story to `failed` (not
`tests_passed`), with the result recorded in `story['last_lint_check']`
alongside `last_test_check`.

These tests are written FIRST (TDD) and must currently be RED because the
implementation does not exist yet:
  - `_run_lint_gate` helper is not defined in `pipeline/server.py`.
  - the `last_lint_check` insertion in `check_story_status` is absent.

The fixtures replicate the local scaffolding pattern used by
`test_pipeline_mcp_server.py`'s `check_story_status` tests (the
`_css_setup` / `plan_dir` pattern), read-only reference, so this file is
self-contained and does not import from that module.
"""

import json
import subprocess
from datetime import timezone

import pytest

from pipeline import server as p


# ---------- Local scaffolding (mirrors test_pipeline_mcp_server.py) ----------

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


def _base_setup(plan_dir, monkeypatch, *, plan_name="lg", story_key="S1",
                test_returncode=0, test_stdout="1 passed",
                lint_returncode=0, lint_stdout="All checks passed!",
                lint_stderr="", detect_lint=None,
                test_cmd=None):
    """Build a worktree + manifest, mock the pid dead so the gate runs the
    tests, mock test detection + new-commits guard, and route subprocess.run
    so the test command returns `test_returncode` while the lint command (if
    invoked) returns `lint_returncode`.

    `detect_lint`, when given, is installed as `p.detect_lint_command`. When
    None it defaults to returning a fixed lint command tuple so a lint signal
    is present. Pass the sentinel `_NO_LINT` to install a detect_lint_command
    that returns None (no lint signal).
    """
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
    monkeypatch.setattr(
        p, "detect_test_command",
        lambda wt: (wt, test_cmd or ["pytest", "-q"]),
    )
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)

    # Default lint detection: a real lint signal pointing at the worktree.
    if detect_lint is _NO_LINT:
        monkeypatch.setattr(p, "detect_lint_command", lambda wt: None)
    elif detect_lint is None:
        monkeypatch.setattr(
            p, "detect_lint_command", lambda wt: (wt, ["ruff", "check", "."]),
        )
    else:
        monkeypatch.setattr(p, "detect_lint_command", detect_lint)

    def run_mock(cmd, **kwargs):
        # Distinguish the lint call from the test call by the lint command.
        lint_cmd = ["ruff", "check", "."]
        if list(cmd) == lint_cmd:
            return subprocess.CompletedProcess(
                cmd, lint_returncode, stdout=lint_stdout, stderr=lint_stderr,
            )
        return subprocess.CompletedProcess(
            cmd, test_returncode, stdout=test_stdout, stderr="",
        )

    monkeypatch.setattr(p.subprocess, "run", run_mock)
    return worktree


_NO_LINT = object()  # sentinel for "detect_lint_command returns None"


# ---------- Required case 6: import landed ----------

def test_detect_lint_command_is_imported_and_callable():
    """Step 1 of the story: `detect_lint_command` must be imported into
    `pipeline.server` and callable as `p.detect_lint_command` (proves the
    one-line import addition landed)."""
    assert hasattr(p, "detect_lint_command"), (
        "pipeline.server must import detect_lint_command from .build_detect"
    )
    assert callable(p.detect_lint_command)


# ---------- Required case 1: tests pass + lint exits 0 -> tests_passed ----------

def test_lint_gate_pass_keeps_tests_passed(plan_dir, monkeypatch):
    """Tests pass and the detected lint command exits 0: the story stays
    `tests_passed` and `last_lint_check` records returncode 0."""
    _base_setup(plan_dir, monkeypatch, lint_returncode=0)

    result = p.check_story_status("lg", "S1")

    assert result["status"] == "tests_passed"
    manifest = _read_manifest(plan_dir, "lg")
    story = manifest["stories"]["S1"]
    assert story["status"] == "tests_passed"
    lint = story["last_lint_check"]
    assert lint["cmd"] == ["ruff", "check", "."]
    assert lint["returncode"] == 0
    assert "All checks passed!" in lint["stdout_tail"]
    assert "ts" in lint


# ---------- Required case 2: tests pass + lint nonzero -> failed ----------

def test_lint_gate_fail_routes_to_failed_not_tests_passed(plan_dir, monkeypatch):
    """Tests pass but lint exits nonzero: the story is routed to `failed`
    (NOT `tests_passed`), and `last_lint_check` captures the nonzero
    returncode plus the lint output tail."""
    _base_setup(
        plan_dir, monkeypatch,
        lint_returncode=1,
        lint_stdout="src/x.py:1:1 E501 line too long",
        lint_stderr="warning: something",
    )

    result = p.check_story_status("lg", "S1")

    assert result["status"] == "failed", (
        "a lint failure after passing tests must route to failed, not "
        "tests_passed"
    )
    manifest = _read_manifest(plan_dir, "lg")
    story = manifest["stories"]["S1"]
    assert story["status"] == "failed"
    lint = story["last_lint_check"]
    assert lint["returncode"] != 0
    assert lint["returncode"] == 1
    assert "E501 line too long" in lint["stdout_tail"]
    assert "something" in lint["stderr_tail"]
    # The test check is still recorded alongside the lint check.
    assert "last_test_check" in story
    assert story["last_test_check"]["returncode"] == 0


# ---------- Required case 3: no lint signal -> unchanged regression bar ----------

def test_no_lint_signal_leaves_story_unchanged(plan_dir, monkeypatch):
    """`detect_lint_command` returns None (no lint signal for this repo):
    the story behaves exactly as before this change -- `tests_passed` and
    NO `last_lint_check` key is added to the story at all."""
    _base_setup(plan_dir, monkeypatch, detect_lint=_NO_LINT)

    result = p.check_story_status("lg", "S1")

    assert result["status"] == "tests_passed"
    manifest = _read_manifest(plan_dir, "lg")
    story = manifest["stories"]["S1"]
    assert story["status"] == "tests_passed"
    assert "last_lint_check" not in story, (
        "no lint signal must not add a last_lint_check key to the story"
    )
    # last_test_check still recorded as before.
    assert "last_test_check" in story


# ---------- Required case 4: tests fail -> lint never invoked ----------

def test_tests_fail_lint_never_invoked(plan_dir, monkeypatch):
    """When tests FAIL, lint must never even be invoked. We mock
    `detect_lint_command` to raise if called -- the existing test-failure
    path is completely unaffected by the lint gate."""
    def _fail_if_called(*a, **k):
        raise AssertionError(
            "detect_lint_command must not be called when tests fail"
        )

    _base_setup(
        plan_dir, monkeypatch,
        test_returncode=1, test_stdout="1 failed",
        detect_lint=_fail_if_called,
    )

    result = p.check_story_status("lg", "S1")

    assert result["status"] == "failed"
    manifest = _read_manifest(plan_dir, "lg")
    story = manifest["stories"]["S1"]
    assert story["status"] == "failed"
    # Lint was never run, so no lint check recorded.
    assert "last_lint_check" not in story
    # Test check still recorded on the failure path.
    assert story["last_test_check"]["returncode"] == 1


# ---------- Required case 5: step-cap interrupt path unaffected ----------

_STEP_CAP_MARKER_LOCAL = "[ended without done — step cap reached]"


def _make_fake_git_run(head_sha="deadbeef"):
    """subprocess.run stub mimicking _commit_wip's git usage."""
    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = f"{head_sha}\n" if cmd[:3] == ["git", "rev-parse", "HEAD"] else ""
            stderr = ""
        return Result()
    return _fake_run


def test_step_cap_interrupt_path_unaffected_and_lint_not_invoked(
    plan_dir, tmp_path, monkeypatch,
):
    """The exact regression guard for what broke last time: a story hitting
    the step cap must STILL get `story['interrupted_at']` set (the line a
    prior replace_lines edit silently deleted three lines away from the
    insertion point), and lint must never be invoked on this path."""
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
    monkeypatch.setattr(
        p.os, "kill",
        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()),
    )
    # Neither test detection nor lint detection may run on this path.
    def _fail_detect_test(*a, **k):
        raise AssertionError(
            "detect_test_command must not run on a step-cap exit")
    monkeypatch.setattr(p, "detect_test_command", _fail_detect_test)

    def _fail_detect_lint(*a, **k):
        raise AssertionError(
            "detect_lint_command must not run on a step-cap exit")
    monkeypatch.setattr(p, "detect_lint_command", _fail_detect_lint)

    monkeypatch.setattr(
        p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    result = p.check_story_status("cap1", "S1")

    assert result["status"] == "interrupted"
    assert result["reason"] == "step_cap_reached"
    manifest = _read_manifest(plan_dir, "cap1")
    story = manifest["stories"]["S1"]
    assert story["status"] == "interrupted"
    # THE regression guard: interrupted_at must still be set.
    assert "interrupted_at" in story, (
        "step-cap interrupt must still set story['interrupted_at'] -- "
        "this is the exact line a prior replace_lines edit deleted"
    )
    assert story["interrupted_at"]  # non-empty
    # Lint never ran.
    assert "last_lint_check" not in story
    # Journal entry so the resume path has context.
    journal = p._read_journal("cap1", "S1")
    assert any(e.get("step") == "step_cap_reached" for e in journal), journal


# ---------- Boundary: lint output tail is truncated to last 2000 chars ----------

def test_lint_gate_stdout_tail_truncated_to_last_2000(plan_dir, monkeypatch):
    """The lint result's stdout_tail/stderr_tail must be the LAST 2000 chars
    (same shape as last_test_check), not the head -- a long lint output must
    keep its trailing summary line, not its leading banner."""
    long_stdout = "B" * 3000 + "TAIL_MARKER"
    _base_setup(
        plan_dir, monkeypatch,
        lint_returncode=1, lint_stdout=long_stdout, lint_stderr="",
    )

    result = p.check_story_status("lg", "S1")

    assert result["status"] == "failed"
    manifest = _read_manifest(plan_dir, "lg")
    lint = manifest["stories"]["S1"]["last_lint_check"]
    assert lint["stdout_tail"].endswith("TAIL_MARKER")
    assert len(lint["stdout_tail"]) <= 2000
    # The leading 1000 B's were truncated away.
    assert lint["stdout_tail"].count("B") == 2000 - len("TAIL_MARKER")


# ---------- Boundary: linter vanishes between detection and run -> fail open ----------

def test_lint_gate_linter_vanishes_fails_open(plan_dir, monkeypatch):
    """A linter that vanishes between detection and run (OSError /
    subprocess.TimeoutExpired on the subprocess call) must fail OPEN: return
    None, never crash the gate, never block on a missing tool. The story
    stays `tests_passed` (tests passed, no usable lint signal)."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("[step 0] bash: pwd\n")
    _write_manifest(plan_dir, "lg", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(
        p.os, "kill",
        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()),
    )
    monkeypatch.setattr(
        p, "detect_test_command", lambda wt: (wt, ["pytest", "-q"]),
    )
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)
    # Lint is detected...
    monkeypatch.setattr(
        p, "detect_lint_command", lambda wt: (wt, ["ruff", "check", "."]),
    )

    def run_mock(cmd, **kwargs):
        if list(cmd) == ["ruff", "check", "."]:
            # Linter binary vanished between detection and run.
            raise FileNotFoundError("[Errno 2] No such file or directory: 'ruff'")
        return subprocess.CompletedProcess(cmd, 0, stdout="1 passed", stderr="")

    monkeypatch.setattr(p.subprocess, "run", run_mock)

    # Must not raise.
    result = p.check_story_status("lg", "S1")

    assert result["status"] == "tests_passed"
    manifest = _read_manifest(plan_dir, "lg")
    story = manifest["stories"]["S1"]
    assert story["status"] == "tests_passed"
    # Fail-open: no lint check recorded because the run could not complete.
    assert "last_lint_check" not in story


# ---------- Boundary: _run_lint_gate helper exists with the right shape ----------

def test_run_lint_gate_helper_exists():
    """Step 2 of the story: a module-level `_run_lint_gate` helper must
    exist on `pipeline.server`."""
    assert hasattr(p, "_run_lint_gate"), (
        "pipeline.server must define a module-level _run_lint_gate helper"
    )
    assert callable(p._run_lint_gate)


def test_run_lint_gate_returns_none_when_no_lint_signal(tmp_path, monkeypatch):
    """`_run_lint_gate` returns None when `detect_lint_command` returns None
    (fail open -- no lint signal for this repo)."""
    assert hasattr(p, "_run_lint_gate"), "missing _run_lint_gate helper"
    monkeypatch.setattr(p, "detect_lint_command", lambda wt: None)
    result = p._run_lint_gate(tmp_path, {})
    assert result is None


def test_run_lint_gate_returns_dict_with_expected_shape(tmp_path, monkeypatch):
    """`_run_lint_gate` returns a dict shaped like `last_test_check`:
    cmd, returncode, stdout_tail, stderr_tail, ts."""
    assert hasattr(p, "_run_lint_gate"), "missing _run_lint_gate helper"
    monkeypatch.setattr(
        p, "detect_lint_command", lambda wt: (wt, ["ruff", "check", "."]),
    )
    monkeypatch.setattr(
        p.subprocess, "run",
        lambda cmd, **k: subprocess.CompletedProcess(
            cmd, 0, stdout="ok", stderr=""),
    )
    result = p._run_lint_gate(tmp_path, {"PATH": "/usr/bin"})
    assert result is not None
    assert result["cmd"] == ["ruff", "check", "."]
    assert result["returncode"] == 0
    assert result["stdout_tail"] == "ok"
    assert result["stderr_tail"] == ""
    # ts must be a parseable ISO timestamp in UTC.
    assert "ts" in result
    from datetime import datetime
    parsed = datetime.fromisoformat(result["ts"])
    assert parsed.tzinfo is not None
    assert parsed.tzinfo.utcoffset(parsed) == timezone.utc.utcoffset(
        datetime.now(timezone.utc))


def test_run_lint_gate_fails_open_on_oserror(tmp_path, monkeypatch):
    """`_run_lint_gate` wraps the subprocess call in
    try/except (OSError, subprocess.TimeoutExpired) and returns None on
    either -- a vanished linter fails open, never crashes."""
    assert hasattr(p, "_run_lint_gate"), "missing _run_lint_gate helper"
    monkeypatch.setattr(
        p, "detect_lint_command", lambda wt: (wt, ["ruff", "check", "."]),
    )

    def _raise(cmd, **k):
        raise OSError("vanished")

    monkeypatch.setattr(p.subprocess, "run", _raise)
    assert p._run_lint_gate(tmp_path, {}) is None


def test_run_lint_gate_fails_open_on_timeout(tmp_path, monkeypatch):
    """Same fail-open contract for subprocess.TimeoutExpired."""
    assert hasattr(p, "_run_lint_gate"), "missing _run_lint_gate helper"
    monkeypatch.setattr(
        p, "detect_lint_command", lambda wt: (wt, ["ruff", "check", "."]),
    )

    def _raise(cmd, **k):
        raise subprocess.TimeoutExpired(cmd, 30)

    monkeypatch.setattr(p.subprocess, "run", _raise)
    assert p._run_lint_gate(tmp_path, {}) is None


def test_run_lint_gate_passes_test_env_to_subprocess(tmp_path, monkeypatch):
    """`_run_lint_gate` must reuse the SAME test_env dict the caller passes
    (the PIPELINE_*/LOCAL_AGENT_*/REPO_ROOT-stripped env), not rebuild it."""
    assert hasattr(p, "_run_lint_gate"), "missing _run_lint_gate helper"
    monkeypatch.setattr(
        p, "detect_lint_command", lambda wt: (wt, ["ruff", "check", "."]),
    )
    seen_env = {}

    def _capture(cmd, **k):
        seen_env["env"] = k.get("env")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(p.subprocess, "run", _capture)
    sentinel_env = {"PATH": "/special", "CUSTOM": "1"}
    p._run_lint_gate(tmp_path, sentinel_env)
    assert seen_env["env"] is sentinel_env, (
        "_run_lint_gate must pass the caller's test_env dict straight through "
        "to subprocess.run without rebuilding it"
    )