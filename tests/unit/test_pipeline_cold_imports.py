"""pipeline modules must import cold, in a fresh interpreter, without help.

pipeline.build_detect used to import pipeline.server eagerly at module level. That
edge closed a cycle (advance -> dispatch -> build_detect -> server -> advance), so
seven modules could only be imported after pipeline.server had already been
imported, and three workarounds grew around it: the end-of-module build_detect
import in pipeline.dispatch, the _ColdOracleGateImport meta_path hook in
pipeline.companion_server, and an "import pipeline.server first" convention in tests.

tests/unit/conftest.py pre-imports pipeline.server, which hides every one of these
failures in-process, so each probe here runs in a subprocess.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _probe(code):
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )


@pytest.mark.parametrize(
    "module",
    [
        "advance",
        "detached_grade",
        "ingest",
        "oracle_gate",
        "plan_conflict_ruling",
        "repo_health",
        "triage",
    ],
)
def test_module_imports_cold_in_a_fresh_interpreter(module):
    result = _probe(f"import pipeline.{module}")
    assert result.returncode == 0, result.stderr


def test_importing_build_detect_does_not_load_the_server():
    result = _probe("import sys, pipeline.build_detect; print('pipeline.server' in sys.modules)")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_build_detect_still_exposes_a_callable_lint_gate():
    result = _probe("import pipeline.build_detect as b; print(callable(b._run_lint_gate))")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


@pytest.mark.parametrize(
    "code",
    [
        "import pipeline.scheduler_daemon",
        "import app.pipeline_mcp_server",
        "import pipeline.server, pipeline.advance, pipeline.triage, pipeline.story_status, pipeline.review_orchestrator",
    ],
    ids=["scheduler_daemon", "mcp_server", "server_first_then_everything"],
)
def test_entry_points_still_import(code):
    result = _probe(code)
    assert result.returncode == 0, result.stderr


def test_companion_installs_no_import_hook():
    result = _probe(
        "import sys, pipeline.companion_server as cs; "
        "print(hasattr(cs, '_ColdOracleGateImport'), "
        "any(type(f).__name__ == '_ColdOracleGateImport' for f in sys.meta_path))"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False False"


def test_dispatch_imports_build_detect_at_the_top_of_the_module():
    tree = ast.parse((REPO_ROOT / "pipeline" / "dispatch.py").read_text())
    index_of_build_detect_import = next(
        i
        for i, node in enumerate(tree.body)
        if isinstance(node, ast.ImportFrom) and node.module == "build_detect" and node.level == 1
    )
    index_of_first_definition = next(
        i for i, node in enumerate(tree.body) if isinstance(node, (ast.FunctionDef, ast.ClassDef))
    )
    assert index_of_build_detect_import < index_of_first_definition
