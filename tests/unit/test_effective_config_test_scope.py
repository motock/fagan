"""get_effective_config reports the plan-level resolved test_scope.

The policy provider, the plan store and the environment are the external
boundaries here; the resolver runs for real. The block is the plan-level
resolution only (no story request), so it carries the same four keys as
pipeline.test_scope.resolve_test_scope plus the env var name.
"""

import json
from pathlib import Path

import pytest

from pipeline import config_provenance
from pipeline import persistence as ppers
from pipeline import server as p
from pipeline import test_scope_dispatch as tsd

REFERENCE_MD = Path(__file__).resolve().parents[2] / "REFERENCE.md"


@pytest.fixture(autouse=True)
def _isolated_scope_inputs(tmp_path, monkeypatch):
    monkeypatch.delenv(tsd.TEST_SCOPE_ENV, raising=False)
    monkeypatch.setattr(tsd, "policy_settings", lambda namespace, team: {})
    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", plans)
    monkeypatch.setattr(ppers, "PLAN_DIR", plans)
    return plans


def _write_manifest(plans, plan_name, **fields):
    (plans / f"{plan_name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": {}, "repo_root": "/tmp", **fields})
    )


# ---------- 1. No plan: default resolution ----------


def test_no_plan_reports_default_full_scope():
    result = p.get_effective_config()

    assert result["test_scope"] == {
        "test_scope": "full",
        "source": "default",
        "lock_rejections": [],
        "invalid_layers": [],
        "env": "PIPELINE_TEST_SCOPE",
    }


def test_no_plan_reports_env_source_when_env_requests_scoped(monkeypatch):
    monkeypatch.setenv(tsd.TEST_SCOPE_ENV, "scoped")

    result = p.get_effective_config()

    assert result["test_scope"]["test_scope"] == "scoped"
    assert result["test_scope"]["source"] == "env"


def test_no_plan_unknown_env_value_resolves_to_full_and_is_reported(monkeypatch):
    monkeypatch.setenv(tsd.TEST_SCOPE_ENV, "partial")

    result = p.get_effective_config()

    assert result["test_scope"]["test_scope"] == "full"
    assert result["test_scope"]["invalid_layers"] == ["env"]


# ---------- 2. Plan manifest layer ----------


def test_plan_manifest_scoped_reports_plan_source(_isolated_scope_inputs):
    _write_manifest(_isolated_scope_inputs, "scopeplan", test_scope="scoped")

    result = p.get_effective_config(plan_name="scopeplan")

    assert result["test_scope"]["test_scope"] == "scoped"
    assert result["test_scope"]["source"] == "plan"


def test_plan_manifest_full_does_not_report_scoped(_isolated_scope_inputs, monkeypatch):
    monkeypatch.setenv(tsd.TEST_SCOPE_ENV, "scoped")
    _write_manifest(_isolated_scope_inputs, "fullplan", test_scope="full")

    result = p.get_effective_config(plan_name="fullplan")

    assert result["test_scope"]["test_scope"] == "full"
    assert result["test_scope"]["source"] == "plan"


# ---------- 3. Org policy lock ----------


def test_org_full_policy_rejects_plan_scoped_request(
    _isolated_scope_inputs, monkeypatch
):
    monkeypatch.setattr(
        tsd,
        "policy_settings",
        lambda namespace, team: {"org": {"test_scope": "full"}, "team": {}},
    )
    _write_manifest(_isolated_scope_inputs, "lockedplan", test_scope="scoped")

    result = p.get_effective_config(plan_name="lockedplan")

    assert result["test_scope"]["test_scope"] == "full"
    assert result["test_scope"]["source"] == "org"
    assert result["test_scope"]["lock_rejections"] == [
        {"layer": "plan", "requested": "scoped", "by": "org"}
    ]


# ---------- 4. Unknown plan and missing manifest ----------


def test_unknown_plan_resolves_to_default_without_raising():
    result = p.get_effective_config(plan_name="no-such-plan")

    assert result["test_scope"]["test_scope"] == "full"
    assert result["test_scope"]["source"] == "default"


# ---------- 5. Catalog and documentation ----------


def test_test_scope_env_var_is_cataloged_with_full_default():
    catalog = {spec.name: spec.default for spec in config_provenance.ENV_VAR_CATALOG}

    assert catalog["PIPELINE_TEST_SCOPE"] == "full"


def test_reference_documents_effective_config_test_scope_block():
    text = REFERENCE_MD.read_text()

    assert (
        "`get_effective_config` reports a `test_scope` block: the plan-level resolution\n"
        "(without any story's own request) with its `source`, `lock_rejections` and\n"
        "`invalid_layers`."
    ) in text
