"""Shared, language-aware detection of test paths.

Two public functions:

* :func:`is_test_path` -- permissive.  A path is a test path when any
  directory component names a test directory, or when the basename
  matches a known test-file pattern for any supported language.
* :func:`is_test_module` -- strict.  Only the basename is considered and
  ``conftest.py`` is deliberately excluded, so callers that must fail
  closed treat it as production code.

Directory matching is by exact path component, never by substring, so
``contests/`` is not ``tests/``.
"""

from __future__ import annotations

_TEST_DIRS = frozenset({"tests", "test", "__tests__", "spec", "Tests"})

_JS_TS_EXTS = ("js", "jsx", "ts", "tsx", "mjs", "cjs", "mts", "cts")

_JS_TS_SUFFIXES = tuple(
    f".{kind}.{ext}" for kind in ("test", "spec") for ext in _JS_TS_EXTS
)

_BASENAME_SUFFIXES = (
    "_test.py",
    "_test.rs",
    "_test.go",
    "Test.java",
    "Tests.java",
    "IT.java",
    "Test.kt",
    "Tests.kt",
    "IT.kt",
    "_spec.rb",
    "Tests.swift",
    "Test.cs",
    "Tests.cs",
)


def _basename_is_test(name: str, *, allow_conftest: bool) -> bool:
    if not name:
        return False
    if allow_conftest and name == "conftest.py":
        return True
    if name.startswith("test_") and name.endswith(".py"):
        return True
    return name.endswith(_BASENAME_SUFFIXES + _JS_TS_SUFFIXES)


def is_test_path(path: str) -> bool:
    """Return True if *path* looks like a test file in any supported language.

    Permissive: a test directory component (``tests``, ``test``,
    ``__tests__``, ``spec``, ``Tests``, or a ``*.Tests`` C# project) or a
    ``src/test/`` Maven/Gradle layout counts, as does a test basename.
    """
    normalised = str(path).replace("\\", "/")
    if not normalised:
        return False
    parts = normalised.split("/")
    for component in parts[:-1]:
        if component in _TEST_DIRS or component.endswith(".Tests"):
            return True
    if normalised.startswith("src/test/") or "/src/test/" in normalised:
        return True
    return _basename_is_test(parts[-1], allow_conftest=True)


def is_test_module(path: str) -> bool:
    """Return True only if the basename looks like a test module.

    Strict: directory components are ignored and ``conftest.py`` is
    excluded, so callers that must fail closed treat it as production
    code.
    """
    normalised = str(path).replace("\\", "/")
    if not normalised:
        return False
    return _basename_is_test(normalised.split("/")[-1], allow_conftest=False)