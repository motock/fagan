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
from datetime import UTC, datetime, timedelta

import pytest

from app import dashboard as d
from app.identity import IdentityError
from app.oidc import OidcProvider
from pipeline import events_nats_consumer, health_freshness
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
