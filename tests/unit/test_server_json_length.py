"""Regression test for server.json description length.

This test intentionally fails until the implementation file `server.json`
contains a description that satisfies the MCP Registry schema's
``maxLength: 100`` constraint. The original manifest shipped a 141‑character
description, which caused registry validation to reject the file.

The test asserts that the length of the description field is at most 100
characters. Until the implementation is fixed, this assertion will raise an
``AssertionError``.
"""

import json
import subprocess
import sys
from pathlib import Path

# Load the manifest
SERVER_JSON = Path("server.json")


def test_description_length_is_within_schema_limit():
    """The description must not exceed 100 characters.

    The original manifest had a 141‑character description, which violated the
    MCP Registry schema ``maxLength: 100``. This test will fail with an
    ``AssertionError`` until the implementation is corrected.
    """
    data = json.load(SERVER_JSON.open("r", encoding="utf-8"))
    description = data.get("description", "")
    assert isinstance(description, str), "description must be a string"
    assert len(description) <= 100, (
        f"description length {len(description)} exceeds the schema's maxLength 100"
    )


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
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(module_path),
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
        f"{module_path.name} fails when pytest is invoked from {tmp_path} "
        "instead of the repo root. Repo-root files must be resolved from "
        "__file__ (REPO_ROOT = Path(__file__).resolve().parents[2]), never from "
        "the process CWD - pyproject.toml documents a past CI breakage from "
        "this exact assumption.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
