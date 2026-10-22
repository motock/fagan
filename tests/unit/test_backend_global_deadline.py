"""Regression pin for the single global role-call deadline across backend retries.

`OllamaDriver._chat` computes one wall-clock `deadline` before its retry loop
and sleeps `min(self.chat_retry_backoff * attempt, remaining)`, so retries can
never extend past the role-call budget. The 2026-09-02 scheduler freeze was a
retry loop whose budget reset on every attempt (3 attempts x a 600s timeout
outlives any single timeout). The existing retry tests assert attempt COUNTS
only, so dropping the `min(..., remaining)` cap - or resetting the deadline per
attempt - would multiply the worst-case stall by the attempt count unnoticed.
These tests pin the actual sleeps and the resulting clock, not just the count.
"""
import pytest

from app import backend_ollama as b


def _fake_clock(monkeypatch):
    """Virtual monotonic clock: `time.sleep` advances it instead of blocking."""
    clock = {"now": 0.0}
    sleeps = []

    monkeypatch.setattr(b.time, "monotonic", lambda: clock["now"])

    def _sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(b.time, "sleep", _sleep)
    return clock, sleeps


def _always_connect_error(monkeypatch, calls):
    """Every wire attempt fails with a transient ConnectError."""

    def _post(*args, **kwargs):
        calls["n"] += 1
        raise b.httpx.ConnectError("connection refused")

    monkeypatch.setattr(b.httpx, "post", _post)


def test_a_deadline_shorter_than_the_backoff_allows_exactly_one_attempt(monkeypatch):
    monkeypatch.setenv("PIPELINE_ROLE_CALL_TIMEOUT_SECONDS", "0.05")
    monkeypatch.setenv("PIPELINE_LOCAL_CHAT_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("PIPELINE_LOCAL_CHAT_RETRY_BACKOFF", "10")
    clock, sleeps = _fake_clock(monkeypatch)
    calls = {"n": 0}
    _always_connect_error(monkeypatch, calls)

    with pytest.raises(RuntimeError, match="unreachable"):
        b.OllamaDriver()._chat([{"role": "user", "content": "p"}], "gpt-oss:20b")

    assert calls["n"] == 1
    assert sleeps == [pytest.approx(0.05)]
    assert clock["now"] == pytest.approx(0.05)


def test_each_retry_sleep_is_capped_by_the_remaining_budget(monkeypatch):
    monkeypatch.setenv("PIPELINE_ROLE_CALL_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("PIPELINE_LOCAL_CHAT_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("PIPELINE_LOCAL_CHAT_RETRY_BACKOFF", "4")
    clock, sleeps = _fake_clock(monkeypatch)
    calls = {"n": 0}
    _always_connect_error(monkeypatch, calls)

    with pytest.raises(RuntimeError, match="unreachable"):
        b.OllamaDriver()._chat([{"role": "user", "content": "p"}], "gpt-oss:20b")

    assert calls["n"] == 2
    assert sleeps == [pytest.approx(4.0), pytest.approx(1.0)]
    assert clock["now"] == pytest.approx(5.0)
