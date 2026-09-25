"""End-to-end suite for the global rules installer (GR-8).

Drives ``scripts/install_global_rules.main`` against a synthetic source tree
and a ``tmp_path`` HOME.  Nothing here reads the real repo's rules files or
the real environment: the source tree and HOME are both built in fixtures.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.managed_block import BEGIN_MARKER, END_MARKER
from scripts import install_global_rules as cli

# Expected instruction-file path for each tool, relative to HOME.
EXPECTED_TARGETS = {
    "claude": Path(".claude") / "CLAUDE.md",
    "codex": Path(".codex") / "AGENTS.md",
    "opencode": Path(".config") / "opencode" / "AGENTS.md",
}


def _make_source(root: Path) -> Path:
    """Build a minimal synthetic source tree the engine can render from."""
    (root / "global-rules").mkdir(parents=True, exist_ok=True)
    (root / "global-rules" / "standards.md").write_text(
        "STANDARDS-BODY\n", encoding="utf-8"
    )
    (root / "global-rules" / "pipeline-workflow.md").write_text(
        "WORKFLOW-BODY\n", encoding="utf-8"
    )
    rules = root / ".claude" / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    (rules / "code-review.md").write_text("RULES-BODY\n", encoding="utf-8")
    return root


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A synthetic HOME with every per-tool override cleared."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    for key in (
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "OPENCODE_CONFIG_DIR",
        "XDG_CONFIG_HOME",
    ):
        monkeypatch.delenv(key, raising=False)
    return h


@pytest.fixture
def source(tmp_path):
    return _make_source(tmp_path / "src")


def _run(source: Path, tools: str = "claude,codex,opencode", *extra: str) -> int:
    return cli.main(["--tools", tools, "--source-root", str(source), *extra])


def _snapshot(root: Path) -> dict[str, bytes]:
    """Every file under *root*, keyed by relative path, as raw bytes."""
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_all_three_tools_install_to_expected_paths(home, source):
    assert _run(source) == 0
    for tool, rel in EXPECTED_TARGETS.items():
        target = home / rel
        assert target.is_file(), f"{tool} target missing: {target}"
        text = target.read_text(encoding="utf-8")
        assert BEGIN_MARKER in text and END_MARKER in text, tool
        assert "STANDARDS-BODY" in text and "WORKFLOW-BODY" in text, tool
        # The bundle's rule files land in the sibling fagan-rules directory.
        assert (target.parent / "fagan-rules" / "code-review.md").read_text(
            encoding="utf-8"
        ) == "RULES-BODY\n", tool


def test_second_run_leaves_every_byte_unchanged(home, source):
    assert _run(source) == 0
    before = _snapshot(home)
    assert before  # sanity: the first run actually wrote something
    assert _run(source) == 0
    assert _snapshot(home) == before


def test_user_text_above_and_below_block_survives_update(home, source):
    assert _run(source, "claude") == 0
    target = home / EXPECTED_TARGETS["claude"]
    text = target.read_text(encoding="utf-8")
    target.write_text("USER-ABOVE\n" + text + "USER-BELOW\n", encoding="utf-8")

    (source / "global-rules" / "standards.md").write_text(
        "STANDARDS-V2\n", encoding="utf-8"
    )
    assert _run(source, "claude") == 0

    updated = target.read_text(encoding="utf-8")
    assert "USER-ABOVE" in updated
    assert "USER-BELOW" in updated
    assert "STANDARDS-V2" in updated


def test_source_change_updates_only_block_and_makes_one_backup(home, source):
    assert _run(source, "claude") == 0
    target = home / EXPECTED_TARGETS["claude"]
    before = target.read_text(encoding="utf-8")
    prefix = before[: before.index(BEGIN_MARKER)]
    suffix = before[before.index(END_MARKER) + len(END_MARKER) :]

    (source / "global-rules" / "standards.md").write_text(
        "STANDARDS-V2\n", encoding="utf-8"
    )
    assert _run(source, "claude") == 0

    after = target.read_text(encoding="utf-8")
    assert after[: after.index(BEGIN_MARKER)] == prefix
    assert after[after.index(END_MARKER) + len(END_MARKER) :] == suffix
    assert "STANDARDS-V2" in after

    backups = list(target.parent.glob(target.name + ".fagan-bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == before


def test_dry_run_changes_nothing(home, source):
    assert _run(source, "claude,codex,opencode", "--dry-run") == 0
    assert _snapshot(home) == {}

    assert _run(source) == 0
    before = _snapshot(home)
    assert _run(source, "claude,codex,opencode", "--dry-run") == 0
    assert _snapshot(home) == before


def test_symlinked_target_is_refused(home, source, capsys):
    real = home / "real.md"
    real.write_text("REAL\n", encoding="utf-8")
    target = home / EXPECTED_TARGETS["claude"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(real)

    assert _run(source, "claude") == 1
    assert target.is_symlink()
    assert real.read_text(encoding="utf-8") == "REAL\n"
    assert capsys.readouterr().err.strip()


def test_corrupt_markers_exit_1_and_leave_file_identical(home, source):
    target = home / EXPECTED_TARGETS["claude"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "USER\n" + BEGIN_MARKER + "\nno end marker here\n", encoding="utf-8"
    )
    before = target.read_bytes()

    assert _run(source, "claude") == 1
    assert target.read_bytes() == before
