"""The outbound guard shared by model endpoints and the OIDC fetches.

Every negative here is a refusal the guard must produce, with name resolution
stubbed so the tests never touch real DNS: a test that depends on what
``example.invalid`` resolves to today is a test that fails on the next
resolver change.
"""

import ipaddress
import socket
import time

import httpx
import pytest

from app import outbound_guard


def _infos(*addresses):
    """getaddrinfo-shaped answers for ``addresses``."""
    return [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 0))
        for address in addresses
    ]


def _stub_resolver(monkeypatch, answer):
    """Stub ``getaddrinfo``; ``answer`` is a list of infos, an exception or a fn."""
    calls = []

    def getaddrinfo(host, port, *args, **kwargs):
        calls.append(host)
        resolved = answer(host) if callable(answer) else answer
        if isinstance(resolved, Exception):
            raise resolved
        return resolved

    monkeypatch.setattr(outbound_guard.socket, "getaddrinfo", getaddrinfo)
    return calls


# --- require_https_url: the syntactic half, no network ----------------------


def test_require_https_url_accepts_a_plain_https_url():
    assert outbound_guard.require_https_url("https://model.example/v1") is None


@pytest.mark.parametrize(
    "url",
    [
        "http://model.example/v1",
        "ftp://model.example/v1",
        "file:///etc/passwd",
        "gopher://model.example/v1",
        "//model.example/v1",
        "model.example/v1",
        "",
    ],
)
def test_require_https_url_refuses_every_other_scheme(url):
    with pytest.raises(outbound_guard.OutboundRefused) as excinfo:
        outbound_guard.require_https_url(url)
    assert excinfo.value.reason == outbound_guard.REASON_SCHEME


def test_require_https_url_refuses_a_url_without_a_host():
    with pytest.raises(outbound_guard.OutboundRefused) as excinfo:
        outbound_guard.require_https_url("https:///v1/chat")
    assert excinfo.value.reason == outbound_guard.REASON_NO_HOST


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@model.example/v1",
        "https://user@model.example/v1",
        "https://user:secret@127.0.0.1/v1",
    ],
)
def test_require_https_url_refuses_embedded_credentials(url):
    with pytest.raises(outbound_guard.OutboundRefused) as excinfo:
        outbound_guard.require_https_url(url)
    assert excinfo.value.reason == outbound_guard.REASON_USERINFO


def test_require_https_url_refuses_a_fragment():
    with pytest.raises(outbound_guard.OutboundRefused) as excinfo:
        outbound_guard.require_https_url("https://model.example/v1#frag")
    assert excinfo.value.reason == outbound_guard.REASON_FRAGMENT


def test_refusal_reason_never_carries_the_url():
    with pytest.raises(outbound_guard.OutboundRefused) as excinfo:
        outbound_guard.require_https_url("http://user:sekrit@model.example/v1")
    assert "sekrit" not in str(excinfo.value)
    assert "model.example" not in excinfo.value.reason


def test_refusal_is_a_typed_error_carrying_its_reason():
    refusal = outbound_guard.OutboundRefused(outbound_guard.REASON_USERINFO)
    assert isinstance(refusal, Exception)
    assert refusal.reason == outbound_guard.REASON_USERINFO
    assert str(refusal) == outbound_guard.REASON_USERINFO


def test_require_https_url_refuses_a_non_string():
    with pytest.raises(outbound_guard.OutboundRefused) as excinfo:
        outbound_guard.require_https_url(None)
    assert excinfo.value.reason == outbound_guard.REASON_NO_HOST


def test_require_https_url_does_not_resolve_anything(monkeypatch):
    calls = _stub_resolver(monkeypatch, AssertionError("must not resolve"))
    assert outbound_guard.require_https_url("https://model.example/v1") is None
    assert calls == []


# --- is_internal_address: the allowlist -------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.0.0.53",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "100.100.100.200",
        "0.0.0.0",
        "::1",
        "fe80::1",
        "fd00::1",
        "::ffff:127.0.0.1",
        "::ffff:100.64.0.1",
        "64:ff9b::7f00:1",
        "2002:7f00:1::",
    ],
)
def test_internal_addresses_are_refused(address):
    assert outbound_guard.is_internal_address(ipaddress.ip_address(address)) is True


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "2001:4860:4860::8888"])
def test_globally_routable_addresses_are_allowed(address):
    assert outbound_guard.is_internal_address(ipaddress.ip_address(address)) is False


# --- is_internal_target: the syntactic host check ---------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost/v1",
        "https://LOCALHOST/v1",
        "https://localhost./v1",
        "https://idp.localhost/v1",
        "https://127.0.0.1/v1",
        "https://169.254.169.254/latest/meta-data/",
        "https://[::1]/v1",
        "https://[::ffff:127.0.0.1]/v1",
        "https:///v1",
        "not a url",
    ],
)
def test_internal_targets_are_refused(url):
    assert outbound_guard.is_internal_target(url) is True


@pytest.mark.parametrize(
    "url", ["https://idp.example/v1", "https://8.8.8.8/v1", "https://notlocalhost/v1"]
)
def test_public_targets_are_not_refused(url):
    assert outbound_guard.is_internal_target(url) is False


# --- resolve_public_address: the resolving half -----------------------------


def test_public_name_resolves_to_its_address(monkeypatch):
    calls = _stub_resolver(monkeypatch, _infos("93.184.216.34"))
    assert (
        outbound_guard.resolve_public_address("https://model.example/v1")
        == "93.184.216.34"
    )
    assert calls == ["model.example"]


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.5", "169.254.169.254", "100.100.100.200", "::1", "fd00::1"],
)
def test_name_resolving_inward_is_refused(monkeypatch, address):
    _stub_resolver(monkeypatch, _infos(address))
    assert outbound_guard.resolve_public_address("https://model.example/v1") is None


def test_one_internal_address_among_public_ones_refuses_the_name(monkeypatch):
    # A rebinding-style answer: whichever address the client picked, the name
    # can reach loopback, so the whole name is refused.
    _stub_resolver(monkeypatch, _infos("93.184.216.34", "127.0.0.1"))
    assert outbound_guard.resolve_public_address("https://model.example/v1") is None


def test_unresolvable_name_is_refused(monkeypatch):
    _stub_resolver(monkeypatch, socket.gaierror("no such host"))
    assert outbound_guard.resolve_public_address("https://model.example/v1") is None


def test_empty_resolver_answer_is_refused(monkeypatch):
    _stub_resolver(monkeypatch, [])
    assert outbound_guard.resolve_public_address("https://model.example/v1") is None


def test_malformed_resolver_answer_is_refused(monkeypatch):
    _stub_resolver(monkeypatch, [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("x", 0))])
    assert outbound_guard.resolve_public_address("https://model.example/v1") is None


def test_empty_resolver_entry_is_refused(monkeypatch):
    _stub_resolver(monkeypatch, [()])
    assert outbound_guard.resolve_public_address("https://model.example/v1") is None


@pytest.mark.parametrize("url", ["https:///v1", "not a url", "https://[::bad/v1", ""])
def test_url_without_a_usable_host_is_refused(monkeypatch, url):
    calls = _stub_resolver(monkeypatch, _infos("93.184.216.34"))
    assert outbound_guard.resolve_public_address(url) is None
    assert calls == []


def test_hanging_resolver_gives_up_within_the_deadline(monkeypatch):
    def hang(host, port, *args, **kwargs):
        time.sleep(0.5)
        return []

    monkeypatch.setattr(outbound_guard.socket, "getaddrinfo", hang)
    started = time.monotonic()
    resolved = outbound_guard.resolve_public_address(
        "https://model.example/v1", timeout=0.05
    )
    assert resolved is None
    assert time.monotonic() - started < 0.5


def test_allow_private_accepts_an_internal_address(monkeypatch):
    _stub_resolver(monkeypatch, _infos("10.0.0.7"))
    assert outbound_guard.resolve_public_address("https://model.example/v1") is None
    assert (
        outbound_guard.resolve_public_address(
            "https://model.example/v1", allow_private=True
        )
        == "10.0.0.7"
    )


def test_allow_private_still_refuses_an_unresolvable_name(monkeypatch):
    _stub_resolver(monkeypatch, socket.gaierror("no such host"))
    assert (
        outbound_guard.resolve_public_address(
            "https://model.example/v1", allow_private=True
        )
        is None
    )


# --- pinned_client / PinnedTransport ---------------------------------------


def _record_transport(monkeypatch, response_factory):
    """Record every request that reaches the underlying transport."""
    seen = []

    def handle_request(self, request):
        seen.append(request)
        return response_factory(request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
    return seen


def test_pinned_client_connects_to_the_validated_address(monkeypatch):
    seen = _record_transport(monkeypatch, lambda request: httpx.Response(200))
    with outbound_guard.pinned_client(
        "https://model.example/v1", "93.184.216.34", 5.0
    ) as client:
        client.get("https://model.example/v1")
    assert len(seen) == 1
    assert seen[0].url.host == "93.184.216.34"
    # The configured name is kept for the Host header and for TLS SNI, so the
    # certificate is verified against the name and never against the raw IP.
    assert seen[0].headers["host"] == "model.example"
    assert seen[0].extensions["sni_hostname"] == "model.example"


def test_pinned_client_does_not_follow_a_redirect_to_an_internal_address(monkeypatch):
    # A tenant endpoint answering ``302 Location: http://127.0.0.1/admin`` must
    # not be followed: the redirect target never passed the guard.
    seen = _record_transport(
        monkeypatch,
        lambda request: httpx.Response(
            302, headers={"location": "http://127.0.0.1/admin"}
        ),
    )
    with outbound_guard.pinned_client(
        "https://model.example/v1", "93.184.216.34", 5.0
    ) as client:
        response = client.get("https://model.example/v1")
    assert response.status_code == 302
    assert len(seen) == 1
    assert all("127.0.0.1" not in str(request.url) for request in seen)


def test_pinned_client_carries_the_timeout():
    client = outbound_guard.pinned_client(
        "https://model.example/v1", "93.184.216.34", 2.5
    )
    try:
        assert client.timeout == httpx.Timeout(2.5)
    finally:
        client.close()


def test_pinned_client_tolerates_a_url_without_a_host():
    client = outbound_guard.pinned_client("https:///v1", "93.184.216.34", 5.0)
    try:
        assert isinstance(client._transport, outbound_guard.PinnedTransport)
    finally:
        client.close()


def test_pinned_transport_is_an_https_transport():
    transport = outbound_guard.PinnedTransport("93.184.216.34", "model.example")
    try:
        assert isinstance(transport, httpx.HTTPTransport)
    finally:
        transport.close()


# --- the OIDC fetches share this guard, not a copy of it --------------------


def test_oidc_login_fetches_through_the_shared_guard(monkeypatch):
    from app import oidc_login

    seen = {}

    def fake_client(url, address, timeout):
        seen["client"] = (url, address, timeout)
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(
        outbound_guard, "resolve_public_address", lambda url, **kw: "1.2.3.4"
    )
    monkeypatch.setattr(outbound_guard, "pinned_client", fake_client)
    with pytest.raises(oidc_login.OidcLoginError):
        oidc_login._fetch_json("https://idp.example/.well-known/openid-configuration")
    assert seen["client"][0] == "https://idp.example/.well-known/openid-configuration"
    assert seen["client"][1] == "1.2.3.4"
    assert seen["client"][2] == oidc_login.HTTP_TIMEOUT_S


def test_oidc_jwks_fetch_uses_the_shared_guard(monkeypatch):
    from app import oidc

    seen = {}

    def fake_client(url, address, timeout):
        seen["client"] = (url, address, timeout)
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(oidc, "_resolve", lambda url, **kw: "1.2.3.4")
    monkeypatch.setattr(outbound_guard, "pinned_client", fake_client)
    settings = oidc.OidcSettings(
        issuer="https://idp.example/",
        audience="api",
        jwks_url="https://idp.example/jwks",
    )
    with pytest.raises(oidc.IdentityError):
        oidc._fetch_jwks(settings)
    assert seen["client"] == (
        "https://idp.example/jwks",
        "1.2.3.4",
        oidc.JWKS_TIMEOUT_S,
    )
