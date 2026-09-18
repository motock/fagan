"""Regression tests for the pipeline.ci <-> pipeline.build_detect <->
pipeline.server circular import.

Live incident (reproduced on a clean master checkout): running
``python -c "from pipeline import ci"`` as the FIRST pipeline import in a
fresh process crashed with::

    ImportError: cannot import name '_PATCHABLE_STORY_FIELDS' from
    partially initialized module 'pipeline.ci' (most likely due to a
    circular import)

The chain: ``pipeline/ci.py`` imports ``pipeline.build_detect`` at its own
module level (line 26), and ``pipeline.build_detect`` used to eagerly
``from . import server as _server`` at ITS module level to rebind
``_run_lint_gate``'s globals. That forced a cold import of
``pipeline.server`` while ``pipeline.ci`` was still half-initialized, and
``pipeline.server`` imports a long list of names back from ``pipeline.ci``
(including ``_PATCHABLE_STORY_FIELDS``) that ci.py had not defined yet.

The fix defers the ``pipeline.server`` import and the ``types.FunctionType``
rebind until ``_run_lint_gate`` is first CALLED, by which point every module
involved has finished importing.

These tests spawn fresh interpreters so the import order under test is the
real one -- an in-process test would see an already-populated
``sys.modules`` and could never reproduce the cycle.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_python(code: str) -> subprocess.CompletedProcess:
    """Run ``code`` in a fresh interpreter rooted at the repo root.

    ``python -c`` puts the current working directory on ``sys.path``, so
    ``cwd=REPO_ROOT`` is what makes ``import pipeline`` resolve to this
    worktree's package.
    """
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_import_ci_first_succeeds():
    """Importing pipeline.ci first, in a fresh process, must not crash.

    This is the exact live incident: ci.py imports build_detect at module
    level, and build_detect used to eagerly import pipeline.server, which
    imports names back from the still-partially-initialized ci.py.
    """
    result = _run_python("from pipeline import ci")
    assert result.returncode == 0, (
        "importing pipeline.ci first failed with returncode "
        f"{result.returncode}:\n{result.stderr}"
    )
    assert "ImportError" not in result.stderr, (
        "importing pipeline.ci first raised an ImportError:\n"
        f"{result.stderr}"
    )


def test_import_server_then_ci_succeeds():
    """The other import order (server first, then ci) must also succeed.

    Guards against "fixing" the cycle by simply moving the crash to the
    opposite order.
    """
    result = _run_python("from pipeline import server\nfrom pipeline import ci")
    assert result.returncode == 0, (
        "importing pipeline.server then pipeline.ci failed with returncode "
        f"{result.returncode}:\n{result.stderr}"
    )
    assert "ImportError" not in result.stderr, (
        "importing pipeline.server then pipeline.ci raised an ImportError:\n"
        f"{result.stderr}"
    )


def test_import_build_detect_alone_succeeds():
    """pipeline.build_detect must be importable on its own.

    Negative case: neither pipeline.ci nor pipeline.server has been imported
    yet, so the module-level rebind block must not drag either in.
    """
    result = _run_python("from pipeline import build_detect")
    assert result.returncode == 0, (
        "importing pipeline.build_detect alone failed with returncode "
        f"{result.returncode}:\n{result.stderr}"
    )
    assert "ImportError" not in result.stderr, (
        "importing pipeline.build_detect alone raised an ImportError:\n"
        f"{result.stderr}"
    )


def test_run_lint_gate_behavior_parity(tmp_path, monkeypatch):
    """The lazily-rebound ``_run_lint_gate`` still behaves like the original.

    Import safety alone is not enough: the wrapper must still resolve bare
    names (``detect_lint_command``, ``subprocess``) against
    ``pipeline.server``'s namespace at call time, and must return the lint
    result rather than ``None``. The second call with identical args
    exercises the memoized (already-bound) path.
    """
    from pipeline import build_detect, server

    # Simple case, mirroring the existing pattern in
    # tests/unit/test_check_story_status_lint_gate.py: no lint signal ->
    # fail open with None.
    monkeypatch.setattr(server, "detect_lint_command", lambda wt: None)
    assert build_detect._run_lint_gate(tmp_path, {}) is None
    assert build_detect._run_lint_gate(tmp_path, {}) is None

    # Non-trivial case: the rebound function must see the monkeypatched
    # server globals and return the full lint-result dict, twice.
    monkeypatch.setattr(
        server, "detect_lint_command", lambda wt: (wt, ["ruff", "check", "."]),
    )
    monkeypatch.setattr(
        server.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(
            cmd, 0, stdout="ok", stderr="",
        ),
    )
    for _ in range(2):
        result = build_detect._run_lint_gate(tmp_path, {"PATH": "/usr/bin"})
        assert result is not None
        assert result["cmd"] == ["ruff", "check", "."]
        assert result["returncode"] == 0
        assert result["stdout_tail"] == "ok"
        assert result["stderr_tail"] == ""
