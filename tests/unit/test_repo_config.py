"""Tests for pipeline.repo_config: the .fagan.json loader/validator."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline.repo_config import (
    RepoConfigError,
    invalid_config_command,
    load_repo_config,
)

VALID = {
    "test_cmd": ["mvn", "-B", "verify"],
    "test_cwd": "backend",
    "test_globs": ["src/it/**/*.java"],
    "lint_cmd": ["./mvnw", "-B", "spotless:check"],
    "build_cmd": ["./mvnw", "-B", "package", "-DskipTests"],
}


def _write(repo: Path, payload) -> None:
    (repo / ".fagan.json").write_text(
        payload if isinstance(payload, str) else json.dumps(payload)
    )


def test_absent_config_returns_none(tmp_path):
    assert load_repo_config(tmp_path) is None


def test_valid_full_config_round_trips(tmp_path):
    (tmp_path / "backend").mkdir()
    _write(tmp_path, VALID)
    assert load_repo_config(tmp_path) == VALID


def test_minimal_empty_object_is_valid(tmp_path):
    _write(tmp_path, {})
    assert load_repo_config(tmp_path) == {}


def test_invalid_json_raises(tmp_path):
    _write(tmp_path, "{not json")
    with pytest.raises(RepoConfigError):
        load_repo_config(tmp_path)


def test_empty_file_raises(tmp_path):
    _write(tmp_path, "")
    with pytest.raises(RepoConfigError):
        load_repo_config(tmp_path)


def test_top_level_list_raises(tmp_path):
    _write(tmp_path, ["mvn", "test"])
    with pytest.raises(RepoConfigError):
        load_repo_config(tmp_path)


def test_unknown_key_raises_and_names_key(tmp_path):
    _write(tmp_path, {"bogus_key": 1})
    with pytest.raises(RepoConfigError) as exc:
        load_repo_config(tmp_path)
    assert "bogus_key" in str(exc.value)


def test_config_path_is_a_directory_raises(tmp_path):
    (tmp_path / ".fagan.json").mkdir()
    with pytest.raises(RepoConfigError):
        load_repo_config(tmp_path)


@pytest.mark.parametrize(
    "bad",
    ["mvn test", [], [""], [1]],
    ids=["string", "empty-list", "empty-string", "non-string"],
)
def test_test_cmd_must_be_non_empty_list_of_non_empty_strings(tmp_path, bad):
    _write(tmp_path, {"test_cmd": bad})
    with pytest.raises(RepoConfigError) as exc:
        load_repo_config(tmp_path)
    assert "test_cmd" in str(exc.value)


def test_lint_cmd_validated_and_names_key(tmp_path):
    _write(tmp_path, {"lint_cmd": "ruff check ."})
    with pytest.raises(RepoConfigError) as exc:
        load_repo_config(tmp_path)
    assert "lint_cmd" in str(exc.value)


def test_build_cmd_validated_and_names_key(tmp_path):
    _write(tmp_path, {"build_cmd": []})
    with pytest.raises(RepoConfigError) as exc:
        load_repo_config(tmp_path)
    assert "build_cmd" in str(exc.value)


def test_test_cwd_absolute_raises_and_names_key(tmp_path):
    _write(tmp_path, {"test_cwd": "/abs"})
    with pytest.raises(RepoConfigError) as exc:
        load_repo_config(tmp_path)
    assert "test_cwd" in str(exc.value)


def test_test_cwd_parent_traversal_raises_and_names_key(tmp_path):
    _write(tmp_path, {"test_cwd": "../x"})
    with pytest.raises(RepoConfigError) as exc:
        load_repo_config(tmp_path)
    assert "test_cwd" in str(exc.value)


def test_test_cwd_missing_directory_raises_and_names_key(tmp_path):
    _write(tmp_path, {"test_cwd": "nope"})
    with pytest.raises(RepoConfigError) as exc:
        load_repo_config(tmp_path)
    assert "test_cwd" in str(exc.value)


def test_test_cwd_must_be_non_empty_string(tmp_path):
    _write(tmp_path, {"test_cwd": ""})
    with pytest.raises(RepoConfigError) as exc:
        load_repo_config(tmp_path)
    assert "test_cwd" in str(exc.value)


def test_test_globs_must_be_a_list_of_strings(tmp_path):
    _write(tmp_path, {"test_globs": "x"})
    with pytest.raises(RepoConfigError) as exc:
        load_repo_config(tmp_path)
    assert "test_globs" in str(exc.value)


def test_test_globs_rejects_empty_string(tmp_path):
    _write(tmp_path, {"test_globs": [""]})
    with pytest.raises(RepoConfigError) as exc:
        load_repo_config(tmp_path)
    assert "test_globs" in str(exc.value)


def test_reference_documents_new_section_before_story_model_pins():
    text = (Path(__file__).resolve().parents[2] / "REFERENCE.md").read_text()
    heading = "## Test, lint and build commands"
    assert heading in text
    assert text.index(heading) < text.index("## Story model pins")
    # no other H2 sits between the new section and the pinned one
    between = text[text.index(heading) + len(heading) : text.index("## Story model pins")]
    assert "\n## " not in between
    for token in (
        "pom.xml",
        "build.gradle",
        "package.json",
        "Makefile",
        "pyproject.toml",
        "Cargo.toml",
        ".fagan.json",
        "test_cmd",
        "test_cwd",
        "test_globs",
        "lint_cmd",
        "build_cmd",
    ):
        assert token in between, token


def test_repo_config_error_is_a_value_error():
    assert issubclass(RepoConfigError, ValueError)


def test_invalid_config_command_is_shell_free_and_fails_loudly():
    err = RepoConfigError("unknown key 'bogus'")
    cmd = invalid_config_command(err)
    assert cmd[0] == sys.executable
    assert cmd[1] == "-c"
    assert isinstance(cmd[2], str)
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "fagan: invalid .fagan.json: " in result.stderr
    assert "unknown key 'bogus'" in result.stderr
