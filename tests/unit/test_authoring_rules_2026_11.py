"""Grades the authoring rules added 2026-11-22 to the shipped rule files."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RULES = ROOT / ".claude" / "rules"
SCHEMA_MD = RULES / "pipeline-story-schema.md"
SIZING_MD = RULES / "agent-dispatch-story-sizing.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_key_bullet_requires_unused_prefix() -> None:
    assert "Choose a prefix no other plan has used" in _read(SCHEMA_MD)


def test_schema_requires_named_gate_commands() -> None:
    assert "Name every gate command" in _read(SCHEMA_MD)


def test_sizing_warns_about_cross_plan_file_overlaps() -> None:
    assert "Across plans too." in _read(SIZING_MD)


def test_schema_documents_unblocking_parked_story_with_uncommitted_work() -> None:
    assert "Unblocking a parked story that has uncommitted work" in _read(SCHEMA_MD)


@pytest.mark.parametrize("path", [SCHEMA_MD, SIZING_MD])
def test_rules_contain_no_personal_paths(path: Path) -> None:
    text = _read(path)
    assert "/Users/" not in text
    assert "/home/" not in text
