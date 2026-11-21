"""The channel backoff is scoped to (namespace, instance name)."""

from __future__ import annotations

import pytest

from pipeline import notification_channels as nc
from pipeline.actor import bind_namespace


class _Stub:
    def __init__(self, name, result):
        self.name = name
        self.result = result
        self.calls = 0

    def send(self, record):
        self.calls += 1
        return self.result


class _Clock:
    now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def _fresh_breakers(monkeypatch):
    monkeypatch.setattr(nc, "_BREAKERS", {})


_RETRY_HOUR = nc.DeliveryResult(nc.DeliveryStatus.RETRY, retry_after_seconds=3600)
_OK = nc.DeliveryResult(nc.DeliveryStatus.DELIVERED)


def test_should_not_back_off_another_namespaces_same_named_channel():
    a = _Stub("alerts", _RETRY_HOUR)
    b = _Stub("alerts", _OK)
    with bind_namespace("team-a"):
        assert nc.as_sender(a, _Clock())({}) is False
    with bind_namespace("team-b"):
        assert nc.as_sender(b, _Clock())({}) is True
    assert b.calls == 1


def test_should_still_back_off_within_the_same_namespace():
    clock = _Clock()
    a = _Stub("alerts", _RETRY_HOUR)
    with bind_namespace("team-a"):
        send = nc.as_sender(a, clock)
        send({})
        assert send({}) is False
        assert a.calls == 1
        clock.now += 3601
        send({})
    assert a.calls == 2


def test_should_cap_a_huge_retry_after_at_one_hour():
    clock = _Clock()
    a = _Stub("alerts", nc.DeliveryResult(nc.DeliveryStatus.RETRY, retry_after_seconds=10**9))
    with bind_namespace("team-a"):
        nc.as_sender(a, clock)({})
        _, blocked_until = nc._BREAKERS[("team-a", "alerts")]
    assert blocked_until - clock.now <= nc._BREAKER_MAX_S
