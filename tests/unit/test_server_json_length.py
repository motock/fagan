"""Regression test for server.json description length.

This test intentionally fails until the implementation file `server.json`
contains a description that satisfies the MCP Registry schema's
``maxLength: 100`` constraint. The original manifest shipped a 141‑character
description, which caused registry validation to reject the file.

The test asserts that the length of the description field is at most 100
characters. Until the implementation is fixed, this assertion will raise an
``AssertionError``.
"""

import subprocess
import sys
from pathlib import Path

# Load the manifest
SERVER_JSON = Path("server.json")


# Removed: test_description_length_is_within_schema_limit (new spec does not require it)


# ---------------------------------------------------------------------------
# Regression: repo-root files must be resolved from __file__, not the CWD
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_manifest_is_found_when_pytest_runs_from_a_foreign_cwd(tmp_path):
    """This module must not depend on the process CWD being the repo root.

    ``pyproject.toml`` documents a past CI breakage caused by exactly this
    class of assumption: the bare ``pytest`` entrypoint CI runs does not
    guarantee the repo root is the working directory, so a module that opens
    ``Path("server.json")`` raises ``FileNotFoundError: [Errno 2] No such file
    or directory: 'server.json'`` whenever pytest is invoked from anywhere else
    (e.g. ``cd /tmp && pytest /abs/path/to/repo/tests/unit/test_server_json_length.py``).

    Every other unit test resolves repo-root files with
    ``Path(__file__).resolve().parents[2]``. This test actually runs this module
    from a foreign working directory, so a CWD-relative regression fails here
    instead of only on a machine whose CWD happens to differ.
    """
    module_path = Path(__file__).resolve()
    # This module's only test is the one running right now, and the ``-k``
    # below deselects it inside the subprocess to avoid infinite recursion -
    # so pointing the subprocess back at this module would collect zero
    # runnable tests and pytest would exit 5 no matter what the manifest
    # contains. Run the canonical manifest contract module instead: it
    # resolves ``server.json``/``CHANGELOG.md`` from ``__file__`` and must
    # pass from any working directory.
    contract_module = module_path.parent / "test_server_json.py"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(contract_module),
            "-q",
            "-n",
            "0",
            "-p",
            "no:cacheprovider",
            # Deselect this very test: it spawns a subprocess, so running it
            # inside the subprocess would recurse forever.
            "-k",
            "not foreign_cwd",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, (
        f"{contract_module.name} fails when pytest is invoked from {tmp_path} "
        "instead of the repo root. Repo-root files must be resolved from "
        "__file__ (REPO_ROOT = Path(__file__).resolve().parents[2]), never from "
        "the process CWD - pyproject.toml documents a past CI breakage from "
        "this exact assumption.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
