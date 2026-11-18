"""The tenant "Customer AI gateway" provider: wiring, wire format, refusals.

The gateway provider fronts a customer-owned OpenAI-compatible gateway. Its
base URL and API key are *per-namespace credentials*, resolved inside every
call (never cached at import), its URL must be a public https endpoint, and
the connection is pinned to the address the URL resolved to.

Every test here stubs the credential seam and the HTTP transport: no test may
touch the network or the process environment's real credentials.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import httpx
import pytest

from app import (
    backend,
    backend_ollama,
    credentials,
    gateway_provider,
    inference_providers,
    outbound_guard,
)
from app.gateway_provider import GatewayProvider
from app.inference_providers import RateLimitedError
from pipeline import strength_class
from pipeline.actor import bind_namespace

BASE_URL = "https://gateway.example"
API_KEY = "tenant-gateway-key-8a7c"
RESOLVED_ADDRESS = "203.0.113.10"
NAMESPACE = "alpha"
OTHER_NAMESPACE = "beta"

# A gateway credential whose name reads like a key holds the API key; every
# other gateway credential holds the base URL.
_KEY_WORDS = ("key", "token", "secret", "password", "credential")

_PAYLOAD = {
    "choices": [{"message": {"role": "assistant", "content": "gateway says hi"}}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 5},
}

DOC = Path(__file__).resolve().parents[2] / "docs" / "DEPLOY_STANDALONE.md"


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=None
            )
        return self

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Stands in for the pinned httpx client the provider sends through."""

    def __init__(self, recorder) -> None:
        self._recorder = recorder

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, **kwargs):
        return self._record("POST", url, kwargs)

    def request(self, method, url, **kwargs):
        return self._record(str(method).upper(), url, kwargs)

    def send(self, request, **kwargs):
        url = str(getattr(request, "url", ""))
        content = getattr(request, "content", b"")
        headers = dict(getattr(request, "headers", {}) or {})
        return self._record(
            str(getattr(request, "method", "POST")),
            url,
            {"content": content, "headers": headers},
        )

    def _record(self, method, url, kwargs):
        self._recorder.calls.append((method, url, kwargs))
        return _FakeResponse(self._recorder.status, self._recorder.payload)


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.clients: list[tuple] = []
        self.refs: list = []
        self.resolved: list[str] = []
        self.base_url = BASE_URL
        self.api_key = API_KEY
        self.address = RESOLVED_ADDRESS
        self.status = 200
        self.payload = dict(_PAYLOAD)
        self.missing = False


@pytest.fixture
def wire(monkeypatch):
    """Stub the credential seam, the address resolution and the transport."""
    recorder = _Recorder()

    def fake_pinned_client(url, address=None, timeout=None, **kwargs):
        recorder.clients.append((url, address, timeout))
        return _FakeClient(recorder)

    def fake_resolve_public_address(url, **kwargs):
        recorder.resolved.append(url)
        return recorder.address

    def fake_resolve_for_injection(ref, **kwargs):
        recorder.refs.append(ref)
        if recorder.missing:
            raise credentials.CredentialMissing("credential_missing")
        if ref.name == "gateway_api_key" or any(
            word in ref.name for word in _KEY_WORDS
        ):
            return recorder.api_key
        return recorder.base_url

    # Patch the seam where it lives AND where it may have been imported from,
    # so the stub applies whichever import style the provider uses.
    for module in (outbound_guard, gateway_provider):
        monkeypatch.setattr(module, "pinned_client", fake_pinned_client, raising=False)
        monkeypatch.setattr(
            module,
            "resolve_public_address",
            fake_resolve_public_address,
            raising=False,
        )
        monkeypatch.setattr(
            module,
            "resolve_for_injection",
            fake_resolve_for_injection,
            raising=False,
        )
    monkeypatch.setattr(
        credentials,
        "resolve_for_injection",
        fake_resolve_for_injection,
        raising=False,
    )
    return recorder


def _body(kwargs: dict) -> dict:
    """The JSON body of a recorded request, whichever way it was sent."""
    if "json" in kwargs:
        return kwargs["json"]
    return json.loads(kwargs["content"])


def _headers(kwargs: dict) -> dict:
    raw = kwargs.get("headers") or {}
    try:
        items = raw.items()
    except AttributeError:
        items = []
    return {str(k).lower(): str(v) for k, v in items}


def _chat(provider, **overrides):
    kwargs = {"model": "tenant-model", "num_ctx": 4096, "temperature": 0.25}
    kwargs.update(overrides)
    return provider.chat([{"role": "user", "content": "hello"}], **kwargs)


# ---- identity and registration ------------------------------------------------


def test_gateway_provider_is_named_gateway_with_no_default_endpoint() -> None:
    provider = GatewayProvider()
    assert provider.name == "gateway"
    assert provider.default_endpoint == ""


def test_gateway_is_registered_as_a_local_provider() -> None:
    provider = inference_providers.get_local_provider("gateway")
    assert isinstance(provider, GatewayProvider)
    assert provider.name == "gateway"


def test_gateway_is_registered_as_a_backend_driver() -> None:
    driver = backend.get_backend("dispatch", name="gateway")
    assert isinstance(driver, backend_ollama.OllamaDriver)
    assert driver.provider.name == "gateway"


def test_gateway_is_registered_as_a_hosted_provider() -> None:
    assert "gateway" in backend_ollama._HOSTED_PROVIDERS


def test_gateway_is_classified_as_a_capable_provider() -> None:
    assert strength_class.strength_class("gateway", "m") == strength_class.CAPABLE
    assert strength_class.strength_class("gateway", None) == strength_class.CAPABLE
    # The registry did not become "everything is capable".
    assert strength_class.strength_class("ollama", "llama3:8b") == (
        strength_class.ON_DEVICE
    )


# ---- the wire format ----------------------------------------------------------


def test_gateway_chat_posts_to_the_credential_base_url_with_the_key(wire) -> None:
    result = _chat(GatewayProvider())

    assert len(wire.calls) == 1
    method, url, kwargs = wire.calls[0]
    assert method == "POST"
    assert url.startswith(BASE_URL)
    assert url.endswith("/v1/chat/completions")

    body = _body(kwargs)
    assert body["model"] == "tenant-model"
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    assert body["stream"] is False
    assert body["temperature"] == 0.25
    assert body["max_tokens"] == 4096
    assert "tools" not in body

    headers = _headers(kwargs)
    assert any(API_KEY in value for value in headers.values()), headers

    assert result["message"] == {"role": "assistant", "content": "gateway says hi"}
    assert result["prompt_eval_count"] == 3
    assert result["eval_count"] == 5


def test_gateway_chat_sends_tool_definitions_when_given(wire) -> None:
    tools = [{"type": "function", "function": {"name": "read_file"}}]
    _chat(GatewayProvider(), tools=tools)
    assert _body(wire.calls[0][2])["tools"] == tools


def test_gateway_pins_the_resolved_public_address(wire) -> None:
    _chat(GatewayProvider())
    assert BASE_URL in wire.resolved
    assert wire.clients, "the request was not sent through a pinned client"
    url, address, _timeout = wire.clients[0]
    assert str(url).startswith(BASE_URL)
    assert address == RESOLVED_ADDRESS


# ---- credentials ---------------------------------------------------------------


def test_gateway_resolves_model_credentials_for_the_current_namespace(wire) -> None:
    with bind_namespace(NAMESPACE):
        _chat(GatewayProvider())

    assert wire.refs, "no credential was resolved"
    for ref in wire.refs:
        assert ref.purpose == "model"
        assert ref.namespace == NAMESPACE
    assert "gateway_api_key" in {ref.name for ref in wire.refs}


def test_gateway_resolves_its_credentials_on_every_call(wire) -> None:
    provider = GatewayProvider()
    _chat(provider)
    first = len(wire.refs)
    assert first >= 1, "no credential was resolved"

    wire.base_url = "https://other-gateway.example"
    with bind_namespace(OTHER_NAMESPACE):
        _chat(provider)

    assert len(wire.refs) > first, "credentials are resolved per call"
    assert wire.calls[-1][1].startswith("https://other-gateway.example")
    assert wire.refs[-1].namespace == OTHER_NAMESPACE


def test_gateway_sends_nothing_when_a_credential_is_missing(wire) -> None:
    wire.missing = True
    with pytest.raises((credentials.CredentialMissing, RuntimeError, ValueError)):
        _chat(GatewayProvider())
    assert wire.calls == []


# ---- refusals ------------------------------------------------------------------


def test_gateway_refuses_a_base_url_that_is_not_https(wire) -> None:
    wire.base_url = "http://gateway.example"
    with pytest.raises(outbound_guard.OutboundRefused):
        _chat(GatewayProvider())
    assert wire.calls == []


def test_gateway_refuses_a_base_url_with_no_public_address(wire) -> None:
    wire.address = None
    with pytest.raises((outbound_guard.OutboundRefused, RuntimeError, ValueError)):
        _chat(GatewayProvider())
    assert wire.calls == []


def test_gateway_reports_a_rate_limited_gateway(wire) -> None:
    wire.status = 429
    with pytest.raises(RateLimitedError):
        _chat(GatewayProvider())
    assert wire.calls


# ---- the driver ----------------------------------------------------------------


def test_gateway_driver_ignores_the_local_endpoint_environment(monkeypatch) -> None:
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:9999")
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT_GATEWAY", "http://localhost:8888")

    driver = backend.get_backend("dispatch", name="gateway")
    assert driver.endpoint == ""

    # Other providers keep honouring the process-wide endpoint.
    assert (
        backend.get_backend("dispatch", name="ollama").endpoint
        == "http://localhost:9999"
    )


def test_gateway_uses_the_credential_base_url_when_the_endpoint_is_empty(wire) -> None:
    _chat(GatewayProvider(), endpoint="")
    assert wire.calls[0][1].startswith(BASE_URL)


# ---- the two providers must share one mapping ---------------------------------


def _provider_modules() -> list:
    """The modules a provider class may live in, most specific first."""
    modules = []
    for module in (gateway_provider, inference_providers):
        if module not in modules:
            modules.append(module)
    return modules


def _provider_chat(provider_cls):
    """The chat method of a provider class, wherever the class is defined."""
    for module in _provider_modules():
        found = getattr(module, provider_cls.__name__, None)
        if found is not None and inspect.isclass(found):
            return found.chat
    return provider_cls.chat


def _helper_candidates() -> list[tuple[object, str, object]]:
    """Every first-party module-level callable the providers could share.

    A helper may live in either provider module, so all of them are
    candidates; the probe below finds out which one is actually shared.
    """
    candidates: list[tuple[object, str, object]] = []
    for module in _provider_modules():
        for name, attr in list(vars(module).items()):
            if inspect.isfunction(attr):
                candidates.append((module, name, attr))
    return candidates


def test_the_two_providers_share_one_request_and_response_mapping(
    monkeypatch,
) -> None:
    """LM Studio and the gateway map requests through the same helper.

    Every module-level callable of the provider modules is replaced by a
    wrapper that records which provider called it and marks the dict it
    returns. A shared helper is one both providers call *and* whose result is
    what one of them sends or returns - two independent copies of the
    mapping would show no such callable.
    """
    candidates = _helper_candidates()
    assert candidates, "no module-level helper is reachable from the providers"

    called_by: dict[str, set[str]] = {}
    marked: dict[str, list[dict]] = {}
    current = {"provider": ""}

    def _wrapper(func, key):
        def wrapper(*args, **kwargs):
            result = func(*args, **kwargs)
            called_by.setdefault(key, set()).add(current["provider"])
            if isinstance(result, dict):
                produced = {**result, "__shared_helper__": key}
                marked.setdefault(key, []).append(produced)
                return produced
            return result

        return wrapper

    # Every *binding* of a candidate is patched, so a helper imported into
    # the other provider's module is intercepted on both sides.
    for holder, name, func in candidates:
        monkeypatch.setattr(
            holder,
            name,
            _wrapper(func, f"{func.__module__}.{func.__qualname__}"),
            raising=False,
        )

    lmstudio_bodies: list[dict] = []
    lmstudio_envelopes: list[dict] = []
    gateway_bodies: list[dict] = []
    gateway_envelopes: list[dict] = []

    def fake_post(url, **kwargs):
        lmstudio_bodies.append(kwargs.get("json") or {})
        return _FakeResponse(
            200,
            {
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2},
            },
        )

    monkeypatch.setattr(httpx, "post", fake_post)

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, **kwargs):
            gateway_bodies.append(kwargs.get("json") or {})
            return _FakeResponse(
                200,
                {
                    "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2},
                },
            )

    def fake_pinned_client(url, address=None, timeout=None, **kwargs):
        return _Client()

    for module in (outbound_guard, gateway_provider):
        monkeypatch.setattr(module, "pinned_client", fake_pinned_client, raising=False)
        monkeypatch.setattr(
            module,
            "resolve_public_address",
            lambda url, **kw: "203.0.113.10",
            raising=False,
        )
        monkeypatch.setattr(
            module,
            "resolve_for_injection",
            lambda ref, **kw: API_KEY if "key" in ref.name else BASE_URL,
            raising=False,
        )
    monkeypatch.setattr(
        credentials,
        "resolve_for_injection",
        lambda ref, **kw: API_KEY if "key" in ref.name else BASE_URL,
        raising=False,
    )

    current["provider"] = "lmstudio"
    lmstudio_envelopes.append(
        inference_providers.LMStudioProvider().chat(
            [{"role": "user", "content": "hello"}],
            model="m",
            num_ctx=128,
            temperature=0.1,
        )
    )

    current["provider"] = "gateway"
    gateway_envelopes.append(
        GatewayProvider().chat(
            [{"role": "user", "content": "hello"}],
            model="m",
            num_ctx=128,
            temperature=0.1,
        )
    )

    def _shaped(observations: list[dict], name: str) -> bool:
        """True when a helper's result is what one of the observations holds."""
        for produced in marked.get(name, []):
            items = {
                key: value
                for key, value in produced.items()
                if key != "__shared_helper__"
            }
            if not items:
                continue
            for observation in observations:
                if all(observation.get(key) == value for key, value in items.items()):
                    return True
        return False

    shared_by_both = [
        name for name in called_by if called_by[name] >= {"lmstudio", "gateway"}
    ]

    shared_bodies = [
        name
        for name in shared_by_both
        if _shaped(lmstudio_bodies + gateway_bodies, name)
    ]
    shared_envelopes = [
        name
        for name in shared_by_both
        if _shaped(lmstudio_envelopes + gateway_envelopes, name)
    ]

    assert shared_bodies, (
        "the request body is not built by a module-level helper both providers "
        "call - the body mapping was copied instead of shared"
    )
    assert shared_envelopes, (
        "the response mapping is not done by a module-level helper both "
        "providers call - the mapping was copied instead of shared"
    )


# ---- the deployment documentation ---------------------------------------------


def _gateway_doc_section() -> str:
    lines = DOC.read_text(encoding="utf-8").splitlines()
    start = level = None
    for index, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if match and "customer ai gateway" in match.group(2).lower():
            start, level = index, len(match.group(1))
            break
    assert start is not None, (
        "docs/DEPLOY_STANDALONE.md has no 'Customer AI gateway' section"
    )
    end = len(lines)
    for index in range(start + 1, len(lines)):
        match = re.match(r"^(#{1,6})\s", lines[index])
        if match and len(match.group(1)) <= level:
            end = index
            break
    return "\n".join(lines[start:end])


def test_deploy_docs_document_the_customer_ai_gateway(wire) -> None:
    _chat(GatewayProvider())
    credential_names = {ref.name for ref in wire.refs}

    section = _gateway_doc_section()
    lowered = section.lower()

    # The registry entry shape.
    assert "providers" in lowered
    assert "gateway" in lowered
    assert "models" in lowered

    # Both credentials the provider resolves.
    assert "gateway_api_key" in section
    url_credentials = credential_names - {"gateway_api_key"}
    assert url_credentials, "the provider resolved no base-URL credential"
    for name in url_credentials:
        assert name in section, f"{name} is not documented"

    # The https and public-address requirements.
    assert "https" in lowered
    assert "public" in lowered
    assert "address" in lowered
