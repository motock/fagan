"""Tests for the backticked-repo-path regex used by story sizing.

``_story_sizing_warning`` derives candidate production file paths from
backticked text in a story's ``agent_instructions`` when the story declares no
``files`` list. This story replaces the old hard-coded
``app|pipeline|static|scripts|tests|docs|src|systemd/`` prefix regex with a
module-level compiled constant ``_BACKTICK_REPO_PATH_RE`` that accepts any
backticked RELATIVE path with at least one ``/`` and a final component that
carries a file extension.

These tests are RED until the implementation lands.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# pipeline.server must load before pipeline.ingest: pipeline.ingest's module
# body imports pipeline.build_detect, which imports pipeline.server, which
# imports _ingest_plan_impl back from pipeline.ingest -- so importing
# pipeline.ingest first re-enters a partially-initialized pipeline.server.
import pipeline.server as p  # noqa: F401
from pipeline import ingest as ingest_mod

_MATCHES = [
    "cmd/server/main.go",
    "internal/x/y.go",
    "packages/web/src/a.ts",
    "crates/core/src/lib.rs",
    "src/main/java/com/x/Foo.java",
    "pipeline/ingest.py",
]

_NON_MATCHES = [
    "foo.py",
    "/abs/path.py",
    "../x/y.py",
    "./x/y.py",
    "pipeline/",
    "mvn -B verify",
    "a/b",
    "https://x.y/z.html",
]


def test_backtick_repo_path_re_is_a_compiled_module_constant():
    assert isinstance(ingest_mod._BACKTICK_REPO_PATH_RE, re.Pattern)


@pytest.mark.parametrize("path", _MATCHES)
def test_backtick_repo_path_re_matches_relative_paths(path):
    assert ingest_mod._BACKTICK_REPO_PATH_RE.findall(f"`{path}`") == [path]


@pytest.mark.parametrize("path", _NON_MATCHES)
def test_backtick_repo_path_re_rejects_non_paths(path):
    assert ingest_mod._BACKTICK_REPO_PATH_RE.findall(f"`{path}`") == []


def _write_lines(root: Path, rel: str, count: int) -> None:
    full = root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("x = 1\n" * count, encoding="utf-8")


def _local_story(instructions: str) -> dict:
    """A local-backend story with no ``files`` list, so the backtick regex
    derivation in ``_story_sizing_warning`` is the one under test."""
    return {"backend": "local", "agent_instructions": instructions}


def test_oversized_backticked_path_in_instructions_warns(tmp_path):
    _write_lines(tmp_path, "internal/big.go", 1001)
    warning = ingest_mod._story_sizing_warning(
        _local_story("Files: `internal/big.go`"), str(tmp_path)
    )
    assert warning is not None
    assert "internal/big.go" in warning


def test_file_exactly_at_size_cap_does_not_warn(tmp_path):
    _write_lines(tmp_path, "internal/big.go", 1000)
    assert (
        ingest_mod._story_sizing_warning(
            _local_story("Files: `internal/big.go`"), str(tmp_path)
        )
        is None
    )


def test_missing_backticked_file_does_not_warn(tmp_path):
    assert (
        ingest_mod._story_sizing_warning(
            _local_story("Files: `internal/big.go`"), str(tmp_path)
        )
        is None
    )


def test_backticked_non_path_does_not_warn(tmp_path):
    assert (
        ingest_mod._story_sizing_warning(
            _local_story("Run `mvn -B verify` first."), str(tmp_path)
        )
        is None
    )


def test_backticked_test_path_is_excluded(tmp_path):
    _write_lines(tmp_path, "tests/unit/test_big.py", 1001)
    assert (
        ingest_mod._story_sizing_warning(
            _local_story("Files: `tests/unit/test_big.py`"), str(tmp_path)
        )
        is None
    )
