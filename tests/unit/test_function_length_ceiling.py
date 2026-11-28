"""Function length ceiling: no function in pipeline/, app/ or scripts/ may exceed 150 lines."""

import ast
from pathlib import Path

import pytest

MAX_LINES = 150
ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("pipeline", "app", "scripts")

# Measured function lengths (path::qualified_name -> lines) of functions already
# over the ceiling. Remove an entry when its function is split; never add one.
BASELINE: dict[str, int] = {
    "pipeline/advance.py::_advance_pipeline_locked_impl": 199,
    "pipeline/config_provenance.py::resolve_role_provenance": 153,
    "pipeline/dispatch_prompt_build.py::build_prompt": 154,
    "pipeline/guard_liveness.py::check_guard_liveness": 157,
    "pipeline/ingest.py::_ingest_plan_impl": 193,
    "pipeline/local_success.py::classify_story": 155,
    "pipeline/plan_approval.py::PlanApprovalMixin._decide_plan": 156,
    "pipeline/review_orchestrator.py::review_story": 198,
    "pipeline/story_status.py::check_story_status": 179,
    "pipeline/watchers.py::scan_all_plans": 163,
    "scripts/smoke_getting_started.py::_announce_dispatch_backend": 231,
    "scripts/smoke_getting_started.py::run_smoke": 166,
}


def measure_functions(source: str, filename: str = "<string>") -> dict[str, int]:
    """Return {qualified_name: line count} for every function in ``source``.

    Raises SyntaxError on unparseable input.
    """
    lengths: dict[str, int] = {}

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = f"{prefix}{child.name}"
                assert child.end_lineno is not None
                lengths[name] = child.end_lineno - child.lineno + 1
                walk(child, f"{name}.<locals>.")
            else:
                walk(child, prefix)

    walk(ast.parse(source, filename=filename), "")
    return lengths


def _measure_repo() -> dict[str, int]:
    found: dict[str, int] = {}
    for d in SCAN_DIRS:
        for path in sorted((ROOT / d).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            for name, n in measure_functions(path.read_text(), rel).items():
                found[f"{rel}::{name}"] = n
    return found


def _func(n_lines: int) -> str:
    return "def f():\n" + "    x = 1\n" * (n_lines - 1)


def test_helper_reports_151_line_function():
    assert measure_functions(_func(151)) == {"f": 151}


def test_helper_150_line_function_is_not_over_ceiling():
    assert measure_functions(_func(150))["f"] <= MAX_LINES


def test_helper_measures_nested_function_on_its_own():
    src = "def outer():\n    def inner():\n        return 1\n    return inner\n"
    assert measure_functions(src) == {"outer": 4, "outer.<locals>.inner": 2}


def test_helper_raises_on_syntax_error():
    with pytest.raises(SyntaxError):
        measure_functions("def broken(:\n")


def test_no_new_function_over_ceiling():
    offenders = {
        k: n for k, n in _measure_repo().items() if n > MAX_LINES and k not in BASELINE
    }
    assert not offenders, f"functions over {MAX_LINES} lines, split them: {offenders}"


def test_baseline_has_no_stale_or_grown_entries():
    found = _measure_repo()
    problems = []
    for key, ceiling in BASELINE.items():
        n = found.get(key)
        if n is None:
            problems.append(f"{key}: function is gone, remove the entry")
        elif n <= MAX_LINES:
            problems.append(f"{key}: now {n} lines, remove the entry")
        elif n > ceiling:
            problems.append(f"{key}: grew to {n}, ceiling {ceiling}")
        elif n < ceiling:
            problems.append(f"{key}: shrank to {n}, lower the entry from {ceiling}")
    assert not problems, "\n".join(problems)
