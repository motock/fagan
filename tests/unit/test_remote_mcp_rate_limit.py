"""Rate limiting at the remote MCP bearer gate (RMH-01).

The middleware is driven directly with a hand-built ASGI ``scope``, a fake
``send`` and a fake clock injected through ``BearerAuthMiddleware(..., clock=)``:
no server, no socket, no Starlette. Each test asserts one property of the gate.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

# pipeline.server is imported explicitly; import order is not required for
# cold-importability (every pipeline module imports cold, see
# tests/unit/test_hub_satellite_cold_imports.py).
import pipeline.server  # noqa: F401
from app.identity import IdentityError, Principal
from app.oidc import OidcProvider
from pipeline import remote_transport
from pipeline.remote_transport import BearerAuthMiddleware

AUTH_FAIL_ENV = "PIPELINE_MCP_AUTH_FAIL_PER_MIN"
REQUESTS_ENV = "PIPELINE_MCP_REQUESTS_PER_MIN"
TRUSTED_PROXIES_ENV = "PIPELINE_MCP_TRUSTED_PROXIES"
MAX_BUCKETS_ENV = "PIPELINE_MCP_MAX_BUCKETS"

TOKEN = "s3cr3t-token"

_PRINCIPALS = {
    "alice-token": Principal(subject="alice", auth_method="oidc"),
    "bob-token": Principal(subject="bob", auth_method="oidc"),
}


class _FakeClock:
    """Injectable monotonic clock: ``now()`` reads whatever the test set."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t


class _StubProvider(OidcProvider):
    """Identity provider double: accepts two known tokens, rejects the rest.

    ``authenticate`` counts its calls so a test can prove a throttled request
    never reaches token verification (hence no JWKS work).
    """

    def __init__(self) -> None:
        self._settings = None
        self.calls = 0

    def authenticate(self, credential: str | None) -> Principal:
        self.calls += 1
        principal = _PRINCIPALS.get(credential or "")
        if principal is None:
            raise IdentityError("credential rejected")
        return principal


class _RecordingApp:
    """Downstream ASGI app: records that it was reached, then sends 200."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        self.calls += 1
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})


def _http_scope(
    *headers: tuple[bytes, bytes], client: str = "203.0.113.9"
) -> dict[str, Any]:
    return {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "client": (client, 51234),
        "headers": list(headers),
    }


def _auth(value: str) -> tuple[bytes, bytes]:
    return (b"authorization", f"Bearer {value}".encode("latin-1"))


def _xff(value: str) -> tuple[bytes, bytes]:
    return (b"x-forwarded-for", value.encode("latin-1"))


def _send(
    middleware: BearerAuthMiddleware,
    scope: dict[str, Any],
    sent: list[dict[str, Any]],
) -> None:
    """Drive exactly one request through the middleware."""

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    asyncio.run(middleware(scope, receive, send))


def _status(sent: list[dict[str, Any]]) -> int:
    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert starts, f"no response start in {sent!r}"
    return int(starts[0]["status"])


def _header(sent: list[dict[str, Any]], name: str) -> bytes | None:
    starts = [m for m in sent if m["type"] == "http.response.start"]
    for key, value in starts[0].get("headers") or []:
        if key.decode("latin-1").lower() == name:
            return value
    return None


def _body(sent: list[dict[str, Any]]) -> bytes:
    return b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )


@pytest.fixture
def clock() -> _FakeClock:
    return _FakeClock()


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in (AUTH_FAIL_ENV, REQUESTS_ENV, TRUSTED_PROXIES_ENV, MAX_BUCKETS_ENV):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def gate(clock: _FakeClock) -> tuple[BearerAuthMiddleware, _RecordingApp, _StubProvider]:
    app = _RecordingApp()
    provider = _StubProvider()
    middleware = BearerAuthMiddleware(app, provider, clock=clock.now)
    return middleware, app, provider


# ---------------------------------------------------------------------------
# Part 1: the failed-authentication bucket
# ---------------------------------------------------------------------------


def test_eleventh_failed_auth_in_a_minute_is_429_without_provider(
    gate: tuple[BearerAuthMiddleware, _RecordingApp, _StubProvider],
) -> None:
    middleware, app, provider = gate

    for attempt in range(10):
        sent: list[dict[str, Any]] = []
        _send(middleware, _http_scope(_auth(TOKEN)), sent)
        assert _status(sent) == 401, f"attempt {attempt + 1} should still be 401"
    assert provider.calls == 10

    sent = []
    _send(middleware, _http_scope(_auth(TOKEN)), sent)

    assert _status(sent) == 429
    assert provider.calls == 10, "a throttled client must cost no token verification"
    assert app.calls == 0
    assert _header(sent, "retry-after") is not None


def test_failed_auth_bucket_refills_after_the_window(
    gate: tuple[BearerAuthMiddleware, _RecordingApp, _StubProvider],
    clock: _FakeClock,
) -> None:
    middleware, _app, provider = gate

    for _ in range(10):
        _send(middleware, _http_scope(_auth(TOKEN)), [])

    sent: list[dict[str, Any]] = []
    _send(middleware, _http_scope(_auth(TOKEN)), sent)
    assert _status(sent) == 429

    clock.t = 60.0
    sent = []
    _send(middleware, _http_scope(_auth("alice-token")), sent)
    assert _status(sent) == 200
    assert provider.calls == 11


def test_failed_auth_bucket_credits_partial_tokens_on_each_denial(
    gate: tuple[BearerAuthMiddleware, _RecordingApp, _StubProvider],
    clock: _FakeClock,
) -> None:
    """The denial path must still credit elapsed time and advance ``last``.

    Capacity 10 per 60 s refills at 1/6 token per second. After ten failures at
    ``t=0`` the bucket is empty, so attempts at ``t=1``..``t=5`` are denied but
    each must bank its 0.1667 token. At ``t=6`` the bucket holds exactly 1.0 and
    the failure is accepted. An implementation that returns the right 429 on a
    denied call yet skips the refill bookkeeping denies here until ``t=7``.
    """
    middleware, _app, provider = gate

    for _ in range(10):
        _send(middleware, _http_scope(_auth(TOKEN)), [])

    for second in range(1, 6):
        clock.t = float(second)
        sent: list[dict[str, Any]] = []
        _send(middleware, _http_scope(_auth(TOKEN)), sent)
        assert _status(sent) == 429, f"t={second} should still be throttled"

    clock.t = 6.0
    sent = []
    _send(middleware, _http_scope(_auth(TOKEN)), sent)

    assert _status(sent) == 401, "t=6 has banked a whole token"
    assert provider.calls == 11


def test_a_backwards_clock_does_not_refill_the_bucket(
    gate: tuple[BearerAuthMiddleware, _RecordingApp, _StubProvider],
    clock: _FakeClock,
) -> None:
    """A clock step backwards must not be banked as elapsed time later.

    Emptying the bucket at ``t=100`` then reading ``t=40`` must credit nothing
    and leave the reference point at 100, so the follow-up at ``t=100`` is still
    throttled. Moving the reference point back to 40 would make ``t=100`` look
    like a whole window of elapsed time, refilling the bucket in zero seconds.
    """
    middleware, _app, _provider = gate

    clock.t = 100.0
    for _ in range(10):
        _send(middleware, _http_scope(_auth(TOKEN)), [])

    clock.t = 40.0
    sent: list[dict[str, Any]] = []
    _send(middleware, _http_scope(_auth(TOKEN)), sent)
    assert _status(sent) == 429

    clock.t = 100.0
    sent = []
    _send(middleware, _http_scope(_auth(TOKEN)), sent)
    assert _status(sent) == 429, "a backwards clock must not refill the bucket"


def test_retry_after_rounds_up_to_the_next_whole_token(
    gate: tuple[BearerAuthMiddleware, _RecordingApp, _StubProvider],
    clock: _FakeClock,
) -> None:
    """At ``t=1`` the bucket is missing 0.8333 tokens at 1/6 per second -> 5 s."""
    middleware, _app, _provider = gate

    for _ in range(10):
        _send(middleware, _http_scope(_auth(TOKEN)), [])

    clock.t = 1.0
    sent: list[dict[str, Any]] = []
    _send(middleware, _http_scope(_auth(TOKEN)), sent)

    assert _status(sent) == 429
    assert _header(sent, "retry-after") == b"5"


def test_spoofed_forwarded_for_is_ignored_without_trusted_proxies(
    gate: tuple[BearerAuthMiddleware, _RecordingApp, _StubProvider],
) -> None:
    middleware, _app, _provider = gate

    for index in range(11):
        sent: list[dict[str, Any]] = []
        _send(
            middleware,
            _http_scope(_auth(TOKEN), _xff(f"198.51.100.{index}"), client="203.0.113.9"),
            sent,
        )

    assert _status(sent) == 429, "an untrusted peer cannot mint fresh buckets"


def test_malformed_forwarded_for_falls_back_to_the_peer(
    env: pytest.MonkeyPatch, clock: _FakeClock
) -> None:
    env.setenv(TRUSTED_PROXIES_ENV, "10.0.0.0/8")
    middleware = BearerAuthMiddleware(_RecordingApp(), _StubProvider(), clock=clock.now)
    proxy = "10.0.0.5"

    for _ in range(10):
        _send(
            middleware,
            _http_scope(_auth(TOKEN), _xff("not-an-ip"), client=proxy),
            [],
        )

    sent: list[dict[str, Any]] = []
    _send(middleware, _http_scope(_auth(TOKEN), _xff("not-an-ip"), client=proxy), sent)
    assert _status(sent) == 429, "a malformed header keys on the proxy, not a new bucket"


def test_two_clients_behind_a_trusted_proxy_get_separate_buckets(
    env: pytest.MonkeyPatch, clock: _FakeClock
) -> None:
    env.setenv(TRUSTED_PROXIES_ENV, "10.0.0.0/8")
    app = _RecordingApp()
    provider = _StubProvider()
    middleware = BearerAuthMiddleware(app, provider, clock=clock.now)
    proxy = "10.0.0.5"

    for _ in range(10):
        _send(
            middleware,
            _http_scope(_auth(TOKEN), _xff("203.0.113.9, 10.0.0.7"), client=proxy),
            [],
        )

    sent: list[dict[str, Any]] = []
    _send(
        middleware,
        _http_scope(_auth(TOKEN), _xff("203.0.113.9, 10.0.0.7"), client=proxy),
        sent,
    )
    assert _status(sent) == 429

    sent = []
    _send(
        middleware,
        _http_scope(_auth("alice-token"), _xff("198.51.100.4, 10.0.0.7"), client=proxy),
        sent,
    )
    assert _status(sent) == 200, "a second client behind the proxy has its own bucket"


# ---------------------------------------------------------------------------
# Part 2: the per-subject request bucket, configuration, audit and body
# ---------------------------------------------------------------------------


def test_subject_over_limit_is_429_and_another_subject_is_unaffected(
    env: pytest.MonkeyPatch, clock: _FakeClock
) -> None:
    env.setenv(REQUESTS_ENV, "2")
    app = _RecordingApp()
    provider = _StubProvider()
    middleware = BearerAuthMiddleware(app, provider, clock=clock.now)

    for _ in range(2):
        sent: list[dict[str, Any]] = []
        _send(middleware, _http_scope(_auth("alice-token")), sent)
        assert _status(sent) == 200
    assert app.calls == 2

    sent = []
    _send(middleware, _http_scope(_auth("alice-token")), sent)
    assert _status(sent) == 429
    assert app.calls == 2, "a throttled subject never reaches the tool"

    sent = []
    _send(middleware, _http_scope(_auth("bob-token")), sent)
    assert _status(sent) == 200, "another subject keeps its own bucket"
    assert app.calls == 3


def test_zero_disables_the_request_bucket(
    env: pytest.MonkeyPatch, clock: _FakeClock
) -> None:
    env.setenv(REQUESTS_ENV, "0")
    middleware = BearerAuthMiddleware(_RecordingApp(), _StubProvider(), clock=clock.now)

    for _ in range(500):
        sent: list[dict[str, Any]] = []
        _send(middleware, _http_scope(_auth("alice-token")), sent)
        assert _status(sent) == 200


def test_zero_disables_the_failed_auth_bucket(
    env: pytest.MonkeyPatch, clock: _FakeClock
) -> None:
    env.setenv(AUTH_FAIL_ENV, "0")
    middleware = BearerAuthMiddleware(_RecordingApp(), _StubProvider(), clock=clock.now)

    for _ in range(500):
        sent: list[dict[str, Any]] = []
        _send(middleware, _http_scope(_auth(TOKEN)), sent)
        assert _status(sent) == 401


@pytest.mark.parametrize("value", ["-1", "abc", "", "1.5", "ten"])
def test_invalid_auth_fail_count_refuses_startup(
    env: pytest.MonkeyPatch, clock: _FakeClock, value: str
) -> None:
    env.setenv(AUTH_FAIL_ENV, value)
    with pytest.raises(ValueError, match=AUTH_FAIL_ENV):
        BearerAuthMiddleware(_RecordingApp(), _StubProvider(), clock=clock.now)


@pytest.mark.parametrize("value", ["-1", "abc", "", "1.5", "many"])
def test_invalid_request_count_refuses_startup(
    env: pytest.MonkeyPatch, clock: _FakeClock, value: str
) -> None:
    env.setenv(REQUESTS_ENV, value)
    with pytest.raises(ValueError, match=REQUESTS_ENV):
        BearerAuthMiddleware(_RecordingApp(), _StubProvider(), clock=clock.now)


def test_malformed_trusted_proxy_cidr_refuses_startup(
    env: pytest.MonkeyPatch, clock: _FakeClock
) -> None:
    env.setenv(TRUSTED_PROXIES_ENV, "10.0.0.0/8, not-an-ip")
    with pytest.raises(ValueError, match=TRUSTED_PROXIES_ENV):
        BearerAuthMiddleware(_RecordingApp(), _StubProvider(), clock=clock.now)


def test_one_host_audit_record_per_client_rejection_window(
    env: pytest.MonkeyPatch, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    records: list[dict[str, Any]] = []

    def _record(**kwargs: Any) -> dict[str, Any]:
        records.append(kwargs)
        return kwargs

    monkeypatch.setattr(remote_transport, "append_host_audit", _record)
    middleware = BearerAuthMiddleware(_RecordingApp(), _StubProvider(), clock=clock.now)

    for _ in range(10):
        _send(middleware, _http_scope(_auth(TOKEN)), [])
    assert [r for r in records if r["action"] == "mcp:rate_limited"] == []

    for _ in range(5):
        sent: list[dict[str, Any]] = []
        _send(middleware, _http_scope(_auth(TOKEN)), sent)
        assert _status(sent) == 429

    limited = [r for r in records if r["action"] == "mcp:rate_limited"]
    assert len(limited) == 1, "only the first rejection of a window is audited"

    clock.t = 120.0
    for _ in range(11):
        _send(middleware, _http_scope(_auth(TOKEN)), [])

    limited = [r for r in records if r["action"] == "mcp:rate_limited"]
    assert len(limited) == 2, "the flag clears when the bucket refills"


def test_429_body_matches_the_401_shape_and_leaks_nothing(
    env: pytest.MonkeyPatch, clock: _FakeClock
) -> None:
    limit = "1"
    env.setenv(REQUESTS_ENV, limit)
    middleware = BearerAuthMiddleware(_RecordingApp(), _StubProvider(), clock=clock.now)

    unauthorised: list[dict[str, Any]] = []
    _send(middleware, _http_scope(_auth(TOKEN)), unauthorised)
    assert _status(unauthorised) == 401

    first: list[dict[str, Any]] = []
    _send(middleware, _http_scope(_auth("alice-token")), first)
    assert _status(first) == 200

    throttled: list[dict[str, Any]] = []
    _send(middleware, _http_scope(_auth("alice-token")), throttled)
    assert _status(throttled) == 429

    body = _body(throttled)
    payload = json.loads(body)
    assert payload == json.loads(_body(unauthorised)), "same JSON shape as the 401 path"
    assert _header(throttled, "content-type") == b"application/json"
    assert _header(throttled, "x-correlation-id") is not None

    retry_after = _header(throttled, "retry-after")
    assert retry_after is not None
    assert int(retry_after) >= 1

    for secret in (TOKEN.encode(), b"alice", b"203.0.113.9", b"ratelimit", b"RateLimit",
                   limit.encode()):
        assert secret not in body


# ---------------------------------------------------------------------------
# Part 5: the bucket map is bounded (client addresses are attacker-influenced)
# ---------------------------------------------------------------------------


def test_bucket_map_is_bounded_and_never_becomes_a_lockout(
    gate: tuple[BearerAuthMiddleware, _RecordingApp, _StubProvider],
    clock: _FakeClock,
) -> None:
    """Rotating source addresses must not grow the bucket map without bound.

    A full map stops *tracking* new keys; it never refuses them. Refusing is
    what would let one attacker who fills the map deny service to every client
    sharing the process, which is a cheaper outage than the memory exhaustion
    the bound exists to prevent.
    """
    middleware, _app, provider = gate
    limiter = middleware._limiter
    limit = limiter.max_entries
    assert limit > 0, "the failed-auth bucket set must be bounded"

    async def run(scope: dict[str, Any]) -> int:
        sent: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        await middleware(scope, receive, send)
        return _status(sent)

    async def burst(clients: list[str], credential: str) -> list[int]:
        return [
            await run(_http_scope(_auth(credential), client=client))
            for client in clients
        ]

    # One distinct client address per request, so each gets its own bucket.
    statuses = asyncio.run(
        burst([f"2001:db8:{index:x}::1" for index in range(limit)], "bad-token")
    )
    assert statuses == [401] * limit, "each distinct client is charged normally"
    assert limiter.failed_auth_buckets <= limit

    before = provider.calls
    overflow = asyncio.run(burst(["2001:db8:dead:beef::1"], "bad-token"))
    assert overflow == [401], "a full map drops tracking, it does not refuse the caller"
    assert provider.calls == before + 1, "an untracked failed auth still verifies"
    assert limiter.failed_auth_buckets <= limit

    # Once the live buckets refill they carry no state worth keeping, so they
    # are dropped and the set tracks new clients again.
    clock.t = 60.0
    fresh = asyncio.run(burst(["2001:db8:dead:beef::2"], "bad-token"))
    assert fresh == [401], "refilled buckets are evictable, so the set recovers"
    assert limiter.failed_auth_buckets <= limit


def test_malformed_forwarded_for_hop_does_not_collapse_clients_into_one_bucket(
    env: pytest.MonkeyPatch, clock: _FakeClock
) -> None:
    """A junk hop must not make every proxied client share the proxy's bucket."""
    env.setenv(TRUSTED_PROXIES_ENV, "10.0.0.0/8")
    middleware = BearerAuthMiddleware(_RecordingApp(), _StubProvider(), clock=clock.now)

    for _ in range(10):
        sent: list[dict[str, Any]] = []
        scope = _http_scope(
            _auth("bad-token"), _xff("203.0.113.9, not-an-ip"), client="10.0.0.5"
        )
        _send(middleware, scope, sent)
        assert _status(sent) == 401

    blocked: list[dict[str, Any]] = []
    _send(
        middleware,
        _http_scope(_auth("bad-token"), _xff("203.0.113.9, not-an-ip"), client="10.0.0.5"),
        blocked,
    )
    assert _status(blocked) == 429

    other: list[dict[str, Any]] = []
    _send(
        middleware,
        _http_scope(
            _auth("bad-token"), _xff("198.51.100.4, not-an-ip"), client="10.0.0.5"
        ),
        other,
    )
    assert _status(other) == 401, "a malformed hop must not widen one client's blast radius"


@pytest.mark.parametrize("value", ["+5", " 5", "5 ", "0x10", "1_000"])
def test_non_canonical_auth_fail_count_refuses_startup(
    env: pytest.MonkeyPatch, clock: _FakeClock, value: str
) -> None:
    env.setenv(AUTH_FAIL_ENV, value)
    with pytest.raises(ValueError, match=AUTH_FAIL_ENV):
        BearerAuthMiddleware(_RecordingApp(), _StubProvider(), clock=clock.now)


def test_non_canonical_request_count_refuses_startup(
    env: pytest.MonkeyPatch, clock: _FakeClock
) -> None:
    env.setenv(REQUESTS_ENV, "+120")
    with pytest.raises(ValueError, match=REQUESTS_ENV):
        BearerAuthMiddleware(_RecordingApp(), _StubProvider(), clock=clock.now)


@pytest.mark.parametrize("value", ["10.0.0.5/8", "2001:db8::1/32", "10.0.0.0/33"])
def test_trusted_proxy_that_widens_the_set_refuses_startup(
    env: pytest.MonkeyPatch, clock: _FakeClock, value: str
) -> None:
    """A CIDR with host bits set would silently trust a wider range than written."""
    env.setenv(TRUSTED_PROXIES_ENV, value)
    with pytest.raises(ValueError, match=TRUSTED_PROXIES_ENV):
        BearerAuthMiddleware(_RecordingApp(), _StubProvider(), clock=clock.now)


def test_trusted_proxy_logs_the_normalised_set_at_startup(
    env: pytest.MonkeyPatch,
    clock: _FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    env.setenv(TRUSTED_PROXIES_ENV, " 10.0.0.0/8 , 2001:db8::/32 ")
    with caplog.at_level("INFO"):
        BearerAuthMiddleware(_RecordingApp(), _StubProvider(), clock=clock.now)
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "10.0.0.0/8" in logged
    assert "2001:db8::/32" in logged


# ---------------------------------------------------------------------------
# Part 6: per-address keying -- one client's failures never charge its neighbours
#
# The failed-auth bucket is keyed on the resolved client address, never on a
# wider block. Carrier NAT and corporate /24s put unrelated callers behind one
# address block, so a block key would let ten failures per minute from one
# caller answer 429 to every other caller in that block, even one presenting a
# valid token -- a shared-bucket lockout. An attacker that rotates source
# addresses can still fill the bounded map; a full map only stops *tracking* new
# keys, so the worst it achieves is losing its own tracking, never another
# caller's service.
# ---------------------------------------------------------------------------


def test_failed_auth_bucket_is_keyed_on_the_exact_client_address(
    env: pytest.MonkeyPatch, clock: _FakeClock
) -> None:
    """Two addresses in one /24 are two clients, not one."""
    env.setenv(AUTH_FAIL_ENV, "2")
    middleware = BearerAuthMiddleware(_RecordingApp(), _StubProvider(), clock=clock.now)
    limiter = middleware._limiter

    for client in ("203.0.113.9", "203.0.113.10"):
        sent: list[dict[str, Any]] = []
        _send(middleware, _http_scope(_auth("bad-token"), client=client), sent)
        assert _status(sent) == 401

    assert limiter.failed_auth_buckets == 2, "each address gets its own bucket"


def test_saturated_address_does_not_throttle_a_neighbour_in_the_same_block(
    env: pytest.MonkeyPatch, clock: _FakeClock
) -> None:
    """10 failures from one address must not answer 429 for the whole /24."""
    env.setenv(AUTH_FAIL_ENV, "10")
    app = _RecordingApp()
    middleware = BearerAuthMiddleware(app, _StubProvider(), clock=clock.now)

    statuses: list[int] = []
    for _ in range(11):
        sent: list[dict[str, Any]] = []
        _send(middleware, _http_scope(_auth("bad-token"), client="203.0.113.9"), sent)
        statuses.append(_status(sent))

    assert statuses[:10] == [401] * 10
    assert statuses[10] == 429, "the address that failed is throttled"

    # A different client in the same /24 with a valid credential is served.
    sent = []
    _send(middleware, _http_scope(_auth("alice-token"), client="203.0.113.10"), sent)
    assert _status(sent) == 200, "a neighbour in the same /24 is not locked out"
    assert app.calls == 1

    # A different client in the same /24 with a bad credential gets its own 401
    # rather than the neighbour's 429.
    sent = []
    _send(middleware, _http_scope(_auth("bad-token"), client="203.0.113.11"), sent)
    assert _status(sent) == 401, "a neighbour starts with a full bucket of its own"


def test_a_valid_token_is_served_while_the_failed_auth_map_is_saturated(
    env: pytest.MonkeyPatch, clock: _FakeClock
) -> None:
    """A full map must not 429 a caller whose credential verifies."""
    env.setenv(MAX_BUCKETS_ENV, "4")
    middleware = BearerAuthMiddleware(_RecordingApp(), _StubProvider(), clock=clock.now)
    limiter = middleware._limiter

    # Four distinct addresses, one failed auth each: every bucket is live (not
    # refilled) so nothing is evictable and the map is pinned at the bound.
    for index in range(4):
        sent: list[dict[str, Any]] = []
        _send(
            middleware,
            _http_scope(_auth("bad-token"), client=f"2001:db8:{index:x}::1"),
            sent,
        )
        assert _status(sent) == 401
    assert limiter.failed_auth_buckets == 4

    # A brand-new client with a valid token, from a fifth address.
    sent = []
    _send(middleware, _http_scope(_auth("bob-token"), client="2001:db8:ffff::1"), sent)
    assert _status(sent) == 200, "saturation must not deny an authenticated request"


def test_failed_auth_bucket_is_not_shared_across_a_block(
    env: pytest.MonkeyPatch, clock: _FakeClock
) -> None:
    """IPv6 neighbours in one /64 are separate clients too."""
    env.setenv(AUTH_FAIL_ENV, "1")
    middleware = BearerAuthMiddleware(_RecordingApp(), _StubProvider(), clock=clock.now)

    # 2001:db8:1:2::9 spends its single token.
    statuses: list[int] = []
    for _ in range(2):
        sent: list[dict[str, Any]] = []
        _send(
            middleware,
            _http_scope(_auth("bad-token"), client="2001:db8:1:2::9"),
            sent,
        )
        statuses.append(_status(sent))
    assert statuses == [401, 429]

    # 2001:db8:1:2::a is in the same /64 and still has its own token.
    sent = []
    _send(middleware, _http_scope(_auth("bad-token"), client="2001:db8:1:2::a"), sent)
    assert _status(sent) == 401, "the /64 is not one bucket"


# ---------------------------------------------------------------------------
# Part 7: the bucket-map bound is operator-configurable
# ---------------------------------------------------------------------------


def test_max_buckets_is_configurable_and_bounds_the_map(
    env: pytest.MonkeyPatch, clock: _FakeClock
) -> None:
    env.setenv(MAX_BUCKETS_ENV, "3")
    middleware = BearerAuthMiddleware(_RecordingApp(), _StubProvider(), clock=clock.now)
    assert middleware._limiter.max_entries == 3


@pytest.mark.parametrize("value", ["0", "-1", "many", "", "1_0", "0x10"])
def test_invalid_max_buckets_refuses_startup(
    env: pytest.MonkeyPatch, clock: _FakeClock, value: str
) -> None:
    """A bound of 0 would deny every new key, so it is refused, not honoured."""
    env.setenv(MAX_BUCKETS_ENV, value)
    with pytest.raises(ValueError, match=MAX_BUCKETS_ENV):
        BearerAuthMiddleware(_RecordingApp(), _StubProvider(), clock=clock.now)


def test_an_absurdly_long_counter_refuses_startup_naming_the_variable(
    env: pytest.MonkeyPatch, clock: _FakeClock
) -> None:
    """A digit string past the interpreter's limit must not leak its own error.

    ``int()`` raises ``ValueError`` for such a string with a message that never
    names the setting, which breaks the promise that every bad value is reported
    as "<variable> must be ...". The length is capped so our own message wins.
    """
    env.setenv(AUTH_FAIL_ENV, "1" * 5000)
    with pytest.raises(ValueError, match=AUTH_FAIL_ENV):
        BearerAuthMiddleware(_RecordingApp(), _StubProvider(), clock=clock.now)


# ---------------------------------------------------------------------------
# Part 8: saturation work is bounded (no O(n) scan per request)
# ---------------------------------------------------------------------------


def test_saturation_does_not_scan_the_whole_map_on_every_request(
    env: pytest.MonkeyPatch, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A saturated map must cost a bounded amount of work, not O(entries).

    Every request from an unknown client triggers the saturation path, so an
    eviction scan that walks all entries turns one attacker request into n units
    of CPU. The scan is capped: it inspects a constant number of entries.
    """
    from pipeline import rate_limit as rate_limit_module

    env.setenv(MAX_BUCKETS_ENV, "64")
    middleware = BearerAuthMiddleware(_RecordingApp(), _StubProvider(), clock=clock.now)
    limiter = middleware._limiter

    # Pin the map full with live buckets, one per distinct client address.
    for index in range(64):
        _send(
            middleware,
            _http_scope(_auth("bad-token"), client=f"2001:db8:{index:x}::1"),
            [],
        )
    assert limiter.failed_auth_buckets == 64

    inspected: list[int] = []
    original = rate_limit_module.TokenBucket.is_refilled

    def _counting(self: Any, now: float | None = None) -> bool:
        inspected.append(1)
        return original(self, now)

    monkeypatch.setattr(rate_limit_module.TokenBucket, "is_refilled", _counting)

    for _ in range(5):
        _send(
            middleware,
            _http_scope(_auth("bad-token"), client="2001:db8:eeee::1"),
            [],
        )

    # The scan is capped at a constant (``EVICT_SCAN_LIMIT``), so five saturated
    # requests cost a fixed amount of work -- never five times the map size.
    assert len(inspected) <= 5 * rate_limit_module.EVICT_SCAN_LIMIT, (
        f"the saturation path inspected {len(inspected)} buckets over 5 requests; "
        "it must stay bounded, not walk the whole map each time"
    )
    assert len(inspected) < 5 * 64, "the scan must not be proportional to the map"


