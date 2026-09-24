"""pipeline.story_status and pipeline.ci must import cold, and still bind to the server.

Both modules used to run ``from pipeline import server as _server`` at module level
so they could rebind functions onto the server's namespace (``types.FunctionType``
with ``_server.__dict__`` as the globals). pipeline.server imports both, so a cold
``import pipeline.story_status`` re-entered a half-initialized module and died with an
ImportError. Each module now exposes ``bind_to_server(server)`` and pipeline.server calls
it right after importing them. tests/unit/conftest.py pre-imports pipeline.server, which
hides the failure in-process, so every probe here runs in a subprocess.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

SERVER_EXPORTS_FROM_STORY_STATUS = [
    "_detached_grade_lifecycle",
    "DETACHED_GRADE_WATCHDOG_SECONDS",
    "DISPATCH_STALE_ACTIVITY_SECONDS",
    "collect_story_wedge_signals",
    "_baseline_exempted_failures",
    "_record_test_check",
    "_clear_failure_streaks",
    "start_detached_grade",
    "collect_detached_grade",
    "_untrack_scratchpad",
    "_plan_conflict_intercept",
]

ALL_PIPELINE_MODULES = sorted(p.stem for p in (REPO_ROOT / "pipeline").glob("*.py") if p.stem != "__init__")


def _probe(code):
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )


@pytest.mark.parametrize("module", ["story_status", "ci"])
def test_satellite_imports_cold_without_loading_the_server(module):
    result = _probe(f"import sys, pipeline.{module}; print('pipeline.server' in sys.modules)")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


@pytest.mark.parametrize("module", ALL_PIPELINE_MODULES)
def test_every_pipeline_module_imports_cold_in_a_fresh_interpreter(module):
    result = _probe(f"import pipeline.{module}")
    assert result.returncode == 0, result.stderr


_BOUND_TO_SERVER = (
    "fn = p.check_story_status; "
    "print(fn is s.check_story_status, fn.__globals__ is p.__dict__, "
    "p.mcp._tool_manager._tools['check_story_status'].fn is fn); "
    "print(p._mark_story_done_impl is c._mark_story_done_impl, p._mark_story_done_impl.__globals__ is p.__dict__); "
    "print(p._record_retro_pending is c._record_retro_pending, p._record_retro_pending.__globals__ is p.__dict__)"
)
_EXPECTED_BOUND = "True True True\nTrue True\nTrue True"


def test_server_first_binds_the_satellite_functions_onto_the_server():
    result = _probe("import pipeline.server as p, pipeline.story_status as s, pipeline.ci as c; " + _BOUND_TO_SERVER)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == _EXPECTED_BOUND


def test_satellites_first_then_server_binds_the_satellite_functions_onto_the_server():
    result = _probe("import pipeline.story_status as s, pipeline.ci as c, pipeline.server as p; " + _BOUND_TO_SERVER)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == _EXPECTED_BOUND


@pytest.mark.parametrize("name", SERVER_EXPORTS_FROM_STORY_STATUS)
def test_story_status_exports_its_collaborators_onto_the_server_namespace(name):
    result = _probe(f"import pipeline.story_status, pipeline.server as p; print(hasattr(p, {name!r}))")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"
