"""Grades global-rules content: exact digests and structural guards."""

import hashlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STANDARDS = ROOT / "global-rules" / "standards.md"
WORKFLOW = ROOT / "global-rules" / "pipeline-workflow.md"
STANDARDS_SHA256 = "f9880ec9d311b266f9b7918daddd5240fef4d05bb453edecd32072e591b94c18"
WORKFLOW_SHA256 = "87172671c1e405fb33b5354ce3f2144cb62e772856b3f0e83ed67ea1bd5dd6ca"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_standards_matches_prescribed_content() -> None:
    assert _sha256(STANDARDS) == STANDARDS_SHA256


def test_workflow_matches_prescribed_content() -> None:
    assert _sha256(WORKFLOW) == WORKFLOW_SHA256


@pytest.mark.parametrize("path", [STANDARDS, WORKFLOW])
def test_content_has_no_personal_paths(path: Path) -> None:
    assert "/Users/" not in path.read_text(encoding="utf-8")


def test_standards_is_tool_neutral() -> None:
    assert "mcp__pipeline__" not in STANDARDS.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "rules_file",
    [
        "pipeline-story-schema.md",
        "agent-dispatch-story-sizing.md",
        "local-dispatch-preflight.md",
        "code-review.md",
        "testing-config-gates.md",
    ],
)
def test_workflow_references_each_rules_file(rules_file: str) -> None:
    assert f"fagan-rules/{rules_file}" in WORKFLOW.read_text(encoding="utf-8")
