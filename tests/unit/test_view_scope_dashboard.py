"""The dashboard honours the view-scope seam (es-04).

Plans are stored per namespace, so a plan view is in scope exactly when the
namespace the request is served in is in the principal's view scope. The gate
is one middleware on ``/api/plans`` and ``/api/config/plans``: it runs before any
plan route, so the plan list and every per-plan read and write share one rule.

A denial is an explicit 403 carrying the reason token, and it writes one host
audit record. With no plugin configured the default scope is the namespace the
resolver reports, so the gate lets every request through.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import dashboard as d
from app import dashboard_routes_plan as routes_plan
from app.identity import LOCAL_OPERATOR
from app.plugin_loader import ALLOWLIST_ENV
from pipeline import view_scope
from pipeline.store_records import DEFAULT_NAMESPACE
from pipeline.view_scope import VIEW_SCOPE_PLUGIN_ENV

DENIED = "view_scope_denied"


@pytest.fixture(autouse=True)
def _clear_view_scope(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(VIEW_SCOPE_PLUGIN_ENV, raising=False)
    monkeypatch.delenv(ALLOWLIST_ENV, raising=False)
    view_scope._PLUGIN_SCOPES.clear()
    yield
    view_scope._PLUGIN_SCOPES.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(d.app)


@pytest.fixture
def audited(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    records: list[dict] = []
    monkeypatch.setattr(
        routes_plan,
        "append_host_audit",
        lambda **kwargs: records.append(kwargs),
        raising=False,
    )
    return records


def _install_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    name = f"view_scope_dash_{uuid.uuid4().hex}"
    (tmp_path / f"{name}.py").write_text(body)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv(VIEW_SCOPE_PLUGIN_ENV, f"{name}:build")
    monkeypatch.setenv(ALLOWLIST_ENV, name)


def _scope_returning(value_source: str) -> str:
    return (
        "from pipeline.view_scope import ALL\n\n\n"
        "class Scope:\n"
        "    def visible(self, principal):\n"
        f"        return {value_source}\n\n\n"
        "def build():\n"
        "    return Scope()\n"
    )


def _hide_default_namespace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_scope(tmp_path, monkeypatch, _scope_returning('frozenset({"alpha"})'))


def _assert_denied(response) -> None:
    assert response.status_code == 403
    assert response.json()["detail"] == DENIED


class TestDefaultScopeChangesNothing:
    def test_plan_list_is_served_with_no_plugin(self, client, audited) -> None:
        response = client.get("/api/plans")

        assert response.status_code == 200
        assert "plans" in response.json()
        assert audited == []

    def test_per_plan_read_reaches_its_route_with_no_plugin(
        self, client, audited
    ) -> None:
        response = client.get("/api/plans/no-such-plan/metrics")

        assert response.status_code == 404
        assert audited == []

    def test_per_plan_write_reaches_its_route_with_no_plugin(
        self, client, audited
    ) -> None:
        response = client.post("/api/plans/no-such-plan/archive")

        assert response.status_code == 404
        assert audited == []

    def test_default_scope_admits_the_default_namespace(self) -> None:
        assert view_scope.can_view(LOCAL_OPERATOR, DEFAULT_NAMESPACE) is True


class TestStubScopeDeniesOutsideItsNamespaces:
    def test_plan_list_is_denied_outside_the_view_scope(
        self, client, audited, tmp_path, monkeypatch
    ) -> None:
        _hide_default_namespace(tmp_path, monkeypatch)

        response = client.get("/api/plans")

        _assert_denied(response)
        assert "plans" not in response.json()

    def test_per_plan_read_is_denied_and_never_reaches_the_service(
        self, client, audited, tmp_path, monkeypatch
    ) -> None:
        _hide_default_namespace(tmp_path, monkeypatch)

        def must_not_run(*args, **kwargs):
            raise AssertionError("per-plan read reached the service")

        monkeypatch.setattr(d._service, "get_manifest_or_none", must_not_run)

        _assert_denied(client.get("/api/plans/some-plan/metrics"))

    def test_per_plan_write_is_denied_and_never_reaches_the_service(
        self, client, audited, tmp_path, monkeypatch
    ) -> None:
        _hide_default_namespace(tmp_path, monkeypatch)

        def must_not_run(*args, **kwargs):
            raise AssertionError("per-plan write reached the service")

        monkeypatch.setattr(d._service, "advance_pipeline", must_not_run)

        _assert_denied(client.post("/api/plans/some-plan/advance"))

    def test_config_plan_route_is_denied_outside_the_view_scope(
        self, client, audited, tmp_path, monkeypatch
    ) -> None:
        _hide_default_namespace(tmp_path, monkeypatch)

        response = client.post(
            "/api/config/plans/some-plan/roles/review",
            json={"provider": "claude", "model": "x"},
        )

        _assert_denied(response)

    def test_each_denial_writes_one_audit_record_with_the_reason(
        self, client, audited, tmp_path, monkeypatch
    ) -> None:
        _hide_default_namespace(tmp_path, monkeypatch)

        client.get("/api/plans/some-plan/metrics")

        assert len(audited) == 1
        assert audited[0]["outcome"] == "denied"
        assert audited[0]["detail"]["reason"] == DENIED

    def test_audit_record_does_not_carry_the_plan_name(
        self, client, audited, tmp_path, monkeypatch
    ) -> None:
        _hide_default_namespace(tmp_path, monkeypatch)

        client.get("/api/plans/secret-plan-name/metrics")

        assert "secret-plan-name" not in repr(audited)

    def test_a_scope_naming_the_default_namespace_lets_plan_routes_through(
        self, client, audited, tmp_path, monkeypatch
    ) -> None:
        _install_scope(
            tmp_path,
            monkeypatch,
            _scope_returning(f'frozenset({{"{DEFAULT_NAMESPACE}", "alpha"}})'),
        )

        assert client.get("/api/plans").status_code == 200
        assert audited == []

    def test_an_all_scope_lets_plan_routes_through(
        self, client, audited, tmp_path, monkeypatch
    ) -> None:
        _install_scope(tmp_path, monkeypatch, _scope_returning("ALL"))

        response = client.get("/api/plans/no-such-plan/metrics")

        assert response.status_code == 404
        assert audited == []


class TestFailClosed:
    def test_a_scope_that_raises_denies_instead_of_erroring_open(
        self, client, audited, tmp_path, monkeypatch
    ) -> None:
        _install_scope(
            tmp_path,
            monkeypatch,
            "class Scope:\n"
            "    def visible(self, principal):\n"
            "        raise RuntimeError('backend down')\n\n\n"
            "def build():\n"
            "    return Scope()\n",
        )

        _assert_denied(client.get("/api/plans"))
        assert audited[0]["detail"]["reason"] == "view_scope_error"

    def test_an_empty_answer_denies_rather_than_reading_as_an_empty_view(
        self, client, audited, tmp_path, monkeypatch
    ) -> None:
        _install_scope(tmp_path, monkeypatch, _scope_returning("frozenset()"))

        _assert_denied(client.get("/api/plans"))

    def test_a_spec_without_the_allowlist_denies_every_plan_route(
        self, client, audited, monkeypatch
    ) -> None:
        monkeypatch.setenv(VIEW_SCOPE_PLUGIN_ENV, "some_module:build")

        _assert_denied(client.get("/api/plans"))
        _assert_denied(client.get("/api/plans/some-plan/metrics"))


class TestRoutesOutsideThePlanScopeAreNotGated:
    def test_health_is_not_gated_by_the_plan_view_scope(
        self, client, audited, tmp_path, monkeypatch
    ) -> None:
        _hide_default_namespace(tmp_path, monkeypatch)

        response = client.get("/api/health")

        assert response.json().get("detail") != DENIED
        assert audited == []
