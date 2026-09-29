"""LAG-5: no repo with source code may get a silently-passing no-op test gate.

``detect_test_command`` used to fall through to ``[sys.executable, "-c",
"pass"]`` for any repo whose markers it did not recognise, so a Go, .NET,
Ruby, PHP, Swift or Elixir repo passed every gate with nothing run. This story
adds ``go.mod`` -> ``["go", "test", "./..."]`` and ``*.sln`` / ``*.csproj`` ->
``["dotnet", "test"]``, plus ``UNSUPPORTED_MARKERS``,
``unsupported_ecosystem_command`` and ``is_unsupported_ecosystem_command`` so
the remaining ecosystems fail loudly instead of passing silently.

Every test builds its own synthetic tree under ``tmp_path``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from pipeline import ecosystem_detect as ed
from pipeline.repo_config import RepoConfigError, invalid_config_command

_NOOP = [sys.executable, "-c", "pass"]
_UNSUPPORTED_MARKERS = ("Gemfile", "composer.json", "Package.swift", "mix.exs")
_REFERENCE = Path(__file__).resolve().parents[2] / "REFERENCE.md"


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=120, check=False
    )


def _detect(cwd: Path) -> tuple[Path, list[str]]:
    result = ed.detect_test_command(cwd)
    assert isinstance(result, tuple) and len(result) == 2, f"bad contract: {result!r}"
    cwd_out, cmd = result
    assert isinstance(cwd_out, Path), f"first element must be a Path: {cwd_out!r}"
    assert isinstance(cmd, list), f"second element must be a list: {cmd!r}"
    return cwd_out, cmd


# --- new recognised markers: go.mod, *.sln, *.csproj ------------------------ #
def test_go_mod_returns_go_test(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/x\n")
    cwd_out, cmd = _detect(tmp_path)
    assert cwd_out == tmp_path
    assert cmd == ["go", "test", "./..."]


def test_sln_returns_dotnet_test(tmp_path: Path) -> None:
    (tmp_path / "Foo.sln").write_text("Microsoft Visual Studio Solution File\n")
    cwd_out, cmd = _detect(tmp_path)
    assert cwd_out == tmp_path
    assert cmd == ["dotnet", "test"]


def test_csproj_returns_dotnet_test(tmp_path: Path) -> None:
    (tmp_path / "Foo.csproj").write_text("<Project></Project>\n")
    cwd_out, cmd = _detect(tmp_path)
    assert cwd_out == tmp_path
    assert cmd == ["dotnet", "test"]


def test_go_mod_in_immediate_subdirectory_is_found(tmp_path: Path) -> None:
    sub = tmp_path / "svc"
    sub.mkdir()
    (sub / "go.mod").write_text("module example.com/svc\n")
    cwd_out, cmd = _detect(tmp_path)
    assert cwd_out == sub
    assert cmd == ["go", "test", "./..."]


# --- existing precedence is unchanged --------------------------------------- #
def test_pom_xml_still_wins_over_go_mod(tmp_path: Path) -> None:
    (tmp_path / "pom.xml").write_text("<project/>\n")
    (tmp_path / "go.mod").write_text("module example.com/x\n")
    cwd_out, cmd = _detect(tmp_path)
    assert cwd_out == tmp_path
    assert cmd == ["mvn", "test"]


# --- unsupported ecosystems fail loudly instead of silently passing --------- #
def test_unsupported_markers_constant_covers_the_named_ecosystems() -> None:
    for marker in _UNSUPPORTED_MARKERS:
        assert marker in ed.UNSUPPORTED_MARKERS, f"{marker} missing from UNSUPPORTED_MARKERS"


@pytest.mark.parametrize("marker", _UNSUPPORTED_MARKERS)
def test_unsupported_ecosystem_command_exits_nonzero_naming_marker(
    marker: str, tmp_path: Path
) -> None:
    cmd = ed.unsupported_ecosystem_command(marker)
    assert isinstance(cmd, list) and cmd, f"bad command: {cmd!r}"
    assert cmd[0] == sys.executable
    assert "-c" in cmd
    result = _run(cmd, tmp_path)
    assert result.returncode != 0, f"command for {marker} exited 0: {cmd!r}"
    combined = result.stdout + result.stderr
    assert marker in combined, f"stderr does not name {marker!r}: {combined!r}"
    assert ".fagan.json" in combined, f"stderr omits .fagan.json: {combined!r}"
    assert "unsupported" in combined.lower(), f"stderr omits 'unsupported': {combined!r}"


@pytest.mark.parametrize("marker", _UNSUPPORTED_MARKERS)
def test_detect_returns_failing_command_for_unsupported_marker(
    marker: str, tmp_path: Path
) -> None:
    (tmp_path / marker).write_text("x\n")
    cwd_out, cmd = _detect(tmp_path)
    assert cwd_out == tmp_path
    assert cmd != _NOOP, f"{marker} repo still gets the silent no-op"
    assert cmd == ed.unsupported_ecosystem_command(marker)
    result = _run(cmd, tmp_path)
    assert result.returncode != 0, f"{marker} repo command exited 0: {cmd!r}"
    combined = result.stdout + result.stderr
    assert marker in combined
    assert ".fagan.json" in combined


def test_unsupported_marker_in_immediate_subdirectory_is_found(tmp_path: Path) -> None:
    sub = tmp_path / "ruby_app"
    sub.mkdir()
    (sub / "Gemfile").write_text("source 'https://rubygems.org'\n")
    cwd_out, cmd = _detect(tmp_path)
    assert cwd_out == sub
    result = _run(cmd, sub)
    assert result.returncode != 0
    assert "Gemfile" in (result.stdout + result.stderr)


# --- .fagan.json test_cmd still wins over everything ------------------------ #
def test_declared_test_cmd_wins_over_unsupported_marker(tmp_path: Path) -> None:
    (tmp_path / "Gemfile").write_text("source 'https://rubygems.org'\n")
    (tmp_path / ".fagan.json").write_text('{"test_cmd": ["bundle", "exec", "rspec"]}\n')
    cwd_out, cmd = _detect(tmp_path)
    assert cwd_out == tmp_path
    assert cmd == ["bundle", "exec", "rspec"]


def test_declared_test_cmd_wins_over_go_mod(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/x\n")
    (tmp_path / ".fagan.json").write_text('{"test_cmd": ["make", "check"]}\n')
    cwd_out, cmd = _detect(tmp_path)
    assert cwd_out == tmp_path
    assert cmd == ["make", "check"]


# --- the no-op survives only for a repo with no marker at all --------------- #
def test_readme_only_repo_keeps_the_noop(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("hello\n")
    cwd_out, cmd = _detect(tmp_path)
    assert cwd_out == tmp_path
    assert cmd == _NOOP


def test_empty_directory_keeps_the_noop(tmp_path: Path) -> None:
    cwd_out, cmd = _detect(tmp_path)
    assert cwd_out == tmp_path
    assert cmd == _NOOP


# --- is_unsupported_ecosystem_command --------------------------------------- #
def test_is_unsupported_true_for_unsupported_ecosystem_command() -> None:
    for marker in _UNSUPPORTED_MARKERS:
        assert ed.is_unsupported_ecosystem_command(
            ed.unsupported_ecosystem_command(marker)
        ), f"not recognised for {marker}"


def test_is_unsupported_true_for_invalid_config_command() -> None:
    cmd = invalid_config_command(RepoConfigError("bad key"))
    assert ed.is_unsupported_ecosystem_command(cmd)


def test_is_unsupported_false_for_noop_and_real_commands() -> None:
    assert not ed.is_unsupported_ecosystem_command(_NOOP)
    assert not ed.is_unsupported_ecosystem_command(["go", "test", "./..."])
    assert not ed.is_unsupported_ecosystem_command(["dotnet", "test"])
    assert not ed.is_unsupported_ecosystem_command(["mvn", "test"])
    assert not ed.is_unsupported_ecosystem_command([])


# --- REFERENCE.md documents the new order and the failure mode -------------- #
def _reference_section(heading: str) -> str:
    text = _REFERENCE.read_text()
    start = text.index(heading)
    rest = text[start + len(heading):]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def test_reference_documents_go_and_dotnet_detection() -> None:
    section = _reference_section("## Test, lint and build commands")
    assert "go.mod" in section, "REFERENCE.md detection order omits go.mod"
    assert ".sln" in section, "REFERENCE.md detection order omits *.sln"
    assert ".csproj" in section, "REFERENCE.md detection order omits *.csproj"


def test_reference_documents_unsupported_ecosystem_failure() -> None:
    section = _reference_section("## Test, lint and build commands")
    lowered = section.lower()
    assert "unsupported" in lowered, "REFERENCE.md omits the unsupported-ecosystem failure"
    assert "ingest" in lowered, "REFERENCE.md omits the ingest rejection"
    assert any(marker in section for marker in _UNSUPPORTED_MARKERS), (
        "REFERENCE.md does not name any unsupported marker"
    )
