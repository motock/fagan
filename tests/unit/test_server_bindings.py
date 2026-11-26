"""pipeline.server_bindings resolves server names for pipeline.ci at call time.

pipeline.ci reads a handful of names that live on pipeline.server. Those names
are looked up through ``server_bindings.server()`` when the function runs, so a
test that monkeypatches them on pipeline.server is honoured, and pipeline.ci
still imports without importing the server.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from pipeline import ci, server_bindings
from pipeline import server as server_module

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_should_return_the_loaded_server_module():
    assert server_bindings.server() is sys.modules["pipeline.server"]


def test_should_raise_runtime_error_when_the_server_is_not_loaded(monkeypatch):
    monkeypatch.delitem(sys.modules, "pipeline.server")

    with pytest.raises(RuntimeError) as excinfo:
        server_bindings.server()

    assert type(excinfo.value) is RuntimeError
    assert str(excinfo.value) == "pipeline.server is not loaded"


def test_should_see_a_monkeypatched_server_name_at_call_time(tmp_path, monkeypatch):
    path = tmp_path / "PENDING.md"
    monkeypatch.setattr(server_module, "RETRO_PENDING_PATH", path)

    ci._record_retro_pending("patched-path-plan", 1)

    assert path.read_text().startswith("- patched-path-plan ")


def test_should_import_ci_without_importing_the_server():
    code = "import sys, pipeline.ci; print('pipeline.server' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"
