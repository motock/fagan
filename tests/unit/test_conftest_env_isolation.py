"""Regression guard for the conftest import-time env clearing that stabilizes
the read-heavy / repetition-guard test family under full-suite load.

ROOT CAUSE these tests guard (retros/tdd-split-unconditional-and-review-race
2026-07-21, A3 P0): scripts/local_agent.py reads module-level constants from
``LOCAL_AGENT_*`` env vars ONCE at import time (e.g.
``READ_HEAVY_WINDOW = int(os.environ.get("LOCAL_AGENT_READ_HEAVY_WINDOW",
"6"))``). When the suite runs inside a dispatched agent's own bash tool, the
scheduler's launchd plist leaks ``LOCAL_AGENT_*`` vars (e.g.
``LOCAL_AGENT_PARK_ENABLED=0``, or a non-default
``LOCAL_AGENT_READ_HEAVY_WINDOW``) into the pytest subprocess. A test that
asserts the DEFAULT constant (e.g. ``test_local_agent_read_heavy_distinct_constants``
pins ``READ_HEAVY_WINDOW == 6``) then fails — but only under that leaked-env
load, never in normal CI where the vars are absent. That is the
"passes standalone, fails under full-suite load" flakiness.

The fix (tests/unit/conftest.py) clears every ``PIPELINE_*`` / ``LOCAL_AGENT_*``
var at conftest's own module-import time, BEFORE test modules import
local_agent. Normal CI has no CI signal for this clearing (no vars to clear),
so silently deleting those three lines would not break CI — only the in-agent
subprocess scenario. These subprocess tests lock the clearing in by simulating
the leak directly.
"""
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_CANARY = "tests/unit/test_local_agent_off_task_and_stream_setup.py::test_local_agent_read_heavy_distinct_constants"


def _run_pytest(args, env_overrides):
    env = {
        # Strip any LOCAL_AGENT_* inherited from this process's own launchd
        # plist so the only leaked var is the one each test sets deliberately.
        k: v for k, v in __import__("os").environ.items()
        if not k.startswith("LOCAL_AGENT_") and not k.startswith("PIPELINE_")
    }
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", *args],
        check=False, cwd=str(REPO_ROOT), capture_output=True, text=True, env=env,
    )


def test_leaked_read_heavy_window_is_neutralized_by_conftest():
    """The load-bearing guard: with LOCAL_AGENT_READ_HEAVY_WINDOW leaked into
    the subprocess env (the scheduler-plist scenario), the real conftest's
    import-time clearing must drop it before local_agent imports, so the
    canary ``READ_HEAVY_WINDOW == 6`` still passes. If the conftest clearing
    is removed, this fails with ``99 != 6``."""
    r = _run_pytest([_CANARY], {"LOCAL_AGENT_READ_HEAVY_WINDOW": "99"})
    assert r.returncode == 0, (
        "conftest import-time clearing failed to neutralize a leaked "
        "LOCAL_AGENT_READ_HEAVY_WINDOW; canary output:\n" + r.stdout + r.stderr
    )


def test_leaked_park_enabled_is_neutralized_by_conftest():
    """LOCAL_AGENT_PARK_ENABLED=0 is the actual plist var observed leaking
    live (2026-07-22). The conftest must clear it too, or every
    park-expecting guard test fails on a correct codebase. The canary does
    not assert PARK_ENABLED, so run a park-expecting test and confirm it
    still passes under the leak."""
    r = _run_pytest(
        [_CANARY], {"LOCAL_AGENT_PARK_ENABLED": "0"}
    )
    assert r.returncode == 0, (
        "conftest failed to neutralize leaked LOCAL_AGENT_PARK_ENABLED;\n"
        + r.stdout + r.stderr
    )


def test_the_leak_is_real_without_a_conftest(tmp_path):
    """Control proving the regression test is meaningful: in a checkout with
    NO conftest discovering the leaked var, local_agent DOES read the leaked
    value (99, not 6). This is the disease the conftest cures — if this test
    ever passes, the leak path changed and the guard above may be vacuous."""
    la_path = REPO_ROOT / "scripts" / "local_agent.py"
    (tmp_path / "test_leak.py").write_text(textwrap.dedent(f"""\
        import importlib.util, os
        os.environ.setdefault("LOCAL_AGENT_MODEL", "test-model")
        _spec = importlib.util.spec_from_file_location(
            "local_agent", r"{la_path}")
        la = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(la)
        def test_default():
            # Leaked env (99) is read because no conftest cleared it first.
            assert la.READ_HEAVY_WINDOW == 99
    """))
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    r = _run_pytest([str(tmp_path / "test_leak.py")], {"LOCAL_AGENT_READ_HEAVY_WINDOW": "99"})
    assert r.returncode == 0, (
        "expected the leaked var to reach local_agent in a conftest-less run "
        "(proving the leak is real); got:\n" + r.stdout + r.stderr
    )