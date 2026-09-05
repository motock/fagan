"""Unit tests for wiring `_lint_acceptance_fixtures` and
`_pytest_acceptance_fixtures` into `_ingest_plan_impl`, gated by the
opt-in `PIPELINE_ACCEPTANCE_FIXTURE_VALIDATE_BLOCK` env var.

Both validators already exist in `pipeline/build_detect.py` with a
`("clean" | "finding" | "skipped", message)` contract - this story only
wires them into ingest, it does not reimplement or modify them. Both are
stubbed via monkeypatch here so these tests are hermetic (no real ruff/
pytest subprocess). Follows the fixture pattern of
`tests/unit/test_ingest_plan_risk_lock.py`: a local `plan_dir` fixture
patches `pipeline.server.PLAN_DIR`, Plane is stubbed to explode if called
(PLANE_* env is unset, so the NullTicketProvider path is what's under
test), and `_notify_user` is captured into a list.
"""
import inspect
import json

import pytest

from pipeline import server as p
from pipeline import ticketing as pt


def _explode_plane(*a, **kw):
    raise AssertionError("Plane should not be called - PLANE_* env is unset in tests")


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    return d


@pytest.fixture
def captured_notifications(monkeypatch):
    captured = []
    monkeypatch.setattr(p, "_notify_user", lambda *a, **kw: captured.append(a))
    return captured


def _plan(tmp_path, fixture_path="tests/acceptance_bad.py"):
    return {
        "repo_root": str(tmp_path),
        "epics": [
            {
                "summary": "E1",
                "stories": [
                    {
                        "summary": "Story with a fixture",
                        "key": "S1",
                        "agent_instructions": "do the thing",
                        "acceptance": [
                            {"path": fixture_path, "source": "def test_x():\n    assert True\n"}
                        ],
                    }
                ],
            }
        ],
    }


FINDING = ("finding", "fixture path tests/acceptance_bad.py has lint violations")
SKIPPED = ("skipped", "tooling unavailable")
CLEAN = ("clean", None)


def _stub_validators(monkeypatch, lint_result, pytest_result):
    monkeypatch.setattr(p, "_lint_acceptance_fixtures", lambda *a, **kw: lint_result)
    monkeypatch.setattr(p, "_pytest_acceptance_fixtures", lambda *a, **kw: pytest_result)


def test_finding_with_block_unset_is_advisory_only(
    plan_dir, tmp_path, monkeypatch, captured_notifications
):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    monkeypatch.delenv("PIPELINE_ACCEPTANCE_FIXTURE_VALIDATE_BLOCK", raising=False)
    _stub_validators(monkeypatch, FINDING, CLEAN)

    (plan_dir / "gate1.json").write_text(json.dumps(_plan(tmp_path)))
    result = p.ingest_plan("gate1")

    assert result["ok"] is True, result
    assert any("S1" in str(a) for a in captured_notifications), captured_notifications
    assert (plan_dir / "gate1.manifest.json").exists()


def test_finding_with_block_enabled_fails_closed_before_write(
    plan_dir, tmp_path, monkeypatch, captured_notifications
):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    monkeypatch.setenv("PIPELINE_ACCEPTANCE_FIXTURE_VALIDATE_BLOCK", "1")
    _stub_validators(monkeypatch, FINDING, CLEAN)

    (plan_dir / "gate2.json").write_text(json.dumps(_plan(tmp_path)))
    result = p.ingest_plan("gate2")

    assert result["ok"] is False, result
    assert "S1" in result["error"]
    assert "tests/acceptance_bad.py" in result["error"]
    assert not (plan_dir / "gate2.manifest.json").exists()


def test_skipped_result_never_blocks_even_with_block_enabled(
    plan_dir, tmp_path, monkeypatch, captured_notifications
):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    monkeypatch.setenv("PIPELINE_ACCEPTANCE_FIXTURE_VALIDATE_BLOCK", "1")
    _stub_validators(monkeypatch, SKIPPED, SKIPPED)

    (plan_dir / "gate3.json").write_text(json.dumps(_plan(tmp_path)))
    result = p.ingest_plan("gate3")

    assert result["ok"] is True, result
    assert (plan_dir / "gate3.manifest.json").exists()


def test_clean_result_with_block_enabled_writes_manifest(
    plan_dir, tmp_path, monkeypatch, captured_notifications
):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    monkeypatch.setenv("PIPELINE_ACCEPTANCE_FIXTURE_VALIDATE_BLOCK", "1")
    _stub_validators(monkeypatch, CLEAN, CLEAN)

    (plan_dir / "gate4.json").write_text(json.dumps(_plan(tmp_path)))
    result = p.ingest_plan("gate4")

    assert result["ok"] is True, result
    assert (plan_dir / "gate4.manifest.json").exists()


def test_block_env_set_to_zero_is_non_blocking(
    plan_dir, tmp_path, monkeypatch, captured_notifications
):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    monkeypatch.setenv("PIPELINE_ACCEPTANCE_FIXTURE_VALIDATE_BLOCK", "0")
    _stub_validators(monkeypatch, FINDING, CLEAN)

    (plan_dir / "gate5.json").write_text(json.dumps(_plan(tmp_path)))
    result = p.ingest_plan("gate5")

    assert result["ok"] is True, result
    assert (plan_dir / "gate5.manifest.json").exists()


def test_the_validators_are_wired_into_ingest_plan():
    # A validator nothing calls flags nothing - see
    # test_acceptance_oracle_platform_lint.py::test_the_lint_is_wired_into_ingest_plan
    # for the sibling pattern this mirrors.
    src = inspect.getsource(p._ingest_plan_impl)
    assert "_lint_acceptance_fixtures" in src
    assert "_pytest_acceptance_fixtures" in src
