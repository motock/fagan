"""Tests for the orchestrator-neutral probes ``/livez`` and ``/readyz``.

Kubernetes (and any future orchestrator) needs two different questions
answered, and conflating them makes an outage worse:

* ``GET /livez`` -- "restart me". 200 ``{"live": true}`` unless the scheduler
  is stale or unreported *while the deployment requires one*
  (``PIPELINE_HEALTH_REQUIRE_SCHEDULER=1``). It must not depend on NATS,
  Keycloak, the outbox or any other external service: a liveness probe that
  fails on a dependency restarts a healthy process and turns a broker blip into
  a restart storm.
* ``GET /readyz`` -- "do not route traffic to me". 200 ``{"ready": true}`` only
  when the instance is live *and* (when ``PIPELINE_EVENT_BUS=nats``) the broker
  is not reported ``{"reachable": false}`` *and* the notification outbox is not
  stale.

Both live outside ``/api/`` so the dashboard middleware's credential gate --
which requires a bearer token on every ``/api/`` route under
``PIPELINE_IDENTITY_PROVIDER=oidc`` -- never applies: an orchestrator has no
credential to present. The bodies carry a boolean plus verdicts drawn from a
closed set of fixed strings, never a path, count, plan name or version, and a
probe never answers 500.

The ``client`` / ``plan_dir`` names are the shared fixtures from
:mod:`tests.unit._dashboard_helpers`.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import pipeline.namespace_resolver as resolver_module
from app import auth
from app import dashboard as d
from app.identity import IdentityError
from app.oidc import OidcProvider
from app.plugin_loader import ALLOWLIST_ENV
from pipeline import events_nats_consumer, health_freshness, paths
from pipeline.actor import ANONYMOUS, current_correlation_id, current_principal
from pipeline.namespace_resolver import NAMESPACE_RESOLVER_PLUGIN_ENV
from tests.unit._dashboard_helpers import client, plan_dir

__all__ = ["client", "plan_dir"]

REQUIRE_ENV = "PIPELINE_HEALTH_REQUIRE_SCHEDULER"
STALE_ENV = "PIPELINE_HEALTH_STALE_SECONDS"
EVENT_BUS_ENV = "PIPELINE_EVENT_BUS"
HEALTH_FILENAME = ".scheduler_health.json"

#: The closed set of verdict strings a probe body may ever contain.
CLOSED = {
    "scheduler_stale",
    "scheduler_unreported",
    "broker_unreachable",
    "outbox_stale",
    "probe_error",
}


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clear_consumer_status_cache():
    """Empty the module-level broker-status cache around every test.

    ``consumer_status`` holds its answer for 30 s. Without this reset, a test
    that stubs the broker as reachable would leave that entry behind and the
    next test's ``{"reachable": false}`` stub would never be consulted.
    """

    def reset() -> None:
        events_nats_consumer._status_cache["expires_at"] = None
        events_nats_consumer._status_cache["value"] = None

    reset()
    yield
    reset()


def _write_heartbeat(directory, age_seconds: float) -> None:
    """Write a scheduler snapshot whose newest timestamp is already *age_seconds* old."""
    stamp = (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat()
    (directory / HEALTH_FILENAME).write_text(
        json.dumps({"heartbeat_ts": stamp}), encoding="utf-8"
    )


def _write_stale_outbox(directory, age_seconds: float = 1200) -> None:
    """Write one outbox spool record that is *age_seconds* old."""
    stamp = (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat()
    (directory / "alpha.outbox.jsonl").write_text(
        json.dumps({"ts": stamp, "message": "hello"}) + "\n", encoding="utf-8"
    )


def _stub_broker(monkeypatch, *, reachable: bool = True, raises: Exception | None = None):
    """Stub the broker read below ``consumer_status`` and count collections.

    Patching ``_collect_consumer_status`` (not ``consumer_status``) keeps the
    real 30 s cache in the path, so a test can prove the probe reuses it
    instead of dialling the broker on every call.
    """
    calls: list[int] = []

    def _collect() -> dict:
        calls.append(1)
        if raises is not None:
            raise raises
        if reachable:
            return {
                "pipeline-build": {
                    "pending": 0,
                    "redelivered": 0,
                    "dead_lettered": 0,
                    "subscribed": True,
                    "oldest_pending_age_s": None,
                }
            }
        return {"reachable": False}

    monkeypatch.setattr(events_nats_consumer, "_collect_consumer_status", _collect)
    return calls


def _assert_probe(
    response,
    *,
    kind: str,
    ok: bool,
    reasons: list[str] | None = None,
    plan_dir=None,
) -> None:
    """Assert the full probe contract: status, closed-set body and hygiene."""
    key = "live" if kind == "livez" else "ready"
    assert response.status_code == (200 if ok else 503)
    body = response.json()
    if ok:
        assert body == {key: True}
    else:
        assert set(body) == {key, "degraded"}
        assert body[key] is False
        assert isinstance(body["degraded"], list)
        assert set(body["degraded"]) <= CLOSED
        if reasons is not None:
            assert set(body["degraded"]) == set(reasons)
    if plan_dir is not None:
        # Aggregate only: no plan directory, path or name leaks into the body.
        assert str(plan_dir) not in response.text


class _DenyAllOidcProvider(OidcProvider):
    """An ``OidcProvider`` that refuses every credential, with no network I/O."""

    def __init__(self) -> None:
        """Skip the parent's settings requirement: this stub never reads them."""

    def authenticate(self, credential: str | None):
        raise IdentityError("invalid token")


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_both_probes_are_ok_when_scheduler_is_fresh(client, plan_dir, monkeypatch):
    monkeypatch.setenv(REQUIRE_ENV, "1")
    _write_heartbeat(plan_dir, 5)

    _assert_probe(
        client.get("/livez"), kind="livez", ok=True, plan_dir=plan_dir
    )
    _assert_probe(
        client.get("/readyz"), kind="readyz", ok=True, plan_dir=plan_dir
    )


def test_unreported_scheduler_is_legitimate_when_not_required(client, plan_dir, monkeypatch):
    """No daemon and no opt-in: both probes stay green."""
    monkeypatch.delenv(REQUIRE_ENV, raising=False)

    _assert_probe(client.get("/livez"), kind="livez", ok=True, plan_dir=plan_dir)
    _assert_probe(client.get("/readyz"), kind="readyz", ok=True, plan_dir=plan_dir)


# --------------------------------------------------------------------------- #
# Liveness: scheduler staleness only
# --------------------------------------------------------------------------- #


def test_stale_scheduler_makes_liveness_red_when_required(client, plan_dir, monkeypatch):
    monkeypatch.setenv(REQUIRE_ENV, "1")
    monkeypatch.setenv(STALE_ENV, "60")
    _write_heartbeat(plan_dir, 120)

    _assert_probe(
        client.get("/livez"),
        kind="livez",
        ok=False,
        reasons=["scheduler_stale"],
        plan_dir=plan_dir,
    )
    _assert_probe(
        client.get("/readyz"),
        kind="readyz",
        ok=False,
        reasons=["scheduler_stale"],
        plan_dir=plan_dir,
    )


def test_unreported_scheduler_makes_liveness_red_when_required(client, plan_dir, monkeypatch):
    monkeypatch.setenv(REQUIRE_ENV, "1")

    _assert_probe(
        client.get("/livez"),
        kind="livez",
        ok=False,
        reasons=["scheduler_unreported"],
        plan_dir=plan_dir,
    )


def test_probing_does_not_refresh_a_stale_scheduler(client, plan_dir, monkeypatch):
    """A probe is read-only: two calls in a row must both report staleness.

    An implementation that "records success" by touching the snapshot would
    answer 200 to the second call and hide a dead scheduler.
    """
    monkeypatch.setenv(REQUIRE_ENV, "1")
    monkeypatch.setenv(STALE_ENV, "60")
    _write_heartbeat(plan_dir, 120)
    before = (plan_dir / HEALTH_FILENAME).read_text(encoding="utf-8")

    _assert_probe(
        client.get("/livez"), kind="livez", ok=False, reasons=["scheduler_stale"]
    )
    _assert_probe(
        client.get("/livez"), kind="livez", ok=False, reasons=["scheduler_stale"]
    )
    assert (plan_dir / HEALTH_FILENAME).read_text(encoding="utf-8") == before


# --------------------------------------------------------------------------- #
# Liveness must not depend on external services
# --------------------------------------------------------------------------- #


def test_unreachable_broker_degrades_readiness_only(client, plan_dir, monkeypatch):
    monkeypatch.setenv(REQUIRE_ENV, "1")
    monkeypatch.setenv(EVENT_BUS_ENV, "nats")
    _write_heartbeat(plan_dir, 5)
    _stub_broker(monkeypatch, reachable=False)

    _assert_probe(client.get("/livez"), kind="livez", ok=True, plan_dir=plan_dir)
    _assert_probe(
        client.get("/readyz"),
        kind="readyz",
        ok=False,
        reasons=["broker_unreachable"],
        plan_dir=plan_dir,
    )


def test_unreachable_broker_is_ignored_when_the_bus_is_not_nats(
    client, plan_dir, monkeypatch
):
    monkeypatch.setenv(REQUIRE_ENV, "1")
    monkeypatch.setenv(EVENT_BUS_ENV, "inprocess")
    _write_heartbeat(plan_dir, 5)
    _stub_broker(monkeypatch, reachable=False)

    _assert_probe(client.get("/readyz"), kind="readyz", ok=True, plan_dir=plan_dir)


def test_liveness_survives_an_exploding_broker_probe(client, plan_dir, monkeypatch):
    """A broker probe that raises must never restart a healthy process."""
    monkeypatch.setenv(REQUIRE_ENV, "1")
    monkeypatch.setenv(EVENT_BUS_ENV, "nats")
    _write_heartbeat(plan_dir, 5)
    _stub_broker(monkeypatch, raises=RuntimeError("nats down"))

    _assert_probe(client.get("/livez"), kind="livez", ok=True, plan_dir=plan_dir)


def test_outbox_staleness_degrades_readiness_only(client, plan_dir, monkeypatch):
    monkeypatch.setenv(REQUIRE_ENV, "1")
    # The outbox now comes from the record store, which reads the spools
    # under the plans directory: point that binding at the seeded files.
    monkeypatch.setattr(paths, "PLAN_DIR", plan_dir)
    _write_heartbeat(plan_dir, 5)
    _write_stale_outbox(plan_dir)

    _assert_probe(client.get("/livez"), kind="livez", ok=True, plan_dir=plan_dir)
    _assert_probe(
        client.get("/readyz"),
        kind="readyz",
        ok=False,
        reasons=["outbox_stale"],
        plan_dir=plan_dir,
    )


# --------------------------------------------------------------------------- #
# Readiness reuses the 30 s consumer cache
# --------------------------------------------------------------------------- #


def test_readiness_reuses_the_consumer_status_cache(client, plan_dir, monkeypatch):
    monkeypatch.setenv(REQUIRE_ENV, "1")
    monkeypatch.setenv(EVENT_BUS_ENV, "nats")
    _write_heartbeat(plan_dir, 5)
    calls = _stub_broker(monkeypatch, reachable=True)

    _assert_probe(client.get("/readyz"), kind="readyz", ok=True)
    assert len(calls) == 1

    # A second probe inside the window is served from the cache: the broker is
    # not asked again, and the cached timestamp is not extended.
    _assert_probe(client.get("/readyz"), kind="readyz", ok=True)
    assert len(calls) == 1

    # The broker dies, but the unexpired entry still says "reachable".
    _stub_broker_calls = calls
    monkeypatch.setattr(
        events_nats_consumer, "_collect_consumer_status", _unreachable_counter(_stub_broker_calls)
    )
    _assert_probe(client.get("/readyz"), kind="readyz", ok=True)
    assert len(calls) == 1

    # Let the entry expire without sleeping: the next probe must see the truth.
    events_nats_consumer._status_cache["expires_at"] = time.monotonic() - 1
    _assert_probe(
        client.get("/readyz"),
        kind="readyz",
        ok=False,
        reasons=["broker_unreachable"],
    )
    assert len(calls) == 2


def _unreachable_counter(calls: list[int]):
    """A ``_collect_consumer_status`` replacement that records and fails."""

    def _collect() -> dict:
        calls.append(1)
        return {"reachable": False}

    return _collect


# --------------------------------------------------------------------------- #
# Errors are 503, never 500
# --------------------------------------------------------------------------- #


def test_internal_error_answers_503_not_500(client, plan_dir, monkeypatch):
    monkeypatch.setenv(REQUIRE_ENV, "1")

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(health_freshness, "health_extras", _boom)

    _assert_probe(
        client.get("/livez"), kind="livez", ok=False, reasons=["probe_error"]
    )
    _assert_probe(
        client.get("/readyz"), kind="readyz", ok=False, reasons=["probe_error"]
    )


def test_broker_probe_error_answers_503_not_500(client, plan_dir, monkeypatch):
    monkeypatch.setenv(REQUIRE_ENV, "1")
    monkeypatch.setenv(EVENT_BUS_ENV, "nats")
    _write_heartbeat(plan_dir, 5)

    def _boom() -> dict:
        raise RuntimeError("nats down")

    monkeypatch.setattr(events_nats_consumer, "consumer_status", _boom)

    _assert_probe(
        client.get("/readyz"), kind="readyz", ok=False, reasons=["probe_error"]
    )


# --------------------------------------------------------------------------- #
# Orchestrator neutrality: no credential, outside /api/
# --------------------------------------------------------------------------- #


def test_probes_need_no_credential_in_oidc_mode(client, plan_dir, monkeypatch):
    monkeypatch.setenv(REQUIRE_ENV, "1")
    _write_heartbeat(plan_dir, 5)
    monkeypatch.setattr(d, "_identity", _DenyAllOidcProvider())

    # The gate is genuinely active for /api/ routes...
    assert client.get("/api/health").status_code == 401

    # ...while both probes answer anonymously.
    assert client.get("/livez").status_code == 200
    assert client.get("/readyz").status_code == 200


def test_probes_are_not_under_the_api_prefix(client):
    assert client.get("/api/livez").status_code == 404
    assert client.get("/api/readyz").status_code == 404


# --------------------------------------------------------------------------- #
# Liveness must never depend on tenancy
# --------------------------------------------------------------------------- #
#
# A deployment may install a namespace resolver plugin
# (``PIPELINE_NAMESPACE_RESOLVER_PLUGIN``). Such a resolver maps a principal to
# the namespaces it may reach, and an unauthenticated probe principal reaches
# none of them: the resolver answers with an empty tuple, which the namespace
# layer refuses rather than reads as "everything". The dashboard middleware
# wraps every request in that namespace check, so without an exemption the two
# probes would be answered 403 ``namespace_error`` -- and a liveness probe that
# fails on tenancy restarts a healthy container for the wrong reason.
#
# The probes therefore answer directly, without the namespace wrapper, and so
# does every other surface that carries no tenant data -- the index page, the
# ``/auth`` login bootstrap and the static assets -- because those must be
# reachable before a principal or a namespace exists. Only the ``/api/`` and
# ``/mcp/`` surfaces carry tenant data, so only they keep the wrapper: an
# ``/api/`` route under the very same resolver is still refused with 403
# ``namespace_error``.

#: The reason token a refused namespace answers with.
NAMESPACE_ERROR = "namespace_error"


@pytest.fixture(autouse=True)
def _isolate_resolver_plugins(monkeypatch):
    """Keep the process-wide namespace-resolver plugin cache out of the tests.

    The active resolver is cached for the life of the process, and the cache is
    consulted before the environment, so a plugin installed by one test would
    otherwise answer the next test's requests.
    """
    monkeypatch.delenv(NAMESPACE_RESOLVER_PLUGIN_ENV, raising=False)
    monkeypatch.delenv(ALLOWLIST_ENV, raising=False)
    monkeypatch.setattr(resolver_module, "_PLUGIN_RESOLVERS", {})
    yield


def _install_resolver_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> str:
    """Write *source* as a fresh module, select it as the resolver, return its name.

    The module name is unique per call, so ``sys.modules`` never hands a later
    test an earlier test's stub. The allowlist names the module: the resolver
    seam is security-sensitive, so a plugin is refused without one.
    """
    name = "probe_nsr_" + uuid.uuid4().hex
    (tmp_path / f"{name}.py").write_text(source, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv(NAMESPACE_RESOLVER_PLUGIN_ENV, f"{name}:build")
    monkeypatch.setenv(ALLOWLIST_ENV, name)
    return name


def _stub_resolver_source(accesses_body: str) -> str:
    """Return plugin source whose ``accesses`` method body is *accesses_body*.

    The body is spliced into the source verbatim, so it may use braces freely:
    only the template text is subject to f-string rules.
    """
    return f'''
from pipeline.namespace_resolver import NamespaceAccess


class StubResolver:
    def accesses(self, principal):
        {accesses_body}


def build():
    return StubResolver()
'''


def _api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Point the dashboard gate at a throwaway key file and return the secret.

    The real repository key file is never read or written by these tests.
    """
    path = tmp_path / ".dashboard_api_key"
    monkeypatch.setattr(auth, "API_KEY_PATH", path)
    return auth.get_or_create_api_key()


def test_probes_answer_200_when_the_resolver_reports_no_namespaces(
    client, plan_dir, monkeypatch, tmp_path
):
    """A resolver that reaches no namespace must not make the probes fail."""
    monkeypatch.delenv(REQUIRE_ENV, raising=False)
    _install_resolver_plugin(
        tmp_path, monkeypatch, _stub_resolver_source("return ()")
    )

    _assert_probe(client.get("/livez"), kind="livez", ok=True, plan_dir=plan_dir)
    _assert_probe(client.get("/readyz"), kind="readyz", ok=True, plan_dir=plan_dir)


def test_probes_answer_200_when_the_resolver_raises(
    client, plan_dir, monkeypatch, tmp_path
):
    """A resolver that blows up must not make the probes fail either."""
    monkeypatch.delenv(REQUIRE_ENV, raising=False)
    _install_resolver_plugin(
        tmp_path,
        monkeypatch,
        _stub_resolver_source("raise RuntimeError('resolver exploded')"),
    )

    _assert_probe(client.get("/livez"), kind="livez", ok=True, plan_dir=plan_dir)
    _assert_probe(client.get("/readyz"), kind="readyz", ok=True, plan_dir=plan_dir)


def test_api_routes_are_still_refused_by_the_namespace_wrapper(
    client, plan_dir, monkeypatch, tmp_path
):
    """Only the probes are exempt: an ``/api/`` route still gets the wrapper."""
    _install_resolver_plugin(
        tmp_path, monkeypatch, _stub_resolver_source("return ()")
    )
    key = _api_key(tmp_path, monkeypatch)

    response = client.get("/api/health", headers={"x-pipeline-api-key": key})

    assert response.status_code == 403
    assert response.json()["detail"] == NAMESPACE_ERROR


def test_api_routes_still_require_the_credential_under_a_plugin_resolver(
    client, plan_dir, monkeypatch, tmp_path
):
    """The credential gate is not skipped for ``/api/`` routes."""
    _install_resolver_plugin(
        tmp_path, monkeypatch, _stub_resolver_source("return ()")
    )
    _api_key(tmp_path, monkeypatch)

    response = client.get("/api/health")

    assert response.status_code == 401
    assert response.json()["detail"] == "unauthorized"


def test_metrics_route_is_still_refused_by_the_namespace_wrapper(
    client, plan_dir, monkeypatch, tmp_path
):
    """``/api/metrics`` keeps the namespace wrapper like every other route."""
    _install_resolver_plugin(
        tmp_path, monkeypatch, _stub_resolver_source("return ()")
    )
    key = _api_key(tmp_path, monkeypatch)

    response = client.get("/api/metrics", headers={"x-pipeline-api-key": key})

    assert response.status_code == 403
    assert response.json()["detail"] == NAMESPACE_ERROR


def test_the_index_page_is_served_without_the_namespace_wrapper(
    client, plan_dir, monkeypatch, tmp_path
):
    """The UI bootstrap carries no tenant data, so an empty resolver must not block it."""
    _install_resolver_plugin(
        tmp_path, monkeypatch, _stub_resolver_source("return ()")
    )

    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_only_the_tenant_data_surfaces_get_the_namespace_wrapper(
    client, plan_dir, monkeypatch, tmp_path
):
    """A path outside /api and /mcp is served; the API keeps the wrapper."""
    _install_resolver_plugin(
        tmp_path, monkeypatch, _stub_resolver_source("return ()")
    )

    assert client.get("/livez/").status_code == 404
    assert client.get("/readyz/extra").status_code == 404
    response = client.get("/api/livez")
    assert response.status_code == 403
    assert response.json()["detail"] == NAMESPACE_ERROR


def test_probes_still_bind_the_principal_and_the_correlation_id(
    client, plan_dir, monkeypatch, tmp_path
):
    """Skipping the namespace wrapper must not skip the request bindings.

    The probe handler is the only place the bound actor can be observed from
    inside the request, so the freshness reader records what is bound while the
    probe runs. The resolver plugin is installed so the probe is served through
    the exempted path: the bindings must survive the exemption.
    """
    monkeypatch.delenv(REQUIRE_ENV, raising=False)
    _install_resolver_plugin(
        tmp_path, monkeypatch, _stub_resolver_source("return ()")
    )
    seen: list[tuple[object, str]] = []
    original = health_freshness.health_extras

    def _spy(*args, **kwargs):
        seen.append((current_principal(), current_correlation_id()))
        return original(*args, **kwargs)

    monkeypatch.setattr(health_freshness, "health_extras", _spy)

    response = client.get("/livez", headers={"x-correlation-id": "probe-corr-1"})

    assert response.status_code == 200
    assert seen, "the probe never read the freshness extras"
    for principal, correlation in seen:
        assert principal == ANONYMOUS
        assert correlation == "probe-corr-1"
    assert response.headers["X-Correlation-ID"] == "probe-corr-1"


def test_the_dashboard_declares_the_tenant_data_paths():
    """The wrapper's scope is a named constant, so it is greppable and reviewable."""
    assert hasattr(d, "TENANT_DATA_PATHS")
    assert set(d.TENANT_DATA_PATHS) == {"/api/", "/mcp/"}


def test_a_static_asset_is_served_without_the_namespace_wrapper(
    client, plan_dir, monkeypatch, tmp_path
):
    """Static files carry no tenant data and must load before login."""
    _install_resolver_plugin(
        tmp_path, monkeypatch, _stub_resolver_source("return ()")
    )

    assert client.get("/app.js").status_code == 200


def test_the_probe_only_exemption_constant_is_gone():
    """The old probe-only constant must not survive as a second source of truth.

    It named the two orchestrator probes as the *only* exempted paths. The
    exemption now covers every non-tenant surface, so a surviving copy would
    be a dead, misleading rule.

    The name is assembled at runtime rather than spelled out, so the
    repository-wide grep for the retired constant stays empty.
    """
    retired = "LIVENESS" + "_PATHS"

    assert not hasattr(d, retired)


def test_the_dashboard_exposes_the_tenant_data_helper():
    """The scope test is a named helper, so it can be called and reviewed."""
    assert hasattr(d, "_serves_tenant_data")


def test_the_tenant_data_helper_serves_the_api_and_mcp_surfaces():
    """Only the API and MCP responses carry tenant data; everything else does not."""
    serves = d._serves_tenant_data

    for path in ("/api", "/api/", "/api/health", "/api/plans", "/mcp", "/mcp/", "/mcp/x"):
        assert serves(path), path


def test_the_tenant_data_helper_exempts_every_other_surface():
    """The bootstrap, the static assets and the probes carry no tenant data."""
    serves = d._serves_tenant_data

    for path in (
        "/",
        "/app.js",
        "/style.css",
        "/index.html",
        "/auth/login",
        "/auth/callback",
        "/auth/logout",
        "/livez",
        "/readyz",
        "/livez/",
        "/readyz/extra",
    ):
        assert not serves(path), path


def test_the_mcp_surface_keeps_the_namespace_wrapper(
    client, plan_dir, monkeypatch, tmp_path
):
    """The MCP responses carry tenant data, so they keep the wrapper."""
    _install_resolver_plugin(
        tmp_path, monkeypatch, _stub_resolver_source("return ()")
    )

    for path in ("/mcp/", "/mcp/anything"):
        response = client.get(path)
        assert response.status_code == 403, path
        assert response.json()["detail"] == NAMESPACE_ERROR, path


def test_an_exempted_non_probe_request_still_binds_the_principal_and_the_correlation_id(
    client, plan_dir, monkeypatch, tmp_path
):
    """Skipping the namespace wrapper must not skip the request bindings.

    The exemption now covers the UI bootstrap and the static assets, not only
    the probes. Every exempted request must still be served with the actor and
    the correlation id bound, exactly like the exempted probes are.
    """
    monkeypatch.delenv(REQUIRE_ENV, raising=False)
    _install_resolver_plugin(
        tmp_path, monkeypatch, _stub_resolver_source("return ()")
    )

    response = client.get("/app.js", headers={"x-correlation-id": "static-corr-1"})

    assert response.status_code == 200
    assert response.headers["X-Correlation-ID"] == "static-corr-1"


def test_the_index_page_loads_in_oidc_mode_with_an_empty_resolver(
    client, plan_dir, monkeypatch, tmp_path
):
    """The dashboard must be able to load its login page at all.

    In OIDC mode a browser arrives with no credential, so the principal is
    ANONYMOUS and the tenancy resolver reaches no namespace. The bootstrap
    surface carries no tenant data, so it must still answer -- this is the
    symptom the story fixes: the login page itself answered 403.
    """
    monkeypatch.setattr(d, "_identity", _DenyAllOidcProvider())
    _install_resolver_plugin(
        tmp_path, monkeypatch, _stub_resolver_source("return ()")
    )

    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
