"""Detached grading primitives for the scheduler-tick recovery path.

Live incident being fixed (observed 2026-09-02): when an in_progress story's
dispatch process dies, ``check_story_status``'s dead-pid fall-through re-grades
the story by running the test suite SYNCHRONOUSLY with ``subprocess.run``
inside the tick.  A full-suite run (~15-270s) blocked the entire tick: no
dispatch, no review, no merge for ANY plan.  The advance-scheduler daemon sat
quiet for ~19 minutes grading one dead ci-plan story while 21 ready stories
waited.

This story adds three module-level members to ``pipeline/story_status.py``
(append-only — nothing existing may change; the wiring into
``check_story_status`` is the NEXT sibling story on this shared file):

  1. ``GRADE_WRAPPER``      — Python source run via
       [sys.executable, '-c', GRADE_WRAPPER, json.dumps(cmd_list),
        str(result_json_path), str(log_path)]
     which runs the command with capture_output=True/text=True and records
     {'returncode', 'stdout', 'stderr'} as JSON at the result path AND as
     plain "stdout then stderr" text at the log path — CompletedProcess
     shaped, so the existing verdict logic can consume it unchanged.
  2. ``start_detached_grade(cmd, cwd, env, result_path, log_path) -> int``
     Popen wrapper with start_new_session=True + DEVNULL pipes; returns the
     pid immediately (no wait, no capture) so the tick returns at once and
     the grade survives the daemon/MCP-server that spawned it.
  3. ``collect_detached_grade(pid, result_path) -> dict | None``
     None while the grading pid is alive (same os.kill(pid, 0) + ps stat=Z
     zombie-aware idiom check_story_status already uses — copied, not
     imported); once gone, the recorded dict, or a fail-closed failed grade
     if the result file is missing/malformed.  Never raises, never hangs.

Written FIRST (TDD): every test here must be RED (AttributeError on the
missing module-level names) until the implementation lands.  Tests use only
fast, bounded child processes (never a real test suite, never a sleep longer
than ~2s), tmp_path for artifacts, and require no network, docker, or venv.
"""

import ast
import inspect
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

# pipeline.story_status imports pipeline.server at module level and
# pipeline.server imports check_story_status back from story_status, so
# pipeline.server must be imported FIRST or collection dies on the cycle.
# Every existing story_status test file does the same.
from pipeline import server as _server  # noqa: F401
from pipeline import story_status as ss

# The exact fail-closed dict the brief specifies for a dead grading process
# whose result file is missing or malformed.  Fail CLOSED to a failed grade —
# never raise, never hang, never guess "passed".
FAIL_CLOSED = {
    "returncode": 1,
    "stdout": "",
    "stderr": "detached grade exited without writing a result",
}

_POLL_INTERVAL = 0.05
_COLLECT_TIMEOUT = 20.0  # generous; children here run well under 2s


# ---------------------------------------------------------------- helpers


def _story_status_source() -> str:
    return Path(ss.__file__).read_text()


def _wait_for_grade(pid: int, result_path, timeout: float = _COLLECT_TIMEOUT):
    """Poll collect_detached_grade until the grading process is gone.

    Returns the collected dict, or None on timeout (callers assert on that so
    a hung/never-completing grade fails the test instead of hanging it).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = ss.collect_detached_grade(pid, str(result_path))
        if result is not None:
            # The wrapper was spawned by THIS process, so once it is gone it
            # is a zombie of ours — reap it (best effort) so it does not
            # linger for the rest of the suite.
            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass
            return result
        time.sleep(_POLL_INTERVAL)
    return None


def _dead_pid() -> int:
    """A pid that is definitely gone: spawn, exit, and REAP it.

    After wait() the child is reaped, so os.kill(pid, 0) raises
    ProcessLookupError — exactly the "process is gone" state
    collect_detached_grade must detect.
    """
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    with pytest.raises(ProcessLookupError):
        os.kill(proc.pid, 0)
    return proc.pid


class _FakeProbe:
    """Stand-in for the ps subprocess.run result, mirroring how the existing
    story_status tests fake the `ps -p <pid> -o stat=` probe."""

    def __init__(self, stat: str):
        self.returncode = 0
        self.stdout = stat
        self.stderr = ""


def _patch_ps(monkeypatch, stat: str) -> None:
    def _fake_run(cmd, **kwargs):
        assert cmd and cmd[0] == "ps", (
            f"liveness probe must go through `ps`, got {cmd!r}"
        )
        return _FakeProbe(stat)

    monkeypatch.setattr(ss.subprocess, "run", _fake_run)


# ------------------------------------------------- 1. GRADE_WRAPPER


def test_grade_wrapper_exists_as_source_string():
    """GRADE_WRAPPER must be a module-level, non-empty, compilable Python
    source string that imports json, subprocess and sys itself (it runs in a
    fresh interpreter via `sys.executable -c`)."""
    wrapper = ss.GRADE_WRAPPER
    assert isinstance(wrapper, str)
    assert wrapper.strip(), "GRADE_WRAPPER must not be empty"
    compile(wrapper, "<GRADE_WRAPPER>", "exec")  # must be valid Python
    assert "import json" in wrapper
    assert "import subprocess" in wrapper
    assert "import sys" in wrapper


def test_grade_wrapper_writes_completedprocess_shaped_result_and_log(tmp_path):
    """Running the wrapper with the documented argv shape must record the
    child's CompletedProcess-shaped outcome at BOTH paths.

    argv contract: [sys.executable, '-c', GRADE_WRAPPER, json.dumps(cmd_list),
    str(result_json_path), str(log_path)].
    """
    result_path = tmp_path / "result.json"
    log_path = tmp_path / "grade.log"
    cmd = [
        sys.executable,
        "-c",
        (
            "import sys; print('grade-stdout-marker'); "
            "print('grade-stderr-marker', file=sys.stderr)"
        ),
    ]
    proc = subprocess.run(
        [sys.executable, "-c", ss.GRADE_WRAPPER, json.dumps(cmd),
         str(result_path), str(log_path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"GRADE_WRAPPER itself crashed: rc={proc.returncode} "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )

    data = json.loads(result_path.read_text())
    # CompletedProcess-shaped so the existing verdict logic (which reads
    # test_result.returncode / .stdout / .stderr) consumes it unchanged.
    assert data["returncode"] == 0
    assert "grade-stdout-marker" in data["stdout"]
    assert "grade-stderr-marker" in data["stderr"]
    assert isinstance(data["stdout"], str) and isinstance(data["stderr"], str)

    # The log path gets the same captured output as plain text,
    # stdout then stderr.
    log_text = log_path.read_text()
    assert "grade-stdout-marker" in log_text
    assert "grade-stderr-marker" in log_text
    assert log_text.index("grade-stdout-marker") < log_text.index(
        "grade-stderr-marker"
    )


def test_grade_wrapper_records_failing_child_returncode(tmp_path):
    """A child that exits non-zero must still be recorded (rc=1), with the
    wrapper itself succeeding — the failure belongs to the graded command."""
    result_path = tmp_path / "result.json"
    log_path = tmp_path / "grade.log"
    cmd = [
        sys.executable,
        "-c",
        'import sys; print("boom", file=sys.stderr); raise SystemExit(1)',
    ]
    proc = subprocess.run(
        [sys.executable, "-c", ss.GRADE_WRAPPER, json.dumps(cmd),
         str(result_path), str(log_path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(result_path.read_text())
    assert data["returncode"] == 1
    assert "boom" in data["stderr"]
    assert "boom" in log_path.read_text()


# --------------------------------------- 2. start_detached_grade


def test_start_detached_grade_signature_matches_wiring_contract():
    """The wiring story will call these with exactly these names."""
    start_sig = inspect.signature(ss.start_detached_grade)
    assert list(start_sig.parameters) == [
        "cmd", "cwd", "env", "result_path", "log_path",
    ]
    collect_sig = inspect.signature(ss.collect_detached_grade)
    assert list(collect_sig.parameters) == ["pid", "result_path"]
    # Return annotations from the brief: -> int and -> dict | None.
    start_ret = start_sig.return_annotation
    assert start_ret is not inspect.Parameter.empty, (
        "start_detached_grade must be annotated -> int"
    )
    assert start_ret in (int, "int")
    collect_ret = collect_sig.return_annotation
    assert collect_ret is not inspect.Parameter.empty, (
        "collect_detached_grade must be annotated -> dict | None"
    )
    assert "dict" in str(collect_ret) and "None" in str(collect_ret)


def test_start_detached_grade_spawns_and_returns_positive_pid(tmp_path):
    """Happy path: positive pid, the spawned grade completes ON ITS OWN, and
    both artifact files exist afterwards."""
    result_path = tmp_path / "result.json"
    log_path = tmp_path / "grade.log"
    pid = ss.start_detached_grade(
        [sys.executable, "-c", "pass"],
        cwd=str(tmp_path),
        env=os.environ.copy(),
        result_path=str(result_path),
        log_path=str(log_path),
    )
    assert isinstance(pid, int)
    assert pid > 0

    result = _wait_for_grade(pid, result_path)
    assert result is not None, "detached grade never completed on its own"
    assert result["returncode"] == 0
    assert result_path.exists(), "wrapper must write the result JSON"
    assert log_path.exists(), "wrapper must write the log file"


def test_start_detached_grade_honors_cwd_and_env(tmp_path):
    """cwd and env must be handed to the spawned grading process."""
    result_path = tmp_path / "result.json"
    log_path = tmp_path / "grade.log"
    cmd = [
        sys.executable,
        "-c",
        (
            "import os; print(os.getcwd()); "
            "print(os.environ.get('DETACHED_GRADE_PROBE', 'MISSING'))"
        ),
    ]
    env = dict(os.environ)
    env["DETACHED_GRADE_PROBE"] = "probe-value-42"
    pid = ss.start_detached_grade(
        cmd,
        cwd=str(tmp_path),
        env=env,
        result_path=str(result_path),
        log_path=str(log_path),
    )
    result = _wait_for_grade(pid, result_path)
    assert result is not None, "detached grade never completed"
    assert result["returncode"] == 0
    assert os.path.realpath(str(tmp_path)) in result["stdout"], (
        f"graded command must run with cwd={tmp_path}, got {result['stdout']!r}"
    )
    assert "probe-value-42" in result["stdout"], (
        f"graded command must see the custom env, got {result['stdout']!r}"
    )


def test_start_detached_grade_starts_new_session(tmp_path):
    """start_new_session=True is REQUIRED: the grade must survive the daemon
    that spawned it.  A setsid'd child is its own session leader, so its sid
    equals its own pid and differs from ours."""
    if sys.platform == "win32":
        pytest.skip("os.getsid is POSIX-only")
    result_path = tmp_path / "result.json"
    log_path = tmp_path / "grade.log"
    pid = ss.start_detached_grade(
        [sys.executable, "-c", "import os; print(os.getsid(0))"],
        cwd=str(tmp_path),
        env=os.environ.copy(),
        result_path=str(result_path),
        log_path=str(log_path),
    )
    result = _wait_for_grade(pid, result_path)
    assert result is not None, "detached grade never completed"
    child_sid = int(result["stdout"].strip())
    assert child_sid == pid, (
        "grading process must be a session leader of its own new session "
        f"(start_new_session=True); got sid={child_sid}, pid={pid}"
    )
    assert child_sid != os.getsid(0), (
        "grading process must NOT share the test process's session"
    )


def test_start_detached_grade_source_uses_start_new_session_and_devnull():
    """The detachment is the whole point — assert the Popen kwargs directly,
    in addition to the behavioral getsid test above."""
    src = inspect.getsource(ss.start_detached_grade)
    assert "start_new_session=True" in src, (
        "start_detached_grade must pass start_new_session=True to Popen"
    )
    assert "DEVNULL" in src, (
        "start_detached_grade must send stdout/stderr to DEVNULL"
    )


# --------------------------------------- 3. collect_detached_grade


def test_collect_detached_grade_returns_none_while_grader_alive(tmp_path):
    """While the grading process is alive collect must return None — and the
    immediately-following call also proves start_detached_grade did NOT wait
    for the ~1s child (a blocking start would leave nothing alive to poll)."""
    result_path = tmp_path / "result.json"
    log_path = tmp_path / "grade.log"
    pid = ss.start_detached_grade(
        [sys.executable, "-c", "import time; time.sleep(1); pass"],
        cwd=str(tmp_path),
        env=os.environ.copy(),
        result_path=str(result_path),
        log_path=str(log_path),
    )
    assert ss.collect_detached_grade(pid, str(result_path)) is None, (
        "collect_detached_grade must return None while the grading pid is alive"
    )
    result = _wait_for_grade(pid, result_path)
    assert result is not None, "slow-but-bounded grade never completed"
    assert result["returncode"] == 0


def test_collect_detached_grade_ignores_stale_result_file_while_alive(
    tmp_path,
):
    """A pre-existing result file must NOT be returned while the grading
    process is still alive — liveness wins over file presence."""
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps({"returncode": 0, "stdout": "stale",
                                       "stderr": ""}))
    pid = ss.start_detached_grade(
        [sys.executable, "-c", "import time; time.sleep(1); pass"],
        cwd=str(tmp_path),
        env=os.environ.copy(),
        result_path=str(result_path),
        log_path=str(tmp_path / "grade.log"),
    )
    assert ss.collect_detached_grade(pid, str(result_path)) is None


def test_collect_detached_grade_records_passing_returncode(tmp_path):
    result_path = tmp_path / "result.json"
    pid = ss.start_detached_grade(
        [sys.executable, "-c", "pass"],
        cwd=str(tmp_path),
        env=os.environ.copy(),
        result_path=str(result_path),
        log_path=str(tmp_path / "grade.log"),
    )
    result = _wait_for_grade(pid, result_path)
    assert result is not None, "detached grade never completed"
    assert result["returncode"] == 0


def test_collect_detached_grade_records_failing_returncode(tmp_path):
    result_path = tmp_path / "result.json"
    pid = ss.start_detached_grade(
        [sys.executable, "-c", "raise SystemExit(1)"],
        cwd=str(tmp_path),
        env=os.environ.copy(),
        result_path=str(result_path),
        log_path=str(tmp_path / "grade.log"),
    )
    result = _wait_for_grade(pid, result_path)
    assert result is not None, "detached grade never completed"
    assert result["returncode"] == 1


def test_collect_detached_grade_returns_recorded_dict_verbatim(tmp_path):
    """Once the process is gone, the dict the result file holds is returned
    as-is (pass-through, not just rc 0/1 shapes)."""
    pid = _dead_pid()
    result_path = tmp_path / "result.json"
    recorded = {"returncode": 3, "stdout": "some output", "stderr": "warned"}
    result_path.write_text(json.dumps(recorded))
    assert ss.collect_detached_grade(pid, str(result_path)) == recorded


def test_collect_detached_grade_missing_result_file_fails_closed(tmp_path):
    """Dead pid + no result file -> the exact fail-closed failed grade.
    Never raise, never hang, never guess 'passed'."""
    pid = _dead_pid()
    missing = tmp_path / "never_written.json"
    assert not missing.exists()
    assert ss.collect_detached_grade(pid, str(missing)) == FAIL_CLOSED


def test_collect_detached_grade_malformed_result_json_fails_closed(tmp_path):
    pid = _dead_pid()
    result_path = tmp_path / "result.json"
    result_path.write_text("{definitely not valid json{{")
    assert ss.collect_detached_grade(pid, str(result_path)) == FAIL_CLOSED


def test_collect_detached_grade_non_dict_result_json_fails_closed(tmp_path):
    """The brief says collect returns 'the dict it holds' — a JSON document
    that parses but is not an object holds no dict, so it is malformed for
    these purposes and must fail closed like any other unreadable result."""
    pid = _dead_pid()
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps(["stdout", "stderr"]))
    assert ss.collect_detached_grade(pid, str(result_path)) == FAIL_CLOSED


def test_collect_detached_grade_treats_zombie_stat_as_gone(monkeypatch, tmp_path):
    """os.kill(pid, 0) succeeds for zombies too — the ps stat=Z check is what
    makes a zombie grading pid count as gone.  Mirror the existing
    story_status tests: fake the ps probe to return 'Z' for a pid that IS
    killable (ours), so the ONLY route to the fail-closed dict is the
    zombie-aware branch."""
    _patch_ps(monkeypatch, "Z")
    result = ss.collect_detached_grade(os.getpid(), str(tmp_path / "gone.json"))
    assert result == FAIL_CLOSED


def test_collect_detached_grade_alive_stat_returns_none(monkeypatch, tmp_path):
    """Contrast with the zombie test: same killable pid, ps reports a live
    stat -> None, even though the result file is missing.  Proves the stat
    probe (not just file existence) drives the verdict."""
    _patch_ps(monkeypatch, "S")
    result = ss.collect_detached_grade(os.getpid(), str(tmp_path / "gone.json"))
    assert result is None


def test_collect_detached_grade_real_zombie_pid_fails_closed(tmp_path):
    """End-to-end zombie: an unreaped dead child of ours is killable (so
    os.kill alone would call it alive) but ps reports stat=Z, so collect must
    treat it as gone and fail closed on the missing result file."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    deadline = time.monotonic() + 10.0
    saw_zombie = False
    while time.monotonic() < deadline:
        probe = subprocess.run(
            ["ps", "-p", str(proc.pid), "-o", "stat="],
            capture_output=True, text=True, check=False,
        )
        if probe.stdout.strip().startswith("Z"):
            saw_zombie = True
            break
        time.sleep(0.02)
    if not saw_zombie:
        proc.wait()
        pytest.skip("could not observe a zombie process on this platform")
    try:
        result = ss.collect_detached_grade(
            proc.pid, str(tmp_path / "never_written.json")
        )
        assert result == FAIL_CLOSED
    finally:
        proc.wait()  # reap so the suite does not leak a zombie


def test_collect_detached_grade_copies_liveness_idiom_dont_import():
    """The zombie-aware liveness pattern must be COPIED into
    collect_detached_grade (os.kill(pid, 0) + `ps -p <pid> -o stat=` + the
    'Z' check), not imported from check_story_status."""
    src = inspect.getsource(ss.collect_detached_grade)
    assert "os.kill" in src, "must probe liveness with os.kill(pid, 0)"
    assert "stat=" in src, "must run `ps -p <pid> -o stat=`"
    assert "Z" in src, "must treat a stat starting with Z as gone"


# --------------------------------- module shape / append-only guards


def _top_level_defs_and_assigns(tree) -> dict:
    """Map top-level name -> line number for defs and simple assignments."""
    found = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found[node.name] = node.lineno
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = node.lineno
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target,
                                                            ast.Name):
            found[node.target.id] = node.lineno
    return found


def test_three_new_names_defined_at_module_level():
    """grep-equivalent: GRADE_WRAPPER, start_detached_grade and
    collect_detached_grade must all be defined at MODULE level in
    pipeline/story_status.py (not nested inside any function)."""
    tree = ast.parse(_story_status_source())
    top = _top_level_defs_and_assigns(tree)
    for name in ("GRADE_WRAPPER", "start_detached_grade",
                 "collect_detached_grade"):
        assert name in top, (
            f"{name} must be defined at module level in pipeline/story_status.py"
        )


def test_new_names_appended_after_existing_code():
    """Append-only story: the three additions sit after check_story_status in
    the module (this file is shared — the next sibling story wires
    check_story_status itself, so only membership/ordering is asserted
    here, never the file's total contents)."""
    tree = ast.parse(_story_status_source())
    top = _top_level_defs_and_assigns(tree)
    assert "check_story_status" in top
    for name in ("GRADE_WRAPPER", "start_detached_grade",
                 "collect_detached_grade"):
        assert name in top
        assert top[name] > top["check_story_status"], (
            f"{name} must be appended after the existing module code "
            f"(at line {top[name]}, check_story_status is at "
            f"{top['check_story_status']})"
        )


def test_existing_story_status_surface_survives():
    """Append-only story: the survivor list must still be present.  Membership
    only — check_story_status itself gets edited by the NEXT sibling story,
    so its body must not be pinned here."""
    src = _story_status_source()
    for needle in (
        "def check_story_status(",
        "_terminate_and_checkpoint",
        "_rebrief_step_cap_struggle",
        "DISPATCH_WATCHDOG_SECONDS",
        "DISPATCH_STARTUP_GRACE_SECONDS",
    ):
        assert needle in src, f"survivor {needle!r} missing from story_status"