"""Retry-once exemption for the DETACHED grade (GRADE_WRAPPER).

The in-process done-gate (``scripts/local_agent_git.py``'s
``_full_suite_result_impl``, merged as FSU-01) re-runs a RED full suite once
before it rejects a story: a green retry proves the failure was not this
story's change (a transient collision with another agent's run, a flaky test,
or a stale recorded failure).  The scheduler-tick recovery path grades the
SAME suite through ``GRADE_WRAPPER`` — stdlib-only source run as a detached
child via ``[sys.executable, "-c", GRADE_WRAPPER, ...]`` — and used to run the
command exactly ONCE, so those same transients still rejected the story there.

This story wires the same retry-once exemption into the wrapper:

  * a red first run is re-run ONCE;
  * a green retry is the grade (rc 0);
  * a red retry still rejects, reported with the FIRST run's output (the first
    run carries the real failure text);
  * a green first run is never re-run (exactly one invocation).

Written FIRST (TDD): every behavioural test here is RED until the retry lands
in ``GRADE_WRAPPER``.  Invocation counts are driven through a counter file the
child command appends to — never by mocking, because the wrapper is source
executed in a fresh child interpreter.  Only fast, bounded child processes
(no real test suite, no sleep longer than ~2s), ``tmp_path`` artifacts, no
network/docker/venv.
"""

import ast
import inspect
import json
import os
import re
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

# The exact fail-closed dict for a dead grading process whose result file is
# missing or malformed.  Fail CLOSED to a failed grade — never raise, never
# hang, never guess "passed".  This shape is a survivor: it must not change.
FAIL_CLOSED = {
    "returncode": 1,
    "stdout": "",
    "stderr": "detached grade exited without writing a result",
}

_RUN_LINE = "subprocess.run(cmd, capture_output=True, text=True)"

_POLL_INTERVAL = 0.05
_COLLECT_TIMEOUT = 20.0  # generous; children here run well under 2s


# ---------------------------------------------------------------- helpers


def _wrapper() -> str:
    return ss.GRADE_WRAPPER


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
    """A pid that is definitely gone: spawn, exit, and REAP it."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    with pytest.raises(ProcessLookupError):
        os.kill(proc.pid, 0)
    return proc.pid


def _counter_child(counter_path: Path, body: str) -> list[str]:
    """A child command that increments ``counter_path`` on every invocation.

    ``body`` runs after the increment with ``n`` bound to the invocation
    number (1-based).  The counter file is the ONLY way to observe how many
    times the wrapper ran the command — the wrapper is source executed in a
    fresh interpreter, so mocking is not an option.
    """
    script = (
        "import pathlib, sys\n"
        "p = pathlib.Path(sys.argv[1])\n"
        "n = int(p.read_text()) if p.exists() else 0\n"
        "n += 1\n"
        "p.write_text(str(n))\n"
        f"{body}\n"
    )
    return [sys.executable, "-c", script, str(counter_path)]


def _grade(cmd: list[str], tmp_path: Path):
    """Run one detached grade of ``cmd`` and return (result, result_path)."""
    result_path = tmp_path / "result.json"
    log_path = tmp_path / "grade.log"
    pid = ss.start_detached_grade(
        cmd,
        cwd=str(tmp_path),
        env=os.environ.copy(),
        result_path=str(result_path),
        log_path=str(log_path),
    )
    result = _wait_for_grade(pid, result_path)
    assert result is not None, "detached grade never completed on its own"
    return result, result_path, log_path


def _invocations(counter_path: Path) -> int:
    assert counter_path.exists(), (
        "the graded command was never invoked (no counter file written)"
    )
    return int(counter_path.read_text())


# ------------------------------------------- 1. red first run, green retry


def test_should_re_run_a_red_suite_once(tmp_path):
    """A red first run is re-run ONCE; the green retry IS the grade.

    The child exits 1 on its first invocation and 0 on its second.  The
    collected grade must be rc 0 (the retry's verdict), the counter must read
    exactly 2 (one retry, never a third attempt), and the result JSON must
    have been written.
    """
    counter = tmp_path / "count.txt"
    cmd = _counter_child(
        counter,
        "print('attempt', n)\n"
        "raise SystemExit(1 if n == 1 else 0)",
    )
    result, result_path, _log_path = _grade(cmd, tmp_path)

    assert result["returncode"] == 0, (
        "a green retry must be the grade — the transient red run must not "
        f"reject the story (got {result!r})"
    )
    assert _invocations(counter) == 2, "a red suite must be re-run exactly once"
    assert result_path.exists(), "the result JSON must be written"
    data = json.loads(result_path.read_text())
    assert data["returncode"] == 0
    assert set(data) == {"returncode", "stdout", "stderr"}


# ------------------------------------- 2. red first run, red retry: reject


def test_should_report_the_first_run_when_the_retry_also_fails(tmp_path):
    """A reproducible failure still rejects — with the FIRST run's output.

    The child always exits 1 and prints a different marker per invocation
    (RUN-1, RUN-2).  The grade must be rc 1 and the collected stdout must
    carry RUN-1 (the real failure text a human reads), not the retry's.
    """
    counter = tmp_path / "count.txt"
    cmd = _counter_child(
        counter,
        "print(f'RUN-{n}')\n"
        "raise SystemExit(1)",
    )
    result, _result_path, log_path = _grade(cmd, tmp_path)

    assert result["returncode"] == 1, (
        "a failure that reproduces on the retry must still reject the story"
    )
    assert "RUN-1" in result["stdout"], (
        "the reported failure text must be the FIRST run's — the first run is "
        f"the one that carries the real failure (got {result['stdout']!r})"
    )
    assert "RUN-2" not in result["stdout"], (
        "the retry's output must be discarded when the retry also fails"
    )
    assert "RUN-1" in log_path.read_text(), (
        "the log tail must also carry the first run's failure text"
    )
    assert _invocations(counter) == 2, (
        "a red retry must NOT trigger a third attempt"
    )


# ------------------------------------------- 3. green first run: no retry


def test_should_not_re_run_a_green_suite(tmp_path):
    """A green first run is never re-run: exactly one invocation."""
    counter = tmp_path / "count.txt"
    cmd = _counter_child(
        counter,
        "print('GREEN')\n"
        "raise SystemExit(0)",
    )
    result, result_path, log_path = _grade(cmd, tmp_path)

    assert result["returncode"] == 0
    assert _invocations(counter) == 1, (
        "a green suite must be invoked exactly once — the retry must not "
        "appear on the passing path"
    )
    assert json.loads(result_path.read_text())["returncode"] == 0
    assert "GREEN" in log_path.read_text()


# --------------------------------- 4. GRADE_WRAPPER source-level contract


def test_grade_wrapper_retry_block_is_present_and_ordered():
    """The retry must be the documented block, in the documented order.

    Two identical ``subprocess.run(cmd, ...)`` calls, the first stashed in
    ``first`` before the retry, and ``proc = first`` restoring the first run's
    output when the retry also fails.
    """
    wrapper = _wrapper()
    compile(wrapper, "<GRADE_WRAPPER>", "exec")  # must stay valid Python

    assert wrapper.count(_RUN_LINE) == 2, (
        "the wrapper must run the command exactly twice at most: the original "
        "run plus one retry"
    )
    assert wrapper.count("if proc.returncode != 0:") == 2, (
        "the retry must be guarded by the red-run check, and the retry's own "
        "result must be checked before falling back to the first run"
    )
    assert "first = proc" in wrapper, (
        "the first run's CompletedProcess must be stashed before the retry"
    )
    assert "proc = first" in wrapper, (
        "a red retry must fall back to the FIRST run's output"
    )

    first_idx = wrapper.index("first = proc")
    retry_idx = wrapper.index(_RUN_LINE, wrapper.index(_RUN_LINE) + 1)
    restore_idx = wrapper.index("proc = first")
    assert first_idx < retry_idx < restore_idx, (
        "order must be: stash first run -> re-run -> restore first run"
    )


def test_grade_wrapper_retry_comment_is_durable():
    """The retry comment must state the rationale and stay true after later
    stories merge — no 'today'/'for now' phrasing."""
    wrapper = _wrapper()
    assert "re-run ONCE" in wrapper, (
        "the comment must state that a red suite is re-run ONCE"
    )
    assert "first run" in wrapper, (
        "the comment must state that the first run carries the real failure"
    )
    lowered = wrapper.lower()
    assert "today" not in lowered, "no 'today' phrasing in the wrapper comment"
    assert "for now" not in lowered, "no 'for now' phrasing in the wrapper comment"


def test_grade_wrapper_stays_stdlib_only():
    """The wrapper runs in a fresh interpreter: json/subprocess/sys only, no
    project imports, no new imports."""
    tree = ast.parse(_wrapper())
    imported: set[str] = set()
    from_imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            from_imports.append(node.module or "")
    assert from_imports == [], (
        f"GRADE_WRAPPER must not use `from ... import` (got {from_imports})"
    )
    assert imported == {"json", "subprocess", "sys"}, (
        f"GRADE_WRAPPER must import exactly json/subprocess/sys, got {imported}"
    )


def test_grade_wrapper_payload_and_log_write_are_unchanged():
    """The payload key names and the log-path append stay byte-identical."""
    wrapper = _wrapper()
    assert re.search(
        r'payload\s*=\s*\{\s*"returncode":\s*proc\.returncode,\s*'
        r'"stdout":\s*proc\.stdout,\s*"stderr":\s*proc\.stderr\s*\}',
        wrapper,
    ), "the payload key names/shape must not change"
    assert 'open(result_path, "w"' in wrapper
    assert 'open(log_path, "a"' in wrapper, "the log path is still appended to"
    assert wrapper.index("fh.write(proc.stdout)") < wrapper.index(
        "fh.write(proc.stderr)"
    ), "the log still gets stdout then stderr"


# --------------------------------------- 5. survivors must not be touched


def test_detached_grade_survivors_unchanged(tmp_path):
    """start_detached_grade/collect_detached_grade signatures and the
    fail-closed dict shape are survivors of this story."""
    assert list(inspect.signature(ss.start_detached_grade).parameters) == [
        "cmd", "cwd", "env", "result_path", "log_path",
    ]
    assert list(inspect.signature(ss.collect_detached_grade).parameters) == [
        "pid", "result_path",
    ]

    pid = _dead_pid()
    missing = tmp_path / "never_written.json"
    assert ss.collect_detached_grade(pid, str(missing)) == FAIL_CLOSED

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{definitely not valid json{{")
    assert ss.collect_detached_grade(pid, str(malformed)) == FAIL_CLOSED

    source = _story_status_source()
    assert 'if "pytest" not in sys.modules:' in source, (
        "the module-level pytest guard at the end of the file is a survivor"
    )
