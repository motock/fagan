"""In-process rate limiting for the remote MCP bearer gate.

Two independent token buckets guard the gate (``pipeline.remote_transport``):

* a **failed-authentication** bucket per client address, checked *before* token
  verification so a blocked client costs no JWKS work; and
* a **request** bucket per authenticated subject, checked after a token is
  verified and before the tool runs.

Both are in-process memory. A multi-replica deployment therefore needs a shared
limiter; the buckets here are per process and are not coordinated across
replicas.

Configuration is validated at construction, never per request: a bad value
raises ``ValueError`` naming the variable, so the transport refuses to start
rather than serving with a limiter that silently disabled itself.
"""

from __future__ import annotations

import ipaddress
import logging
import math
import os
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: A counter value: decimal digits only, no sign, spaces or underscores.
_COUNT_RE = re.compile(r"[0-9]+")

AUTH_FAIL_ENV = "PIPELINE_MCP_AUTH_FAIL_PER_MIN"
REQUESTS_ENV = "PIPELINE_MCP_REQUESTS_PER_MIN"
TRUSTED_PROXIES_ENV = "PIPELINE_MCP_TRUSTED_PROXIES"

DEFAULT_AUTH_FAIL_PER_MIN = 10
DEFAULT_REQUESTS_PER_MIN = 120

#: The window every counter is expressed in: "per minute".
WINDOW_SECONDS = 60.0

#: Upper bound on the number of live buckets a single set may hold.
#:
#: Bucket keys are attacker-influenced: a failed-auth key is a client address,
#: and a client can rotate source addresses cheaply (an IPv6 /64 is free), so an
#: unbounded map turns the anti-DoS control into a memory-exhaustion DoS. At the
#: bound, refilled buckets -- which are indistinguishable from fresh ones -- are
#: evicted; if every entry still carries live throttling state, the caller fails
#: closed instead of allocating.
DEFAULT_MAX_BUCKET_ENTRIES = 4096

#: An ``ipaddress`` network of either family.
Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_count(
    var_name: str, default: int, environ: Mapping[str, str] | None = None
) -> int:
    """Return the positive integer in *var_name*, or *default* when unset.

    ``0`` is the explicit off switch. Anything else that is not a positive
    integer -- a negative number, a non-integer, or an explicitly empty string
    -- raises ``ValueError`` naming the variable: a limiter that silently falls
    back to a default after a typo is a limiter nobody can reason about.
    """
    env = os.environ if environ is None else environ
    raw = env.get(var_name)
    if raw is None:
        return default
    text = str(raw)
    # Canonical decimal digits only: ``int()`` also accepts "+5", " 5" and
    # "1_000", so a typo would look like a working setting. ``0`` stays valid.
    if not _COUNT_RE.fullmatch(text):
        raise ValueError(
            f"{var_name} must be a positive integer (0 disables it); got {text!r}"
        )
    return int(text)


def parse_trusted_proxies(
    raw: str | None = None, environ: Mapping[str, str] | None = None
) -> list[Network]:
    """Parse a comma-separated list of IPs/CIDRs; empty means "trust nobody".

    A malformed entry raises ``ValueError`` naming
    ``PIPELINE_MCP_TRUSTED_PROXIES``: silently dropping a typo'd proxy would
    quietly restore the every-client-shares-one-bucket problem, and silently
    *widening* the set would trust a header from an attacker.
    """
    if raw is None:
        env = os.environ if environ is None else environ
        raw = env.get(TRUSTED_PROXIES_ENV)
    if raw is None:
        return []
    text = str(raw).strip()
    if not text:
        return []
    networks: list[Network] = []
    for part in text.split(","):
        entry = part.strip()
        if not entry:
            raise ValueError(
                f"{TRUSTED_PROXIES_ENV} must be a comma-separated list of IPs or "
                "CIDRs with no empty entry"
            )
        try:
            # strict=True: "10.0.0.5/8" means "10.0.0.0/8" to a lenient parser,
            # silently trusting a far wider range than written. Refuse instead.
            networks.append(ipaddress.ip_network(entry, strict=True))
        except ValueError:
            raise ValueError(
                f"{TRUSTED_PROXIES_ENV} must be a comma-separated list of IPs or "
                f"CIDRs with no host bits set; got {entry!r}"
            ) from None
    normalised = [str(network) for network in networks]
    logger.info(
        "trusted proxy set configured: %s",
        ", ".join(normalised),
        extra={"trusted_proxies": normalised},
    )
    return networks


def _header_values(scope: dict[str, Any], name: str) -> list[str]:
    """Return every value of header *name* in a raw ASGI scope, case-insensitively."""
    wanted = name.lower().encode("latin-1")
    return [
        value.decode("latin-1")
        for key, value in scope.get("headers") or ()
        if key.lower() == wanted
    ]


def _peer_ip(scope: dict[str, Any]) -> str:
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client:
        return str(client[0])
    return ""


def client_ip(scope: dict[str, Any], trusted: Iterable[Network] = ()) -> str:
    """Return the client address to key a bucket on.

    The ASGI peer is the *proxy* behind a reverse proxy, so keying on it alone
    gives every client one shared bucket and lets one attacker lock everyone
    out. When -- and only when -- the peer is a trusted proxy, walk
    ``X-Forwarded-For`` from the right: the right-most entry that is not itself
    a trusted proxy is the first hop the trust chain actually vouches for.

    Trusted ``10.0.0.0/8``, peer ``10.0.0.5``, ``X-Forwarded-For:
    203.0.113.9, 10.0.0.7`` -> walk right to left; ``10.0.0.7`` is trusted so
    skip it; ``203.0.113.9`` is not, so key on it. A second client behind the
    same proxy sends ``198.51.100.4, 10.0.0.7`` -> key ``198.51.100.4``, a
    separate bucket. Taking the left-most entry hands the client control of its
    own bucket; taking the right-most blindly collapses every proxied client
    into ``10.0.0.7``; honouring the header from an untrusted peer lets anyone
    mint unlimited fresh buckets. A hop that is not an IP address is skipped --
    it is client-supplied, and treating it as "give up" would collapse every
    client that sends one onto the proxy's shared bucket. When no well-formed
    untrusted hop is left, the peer is the key.
    """
    peer = _peer_ip(scope)
    networks = tuple(trusted)
    if not networks:
        return peer
    try:
        peer_address = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if not any(peer_address in network for network in networks):
        return peer
    hops: list[str] = []
    for value in _header_values(scope, "x-forwarded-for"):
        hops.extend(part.strip() for part in value.split(",") if part.strip())
    for entry in reversed(hops):
        try:
            candidate = ipaddress.ip_address(entry)
        except ValueError:
            # A junk hop is client-supplied: skip it rather than collapsing
            # every such client onto the proxy's address, which would hand an
            # attacker a way to widen its own blast radius to everyone.
            continue
        if not any(candidate in network for network in networks):
            return str(candidate)
    return peer


class TokenBucket:
    """Token bucket: *capacity* tokens, refilled over *window_seconds*.

    Capacity 10 over a 60 s window refills at 10/60 = 1/6 token per second and
    starts full at 10.0.

    Worked example (capacity 10, window 60 s, ``last = 0``): ten failures at
    ``t=0`` consume all ten (tokens 0.0). The 11th at ``t=0`` is denied and
    tokens stay 0.0. At ``t=1`` the next attempt credits 1 x 1/6 = 0.1667
    tokens, still < 1, so it is denied -- and ``last`` becomes 1. Same at
    ``t=2`` (0.3333), ``t=3`` (0.5), ``t=4`` (0.6667), ``t=5`` (0.8333), all
    denied; at ``t=6`` tokens reach 1.0, the failure is accepted, tokens go back
    to 0.0 and ``last`` becomes 6. A denial path that skips the refill
    bookkeeping (credits nothing, leaves ``last`` alone) still returns the
    correct 429 on the 11th call, then wrongly denies at ``t=6`` -- losing a
    whole token of credit per rejection.

    Backwards clock (also a real case under an NTP step correction): tokens 0.0,
    ``last = 100``. The clock jumps back to 40, so elapsed is negative; credit 0
    tokens, leave tokens at 0.0 and keep ``last = 100`` (never move it
    backwards), and deny that call. A follow-up at ``t=100`` sees elapsed 0 and
    is still denied. Setting ``last = 40`` while still returning "denied" makes
    ``t=100`` compute elapsed 60 -> 10 tokens, refilling a full window in zero
    real seconds.
    """

    def __init__(
        self,
        capacity: int,
        window_seconds: float = WINDOW_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.capacity = float(capacity)
        self.window_seconds = float(window_seconds)
        self._clock = clock or time.monotonic
        self._rate = self.capacity / self.window_seconds if self.window_seconds else 0.0
        self._tokens = self.capacity
        self._last = self._clock()
        self._audited = False

    @property
    def rate(self) -> float:
        """Tokens credited per second."""
        return self._rate

    def _credited(self, now: float) -> float:
        """Tokens *now* would hold, without mutating anything."""
        elapsed = now - self._last
        if elapsed <= 0:
            return self._tokens
        return min(self.capacity, self._tokens + elapsed * self._rate)

    def _refill(self, now: float) -> None:
        """Credit elapsed time; a rejection window ends when the bucket refills."""
        elapsed = now - self._last
        if elapsed <= 0:
            return
        self._tokens = min(self.capacity, self._tokens + elapsed * self._rate)
        self._last = now
        if self._tokens >= self.capacity:
            self._audited = False

    def peek(self) -> tuple[bool, float]:
        """Return ``(allowed, retry_after_seconds)`` without changing any state."""
        return self._decide(self._credited(self._clock()))

    def consume(self) -> tuple[bool, float]:
        """Take one token if available; return ``(allowed, retry_after_seconds)``."""
        self._refill(self._clock())
        allowed, retry_after = self._decide(self._tokens)
        if allowed:
            self._tokens -= 1.0
        return allowed, retry_after

    def _decide(self, tokens: float) -> tuple[bool, float]:
        if tokens >= 1.0:
            return True, 0.0
        missing = 1.0 - tokens
        seconds = missing / self._rate if self._rate > 0 else self.window_seconds
        # Round up: a client that retries on ``floor`` comes back too early and
        # stays throttled. Round first, because float error alone can turn an
        # exact 5.0 into 5.000000000000001 and round it up to 6.
        return False, max(1.0, math.ceil(round(seconds, 9)))

    def is_refilled(self, now: float | None = None) -> bool:
        """Return ``True`` when the bucket behaves exactly like a fresh one.

        A fresh bucket starts full, and the rejection-audit flag clears once the
        bucket refills, so a refilled bucket carries no state worth keeping and
        can be dropped without loosening the limit.
        """
        moment = self._clock() if now is None else now
        return self._credited(moment) >= self.capacity

    def mark_audited(self) -> bool:
        """Return ``True`` only for the first rejection recorded in this window."""
        if self._audited:
            return False
        self._audited = True
        return True


@dataclass
class Decision:
    """One rate-limit decision: allowed, how long to wait, and what to audit."""

    allowed: bool
    retry_after: int = 0
    audit_key: str | None = None


@dataclass
class _BucketSet:
    """The buckets of one limiter, keyed by client IP or by subject.

    The map is bounded by *max_entries*. Keys are attacker-influenced (a client
    address is, and rotating addresses is cheap), so an unbounded map would let
    the rate limiter be the memory-exhaustion vector it exists to prevent. When
    the map is full, buckets that have refilled are dropped first -- they are
    behaviourally identical to a fresh bucket -- and if nothing is evictable the
    set refuses to allocate and the caller fails closed.
    """

    capacity: int
    clock: Callable[[], float]
    max_entries: int = DEFAULT_MAX_BUCKET_ENTRIES
    buckets: dict[str, TokenBucket] = field(default_factory=dict)
    #: Set once a rejection was audited because the map was too full to hold a
    #: bucket for the key; clears again as soon as the map has room. Keeps the
    #: fail-closed path from writing one audit record per request.
    saturated_audited: bool = False

    def bucket(self, key: str) -> TokenBucket | None:
        """Return the bucket for *key*, allocating it, or ``None`` when full."""
        found = self.buckets.get(key)
        if found is not None:
            return found
        if len(self.buckets) >= self.max_entries:
            self._evict_refilled()
            if len(self.buckets) >= self.max_entries:
                return None
        found = TokenBucket(self.capacity, WINDOW_SECONDS, self.clock)
        self.buckets[key] = found
        self.saturated_audited = False
        return found

    def _evict_refilled(self) -> None:
        """Drop every bucket that has refilled; oldest entries are scanned first."""
        now = self.clock()
        for key in [k for k, b in self.buckets.items() if b.is_refilled(now)]:
            del self.buckets[key]

    def note_saturated(self) -> bool:
        """Return ``True`` only the first time saturation is the reason to audit."""
        if self.saturated_audited:
            return False
        self.saturated_audited = True
        return True


class RateLimiter:
    """The gate's two buckets: failed auths per client IP, requests per subject.

    A capacity of ``0`` disables that bucket explicitly: every call is allowed
    and nothing is tracked. Buckets are in-process memory; a multi-replica
    deployment needs a shared limiter instead.

    Each bucket set is bounded (``max_entries``). Client addresses are
    attacker-supplied, so a full set evicts refilled buckets and otherwise
    refuses new ones: the limiter fails closed rather than growing without
    bound, which would make it the memory-exhaustion vector it guards against.
    """

    def __init__(
        self,
        auth_fail_per_min: int,
        requests_per_min: int,
        *,
        clock: Callable[[], float] | None = None,
        trusted_proxies: Iterable[Network] = (),
        max_entries: int = DEFAULT_MAX_BUCKET_ENTRIES,
    ) -> None:
        self._clock = clock or time.monotonic
        self.max_entries = max_entries
        self._failed_auth = _BucketSet(auth_fail_per_min, self._clock, max_entries)
        self._requests = _BucketSet(requests_per_min, self._clock, max_entries)
        self.trusted_proxies: tuple[Network, ...] = tuple(trusted_proxies)

    def client_ip(self, scope: dict[str, Any]) -> str:
        """Resolve the client address for *scope* under the trusted-proxy rule."""
        return client_ip(scope, self.trusted_proxies)

    @property
    def failed_auth_buckets(self) -> int:
        """Number of live client buckets; bounded by ``max_entries``."""
        return len(self._failed_auth.buckets)

    @property
    def request_buckets(self) -> int:
        """Number of live subject buckets; bounded by ``max_entries``."""
        return len(self._requests.buckets)

    def peek_failed_auth(self, client: str) -> Decision:
        """Read-only check of the failed-auth bucket: consumes no token.

        The pre-auth check must not consume and must not touch the
        rejection-audit flag, because only a request actually answered ``401``
        charges the bucket. A client with no bucket at all is allowed unless the
        set is saturated, in which case it is refused (fail closed) without
        allocating.
        """
        if self._failed_auth.capacity <= 0:
            return Decision(True)
        bucket = self._failed_auth.bucket(client)
        if bucket is None:
            return Decision(False, _saturated_retry_after())
        allowed, retry_after = bucket.peek()
        return Decision(allowed, int(retry_after))

    def record_failed_auth(self, client: str) -> Decision:
        """Charge one failed-authentication token for *client*.

        Consumed only when a ``401`` is actually produced, so a request that
        succeeds never pays for its own retry.
        """
        if self._failed_auth.capacity <= 0:
            return Decision(True)
        bucket = self._failed_auth.bucket(client)
        if bucket is None:
            return Decision(False, _saturated_retry_after())
        allowed, retry_after = bucket.consume()
        return Decision(allowed, int(retry_after))

    def note_rejection(self, client: str) -> bool:
        """Mark a rejection of *client*; ``True`` only the first one per window."""
        if self._failed_auth.capacity <= 0:
            return False
        bucket = self._failed_auth.bucket(client)
        if bucket is None:
            return self._failed_auth.note_saturated()
        return bucket.mark_audited()

    def check_request(self, subject: str) -> Decision:
        """Charge one request token for *subject*, or refuse the request."""
        if self._requests.capacity <= 0:
            return Decision(True)
        bucket = self._requests.bucket(subject)
        if bucket is None:
            decision = Decision(False, _saturated_retry_after())
            if self._requests.note_saturated():
                decision.audit_key = subject
            return decision
        allowed, retry_after = bucket.consume()
        decision = Decision(allowed, int(retry_after))
        if not allowed and bucket.mark_audited():
            decision.audit_key = subject
        return decision


def _saturated_retry_after() -> int:
    """Retry delay when the bucket map is full of live buckets: one window."""
    return max(1, math.ceil(WINDOW_SECONDS))
