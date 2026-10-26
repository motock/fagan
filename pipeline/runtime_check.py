"""Interpreter-tree integrity check.

On 2026-10-26 Homebrew installed ``python@3.14.10`` and deleted the
``3.14.9`` Cellar tree. Two long-lived processes were still running from the
deleted path: already-imported modules kept working, so they looked healthy,
while any lazily imported stdlib submodule failed with
``ModuleNotFoundError: No module named 'importlib.resources'``. The failure was
silent and per-import.

``assert_interpreter`` turns that silent, per-import failure into one loud
startup failure. Exiting is the point: launchd's ``KeepAlive`` and a supervised
MCP client both restart the process on exit, and a restart lands on the current
interpreter.

Only the filesystem path is ever reported — never an environment value, and
never the Python version (a different patch version is legitimate; a missing
tree is the defect).
"""

import sys
from pathlib import Path


def interpreter_problem() -> str | None:
    """Return a sentence naming the missing base prefix, or None if healthy.

    ``sys.base_prefix`` is read at call time (never cached at import) so the
    answer describes the process as it is now.
    """
    base_prefix = Path(sys.base_prefix)
    if not base_prefix.is_dir():
        return f"Python interpreter base prefix is missing: {base_prefix}"
    return None


def assert_interpreter() -> None:
    """Exit with status 1 if the interpreter's base prefix is gone."""
    problem = interpreter_problem()
    if problem is not None:
        print(f"pipeline.runtime_check: ERROR {problem}", file=sys.stderr)
        raise SystemExit(1)
