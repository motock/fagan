"""Grades the cross-repository authoring guidance in the tool docstring, the
shipped rule file and the reference docs."""

from pathlib import Path

from pipeline import server_tools_read

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_MD = ROOT / ".claude" / "rules" / "pipeline-story-schema.md"
REFERENCE_MD = ROOT / "REFERENCE.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _list_repositories_doc() -> str:
    return server_tools_read.list_repositories.__doc__ or ""


def test_list_repositories_docstring_tells_author_to_search_other_repositories() -> None:
    assert "search the other repositories" in _list_repositories_doc()


def test_list_repositories_docstring_tells_author_to_order_with_blocked_by() -> None:
    assert "blocked_by" in _list_repositories_doc()


def test_list_repositories_docstring_keeps_its_first_sentence() -> None:
    assert "List the repositories the caller may work with" in _list_repositories_doc()


def test_schema_rule_has_cross_repository_heading() -> None:
    assert "A change that crosses repositories" in _read(SCHEMA_MD)


def test_schema_rule_says_merged_is_not_published() -> None:
    text = _read(SCHEMA_MD)
    assert "merged is" in text
    assert "not published" in text


def test_schema_rule_contains_no_personal_paths() -> None:
    text = _read(SCHEMA_MD)
    assert "/Users/" not in text
    assert "/home/" not in text


def test_reference_documents_cross_repository_ordering() -> None:
    assert "The plans may target different repositories" in _read(REFERENCE_MD)
