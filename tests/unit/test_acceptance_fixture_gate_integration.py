"""Acceptance oracle for the ingest-time acceptance-fixture validation
 gate (blocking mode).

 A plan whose acceptance fixture carries a real ruff violation (the exact
 2026-08-05 / PR #235 class: an unused import in a fixture the dispatched
 agent is forbidden from editing) must make ingest_plan FAIL CLOSED when
 PIPELINE_ACCEPTANCE_FIXTURE_VALIDATE_BLOCK=1 -- driven through the real
 ingest_plan entry point with real ruff/pytest subprocesses, not by calling
 the validator functions directly. A validator nothing calls, or a gate
 tested only through stubs, flags and blocks nothing.
 """
import json
import shutil

import pytest
from pipeline import server as p
from pipeline import ticketing as pt

BROKEN_FIXTURE_SOURCE = (
    "import os\n"
    "\n"
    "\n"
    "def test_placeholder():\n"
    "    assert True\n"
)  # ruff F401: unused import -- the PR #235 violation class.


def _explode_plane(*a, **kw):
    raise AssertionError("Plane should not be called - PLANE_* env is unset in tests")


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    return d


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff not installed")
def test_ingest_fails_closed_on_lint_broken_fixture_in_blocking_mode(
    plan_dir, tmp_path, monkeypatch
):
    captured = []
    monkeypatch.setattr(p, "_notify_user", lambda *a, **kw: captured.append(a))
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    monkeypatch.setenv("PIPELINE_ACCEPTANCE_FIXTURE_VALIDATE_BLOCK", "1")

    plan = {
        "repo_root": str(tmp_path),
        "epics": [
            {
                "summary": "E1",
                "stories": [
                    {
                        "summary": "Story with lint-broken fixture",
                        "agent_instructions": "do the thing",
                        "acceptance": [
                            {
                                "path": "tests/acceptance_broken.py",
                                "source": BROKEN_FIXTURE_SOURCE,
                            }
                        ],
                    }
                ],
            }
        ],
    }
    (plan_dir / "g1gate.json").write_text(json.dumps(plan))

    result = p.ingest_plan("g1gate")

    assert result.get("ok") is False, result
    assert "Story with lint-broken fixture" in result.get("error", "")
    assert "acceptance_broken.py" in result.get("error", "")
    # The block fires before any manifest write.
    assert not (plan_dir / "g1gate.manifest.json").exists()


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff not installed")
def test_ingest_stays_open_in_default_mode_on_lint_broken_fixture(
    plan_dir, tmp_path, monkeypatch
):
    captured = []
    monkeypatch.setattr(p, "_notify_user", lambda *a, **kw: captured.append(a))
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    # Default mode: PIPELINE_ACCEPTANCE_FIXTURE_VALIDATE_BLOCK unset.
    monkeypatch.delenv("PIPELINE_ACCEPTANCE_FIXTURE_VALIDATE_BLOCK", raising=False)

    plan = {
        "repo_root": str(tmp_path),
        "epics": [
            {
                "summary": "E1",
                "stories": [
                    {
                        "summary": "Story with lint-broken fixture",
                        "agent_instructions": "do the thing",
                        "acceptance": [
                            {
                                "path": "tests/acceptance_broken.py",
                                "source": BROKEN_FIXTURE_SOURCE,
                            }
                        ],
                    }
                ],
            }
        ],
    }
    (plan_dir / "g1gate2.json").write_text(json.dumps(plan))

    result = p.ingest_plan("g1gate2")

    assert result.get("ok") is True, result
    assert any("g1gate2" in str(a) or "Story with lint-broken fixture" in str(a) for a in captured), (
        "advisory notification must still fire in non-blocking mode"
    )
    assert (plan_dir / "g1gate2.manifest.json").exists()
