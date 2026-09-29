"""Tests for the shared, language-aware test-file classifier.

``pipeline.testfiles`` is the single answer to "is this a test file", shared by
the scope gate, the ingest sizing check and the plan-conflict intercept.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from pipeline import ingest, plan_conflict_ruling, scope_gate, testfiles

# The mandated JUnit test that the scope gate wrongly rejected (anagram DSG-1).
JUNIT_TEST = "src/test/java/com/example/anagram/ProjectStructureTests.java"


@pytest.mark.parametrize(
    "path",
    [
        JUNIT_TEST,
        "service/src/test/java/a/FooIT.java",
        "src/test/kotlin/FooTest.kt",
        "pkg/foo_test.go",
        "web/src/foo.test.ts",
        "web/src/foo.spec.tsx",
        "app/__tests__/foo.js",
        "spec/models/user_spec.rb",
        "Tests/MyLibTests/FooTests.swift",
        "Foo.Tests/BarTests.cs",
        "tests/helpers.py",
        "test_x.py",
        "a/b_test.py",
        "conftest.py",
        "crates/x/tests/it.rs",
        "src/a_test.rs",
    ],
)
def test_is_test_path_true(path: str) -> None:
    assert testfiles.is_test_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "",
        "contests/x.rs",
        "src/tests.rs",
        "src/main/java/TestUtils.java",
        "src/main/java/Tester.java",
        "attest.py",
        "latest.go",
        "src/protest.ts",
        "specification/x.md",
        "pipeline/testfiles.py",
    ],
)
def test_is_test_path_false(path: str) -> None:
    assert testfiles.is_test_path(path) is False


def test_is_test_path_normalises_backslashes() -> None:
    assert testfiles.is_test_path("src\\test\\java\\FooTest.java") is True
    assert testfiles.is_test_path("tests\\helpers.py") is True


def test_is_test_path_directory_match_is_exact_component() -> None:
    # A directory merely containing "test" as a substring is not a test dir.
    assert testfiles.is_test_path("contests/x.rs") is False
    assert testfiles.is_test_path("latest/tests_helpers.py") is False


@pytest.mark.parametrize(
    "path",
    ["foo_test.py", "src/test/java/FooTest.java", "x/foo.spec.ts"],
)
def test_is_test_module_true(path: str) -> None:
    assert testfiles.is_test_module(path) is True


@pytest.mark.parametrize(
    "path",
    ["conftest.py", "tests/helpers.py", "tests/", "src/test/resources/data.json", ""],
)
def test_is_test_module_false(path: str) -> None:
    assert testfiles.is_test_module(path) is False


def test_is_test_module_ignores_directories() -> None:
    # Strict: only the basename counts, never a directory component.
    assert testfiles.is_test_module("tests/helpers.py") is False
    assert testfiles.is_test_module("src/test/java/FooTest.java") is True


def test_module_exposes_exactly_two_public_functions() -> None:
    public = {
        name
        for name, obj in inspect.getmembers(testfiles, inspect.isfunction)
        if not name.startswith("_") and obj.__module__ == testfiles.__name__
    }
    assert public == {"is_test_path", "is_test_module"}


def test_scope_gate_delegates() -> None:
    assert scope_gate.is_test_path(JUNIT_TEST) is True
    assert scope_gate.is_test_path("pipeline/scope_gate.py") is False


def test_ingest_delegates() -> None:
    assert ingest._is_test_path(JUNIT_TEST) is True
    assert ingest._is_test_path(Path("src/test/java/a/FooIT.java")) is True
    assert ingest._is_test_path("pipeline/ingest.py") is False


def test_plan_conflict_ruling_delegates_strictly() -> None:
    assert plan_conflict_ruling._is_test_path(JUNIT_TEST) is True
    assert plan_conflict_ruling._is_test_path("tests/helpers.py") is False
    assert plan_conflict_ruling._is_test_path("conftest.py") is False


def _story_file_scope_section() -> str:
    text = Path("REFERENCE.md").read_text(encoding="utf-8")
    marker = "## Story file scope (`files`)"
    start = text.index(marker)
    rest = text[start + len(marker) :]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


@pytest.mark.parametrize(
    "token",
    [
        "src/test/",
        "__tests__",
        "*Test.java",
        "*_test.go",
        "*_spec.rb",
        "*Tests.swift",
        "*Tests.cs",
    ],
)
def test_reference_documents_recognised_test_patterns(token: str) -> None:
    assert token in _story_file_scope_section()


def test_reference_drops_the_old_python_only_prose() -> None:
    section = _story_file_scope_section()
    assert "anything under `tests/`, or whose basename matches" not in section
    assert "anything under `tests/`, or a `test_*.py`" not in section
