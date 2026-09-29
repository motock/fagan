"""Loader/validator for a repo's optional root ``.fagan.json`` config.

A target repo may declare how fagan tests, lints and builds it by placing a
``.fagan.json`` file at its root. When present it wins over marker detection.
"""

import json
import sys
from pathlib import Path

_ALLOWED_KEYS = {"test_cmd", "test_cwd", "test_globs", "lint_cmd", "build_cmd"}
_COMMAND_KEYS = ("test_cmd", "lint_cmd", "build_cmd")


class RepoConfigError(ValueError):
    """Raised when a repo's ``.fagan.json`` is missing, malformed or invalid."""


def load_repo_config(repo_root: Path) -> dict | None:
    """Load and validate ``<repo_root>/.fagan.json``.

    Returns ``None`` when the file does not exist. Otherwise parses and
    validates it, raising :class:`RepoConfigError` with a message naming the
    offending key. Never falls back silently.
    """
    path = Path(repo_root) / ".fagan.json"
    if not path.exists():
        return None
    if not path.is_file():
        raise RepoConfigError(f".fagan.json is not a regular file: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RepoConfigError(f"invalid JSON in .fagan.json: {error}") from error
    if not isinstance(data, dict):
        raise RepoConfigError(
            f".fagan.json must contain a JSON object, got {type(data).__name__}"
        )
    for key in data:
        if key not in _ALLOWED_KEYS:
            raise RepoConfigError(f"unknown key in .fagan.json: {key!r}")
    for key in _COMMAND_KEYS:
        if key in data:
            _validate_command_key(key, data[key])
    if "test_cwd" in data:
        _validate_test_cwd(Path(repo_root), data["test_cwd"])
    if "test_globs" in data:
        value = data["test_globs"]
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item for item in value
        ):
            raise RepoConfigError(
                "test_globs in .fagan.json must be a list of non-empty strings"
            )
    return data


def _validate_command_key(key: str, value) -> None:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise RepoConfigError(
            f"{key} in .fagan.json must be a non-empty list of non-empty strings"
        )


def _validate_test_cwd(repo_root: Path, value) -> None:
    if not isinstance(value, str) or not value:
        raise RepoConfigError("test_cwd in .fagan.json must be a non-empty string")
    relative = Path(value)
    if relative.is_absolute():
        raise RepoConfigError(f"test_cwd in .fagan.json must be relative: {value!r}")
    if ".." in relative.parts:
        raise RepoConfigError(
            f"test_cwd in .fagan.json must not contain '..': {value!r}"
        )
    root = Path(repo_root).resolve()
    resolved = (root / relative).resolve()
    if not resolved.is_dir() or not resolved.is_relative_to(root):
        raise RepoConfigError(
            f"test_cwd in .fagan.json must be an existing directory inside the repo: {value!r}"
        )


def invalid_config_command(error: RepoConfigError) -> list[str]:
    """Return a shell-free command that exits non-zero printing the reason.

    Used so a gate goes visibly red instead of crashing the scheduler when a
    repo's ``.fagan.json`` is invalid.
    """
    message = "fagan: invalid .fagan.json: " + str(error)
    return [sys.executable, "-c", f"import sys; sys.exit({message!r})"]