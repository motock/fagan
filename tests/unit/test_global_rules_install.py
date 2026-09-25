"""Tests for pipeline.global_rules_install (GR-4).

The module under test does not exist yet in this story's first dispatch, so
this file is expected to fail at import time until it is implemented.
"""

from __future__ import annotations

import dataclasses
import os
import re
from pathlib import Path

import pytest

from pipeline import global_rules_install as gri
from pipeline import global_rules_targets as targets
from pipeline.managed_block import BEGIN_MARKER, END_MARKER

RULES_NAMES = (
    "agent-dispatch-story-sizing.md",
    "code-review.md",
    "local-dispatch-preflight.md",
    "pipeline-story-schema.md",
    "testing-config-gates.md",
)


def _make_source(root: Path, standards: str = "STANDARDS-BODY", workflow: str = "WORKFLOW-BODY") -> Path:
    (root / "global-rules").mkdir(parents=True, exist_ok=True)
    (root / "global-rules" / "standards.md").write_text(standards, encoding="utf-8")
    (root / "global-rules" / "pipeline-workflow.md").write_text(workflow, encoding="utf-8")
    rules = root / ".claude" / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    for name in RULES_NAMES:
        (rules / name).write_text(f"RULES-{name}", encoding="utf-8")
    return root


def _snapshot(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        out[str(path.relative_to(root))] = "<dir>" if path.is_dir() else path.read_text(encoding="utf-8")
    return out


def _backups(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if ".fagan-bak-" in p.name)


def test_api_shape() -> None:
    fields = {f.name for f in dataclasses.fields(gri.InstallResult)}
    assert {"tool", "target", "changed", "backup", "warning"} <= fields
    result = gri.InstallResult(tool="claude", target=Path("/x"), changed=False, backup=None, warning=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.changed = True  # type: ignore[misc]
    with pytest.raises(TypeError):
        gri.install_for_tool("claude", Path("/src"), {"HOME": "/h"}, True)  # type: ignore[misc]


def test_render_block_contains_both_sources_and_absolute_rules_dir(tmp_path: Path) -> None:
    source_root = _make_source(tmp_path / "src", standards="STANDARDS-BODY-1", workflow="WORKFLOW-BODY-2")
    rules_dir = tmp_path / "out" / "fagan-rules"

    block = gri.render_block(source_root, rules_dir)

    assert "STANDARDS-BODY-1" in block
    assert "WORKFLOW-BODY-2" in block
    assert block.index("STANDARDS-BODY-1") < block.index("WORKFLOW-BODY-2")
    assert BEGIN_MARKER not in block and END_MARKER not in block
    last_line = block.rstrip("\n").splitlines()[-1]
    assert str(rules_dir) in last_line or str(rules_dir.resolve()) in last_line


def test_fresh_install_writes_target_and_copies_rules(tmp_path: Path) -> None:
    source_root = _make_source(tmp_path / "src")
    env = {"HOME": str(tmp_path / "home")}
    rules_dir = targets.rules_dir("claude", env)
    rules_dir.mkdir(parents=True)
    (rules_dir / "code-review.md").write_text("STALE", encoding="utf-8")

    result = gri.install_for_tool("claude", source_root, env, dry_run=False)

    assert isinstance(result, gri.InstallResult)
    assert result.tool == "claude"
    assert result.target == targets.instructions_path("claude", env)
    assert result.changed is True
    assert result.backup is None
    assert result.warning is None

    text = result.target.read_text(encoding="utf-8")
    assert text.startswith(BEGIN_MARKER)
    assert END_MARKER in text
    assert "STANDARDS-BODY" in text and "WORKFLOW-BODY" in text
    assert str(rules_dir) in text or str(rules_dir.resolve()) in text
    for name in RULES_NAMES:
        assert (rules_dir / name).read_text(encoding="utf-8") == f"RULES-{name}"


def test_second_run_changed_false_and_touches_nothing(tmp_path: Path) -> None:
    source_root = _make_source(tmp_path / "src")
    env = {"HOME": str(tmp_path / "home")}
    first = gri.install_for_tool("claude", source_root, env, dry_run=False)
    before = _snapshot(tmp_path)

    second = gri.install_for_tool("claude", source_root, env, dry_run=False)

    assert second.changed is False
    assert second.backup is None
    assert second.target == first.target
    assert _snapshot(tmp_path) == before


def test_refresh_preserves_user_text(tmp_path: Path) -> None:
    source_root = _make_source(tmp_path / "src", standards="STANDARDS-V1")
    env = {"HOME": str(tmp_path / "home")}
    target = targets.instructions_path("claude", env)
    target.parent.mkdir(parents=True)
    target.write_text("USER-NOTE\n", encoding="utf-8")

    gri.install_for_tool("claude", source_root, env, dry_run=False)
    assert "USER-NOTE" in target.read_text(encoding="utf-8")

    (source_root / "global-rules" / "standards.md").write_text("STANDARDS-V2", encoding="utf-8")
    result = gri.install_for_tool("claude", source_root, env, dry_run=False)

    assert result.changed is True
    text = target.read_text(encoding="utf-8")
    assert "USER-NOTE" in text
    assert "STANDARDS-V2" in text
    assert "STANDARDS-V1" not in text


def test_backup_created_only_when_target_preexisted(tmp_path: Path) -> None:
    source_root = _make_source(tmp_path / "src", standards="STANDARDS-V1")
    env = {"HOME": str(tmp_path / "home")}
    target = targets.instructions_path("claude", env)

    fresh = gri.install_for_tool("claude", source_root, env, dry_run=False)
    assert fresh.backup is None
    assert _backups(target.parent) == []

    previous = target.read_text(encoding="utf-8")
    (source_root / "global-rules" / "standards.md").write_text("STANDARDS-V2", encoding="utf-8")
    result = gri.install_for_tool("claude", source_root, env, dry_run=False)

    assert result.backup is not None
    assert result.backup.parent == target.parent
    assert re.fullmatch(r"CLAUDE\.md\.fagan-bak-\d{8}T\d{6}Z", result.backup.name)
    assert result.backup.read_text(encoding="utf-8") == previous


def test_warning_none_when_no_shadowing(tmp_path: Path) -> None:
    source_root = _make_source(tmp_path / "src")
    env = {"HOME": str(tmp_path / "home")}

    result = gri.install_for_tool("codex", source_root, env, dry_run=False)

    assert result.warning is None
    assert result.changed is True


def test_warning_set_when_shadowing_exists(tmp_path: Path) -> None:
    source_root = _make_source(tmp_path / "src")
    env = {"HOME": str(tmp_path / "home")}
    shadow = targets.shadowing_path("codex", env)
    assert shadow is not None
    shadow.parent.mkdir(parents=True)
    shadow.write_text("override\n", encoding="utf-8")

    result = gri.install_for_tool("codex", source_root, env, dry_run=False)

    assert result.warning is not None
    assert "\n" not in result.warning
    assert "codex" in result.warning
    assert str(shadow) in result.warning
    assert result.changed is True
    assert BEGIN_MARKER in result.target.read_text(encoding="utf-8")


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    source_root = _make_source(tmp_path / "src")
    home = tmp_path / "home"
    env = {"HOME": str(home)}
    before = _snapshot(tmp_path)

    result = gri.install_for_tool("claude", source_root, env, dry_run=True)

    assert result.changed is True
    assert result.backup is None
    assert _snapshot(tmp_path) == before
    assert not home.exists()
    assert not targets.instructions_path("claude", env).exists()
    assert not targets.rules_dir("claude", env).exists()


def test_dry_run_leaves_existing_target_untouched(tmp_path: Path) -> None:
    source_root = _make_source(tmp_path / "src")
    env = {"HOME": str(tmp_path / "home")}
    target = targets.instructions_path("claude", env)
    target.parent.mkdir(parents=True)
    target.write_text("USER-NOTE\n", encoding="utf-8")

    result = gri.install_for_tool("claude", source_root, env, dry_run=True)

    assert result.changed is True
    assert target.read_text(encoding="utf-8") == "USER-NOTE\n"
    assert _backups(target.parent) == []


def test_unsupported_tool_refused(tmp_path: Path) -> None:
    source_root = _make_source(tmp_path / "src")
    with pytest.raises(ValueError):
        gri.install_for_tool("bogus", source_root, {"HOME": str(tmp_path / "home")}, dry_run=False)


def test_symlinked_target_refused(tmp_path: Path) -> None:
    source_root = _make_source(tmp_path / "src")
    env = {"HOME": str(tmp_path / "home")}
    target = targets.instructions_path("claude", env)
    target.parent.mkdir(parents=True)
    real = tmp_path / "real-claude.md"
    real.write_text("REAL\n", encoding="utf-8")
    target.symlink_to(real)

    with pytest.raises(ValueError):
        gri.install_for_tool("claude", source_root, env, dry_run=False)

    assert real.read_text(encoding="utf-8") == "REAL\n"
    assert target.is_symlink()
    assert _backups(target.parent) == []


def test_symlinked_rules_dir_refused(tmp_path: Path) -> None:
    source_root = _make_source(tmp_path / "src")
    env = {"HOME": str(tmp_path / "home")}
    rules_dir = targets.rules_dir("claude", env)
    rules_dir.parent.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    rules_dir.symlink_to(elsewhere)

    with pytest.raises(ValueError):
        gri.install_for_tool("claude", source_root, env, dry_run=False)

    assert list(elsewhere.iterdir()) == []
    assert rules_dir.is_symlink()


def test_missing_source_file_raises_filenotfound(tmp_path: Path) -> None:
    source_root = _make_source(tmp_path / "src")
    env = {"HOME": str(tmp_path / "home")}
    (source_root / "global-rules" / "standards.md").unlink()

    with pytest.raises(FileNotFoundError) as excinfo:
        gri.install_for_tool("claude", source_root, env, dry_run=False)
    assert "standards.md" in str(excinfo.value)

    _make_source(source_root)
    (source_root / "global-rules" / "pipeline-workflow.md").unlink()
    with pytest.raises(FileNotFoundError) as excinfo:
        gri.install_for_tool("claude", source_root, env, dry_run=False)
    assert "pipeline-workflow.md" in str(excinfo.value)


def test_corrupt_markers_propagate_valueerror_without_modifying(tmp_path: Path) -> None:
    source_root = _make_source(tmp_path / "src")
    env = {"HOME": str(tmp_path / "home")}
    target = targets.instructions_path("claude", env)
    target.parent.mkdir(parents=True)
    corrupt = f"{BEGIN_MARKER}\nfoo\n{BEGIN_MARKER}\nbar\n{END_MARKER}\n"
    target.write_text(corrupt, encoding="utf-8")

    with pytest.raises(ValueError):
        gri.install_for_tool("claude", source_root, env, dry_run=False)

    assert target.read_text(encoding="utf-8") == corrupt
    assert _backups(target.parent) == []


def test_write_atomic_replaces_via_temp_in_same_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "out"
    directory.mkdir()
    path = directory / "file.txt"

    gri._write_atomic(path, "hello\n")
    assert path.read_text(encoding="utf-8") == "hello\n"
    gri._write_atomic(path, "world\n")
    assert path.read_text(encoding="utf-8") == "world\n"
    assert [p.name for p in directory.iterdir()] == ["file.txt"]

    seen: list[tuple[Path, Path]] = []

    def boom(src: object, dst: object) -> None:
        seen.append((Path(str(src)), Path(str(dst))))
        raise OSError("boom")

    monkeypatch.setattr(os, "replace", boom)
    if hasattr(gri, "replace"):
        monkeypatch.setattr(gri, "replace", boom)
    with pytest.raises(OSError):
        gri._write_atomic(path, "new\n")

    assert seen, "os.replace was not used"
    src, dst = seen[0]
    assert src.parent == directory and dst == path
    assert path.read_text(encoding="utf-8") == "world\n"
