"""Shared fixtures/helpers for the dashboard test suite, split across
test_dashboard_*.py files (originally one 2,397-line test_dashboard.py) to
keep each file under the project's line-count target.
"""
import json

import pytest
from fastapi.testclient import TestClient

from app import dashboard as d


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "PLAN_DIR", tmp_path)
    from pipeline import server as _srv
    monkeypatch.setattr(_srv, "PLAN_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def client():
    return TestClient(d.app)


def _write_manifest(plan_dir, name, stories, epics=None, paused=False):
    manifest = {"epics": epics or {}, "stories": stories}
    if paused:
        manifest["paused"] = True
    (plan_dir / f"{name}.manifest.json").write_text(json.dumps(manifest))


__all__ = ["_write_manifest", "client", "plan_dir"]
