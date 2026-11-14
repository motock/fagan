"""test_scope and grade_xdist_workers resolve through the tenant settings Store.

The Store is the real server record store under ``tmp_path``. The policy
provider is the only external boundary faked here (org layer); the resolver
runs for real. Each test names the (namespace, team, repo) context it resolves.
"""

import pytest

from pipeline import detached_grade, persistence, server, settings_store
from pipeline import test_scope_dispatch as tsd

REPO_URL = "https://github.com/acme/app"
REPO = "github.com/acme/app"
OTHER_REPO_URL = "https://github.com/acme/billing"


@pytest.fixture(autouse=True)
def plan_dir(tmp_path, monkeypatch):
    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(server, "PLAN_DIR", plans)
    monkeypatch.setattr(persistence, "PLAN_DIR", plans)
    monkeypatch.delenv(tsd.TEST_SCOPE_ENV, raising=False)
    monkeypatch.delenv("PIPELINE_GRADE_XDIST_WORKERS", raising=False)
    monkeypatch.setattr(settings_store, "provider_configured", lambda: False)
    return plans


@pytest.fixture
def records():
    return server._store.records()


def _put(records, scope, scope_id="", repo="", values=None, allow_loosen=None, namespace="default"):
    records.put_settings(
        namespace,
        scope,
        scope_id,
        repo,
        values or {},
        allow_loosen or [],
        "ops",
        None,
    )


def _org_policy(monkeypatch, org_layer):
    """Stand in for the operator's policy provider with one org layer."""
    monkeypatch.setattr(settings_store, "provider_configured", lambda: True)
    monkeypatch.setattr(
        settings_store,
        "policy_settings",
        lambda namespace, team: {"org": org_layer},
    )


def _manifest(team="alpha", repo_url=REPO_URL, **extra):
    return {"team": team, "repo_url": repo_url, **extra}


# ---------- test_scope: team_repo layer ----------


def test_team_repo_scoped_request_is_honoured_for_the_opted_in_team(records):
    _put(records, "team_repo", "alpha", REPO, {"test_scope": "scoped"})

    result = tsd.effective_test_scope(_manifest(team="alpha"))

    assert (result["test_scope"], result["source"]) == ("scoped", "team_repo")


def test_team_repo_scoped_does_not_apply_to_another_team_on_the_same_repo(records):
    _put(records, "team_repo", "alpha", REPO, {"test_scope": "scoped"})

    result = tsd.effective_test_scope(_manifest(team="beta"))

    assert (result["test_scope"], result["source"]) == ("full", "default")


def test_team_repo_scoped_does_not_apply_to_another_repo_for_the_same_team(records):
    _put(records, "team_repo", "alpha", REPO, {"test_scope": "scoped"})

    result = tsd.effective_test_scope(_manifest(team="alpha", repo_url=OTHER_REPO_URL))

    assert (result["test_scope"], result["source"]) == ("full", "default")


def test_org_full_refuses_team_repo_scoped_without_allow_loosen(monkeypatch, records):
    _org_policy(monkeypatch, {"test_scope": "full"})
    _put(records, "team_repo", "alpha", REPO, {"test_scope": "scoped"})

    result = tsd.effective_test_scope(_manifest(team="alpha"))

    assert result["test_scope"] == "full"
    assert result["source"] == "org"
    assert result["lock_rejections"] == [
        {"layer": "team_repo", "requested": "scoped", "by": "org"}
    ]


def test_org_full_allows_team_repo_scoped_when_org_lists_test_scope(monkeypatch, records):
    _org_policy(monkeypatch, {"test_scope": "full", "allow_loosen": ["test_scope"]})
    _put(records, "team_repo", "alpha", REPO, {"test_scope": "scoped"})

    result = tsd.effective_test_scope(_manifest(team="alpha"))

    assert (result["test_scope"], result["source"]) == ("scoped", "team_repo")
    assert result["lock_rejections"] == []


def test_namespace_comes_from_the_manifest(monkeypatch):
    # The file store serves only the default namespace, so assert the context
    # handed to the settings read rather than a stored record.
    reads = []

    def _read(namespace, *, team, repo):
        reads.append((namespace, team, repo))
        return {}

    monkeypatch.setattr(tsd, "read_layers", _read)

    tsd.effective_test_scope(_manifest(team="alpha", namespace="staging"))
    tsd.effective_test_scope(_manifest(team="alpha"))

    assert reads == [("staging", "alpha", REPO), ("default", "alpha", REPO)]


def test_env_request_is_no_longer_a_source(monkeypatch, records):
    monkeypatch.setenv(tsd.TEST_SCOPE_ENV, "scoped")

    result = tsd.effective_test_scope(_manifest())

    assert (result["test_scope"], result["source"]) == ("full", "default")


def test_plan_request_still_applies_when_no_policy_refuses_it(records):
    result = tsd.effective_test_scope(_manifest(test_scope="scoped"))

    assert (result["test_scope"], result["source"]) == ("scoped", "plan")


def test_plan_scoped_is_refused_by_team_repo_full(records):
    _put(records, "team_repo", "alpha", REPO, {"test_scope": "full"})

    result = tsd.effective_test_scope(_manifest(team="alpha", test_scope="scoped"))

    assert result["test_scope"] == "full"
    assert result["source"] == "team_repo"
    assert result["lock_rejections"] == [
        {"layer": "plan", "requested": "scoped", "by": "team_repo"}
    ]


# ---------- grade_xdist_workers: ceiling across layers ----------


def test_grade_workers_team_repo_tightens_deployment(records):
    _put(records, "deployment", values={"grade_xdist_workers": 4})
    _put(records, "team_repo", "alpha", REPO, {"grade_xdist_workers": 2})

    assert detached_grade.xdist_workers(_manifest(team="alpha")) == 2


def test_grade_workers_team_repo_cannot_loosen_deployment_without_allow(records):
    _put(records, "deployment", values={"grade_xdist_workers": 4})
    _put(records, "team_repo", "alpha", REPO, {"grade_xdist_workers": 8})

    assert detached_grade.xdist_workers(_manifest(team="alpha")) == 4


def test_grade_workers_unset_everywhere_leaves_the_command_alone(records):
    assert detached_grade.xdist_workers(_manifest()) is None


def test_grade_workers_env_is_not_a_source(monkeypatch, records):
    monkeypatch.setenv("PIPELINE_GRADE_XDIST_WORKERS", "3")

    assert detached_grade.xdist_workers(_manifest()) is None
