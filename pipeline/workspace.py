"""Utility functions for workspace path handling and validation.

This module provides two public functions:

* :func:`normalize_workspace_path` – performs path safety checks and returns a
  resolved absolute :class:`pathlib.Path`.
* :func:`validate_workspace` – uses :func:`normalize_workspace_path` and
  performs existence, directory, git repository, and commit checks.

The implementation follows the specification in the test suite.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

__all__ = ["normalize_workspace_path", "validate_workspace"]


def normalize_workspace_path(raw: str | None) -> Path:
    """Return a resolved absolute :class:`Path` for *raw*.

    The function enforces the following rules:

    * ``raw`` must not be ``None`` or empty/whitespace‑only.
    * The raw string must not contain a ``..`` path segment.
    * After expanding ``~`` with :func:`os.path.expanduser`, the path must be
      absolute.
    * The path is resolved with :meth:`Path.resolve` and returned.

    Raises
    ------
    ValueError
        If any of the safety checks fail.
    """
    if raw is not None and not isinstance(raw, str):
        raise ValueError("raw path must be a string or None")
    if raw is None or raw.strip() == "":
        raise ValueError("raw path must not be None or empty")

    # Reject any '..' segment in the raw input before any expansion.
    if ".." in Path(raw).parts:
        raise ValueError("path must not contain '..' segments")

    expanded = os.path.expanduser(raw)
    path_obj = Path(expanded)
    if not path_obj.is_absolute():
        raise ValueError("path must be absolute")

    return path_obj.resolve()


def validate_workspace(raw: str | None) -> dict:
    """Validate that *raw* points to an existing git repository with commits.

    The function never raises; it converts any :class:`ValueError` from
    :func:`normalize_workspace_path` into an ``ok=False`` result.

    Returns
    -------
    dict
        ``{"ok": bool, "path": str, "error": str | None}``
    """
    try:
        path = normalize_workspace_path(raw)
    except ValueError as exc:
        return {"ok": False, "path": "", "error": str(exc)}

    if not path.exists():
        return {"ok": False, "path": str(path), "error": "path does not exist"}
    if not path.is_dir():
        return {"ok": False, "path": str(path), "error": "path is not a directory"}

    # Check for a git repository.
    git_dir_result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--git-dir"],
        capture_output=True,
        check=False,
    )
    if git_dir_result.returncode != 0:
        return {"ok": False, "path": str(path), "error": "not a git repository"}

    # Check for at least one commit.
    head_result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        check=False,
    )
    if head_result.returncode != 0:
        return {"ok": False, "path": str(path), "error": "git repository has no commits"}

    return {"ok": True, "path": str(path), "error": None}

def create_workspace(raw: str | None) -> dict:
    """Create a new git workspace at *raw*.

    The function returns the same shape as :func:`validate_workspace`:
    ``{"ok": bool, "path": str, "error": str | None}``.
    """
    try:
        path = normalize_workspace_path(raw)
    except ValueError as exc:
        return {"ok": False, "path": "", "error": str(exc)}

    # If path exists
    if path.exists():
        if path.is_file():
            return {"ok": False, "path": str(path), "error": "path is not a directory"}
        # Directory exists
        if any(path.iterdir()):
            return {"ok": False, "path": str(path), "error": "path is not empty"}
        # Empty dir: proceed to init
    else:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"ok": False, "path": str(path), "error": str(exc)}

    # Run git init
    init_result = subprocess.run(
        ["git", "init"],
        cwd=str(path),
        capture_output=True,
        text=True,
        check=False,
    )
    if init_result.returncode != 0:
        return {"ok": False, "path": str(path), "error": init_result.stderr.strip()}

    # Commit empty
    commit_result = subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "Initial commit"],
        cwd=str(path),
        capture_output=True,
        text=True,
        check=False,
    )
    if commit_result.returncode != 0:
        # Try setting config
        subprocess.run(
            ["git", "config", "user.name", "pipeline"],
            cwd=str(path),
            capture_output=True,
            text=True,
            check=False,
        )
        subprocess.run(
            ["git", "config", "user.email", "pipeline@example.com"],
            cwd=str(path),
            capture_output=True,
            text=True,
            check=False,
        )
        commit_result = subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "Initial commit"],
            cwd=str(path),
            capture_output=True,
            text=True,
            check=False,
        )
        if commit_result.returncode != 0:
            return {"ok": False, "path": str(path), "error": commit_result.stderr.strip()}

    return {"ok": True, "path": str(path), "error": None}

