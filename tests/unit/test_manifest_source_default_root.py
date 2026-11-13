"""Manifest readers resolve the host-state plans root when no ``plans_root`` is given.

``read_plan_manifest`` and ``read_plan_names`` used to require the caller's own
``plans_root`` binding. They now default to the host state's root
(``build_host_state().root()``), which reads ``pipeline.server.PLAN_DIR`` live, so a
test that patches that binding still steers every reader. An explicit
``plans_root`` keeps working and wins over the default.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import manifest_source
from pipeline import server as p
from pipeline.store import FileStore


def _write_manifest(plan_dir: Path, plan_name: str, marker: str) -> None:
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps({"stories": {}, "marker": marker}), encoding="utf-8"
    )


@pytest.fixture
def host_root(tmp_path, monkeypatch):
    """The host-state root: ``pipeline.server.PLAN_DIR`` with the file store."""
    root = tmp_path / "host-plans"
    monkeypatch.setattr(p, "PLAN_DIR", root)
    monkeypatch.setattr(p, "_store", FileStore())
    return root


def test_read_plan_manifest_without_plans_root_reads_the_host_root(host_root):
    _write_manifest(host_root, "plan-a", "host")

    manifest = manifest_source.read_plan_manifest("plan-a", fallback_on_error=False)

    assert manifest["marker"] == "host"


def test_read_plan_manifest_explicit_plans_root_wins_over_the_host_root(
    host_root, tmp_path
):
    _write_manifest(host_root, "plan-a", "host")
    explicit = tmp_path / "explicit"
    _write_manifest(explicit, "plan-a", "explicit")

    manifest = manifest_source.read_plan_manifest(
        "plan-a", fallback_on_error=False, plans_root=explicit
    )

    assert manifest["marker"] == "explicit"


def test_read_plan_manifest_missing_plan_under_host_root_is_none(host_root):
    host_root.mkdir(parents=True)

    assert manifest_source.read_plan_manifest("absent", fallback_on_error=False) is None


def test_read_plan_manifest_unreadable_file_fails_closed_without_root(host_root):
    host_root.mkdir(parents=True)
    (host_root / "corrupt.manifest.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError):
        manifest_source.read_plan_manifest("corrupt", fallback_on_error=False)


def test_read_plan_manifest_unreadable_file_degrades_to_none_without_root(host_root):
    host_root.mkdir(parents=True)
    (host_root / "corrupt.manifest.json").write_text("{not json", encoding="utf-8")

    assert manifest_source.read_plan_manifest("corrupt", fallback_on_error=True) is None


def test_read_plan_names_without_plans_root_lists_the_host_root(host_root):
    _write_manifest(host_root, "b-plan", "host")
    _write_manifest(host_root, "a-plan", "host")

    assert manifest_source.read_plan_names(fallback_on_error=False) == ["a-plan", "b-plan"]


def test_read_plan_names_explicit_plans_root_wins_over_the_host_root(host_root, tmp_path):
    _write_manifest(host_root, "host-plan", "host")
    explicit = tmp_path / "explicit"
    _write_manifest(explicit, "explicit-plan", "explicit")

    names = manifest_source.read_plan_names(
        fallback_on_error=False, plans_root=explicit
    )

    assert names == ["explicit-plan"]


def test_read_plan_names_without_plans_root_and_no_plans_is_empty(host_root):
    host_root.mkdir(parents=True)

    assert manifest_source.read_plan_names(fallback_on_error=False) == []


def test_read_plan_names_unreadable_root_fails_closed_without_root(host_root, monkeypatch):
    host_root.mkdir(parents=True)

    def unreadable(self, pattern):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "glob", unreadable)

    with pytest.raises(PermissionError):
        manifest_source.read_plan_names(fallback_on_error=False)


def test_read_plan_names_unreadable_root_degrades_to_empty_without_root(host_root, monkeypatch):
    host_root.mkdir(parents=True)

    def unreadable(self, pattern):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "glob", unreadable)

    assert manifest_source.read_plan_names(fallback_on_error=True) == []
