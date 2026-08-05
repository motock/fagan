"""Acceptance fixture (read-only, do not edit): OllamaDriver._chat must
retry transient httpx failures with backoff instead of failing on the
first blip, mirroring scripts/local_agent.py's chat() retry contract for
the dispatch path. Exercises the real driver.complete() entrypoint (the
actual production call site used by review/planner/overlord/decompose),
not _chat in isolation, so the fix is proven wired into the real call
chain rather than merely present as an unused method.
"""
import pytest

from app import backend as b


class _FakeOkResponse:
    def __init__(self, content="ok after retry"):
        self.status_code = 200
        self._content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"message": {"content": self._content}}


class _FakeStatusResponse:
    def __init__(self, status_code):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise b.httpx.HTTPStatusError(
                f"{self.status_code} simulated", request=None, response=self
            )

    def json(self):  # pragma: no cover - never reached when status >= 400
        return {"message": {"content": "unreachable"}}


def test_complete_retries_transient_connect_error_then_succeeds(monkeypatch):
    monkeypatch.setattr(b.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def _flaky_post(url, json, timeout):
        calls["n"] += 1
        if calls["n"] < 2:
            raise b.httpx.ConnectError("transient stall")
        return _FakeOkResponse()

    monkeypatch.setattr(b.httpx, "post", _flaky_post)

    result = b.OllamaDriver().complete("p", model="gpt-oss:20b")

    assert result == "ok after retry"
    assert calls["n"] == 2


def test_complete_retries_5xx_then_succeeds(monkeypatch):
    monkeypatch.setattr(b.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def _flaky_post(url, json, timeout):
        calls["n"] += 1
        return _FakeStatusResponse(503) if calls["n"] < 2 else _FakeOkResponse()

    monkeypatch.setattr(b.httpx, "post", _flaky_post)

    result = b.OllamaDriver().complete("p", model="gpt-oss:20b")

    assert result == "ok after retry"
    assert calls["n"] == 2


def test_complete_does_not_retry_4xx(monkeypatch):
    monkeypatch.setattr(b.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def _boom(url, json, timeout):
        calls["n"] += 1
        return _FakeStatusResponse(400)

    monkeypatch.setattr(b.httpx, "post", _boom)

    with pytest.raises(RuntimeError):
        b.OllamaDriver().complete("p", model="gpt-oss:20b")

    assert calls["n"] == 1


def test_complete_raises_after_exhausting_all_retry_attempts(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_CHAT_MAX_ATTEMPTS", "3")
    monkeypatch.setattr(b.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def _always_boom(url, json, timeout):
        calls["n"] += 1
        raise b.httpx.ConnectError("permanently down")

    monkeypatch.setattr(b.httpx, "post", _always_boom)

    with pytest.raises(RuntimeError, match="unreachable"):
        b.OllamaDriver().complete("p", model="gpt-oss:20b")

    assert calls["n"] == 3


def test_complete_rate_limited_propagates_immediately_without_retry(monkeypatch):
    monkeypatch.setattr(b.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def _rate_limited(url, json, timeout):
        calls["n"] += 1
        return _FakeStatusResponse(429)

    monkeypatch.setattr(b.httpx, "post", _rate_limited)

    with pytest.raises(b.RateLimitedError):
        b.OllamaDriver().complete("p", model="gpt-oss:20b")

    assert calls["n"] == 1
