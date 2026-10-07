"""matrix.py must import when run as a script, not only under pytest.

A module-level `from app.role_registry import load_registry` in
tests/benchmark/matrix.py (added by a8b268b) runs before argparse. Invoked as
a script -- the documented `python matrix.py` from tests/benchmark, and what
the published-run script does -- Python puts only the script's own directory
on sys.path, so `app` is unimportable and the process dies with
ModuleNotFoundError before printing anything. The pytest-driven checks never
saw it because pytest puts the repo root on sys.path; harness.py already
applies the sys.path guard matrix.py was missing.

Every check drives matrix.py as a real child process with the repo root
scrubbed out of the child's PYTHONPATH, so before the fix these fail with the
actual ModuleNotFoundError rather than with an import error inside the test
itself (an in-process `from tests.benchmark import matrix` cannot work here:
matrix.py imports its sibling `scorecard` module, which is only importable
when tests/benchmark itself is on sys.path).
"""
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BENCH = REPO / "tests" / "benchmark"
MATRIX_PY = BENCH / "matrix.py"

# Run with `python -c <this> <matrix.py> <repo root>`: imports matrix.py by
# path twice under two distinct module names (so the module body executes
# twice in one interpreter) and prints how many sys.path entries resolve to
# the repo root. An unconditional sys.path.insert shows up as a duplicate.
_DOUBLE_IMPORT = """
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]).resolve().parent))
for name in ("matrix_load_one", "matrix_load_two"):
    spec = importlib.util.spec_from_file_location(name, sys.argv[1])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
print(sum(1 for entry in sys.path
         if entry and Path(entry).resolve() == Path(sys.argv[2]).resolve()))
"""


def _child_env(pythonpath=None):
    """os.environ with PYTHONPATH dropped, optionally set to something else."""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    if pythonpath is not None:
        env["PYTHONPATH"] = str(pythonpath)
    return env


def _run_matrix(args, cwd, pythonpath=None):
    return subprocess.run(
        [sys.executable, str(MATRIX_PY), *args],
        cwd=str(cwd), env=_child_env(pythonpath),
        capture_output=True, text=True, timeout=120, check=False,
    )


def _show(proc):
    return f"exit={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"


def test_help_runs_from_another_directory_without_pythonpath(tmp_path):
    """The reported breakage: bare `python matrix.py --help` from anywhere."""
    proc = _run_matrix(["--help"], cwd=tmp_path)
    assert proc.returncode == 0, _show(proc)
    assert "usage" in proc.stdout, _show(proc)
    assert "No module named" not in (proc.stdout + proc.stderr), _show(proc)
    # main()'s documented CLI surface must survive the edit (membership only:
    # later stories may add flags).
    for flag in ("--tasks", "--models", "--trials", "--resume"):
        assert flag in proc.stdout, f"{flag} vanished from --help\n" + _show(proc)


def test_help_runs_from_the_documented_benchmark_directory():
    """`python matrix.py` from inside tests/benchmark, the documented call."""
    proc = _run_matrix(["--help"], cwd=BENCH)
    assert proc.returncode == 0, _show(proc)
    assert "usage" in proc.stdout, _show(proc)


def test_help_does_not_depend_on_the_caller_pythonpath(tmp_path):
    """An unrelated PYTHONPATH must neither rescue nor break the guard."""
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    proc = _run_matrix(["--help"], cwd=tmp_path, pythonpath=unrelated)
    assert proc.returncode == 0, _show(proc)
    assert "usage" in proc.stdout, _show(proc)


def test_help_runs_with_an_empty_pythonpath(tmp_path):
    """Boundary: `PYTHONPATH= python matrix.py --help` (empty, not unset)."""
    proc = _run_matrix(["--help"], cwd=tmp_path, pythonpath="")
    assert proc.returncode == 0, _show(proc)
    assert "usage" in proc.stdout, _show(proc)


def test_unknown_flag_is_an_argparse_error_not_an_import_error(tmp_path):
    """`--bogus` must reach argparse (exit 2), not die importing `app`."""
    proc = _run_matrix(["--bogus"], cwd=tmp_path)
    assert proc.returncode == 2, _show(proc)
    assert "No module named" not in (proc.stdout + proc.stderr), _show(proc)
    assert "usage" in proc.stderr, _show(proc)


def test_reimport_leaves_exactly_one_repo_root_on_sys_path(tmp_path):
    """Importing the module twice must not duplicate the inserted path."""
    proc = subprocess.run(
        [sys.executable, "-c", _DOUBLE_IMPORT, str(MATRIX_PY), str(REPO)],
        cwd=str(tmp_path), env=_child_env(),
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert proc.returncode == 0, _show(proc)
    assert proc.stdout.strip() == "1", (
        "expected exactly one sys.path entry resolving to the repo root after "
        "importing matrix.py twice in one interpreter\n" + _show(proc)
    )


def test_matrix_passes_the_repo_lint_gate():
    """The guard must not need a `# noqa` marker to pass ruff.

    E402 (module level import not at top of file) is not enabled in this
    repo's ruff config, so a noqa comment on the guard would be an unused
    suppression: RUF100 flags it and `ruff check .` fails the CI lint gate.
    This runs the pinned ruff from the repo venv against matrix.py itself,
    so it fails both if the guard is added with a stray noqa and if the edit
    introduces any other lint error in that file.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--no-cache", str(MATRIX_PY)],
        cwd=str(REPO), capture_output=True, text=True, timeout=120,
        check=False,
    )
    assert proc.returncode == 0, (
        "ruff (pinned 0.16.9) rejects tests/benchmark/matrix.py - the sys.path "
        "guard must not carry a `# noqa` marker (RUF100 flags unused noqa)\n"
        + _show(proc)
    )