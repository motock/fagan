"""TDD suite for scripts/install_global_rules.py (GR-5).

Written BEFORE the implementation: this file is expected to fail at import
time (ModuleNotFoundError) until ``scripts/install_global_rules.py`` exists.
It grades only the CLI surface this story adds - the engine
(``pipeline.global_rules_install``) is exercised through it, never re-tested.
"""

from __future__ import annotations

import inspect
import os
import re
from pathlib import Path

import pytest

from pipeline import global_rules_targets
from scripts import install_global_rules as cli

LINE_RE = re.compile(
    r"^(claude|codex|opencode): .+ \((created|updated|unchanged|would-create|would-update)\)$"
)


def _make_source(root: Path) -> Path:
    """A minimal source root the engine can render from."""
    (root / "global-rules").mkdir(parents=True, exist_ok=True)
    (root / "global-rules" / "standards.md").write_text("STANDARDS-BODY\n", encoding="utf-8")
    (root / "global-rules" / "pipeline-workflow.md").write_text("WORKFLOW-BODY\n", encoding="utf-8")
    rules = root / ".claude" / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    (rules / "code-review.md").write_text("RULES-BODY\n", encoding="utf-8")
    return root


def _run(argv: list[str]) -> int:
    """Call main, normalising argparse's SystemExit(2) into a return code."""
    try:
        return cli.main(argv)
    except SystemExit as exc:
        if exc.code is None:
            return 0
        return exc.code if isinstance(exc.code, int) else 1


def _target(tool: str, home: Path) -> Path:
    return global_rules_targets.instructions_path(tool, {"HOME": str(home)})


def _snapshot(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def _assert_line(out: str, tool: str, target: Path, status: str) -> None:
    lines = [line for line in out.splitlines() if line.startswith(f"{tool}: ")]
    assert len(lines) == 1, out
    match = re.match(rf"^{tool}: (.+) \((created|updated|unchanged|would-create|would-update)\)$", lines[0])
    assert match, lines[0]
    assert Path(match.group(1)).resolve() == target.resolve(), lines[0]
    assert match.group(2) == status, lines[0]


# --- module shape / executable bit -----------------------------------------

def test_module_shape_and_executable_bit() -> None:
    script = Path(cli.__file__).resolve()
    assert script.name == "install_global_rules.py"
    assert script.parent.name == "scripts"
    # install.sh invokes it directly, and ruff's EXE001 flags a shebang
    # without the executable bit.
    assert script.read_bytes().startswith(b"#!")
    assert os.stat(script).st_mode & 0o111

    public = {
        name
        for name, obj in vars(cli).items()
        if not name.startswith("_") and inspect.isfunction(obj) and obj.__module__ == cli.__name__
    }
    assert public == {"main"}
    params = inspect.signature(cli.main).parameters
    assert "argv" in params and params["argv"].default is None


# --- positive paths ---------------------------------------------------------

def test_one_tool_creates_target_and_reports_created(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    src = _make_source(tmp_path / "src")
    assert _run(["--tools", "claude", "--source-root", str(src)]) == 0
    target = _target("claude", tmp_path)
    assert target.is_file()
    assert "STANDARDS-BODY" in target.read_text(encoding="utf-8")
    _assert_line(capsys.readouterr().out, "claude", target, "created")


def test_all_three_tools_each_get_a_line_and_a_target(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    src = _make_source(tmp_path / "src")
    assert _run(["--tools", "claude,codex,opencode", "--source-root", str(src)]) == 0
    lines = capsys.readouterr().out.splitlines()
    for tool in ("claude", "codex", "opencode"):
        assert any(line.startswith(f"{tool}: ") for line in lines), lines
        assert _target(tool, tmp_path).is_file()
    assert all(LINE_RE.match(line) for line in lines if line.strip()), lines


def test_second_run_reports_unchanged(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    src = _make_source(tmp_path / "src")
    argv = ["--tools", "claude", "--source-root", str(src)]
    assert _run(argv) == 0
    capsys.readouterr()
    assert _run(argv) == 0
    _assert_line(capsys.readouterr().out, "claude", _target("claude", tmp_path), "unchanged")


def test_existing_target_reports_updated(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    src = _make_source(tmp_path / "src")
    target = _target("claude", tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# pre-existing\n", encoding="utf-8")
    assert _run(["--tools", "claude", "--source-root", str(src)]) == 0
    _assert_line(capsys.readouterr().out, "claude", target, "updated")
    text = target.read_text(encoding="utf-8")
    assert "# pre-existing" in text and "STANDARDS-BODY" in text


def test_dry_run_reports_would_create_and_writes_nothing(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    src = _make_source(tmp_path / "src")
    before = _snapshot(tmp_path)
    assert _run(["--tools", "claude", "--dry-run", "--source-root", str(src)]) == 0
    _assert_line(capsys.readouterr().out, "claude", _target("claude", tmp_path), "would-create")
    assert _snapshot(tmp_path) == before


def test_dry_run_reports_would_update_and_leaves_file_alone(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    src = _make_source(tmp_path / "src")
    target = _target("claude", tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# pre-existing\n", encoding="utf-8")
    assert _run(["--tools", "claude", "--dry-run", "--source-root", str(src)]) == 0
    _assert_line(capsys.readouterr().out, "claude", target, "would-update")
    assert target.read_text(encoding="utf-8") == "# pre-existing\n"


def test_warning_is_printed_on_its_own_indented_line(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    src = _make_source(tmp_path / "src")
    codex_home = tmp_path / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "AGENTS.override.md").write_text("shadow\n", encoding="utf-8")
    assert _run(["--tools", "codex", "--source-root", str(src)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert any(line[:1].isspace() and "AGENTS.override.md" in line for line in lines), lines


def test_env_comes_from_os_environ(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    custom = tmp_path / "custom-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
    src = _make_source(tmp_path / "src")
    assert _run(["--tools", "claude", "--source-root", str(src)]) == 0
    target = custom / "CLAUDE.md"
    assert target.is_file()
    _assert_line(capsys.readouterr().out, "claude", target, "created")


def test_default_source_root_is_the_repo_root_not_cwd(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert _run(["--tools", "claude"]) == 0
    repo_root = Path(cli.__file__).resolve().parent.parent
    standards = (repo_root / "global-rules" / "standards.md").read_text(encoding="utf-8")
    assert standards in _target("claude", tmp_path).read_text(encoding="utf-8")


# --- negative paths ---------------------------------------------------------

def test_missing_tools_is_usage_error_and_writes_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    src = _make_source(tmp_path / "src")
    before = _snapshot(tmp_path)
    assert _run(["--source-root", str(src)]) == 2
    assert _snapshot(tmp_path) == before


def test_unknown_tool_is_usage_error_and_writes_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    src = _make_source(tmp_path / "src")
    before = _snapshot(tmp_path)
    assert _run(["--tools", "bogus", "--source-root", str(src)]) == 2
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("tools", ["claude,,codex", "claude,", ",claude"])
def test_empty_tool_entry_is_usage_error_and_writes_nothing(tmp_path, monkeypatch, tools) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    src = _make_source(tmp_path / "src")
    before = _snapshot(tmp_path)
    assert _run(["--tools", tools, "--source-root", str(src)]) == 2
    assert _snapshot(tmp_path) == before


def test_engine_error_exits_1_with_stderr_message_and_no_traceback(tmp_path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    src = tmp_path / "src"  # no global-rules/ -> the engine raises
    src.mkdir()
    assert _run(["--tools", "claude", "--source-root", str(src)]) == 1
    err = capsys.readouterr().err
    assert err.strip()
    assert "Traceback" not in err
    # No home-directory paths beyond the target: any occurrence of the home
    # dir must be the target path itself.
    for match in re.finditer(re.escape(str(home)), err):
        assert err[match.end():].startswith("/.claude/CLAUDE.md"), err
