"""Message-wording regression tests for the role-call timeout fix
(review round 2 findings: the exhaustion message must not claim attempts
that never ran, and the review-loop exhaustion path must keep its
"during review" wording).

Reuses the stubbing patterns of tests/unit/test_role_call_timeout.py
(read-only) - httpx is a shared singleton module, so one monkeypatch on
httpx.post intercepts the wire calls from both app.backend_ollama and
app.inference_providers.
"""
from __future__ import annotations

import re
import threading
import time

import httpx
import pytest

from app.backend_ollama import OllamaDriver

ROLE_TIMEOUT_ENV = "PIPELINE_ROLE_CALL_TIMEOUT_SECONDS"
ENDPOINT = "http://localhost:11434"  # provider default; conftest clears PIPELINE_*
_SECONDS_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:ms|s|sec|secs|seconds)\b")
_ATTEMPTS_RE = re.compile(r"after \d+ attempt\(s\)")


def _ok_envelope(content: str = "ok") -> dict:
    return {
        "message": {"role": "assistant", "content": content},
        "prompt_eval_count": 11,
        "eval_count": 7,
        "total_duration": 1_000_000,
    }


class _FakeResponse:
    def __init__(self, payload: dict):
        self.status_code = 200
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _RecordingOkServer:
    """Answers immediately with a valid envelope; records wire timeouts."""

    def __init__(self, payload: dict | None = None) -> None:
        self.calls: list = []
        self.timeouts: list = []
        self._payload = payload if payload is not None else _ok_envelope()

    def __call__(self, url, json=None, timeout=None, **kwargs):
        self.calls.append(url)
        self.timeouts.append(timeout)
        return _FakeResponse(self._payload)


class _NeverRespondingServer:
    """Accepts the connection and never answers - the incident shape.

    Consumes slightly MORE than the granted timeout before raising, because
    a real server's read timeout fires at exactly the grant while
    Event.wait() can wake marginally early - the slack keeps the
    one-attempt outcome deterministic instead of jitter-dependent.
    """

    def __init__(self) -> None:
        self.calls: list = []
        self.timeouts: list = []

    def __call__(self, url, json=None, timeout=None, **kwargs):
        self.calls.append(url)
        self.timeouts.append(timeout)
        done = threading.Event()
        done.wait((timeout if timeout is not None else 30) + 0.05)
        raise httpx.ReadTimeout(f"stub: no response from {url}")


def _driver(**overrides) -> OllamaDriver:
    driver = OllamaDriver()
    for name, value in overrides.items():
        setattr(driver, name, value)
    return driver


def test_zero_attempt_exhaustion_message_does_not_claim_attempts(monkeypatch):
    """When the wall-clock budget is already spent before the first attempt,
    the RuntimeError must say the budget expired before any attempt - not
    "after N attempt(s)", which implies attempts ran. The deadline is
    computed once from the (fake) clock at T0=1000.0; every later clock read
    returns 1601.0, so with the 600s default budget the first remaining-
    budget check is 1.0s overdue and the loop exits with 0 attempts made."""
    monkeypatch.delenv(ROLE_TIMEOUT_ENV, raising=False)
    reads = {"n": 0}

    def _fake_monotonic() -> float:
        reads["n"] += 1
        return 1000.0 if reads["n"] == 1 else 1601.0

    monkeypatch.setattr("app.backend_ollama.time.monotonic", _fake_monotonic)
    monkeypatch.setattr("app.backend_ollama.time.sleep", lambda s: None)
    server = _RecordingOkServer()
    monkeypatch.setattr(httpx, "post", server)
    driver = _driver()
    with pytest.raises(RuntimeError) as excinfo:
        driver.complete("hello", model="test:24b")
    message = str(excinfo.value)
    assert "after 0 attempt(s)" not in message, (
        f"the message must not claim attempts that never ran: {message!r}")
    assert not _ATTEMPTS_RE.search(message), (
        f"zero attempts ran, so the message must not report an attempt "
        f"count at all: {message!r}")
    assert "before any attempt" in message, (
        f"the zero-attempt message must say the budget expired before any "
        f"attempt: {message!r}")
    assert ENDPOINT in message, (
        f"RuntimeError message must include the endpoint: {message!r}")
    assert _SECONDS_RE.search(message), (
        f"RuntimeError message must include the elapsed time: {message!r}")
    assert server.calls == [], (
        f"no attempt should have reached the wire: {server.calls!r}")


def test_followup_call_after_zero_attempt_exhaustion_recomputes_deadline(
        monkeypatch):
    """State check: the deadline and attempt counter must be recomputed per
    call, never cached. After a zero-attempt exhaustion, a follow-up call
    with a 5s budget against a hang stub must make exactly 1 real attempt
    (wire timeout 5s), then raise "after 1 attempt(s)" with elapsed ~5s - an
    instant zero-attempt raise would mean the deadline leaked across calls."""
    monkeypatch.setenv(ROLE_TIMEOUT_ENV, "5")
    monkeypatch.setenv("PIPELINE_LOCAL_CHAT_RETRY_BACKOFF", "0")
    real_monotonic = time.monotonic
    monkeypatch.setattr("app.backend_ollama.time.monotonic", real_monotonic)
    monkeypatch.setattr("app.backend_ollama.time.sleep", lambda s: None)
    server = _NeverRespondingServer()
    monkeypatch.setattr(httpx, "post", server)
    driver = _driver()
    started = real_monotonic()
    with pytest.raises(RuntimeError) as excinfo:
        driver.complete("hello", model="test:24b")
    elapsed = real_monotonic() - started
    message = str(excinfo.value)
    assert len(server.calls) == 1, (
        f"expected exactly 1 attempt within the 5s budget, saw "
        f"{len(server.calls)}: {server.calls!r}")
    assert "after 1 attempt(s)" in message, (
        f"the follow-up call must report its real attempt count: {message!r}")
    assert ENDPOINT in message
    assert elapsed < 10.0, f"follow-up call took {elapsed:.2f}s"


def test_review_loop_exhaustion_message_keeps_during_review_wording(
        monkeypatch, tmp_path):
    """The review-loop exhaustion path (budget spent with no verdict) must
    keep the "during review" wording in its RuntimeError - type and endpoint
    unchanged, so callers matching on the message are unaffected."""
    monkeypatch.setenv(ROLE_TIMEOUT_ENV, "0.2")
    monkeypatch.setenv("PIPELINE_LOCAL_CHAT_RETRY_BACKOFF", "0")
    server = _NeverRespondingServer()
    monkeypatch.setattr(httpx, "post", server)
    driver = _driver()
    with pytest.raises(RuntimeError) as excinfo:
        driver.complete("review this", model="test:24b",
                        allowed_tools="Bash,Read", cwd=str(tmp_path))
    message = str(excinfo.value)
    assert "during review" in message, (
        f"the review-loop exhaustion message must keep the 'during review' "
        f"wording: {message!r}")
    assert ENDPOINT in message, (
        f"RuntimeError message must include the endpoint: {message!r}")