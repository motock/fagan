"""Tests for .fagan.json wiring into pipeline.ecosystem_detect detection."""

import json
import subprocess
from pathlib import Path

from pipeline.ecosystem_detect import (
    detect_build_command,
    detect_lint_command,
    detect_test_command,
)


def _write_config(repo: Path, payload) -> None:
    (repo / ".fagan.json").write_text(json.dumps(payload))


def test_config_test_cmd_wins_over_pom_xml(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    _write_config(tmp_path, {"test_cmd": ["mvn", "-B", "verify"]})
    cwd, cmd = detect_test_command(tmp_path)
    assert cwd == tmp_path
    assert cmd == ["mvn", "-B", "verify"]


def test_config_test_cwd_is_honoured(tmp_path):
    (tmp_path / "backend").mkdir()
    _write_config(tmp_path, {"test_cmd": ["mvn", "-B", "verify"], "test_cwd": "backend"})
    cwd, cmd = detect_test_command(tmp_path)
    assert cwd == tmp_path / "backend"
    assert cmd == ["mvn", "-B", "verify"]


def test_config_test_cmd_gets_no_pytest_overrides(tmp_path):
    _write_config(tmp_path, {"test_cmd": ["pytest"]})
    cwd, cmd = detect_test_command(tmp_path)
    assert cwd == tmp_path
    assert cmd == ["pytest"]


def test_no_config_still_detects_pom_xml(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    cwd, cmd = detect_test_command(tmp_path)
    assert cwd == tmp_path
    assert cmd == ["mvn", "test"]


def test_invalid_config_test_command_fails_loudly(tmp_path):
    _write_config(tmp_path, {"bogus_key": 1})
    cwd, cmd = detect_test_command(tmp_path)
    assert cwd == tmp_path
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "invalid .fagan.json" in result.stderr


def test_config_lint_cmd_is_honoured(tmp_path):
    _write_config(tmp_path, {"lint_cmd": ["./mvnw", "-B", "spotless:check"]})
    cwd, cmd = detect_lint_command(tmp_path)
    assert cwd == tmp_path
    assert cmd == ["./mvnw", "-B", "spotless:check"]


def test_invalid_config_lint_command_fails_loudly(tmp_path):
    _write_config(tmp_path, {"bogus_key": 1})
    cwd, cmd = detect_lint_command(tmp_path)
    assert cwd == tmp_path
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "invalid .fagan.json" in result.stderr


def test_config_build_cmd_is_honoured(tmp_path):
    _write_config(tmp_path, {"build_cmd": ["./mvnw", "-B", "package", "-DskipTests"]})
    cwd, cmd = detect_build_command(tmp_path)
    assert cwd == tmp_path
    assert cmd == ["./mvnw", "-B", "package", "-DskipTests"]


def test_invalid_config_build_command_fails_loudly(tmp_path):
    _write_config(tmp_path, {"bogus_key": 1})
    cwd, cmd = detect_build_command(tmp_path)
    assert cwd == tmp_path
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "invalid .fagan.json" in result.stderr
