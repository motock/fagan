"""PIPELINE_PYTHON interpreter selection for scripts/dashboard.sh (dst-01).

The script used to pick its interpreter only from `.venv` or whatever
`python3` is on PATH, so a git worktree with no `.venv` could run the
dashboard lifecycle tests with an interpreter that lacked fastapi/uvicorn.
An explicit PIPELINE_PYTHON now wins, and an unusable one is refused.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from tests.unit.test_dashboard_script import (  # noqa: F401  (env: pytest fixture)
    REPO_ROOT,
    _run_script,
    env,
)

_REFUSAL = "ERROR: PIPELINE_PYTHON is set but is not an executable python"


def _write_recording_python(tmp_path: Path, marker: Path) -> Path:
    """A shell shim that records each invocation, then runs the real python."""
    shim = tmp_path / "recording-python"
    shim.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{marker}"\n'
        f'exec "{sys.executable}" "$@"\n'
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    return shim


def test_should_use_pipeline_python_when_set(env, tmp_path):
    env_, _port = env
    marker = tmp_path / "invocations.log"
    shim = _write_recording_python(tmp_path, marker)
    env_["PIPELINE_PYTHON"] = str(shim)

    res = _run_script("start", env=env_)

    assert res.returncode == 0, res.stderr
    assert "import fastapi, uvicorn" in marker.read_text()


def test_should_refuse_an_unusable_pipeline_python(env):
    env_, port = env
    env_["PIPELINE_PYTHON"] = str(REPO_ROOT / "no-such-python-dst-01")

    res = _run_script("start", env=env_)

    assert res.returncode == 1
    assert res.stderr.strip() == _REFUSAL
    assert not (REPO_ROOT / f".dashboard.{port}.pid").exists()


def test_should_refuse_a_non_executable_pipeline_python(env, tmp_path):
    env_, port = env
    not_exec = tmp_path / "python-not-executable"
    not_exec.write_text("#!/bin/sh\n")
    not_exec.chmod(0o644)
    env_["PIPELINE_PYTHON"] = str(not_exec)

    res = _run_script("start", env=env_)

    assert res.returncode == 1
    assert res.stderr.strip() == _REFUSAL
    assert not (REPO_ROOT / f".dashboard.{port}.pid").exists()


def test_should_ignore_an_empty_pipeline_python(env):
    env_, _port = env
    env_["PIPELINE_PYTHON"] = ""

    res = _run_script("start", env=env_)

    assert "PIPELINE_PYTHON" not in res.stderr
    assert "PIPELINE_PYTHON" not in res.stdout


def test_should_accept_a_pipeline_python_name_found_on_path(env, tmp_path):
    env_, _port = env
    marker = tmp_path / "invocations.log"
    shim = _write_recording_python(tmp_path, marker)
    shim_on_path = tmp_path / "dst01-recording-python"
    shim_on_path.write_bytes(shim.read_bytes())
    shim_on_path.chmod(shim.stat().st_mode)
    env_["PATH"] = f"{tmp_path}{os.pathsep}{env_['PATH']}"
    env_["PIPELINE_PYTHON"] = shim_on_path.name

    res = _run_script("start", env=env_)

    assert res.returncode == 0, res.stderr
    assert "import fastapi, uvicorn" in marker.read_text()
