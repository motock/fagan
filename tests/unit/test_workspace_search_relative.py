"""Workspace search reports paths relative to the repository root.

``PipelineService.search_workspace`` runs grep with ``cwd`` set to the
workspace root and searches ``.``, so each match line starts with the
repository-relative path (``src/a.py:12:...``) and never with the server's
absolute clone path. These tests run real grep against a ``tmp_path`` tree.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import pipeline.service as service_mod
from pipeline.service import PipelineService

requires_grep = pytest.mark.skipif(shutil.which("grep") is None, reason="grep not installed")


@pytest.fixture
def service():
    return PipelineService()


def _stub_active_workspace(monkeypatch, service, path):
    monkeypatch.setattr(service, "get_active_workspace", lambda: path)
    store = getattr(service_mod, "_store", None)
    if store is not None and hasattr(store, "get_active_workspace"):
        monkeypatch.setattr(store, "get_active_workspace", lambda *a, **k: path)


def _seed_tree(root: Path) -> None:
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("needle = 1\n", encoding="utf-8")


@requires_grep
def test_match_in_a_nested_file_is_reported_relative_to_the_workspace(service, monkeypatch, tmp_path):
    _seed_tree(tmp_path)
    _stub_active_workspace(monkeypatch, service, str(tmp_path))

    result = service.search_workspace("needle")

    assert result == {"ok": True, "matches": ["src/a.py:1:needle = 1"], "truncated": False}


@requires_grep
def test_match_under_a_managed_clone_root_is_reported_relative_to_the_clone(service, tmp_path):
    _seed_tree(tmp_path)

    result = service.search_workspace("needle", root=str(tmp_path))

    assert result == {"ok": True, "matches": ["src/a.py:1:needle = 1"], "truncated": False}


@requires_grep
def test_no_match_line_carries_the_absolute_root_or_a_dot_prefix(service, monkeypatch, tmp_path):
    _seed_tree(tmp_path)
    (tmp_path / "top.txt").write_text("needle\n", encoding="utf-8")
    _stub_active_workspace(monkeypatch, service, str(tmp_path))

    matches = service.search_workspace("needle")["matches"]

    assert sorted(matches) == ["src/a.py:1:needle = 1", "top.txt:1:needle"]
    for line in matches:
        assert str(tmp_path) not in line
        assert not line.startswith("/")
        assert not line.startswith("./")


@requires_grep
def test_leading_dot_of_a_dotted_directory_is_kept(service, monkeypatch, tmp_path):
    (tmp_path / ".config").mkdir()
    (tmp_path / ".config" / "app.toml").write_text("needle\n", encoding="utf-8")
    _stub_active_workspace(monkeypatch, service, str(tmp_path))

    assert service.search_workspace("needle")["matches"] == [".config/app.toml:1:needle"]


@requires_grep
def test_file_named_with_a_leading_dash_is_searched_as_data(service, monkeypatch, tmp_path):
    (tmp_path / "-n").write_text("needle\n", encoding="utf-8")
    _stub_active_workspace(monkeypatch, service, str(tmp_path))

    assert service.search_workspace("needle") == {
        "ok": True,
        "matches": ["-n:1:needle"],
        "truncated": False,
    }


@requires_grep
def test_pattern_starting_with_a_dash_is_searched_as_data(service, monkeypatch, tmp_path):
    (tmp_path / "flags.txt").write_text("set -rf now\n", encoding="utf-8")
    _stub_active_workspace(monkeypatch, service, str(tmp_path))

    assert service.search_workspace("-rf") == {
        "ok": True,
        "matches": ["flags.txt:1:set -rf now"],
        "truncated": False,
    }


@requires_grep
def test_nul_byte_pattern_keeps_the_fixed_invalid_pattern_error(service, monkeypatch, tmp_path):
    _stub_active_workspace(monkeypatch, service, str(tmp_path))

    assert service.search_workspace("needle\x00") == {"ok": False, "error": "invalid pattern"}


@requires_grep
def test_empty_pattern_keeps_the_fixed_invalid_pattern_error(service, monkeypatch, tmp_path):
    _stub_active_workspace(monkeypatch, service, str(tmp_path))

    assert service.search_workspace("") == {"ok": False, "error": "invalid pattern"}


@requires_grep
def test_match_inside_git_directory_is_still_excluded(service, monkeypatch, tmp_path):
    _seed_tree(tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("needle\n", encoding="utf-8")
    _stub_active_workspace(monkeypatch, service, str(tmp_path))

    assert service.search_workspace("needle")["matches"] == ["src/a.py:1:needle = 1"]
