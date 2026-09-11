"""Tests for RENAMEFAGAN-1: the project is renamed to Fagan.

README.md, CONTRIBUTING.md, NOTICE, REFERENCE.md and the package docstring
must use the new name and the new GitHub slug (motock/fagan), while the
historical planning record under docs/plans/ must keep the old name
untouched (no repo-wide find/replace was run).
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
README = REPO_ROOT / "README.md"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"
NOTICE = REPO_ROOT / "NOTICE"
REFERENCE = REPO_ROOT / "REFERENCE.md"
PIPELINE_INIT = REPO_ROOT / "pipeline" / "__init__.py"
PLANS_DIR = REPO_ROOT / "docs" / "plans"


def test_readme_h1_is_fagan():
    first_line = README.read_text(encoding="utf-8").splitlines()[0]
    assert first_line == "# Fagan"


def test_readme_has_no_old_repo_slug():
    text = README.read_text(encoding="utf-8")
    assert "claude-pipeline-mcp" not in text


def test_contributing_has_no_old_repo_slug():
    text = CONTRIBUTING.read_text(encoding="utf-8")
    assert "claude-pipeline-mcp" not in text


def test_notice_first_line_is_fagan():
    first_line = NOTICE.read_text(encoding="utf-8").splitlines()[0]
    assert first_line == "Fagan"


def test_notice_keeps_copyright_and_license():
    text = NOTICE.read_text(encoding="utf-8")
    assert "Copyright 2026 Jesse Carroll" in text
    assert "Apache License" in text


def test_pipeline_docstring_first_line_is_fagan():
    first_line = PIPELINE_INIT.read_text(encoding="utf-8").splitlines()[0]
    assert first_line.startswith('"""Fagan')


def test_plans_history_still_contains_old_name():
    matches = [
        path
        for path in PLANS_DIR.rglob("*")
        if path.suffix in (".md", ".json")
        and "Autonomous SDLC Agent Pipeline" in path.read_text(encoding="utf-8")
    ]
    assert matches, (
        "No file under docs/plans/ contains 'Autonomous SDLC Agent Pipeline'; "
        "a repo-wide find/replace appears to have rewritten the historical record."
    )