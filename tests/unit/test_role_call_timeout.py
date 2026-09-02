"""Regression tests for the 2026-09-02 scheduler freeze (daemon pid 1609).

The scheduler froze for ~40 minutes blocked in sock_recv on a TCP
connection to localhost:11434 while Ollama itself was healthy (a probe
generation returned fine mid-freeze). One wedged OllamaDriver.complete()
call stalled advance ticks for ALL plans. The daemon nominally carries
httpx timeouts (PIPELINE_LOCAL_TIMEOUT_SECONDS, default 600), yet the
read outlived any 600s deadline - so some scheduler-side call path either
bypassed the timeout (a timeout=None caller), consumed a response outside
the timeout's scope, or reset the deadline in its retry loop.

Contract pinned here (the fix is confined to app/inference_providers.py
and app/backend_ollama.py):

- PIPELINE_ROLE_CALL_TIMEOUT_SECONDS (default 600) is read in BOTH files,
  and every outbound chat/complete httpx call reachable from the
  scheduler-side complete() path passes a finite, env-tunable timeout
  explicitly - never None, never a caller-overridable None.
- A server that accepts the connection but never responds cannot hang the
  caller: the timeout fires, retries are bounded (3 attempts), and on
  exhaustion complete() raises the existing RuntimeError contract with
  the endpoint AND the elapsed time in the message.
- The retry loop's wall-clock deadline is computed ONCE before the first
  attempt and enforced across retries (a loop that resets a per-attempt
  deadline can outlive any single timeout).
- The fast path (server responds normally) is unchanged.

The HTTP seam is stubbed at httpx.post: httpx is a shared singleton
module, so one monkeypatch intercepts calls from both app.backend_ollama
and app.inference_providers (see inference_providers.py's docstring).
The stubs honor the timeout kwarg exactly like real httpx: None blocks
forever (the incident shape); a finite float raises httpx.ReadTimeout
once it expires.
"""
from __future__ import annotations

import ast
import math
import re
import threading
import time
from pathlib import Path

import httpx
import pytest

from app import inference_providers
from app.backend_ollama import OllamaDriver

ROLE_TIMEOUT_ENV = "PIPELINE_ROLE_CALL_TIMEOUT_SECONDS"
ENDPOINT = "http://localhost:11434"  # provider default; conftest clears PIPELINE_*
REPO_ROOT = Path(__file__).resolve().parents[2]
# Generous wall-clock bound for the hang tests: a fixed call finishes in
# well under a second; a wedged one must be detected long before this.
HANG_BOUND_S = 15.0
_SECONDS_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:ms|s|sec|secs|seconds)\b")


def _ok_envelope(content: str = "ok") -> dict:
    return {
        "message": {"role": "assistant", "content": content},
        "prompt_eval_count": 11,
        "eval_count": 7,
        "total_duration": 1_000_000,
    }


def _review_envelope() -> dict:
    """Envelope whose assistant turn calls submit_review(APPROVE) at once."""
    return {
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "function": {
                    "name": "submit_review",
                    "arguments": {"verdict": "APPROVE", "pr_title": "t",
                                  "pr_body": "b"},
                },
            }],
        },
        "prompt_eval_count": 11,
        "eval_count": 7,
    }


class _FakeResponse:
    """Minimal httpx.Response stand-in for the reads provider.chat() makes."""

    def __init__(self, payload: dict):
        self.status_code = 200
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _StubServer:
    """Base httpx.post stub: records every (url, timeout) the wire saw."""

    def __init__(self) -> None:
        self.calls: list = []
        self.timeouts: list = []

    @staticmethod
    def _block(timeout) -> None:
        done = threading.Event()
        if timeout is None:
            done.wait()  # the incident: no deadline -> blocked in sock_recv
        else:
            done.wait(timeout)


class _RecordingOkServer(_StubServer):
    """Answers immediately with a valid envelope; records wire timeouts."""

    def __init__(self, payload: dict | None = None) -> None:
        super().__init__()
        self._payload = payload if payload is not None else _ok_envelope(
            "hello from stub")

    def __call__(self, url, json=None, timeout=None, **kwargs):
        self.calls.append(url)
        self.timeouts.append(timeout)
        return _FakeResponse(self._payload)


class _NeverRespondingServer(_StubServer):
    """Accepts the connection and never answers - the incident shape."""

    def __call__(self, url, json=None, timeout=None, **kwargs):
        self.calls.append(url)
        self.timeouts.append(timeout)
        self._block(timeout)
        raise httpx.ReadTimeout(
            f"stub: no response from {url} (timeout={timeout})")


class _InstantlyDeadServer(_StubServer):
    """Fails every attempt immediately (fast-fail retry-count probe)."""

    def __call__(self, url, json=None, timeout=None, **kwargs):
        self.calls.append(url)
        self.timeouts.append(timeout)
        raise httpx.ReadTimeout("stub: connection reset before any byte")


class _SlowThenOkServer(_StubServer):
    """First call hangs until its timeout; later calls answer after
    `respond_after` seconds when the granted timeout allows it, else hang
    out their own timeout - how a real slow server + httpx read-timeout act."""

    def __init__(self, respond_after: float) -> None:
        super().__init__()
        self.respond_after = respond_after

    def __call__(self, url, json=None, timeout=None, **kwargs):
        self.calls.append(url)
        self.timeouts.append(timeout)
        if len(self.calls) == 1:
            self._block(timeout)
            raise httpx.ReadTimeout("stub: first read hung until timeout")
        if timeout is None:
            self._block(None)
            raise AssertionError("stub: unbounded hang reached")
        if timeout >= self.respond_after:
            time.sleep(self.respond_after)
            return _FakeResponse(_ok_envelope("slow but fine"))
        self._block(timeout)
        raise httpx.ReadTimeout("stub: read outlived the attempt timeout")


def _run_with_wall_clock_bound(fn, bound_s: float = HANG_BOUND_S):
    """Run fn() in a daemon thread; return (thread, box, elapsed). The box
    holds {"result": ...} or {"error": exc} once the call finishes."""
    box: dict = {}

    def _target() -> None:
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-inspected by callers
            box["error"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    started = time.monotonic()
    thread.start()
    thread.join(bound_s)
    return thread, box, time.monotonic() - started


def _driver(**overrides) -> OllamaDriver:
    """An OllamaDriver built with PIPELINE_* env already set, plus any
    attribute overrides (used to simulate caller-side timeout=None)."""
    driver = OllamaDriver()
    for name, value in overrides.items():
        setattr(driver, name, value)
    return driver


def _assert_finite_wire_timeout(server, expected: float) -> None:
    """The last timeout httpx.post actually saw must be finite and equal to
    `expected` - never None (the incident's unbounded read)."""
    assert server.timeouts, "httpx.post was never called"
    last = server.timeouts[-1]
    assert last is not None, (
        f"a None timeout reached httpx.post: {server.timeouts!r}")
    assert math.isfinite(float(last)), (
        f"a non-finite timeout reached httpx.post: {server.timeouts!r}")
    assert float(last) == pytest.approx(expected, rel=0.01), (
        f"httpx.post saw timeout={last!r}, expected {expected} "
        f"(from {ROLE_TIMEOUT_ENV}): {server.timeouts!r}")


# ---------------------------------------------------------------------------
# Fast path: a normally-responding server is zero behavior change.
# ---------------------------------------------------------------------------


def test_fast_path_normal_response_is_unchanged(monkeypatch):
    """Server responds normally -> complete() returns the content, and the
    one httpx.post call carries the env-tunable role-call timeout."""
    monkeypatch.setenv(ROLE_TIMEOUT_ENV, "0.2")
    server = _RecordingOkServer()
    monkeypatch.setattr(httpx, "post", server)
    driver = _driver()
    out = driver.complete("hello", model="test:24b")
    assert out == "hello from stub"
    assert server.calls == [f"{ENDPOINT}/api/chat"]
    assert len(server.timeouts) == 1
    _assert_finite_wire_timeout(server, expected=0.2)


def test_role_call_timeout_defaults_to_600_when_env_unset(monkeypatch):
    """PIPELINE_ROLE_CALL_TIMEOUT_SECONDS unset -> 600 hardcoded fallback
    reaches the wire, and the env var name is read in BOTH production files."""
    monkeypatch.delenv(ROLE_TIMEOUT_ENV, raising=False)
    server = _RecordingOkServer()
    monkeypatch.setattr(httpx, "post", server)
    driver = _driver()
    out = driver.complete("hello", model="test:24b")
    assert out == "hello from stub"
    _assert_finite_wire_timeout(server, expected=600.0)
    providers_src = (REPO_ROOT / "app" / "inference_providers.py").read_text()
    backend_src = (REPO_ROOT / "app" / "backend_ollama.py").read_text()
    assert ROLE_TIMEOUT_ENV in providers_src, (
        "app/inference_providers.py must read PIPELINE_ROLE_CALL_TIMEOUT_SECONDS")
    assert ROLE_TIMEOUT_ENV in backend_src, (
        "app/backend_ollama.py must read PIPELINE_ROLE_CALL_TIMEOUT_SECONDS")


# ---------------------------------------------------------------------------
# The incident shape: server accepts the connection but never responds.
# ---------------------------------------------------------------------------


def test_never_responding_server_cannot_hang_complete(monkeypatch):
    """HEADLINE hang test: with a small injected role-call timeout, a server
    that never responds must (a) not hang past the wall-clock bound, (b)
    stay within the bounded retry count, (c) end in the RuntimeError
    contract with the endpoint and elapsed time in the message."""
    monkeypatch.setenv(ROLE_TIMEOUT_ENV, "0.2")
    monkeypatch.setenv("PIPELINE_LOCAL_CHAT_RETRY_BACKOFF", "0")
    server = _NeverRespondingServer()
    monkeypatch.setattr(httpx, "post", server)
    driver = _driver()
    thread, box, elapsed = _run_with_wall_clock_bound(
        lambda: driver.complete("hello", model="test:24b"))
    assert not thread.is_alive(), (
        "OllamaDriver.complete() hung past the 15s wall-clock bound on a "
        "server that accepts connections but never responds - the "
        "2026-09-02 incident shape. The role-call timeout must fire and "
        "the call must raise.")
    assert isinstance(box.get("error"), RuntimeError), (
        f"expected the RuntimeError contract, got: {box!r}")
    message = str(box["error"])
    assert ENDPOINT in message, (
        f"RuntimeError message must include the endpoint: {message!r}")
    assert _SECONDS_RE.search(message), (
        f"RuntimeError message must include the elapsed time: {message!r}")
    # Bounded: a hung read consumes the whole budget, so a compliant loop
    # makes between 1 and 3 attempts - never an unbounded series.
    assert 1 <= len(server.calls) <= 3, server.calls
    assert all(
        t is not None and math.isfinite(float(t)) for t in server.timeouts
    ), f"None/non-finite timeout reached httpx: {server.timeouts!r}"
    assert float(server.timeouts[0]) == pytest.approx(0.2, abs=0.05), (
        f"first attempt must use the 0.2s env override, saw "
        f"{server.timeouts[0]!r}")
    assert elapsed < HANG_BOUND_S


def test_retries_bounded_at_3_and_exhaustion_raises_runtime_error(monkeypatch):
    """Fast-failing attempts retry up to the bounded count (3) and then the
    existing RuntimeError contract fires with endpoint + elapsed time."""
    monkeypatch.setenv(ROLE_TIMEOUT_ENV, "0.2")
    monkeypatch.setenv("PIPELINE_LOCAL_CHAT_RETRY_BACKOFF", "0")
    server = _InstantlyDeadServer()
    monkeypatch.setattr(httpx, "post", server)
    driver = _driver()
    thread, box, elapsed = _run_with_wall_clock_bound(
        lambda: driver.complete("hello", model="test:24b"))
    assert not thread.is_alive()
    assert isinstance(box.get("error"), RuntimeError), (
        f"expected the RuntimeError contract after retries are exhausted, "
        f"got: {box!r}")
    message = str(box["error"])
    assert ENDPOINT in message, (
        f"RuntimeError message must include the endpoint: {message!r}")
    assert _SECONDS_RE.search(message), (
        f"RuntimeError message must include the elapsed time: {message!r}")
    assert len(server.calls) == 3, (
        f"expected exactly 3 bounded attempts, saw {len(server.calls)}")
    for t in server.timeouts:
        assert t is not None and math.isfinite(float(t)), (
            f"None/non-finite timeout reached httpx: {server.timeouts!r}")
        assert float(t) < 1.0, (
            f"attempt timeout {t!r} is not derived from the 0.2s env "
            f"override (600s default leaked?): {server.timeouts!r}")
    assert elapsed < HANG_BOUND_S


def test_retry_loop_deadline_is_global_not_reset_per_attempt(monkeypatch):
    """The wall-clock deadline is computed ONCE before the first attempt and
    enforced across retries. A loop that resets a fresh per-attempt timeout
    on every retry would eventually SUCCEED here (each attempt individually
    fits in a fresh 0.5s) and outlive the role-call budget - it must fail
    with the RuntimeError contract instead."""
    monkeypatch.setenv(ROLE_TIMEOUT_ENV, "0.5")
    monkeypatch.setenv("PIPELINE_LOCAL_CHAT_RETRY_BACKOFF", "0")
    server = _SlowThenOkServer(respond_after=0.4)
    monkeypatch.setattr(httpx, "post", server)
    driver = _driver()
    thread, box, elapsed = _run_with_wall_clock_bound(
        lambda: driver.complete("hello", model="test:24b"))
    assert not thread.is_alive()
    assert isinstance(box.get("error"), RuntimeError), (
        "a retry loop that resets the per-attempt deadline would succeed "
        "here and outlive the role-call wall-clock budget; the deadline "
        f"must be computed once and enforced across retries. Got: {box!r}")
    assert elapsed < 5.0, (
        f"complete() took {elapsed:.2f}s with a 0.5s role-call timeout - "
        "the deadline was reset per attempt")


# ---------------------------------------------------------------------------
# The env var must reach the wire on every scheduler-side chat/complete path.
# ---------------------------------------------------------------------------


def test_review_loop_complete_passes_env_timeout(monkeypatch, tmp_path):
    """The review-style complete() path (cwd + Bash allowed_tools -> the
    blocking read-only tool loop) must pass the env-tunable timeout too."""
    monkeypatch.setenv(ROLE_TIMEOUT_ENV, "0.2")
    server = _RecordingOkServer(payload=_review_envelope())
    monkeypatch.setattr(httpx, "post", server)
    driver = _driver()
    out = driver.complete("review this", model="test:24b",
                          allowed_tools="Bash,Read", cwd=str(tmp_path))
    assert out.startswith("VERDICT: APPROVE")
    assert len(server.timeouts) == 1
    _assert_finite_wire_timeout(server, expected=0.2)


@pytest.mark.parametrize("provider_name", ["ollama", "lmstudio", "mlx"])
def test_provider_chat_default_timeout_is_env_tunable(monkeypatch, provider_name):
    """provider.chat() with NO explicit timeout kwarg must default to
    PIPELINE_ROLE_CALL_TIMEOUT_SECONDS (not a hardcoded 600 that ignores
    the env) - a never-responding server must time out within the bound."""
    monkeypatch.setenv(ROLE_TIMEOUT_ENV, "0.2")
    server = _NeverRespondingServer()
    monkeypatch.setattr(httpx, "post", server)
    provider = inference_providers.get_local_provider(provider_name)
    thread, box, _elapsed = _run_with_wall_clock_bound(
        lambda: provider.chat([{"role": "user", "content": "hi"}],
                              model="test:24b", num_ctx=128, temperature=0.1),
        bound_s=5.0)
    assert not thread.is_alive(), (
        f"{provider_name}.chat() with no explicit timeout hung past 5s - "
        "its default timeout must come from PIPELINE_ROLE_CALL_TIMEOUT_SECONDS")
    assert isinstance(box.get("error"), httpx.ReadTimeout), (
        f"expected httpx.ReadTimeout to propagate out of {provider_name}"
        f".chat(), got: {box!r}")
    assert server.timeouts and server.timeouts[0] is not None
    assert float(server.timeouts[0]) == pytest.approx(0.2, abs=0.05), (
        f"{provider_name}.chat() default timeout {server.timeouts[0]!r} is "
        f"not derived from the 0.2s env override")


def test_provider_chat_coerces_explicit_none_timeout(monkeypatch):
    """An explicit timeout=None caller must never put None on the wire: the
    provider coerces it to the env-tunable default (or refuses outright)."""
    monkeypatch.delenv(ROLE_TIMEOUT_ENV, raising=False)
    server = _RecordingOkServer()
    monkeypatch.setattr(httpx, "post", server)
    provider = inference_providers.get_local_provider("ollama")
    try:
        out = provider.chat([{"role": "user", "content": "hi"}],
                            model="test:24b", num_ctx=128, temperature=0.1,
                            timeout=None)
    except ValueError:
        return  # refusing an explicit None outright is also acceptable
    assert out["message"]["content"] == "hello from stub"
    _assert_finite_wire_timeout(server, expected=600.0)


def test_driver_caller_override_to_none_still_yields_finite_timeout(monkeypatch):
    """Even when the driver's own timeout attribute is overridden to None
    (the 'timeout=None caller' bypass), the httpx call must carry a finite
    env-derived timeout - never None."""
    monkeypatch.delenv(ROLE_TIMEOUT_ENV, raising=False)
    server = _RecordingOkServer()
    monkeypatch.setattr(httpx, "post", server)
    driver = _driver(timeout=None)
    try:
        out = driver.complete("hello", model="test:24b")
    except ValueError:
        return  # refusing a None override outright is also acceptable
    assert out == "hello from stub"
    _assert_finite_wire_timeout(server, expected=600.0)


def test_malformed_env_value_never_yields_unbounded_timeout(monkeypatch):
    """A malformed PIPELINE_ROLE_CALL_TIMEOUT_SECONDS must either be refused
    (ValueError) or fall back to the 600 default - never produce a None or
    non-finite timeout on the wire."""
    monkeypatch.setenv(ROLE_TIMEOUT_ENV, "not-a-number")
    server = _RecordingOkServer()
    monkeypatch.setattr(httpx, "post", server)
    try:
        driver = _driver()
    except ValueError:
        return  # refusing a malformed override outright is acceptable
    out = driver.complete("hello", model="test:24b")
    assert out == "hello from stub"
    _assert_finite_wire_timeout(server, expected=600.0)


# ---------------------------------------------------------------------------
# Static wire audit: every httpx call site in the two production files.
# ---------------------------------------------------------------------------


_HTTPX_CALL_ATTRS = {"post", "get", "stream", "request"}


def test_every_httpx_call_site_passes_explicit_non_none_timeout():
    """Every httpx.post/get/stream/request call site in the two production
    files passes an explicit timeout keyword whose argument is not the None
    literal - the static half of 'every outbound call passes a finite,
    env-tunable timeout'."""
    for relpath in ("app/inference_providers.py", "app/backend_ollama.py"):
        tree = ast.parse((REPO_ROOT / relpath).read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "httpx"
                    and node.func.attr in _HTTPX_CALL_ATTRS):
                continue
            keywords = {kw.arg: kw.value for kw in node.keywords}
            assert "timeout" in keywords, (
                f"{relpath}:{node.lineno} httpx.{node.func.attr}() has no "
                "explicit timeout argument")
            value = keywords["timeout"]
            assert not (isinstance(value, ast.Constant) and value.value is None), (
                f"{relpath}:{node.lineno} httpx.{node.func.attr}(timeout=None) "
                "- an unbounded read on the wire")
    providers_src = (REPO_ROOT / "app" / "inference_providers.py").read_text()
    backend_src = (REPO_ROOT / "app" / "backend_ollama.py").read_text()
    assert ROLE_TIMEOUT_ENV in providers_src
    assert ROLE_TIMEOUT_ENV in backend_src