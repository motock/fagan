"""SA-01: remote callers see role routing only from ``get_effective_config``.

A remote caller needs the plan's role routing and nothing else: host
environment values (``env``, ``ignored_env_vars``) and filesystem paths
(``sources``) are host-private. Trusted local principals (``LOCAL_OPERATOR``
over stdio/the dashboard, ``SYSTEM_SCHEDULER``/``SYSTEM_WORKER`` in the daemon)
keep the full result.

The gate is deny-by-default: only the auth methods that identify a caller
already running as the operator on this host are trusted. An unauthenticated
principal (``ANONYMOUS``, ``auth_method="none"``) and an unknown or missing
auth method are treated as remote, so a future network transport can never
inherit the full host config by simply not being OIDC.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.identity import (
    LOCAL_OPERATOR,
    SYSTEM_SCHEDULER,
    SYSTEM_WORKER,
    Principal,
)
from pipeline import actor, remote_scope, service
from pipeline.actor import ANONYMOUS
from pipeline.service import PipelineService

OIDC_PRINCIPAL = Principal(subject="remote-alice", auth_method="oidc")

#: A stand-in for the full host-visible result. The env value is obviously
#: fake and is never printed by these tests.
FULL_CONFIG = {
    "ok": True,
    "roles": ["r1"],
    "env": {"API_KEY": "not-a-real-secret"},
    "ignored_env_vars": ["IGNORED"],
    "sources": {
        "launchd_plist": "/x/launchd.plist",
        "mcp_config": "/x/mcp.json",
        "registry": "/x/registry.json",
    },
}

REFUSAL = {"ok": False, "error": "plan_name is required for a remote caller"}


# --- is_network_principal -------------------------------------------------


def test_is_network_principal_true_for_oidc():
    assert remote_scope.is_network_principal(OIDC_PRINCIPAL) is True


@pytest.mark.parametrize("principal", [LOCAL_OPERATOR, SYSTEM_SCHEDULER, SYSTEM_WORKER])
def test_is_network_principal_false_for_trusted_local_principals(principal):
    assert remote_scope.is_network_principal(principal) is False


@pytest.mark.parametrize(
    ("label", "principal"),
    [
        ("unauthenticated", ANONYMOUS),
        # A future network transport may bind a principal that is not OIDC.
        ("unknown-auth-method", Principal(subject="remote-bob", auth_method="token")),
        ("unset-auth-method", Principal(subject="remote-carol")),
        ("no-principal", None),
    ],
)
def test_is_network_principal_true_for_everything_else(label, principal):
    """Deny by default: anything not provably local is treated as remote."""
    assert remote_scope.is_network_principal(principal) is True, label


# --- remote_effective_config ---------------------------------------------


def test_remote_effective_config_refuses_missing_plan_name():
    assert remote_scope.remote_effective_config(FULL_CONFIG, None) == REFUSAL


def test_remote_effective_config_refuses_empty_plan_name():
    assert remote_scope.remote_effective_config(FULL_CONFIG, "") == REFUSAL


def test_remote_effective_config_returns_only_role_routing():
    result = remote_scope.remote_effective_config(FULL_CONFIG, "nightly")
    assert result == {"ok": True, "roles": ["r1"]}


def test_remote_effective_config_refuses_a_failed_result():
    """Never relay ``{"ok": None, "roles": None}`` to a remote caller."""
    result = remote_scope.remote_effective_config(
        {"ok": False, "error": "unknown plan"}, "nightly"
    )
    assert result["ok"] is False
    assert set(result) == {"ok", "error"}
    assert "env" not in result


def test_remote_effective_config_refuses_a_result_without_ok():
    result = remote_scope.remote_effective_config({"roles": ["r1"]}, "nightly")
    assert result["ok"] is False
    assert set(result) == {"ok", "error"}


# --- get_effective_config through the service ----------------------------


def test_remote_caller_without_plan_name_is_refused_without_reading_config():
    pipeline_service = PipelineService()
    with (
        patch.object(service, "_get_effective_config_impl") as impl,
        actor.bind_principal(OIDC_PRINCIPAL),
    ):
        result = pipeline_service.get_effective_config(None)
    assert result == REFUSAL
    assert impl.call_count == 0


def test_remote_caller_with_plan_name_sees_only_role_routing():
    pipeline_service = PipelineService()
    with (
        patch.object(service, "_get_effective_config_impl") as impl,
        actor.bind_principal(OIDC_PRINCIPAL),
    ):
        impl.return_value = FULL_CONFIG
        result = pipeline_service.get_effective_config("nightly")
    assert set(result) == {"ok", "roles"}
    assert "env" not in result
    assert "ignored_env_vars" not in result
    assert "sources" not in result
    assert impl.call_count == 1


def test_unauthenticated_caller_sees_only_role_routing():
    """ANONYMOUS is not local: a dashboard pass-through must not read env."""
    pipeline_service = PipelineService()
    with (
        patch.object(service, "_get_effective_config_impl") as impl,
        actor.bind_principal(ANONYMOUS),
    ):
        impl.return_value = FULL_CONFIG
        result = pipeline_service.get_effective_config("nightly")
    assert set(result) == {"ok", "roles"}
    assert "env" not in result
    assert "sources" not in result


def test_unknown_auth_method_sees_only_role_routing():
    pipeline_service = PipelineService()
    principal = Principal(subject="remote-bob", auth_method="token")
    with (
        patch.object(service, "_get_effective_config_impl") as impl,
        actor.bind_principal(principal),
    ):
        impl.return_value = FULL_CONFIG
        result = pipeline_service.get_effective_config("nightly")
    assert set(result) == {"ok", "roles"}
    assert "env" not in result


def test_local_operator_sees_full_config():
    pipeline_service = PipelineService()
    with (
        patch.object(service, "_get_effective_config_impl") as impl,
        actor.bind_principal(LOCAL_OPERATOR),
    ):
        impl.return_value = FULL_CONFIG
        result = pipeline_service.get_effective_config("nightly")
    assert result == FULL_CONFIG
    assert impl.call_count == 1


def test_system_scheduler_sees_full_config():
    pipeline_service = PipelineService()
    with (
        patch.object(service, "_get_effective_config_impl") as impl,
        actor.bind_principal(SYSTEM_SCHEDULER),
    ):
        impl.return_value = FULL_CONFIG
        result = pipeline_service.get_effective_config("nightly")
    assert result == FULL_CONFIG
    assert impl.call_count == 1


def test_system_worker_sees_full_config():
    pipeline_service = PipelineService()
    with (
        patch.object(service, "_get_effective_config_impl") as impl,
        actor.bind_principal(SYSTEM_WORKER),
    ):
        impl.return_value = FULL_CONFIG
        result = pipeline_service.get_effective_config("nightly")
    assert result == FULL_CONFIG
    assert impl.call_count == 1


# --- the dashboard's /api/config entry point -----------------------------
#
# The dashboard sits behind the proxy too (STANDALONE_SERVER_PLAN SA-15/SA-16)
# and its middleware binds the authenticated principal for the whole request,
# so an OIDC browser request reaches effective_config() as a network caller.
# Without the same gate there, the policy above is bypassable.

FAKE_ROLES = [
    {"role": "review", "provider": "mlx", "provider_source": "registry"},
]


@pytest.mark.parametrize(
    ("label", "principal"),
    [("oidc", OIDC_PRINCIPAL), ("unauthenticated", ANONYMOUS)],
)
def test_dashboard_config_hides_host_detail_from_a_network_caller(label, principal):
    import json

    from app import dashboard_routes, role_registry
    from pipeline import config_provenance

    with (
        patch.object(
            config_provenance, "effective_role_config", return_value=FAKE_ROLES
        ),
        patch.object(config_provenance, "effective_env_config") as env_mock,
        patch.object(config_provenance, "ignored_env_vars_present") as ignored_mock,
        patch.object(role_registry, "load_registry", return_value={}),
        actor.bind_principal(principal),
    ):
        env_mock.return_value = {"API_KEY": "not-a-real-secret"}
        ignored_mock.return_value = ["IGNORED"]
        result = dashboard_routes.effective_config()

    assert set(result) == {"roles"}, label
    assert result["roles"] == FAKE_ROLES
    assert "env" not in result
    assert "ignored_env_vars" not in result
    assert "sources" not in result
    # The host environment is not merely withheld: it is never resolved, so a
    # network caller cannot even observe which variables are configured.
    assert env_mock.call_count == 0
    assert ignored_mock.call_count == 0
    assert "not-a-real-secret" not in json.dumps(result)


def test_dashboard_config_keeps_host_detail_for_the_local_operator():
    import json

    from app import dashboard_routes, role_registry
    from pipeline import config_provenance

    with (
        patch.object(
            config_provenance, "effective_role_config", return_value=FAKE_ROLES
        ),
        patch.object(
            config_provenance,
            "effective_env_config",
            return_value={"API_KEY": "not-a-real-secret"},
        ),
        patch.object(config_provenance, "ignored_env_vars_present", return_value=["I"]),
        patch.object(role_registry, "load_registry", return_value={}),
        actor.bind_principal(LOCAL_OPERATOR),
    ):
        result = dashboard_routes.effective_config()

    assert set(result) == {"roles", "env", "ignored_env_vars", "sources"}
    assert result["roles"] == FAKE_ROLES
    assert result["env"] == {"API_KEY": "not-a-real-secret"}
    assert json.dumps(result)
