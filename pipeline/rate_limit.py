"""In-process rate limiting for the remote MCP bearer gate.

Two independent token buckets guard the gate (``pipeline.remote_transport``):

* a **failed-authentication** bucket per resolved client address, checked
  *before* token verification so a blocked client costs no JWKS work; and
* a **request** bucket per authenticated subject, checked after a token is
  verified and before the tool runs.

The failed-auth key is the client address itself, never a wider block. Any
aggregation wider than one client -- the raw proxy peer, an IPv4 /24, an IPv6
/64 -- makes one caller's failures throttle its neighbours, and carrier NAT and
corporate /24s routinely put unrelated callers in the same block; ten failures
per minute from one of them would lock the rest out even with valid tokens. The
bucket map is bounded instead, so an attacker rotating addresses evicts refilled
buckets and can at worst lose its *own* tracking.

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
MAX_BUCKETS_ENV = "PIPELINE_MCP_MAX_BUCKETS"

DEFAULT_AUTH_FAIL_PER_MIN = 10
DEFAULT_REQUESTS_PER_MIN = 120

#: The window every counter is expressed in: "per minute".
WINDOW_SECONDS = 60.0

#: Upper bound on the number of live buckets a single set may hold.
#:
#: Bucket keys are attacker-influenced: a failed-auth key is the client address,
#: and addresses can be rotated, so an unbounded map turns the anti-DoS control
#: into a memory-exhaustion DoS. At the bound, refilled buckets -- which are
#: indistinguishable from fresh ones -- are evicted; a key that cannot get a
#: bucket after that is simply not tracked. Tracking is the only thing lost:
#: see ``_BucketSet`` for why saturation must never refuse a request.
DEFAULT_MAX_BUCKET_ENTRIES = 4096

#: How many of the oldest keys one eviction pass inspects.
#:
#: A saturated map is attacker-reachable, so the eviction scan must not be O(n):
#: every request from an unknown key would otherwise buy the attacker n units of
#: CPU work. The oldest keys are inspected because they are the ones most likely
#: to have refilled already.
EVICT_SCAN_LIMIT = 16

#: Longest counter value accepted: enough for any per-minute limit, and short
#: enough that ``int()`` cannot trip the interpreter's digit-count limit, which
#: raises an error that does not name the variable.
_MAX_COUNT_DIGITS = 9

#: An ``ipaddress`` network of either family.
Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_count(
    var_name: str,
    default: int,
    environ: Mapping[str, str] | None = None,
    *,
    allow_zero: bool = True,
) -> int:
    """Return the positive integer in *var_name*, or *default* when unset.

    ``0`` is the explicit off switch. Anything else that is not a positive
    integer -- a negative number, a non-integer, or an explicitly empty string
    -- raises ``ValueError`` naming the variable: a limiter that silently falls
    back to a default after a typo is a limiter nobody can reason about.

    Use *allow_zero* = ``False`` for a setting whose ``0`` would be a nonsense
    value rather than an off switch (a bucket-map bound of 0 would deny every
    new key), so it is refused instead of honoured.
    """
    env = os.environ if environ is None else environ
    raw = env.get(var_name)
    if raw is None:
        return default
    text = str(raw)
    # Canonical decimal digits only: ``int()`` also accepts "+5", " 5" and
    # "1_000", so a typo would look like a working setting. ``0`` stays valid
    # where it means "off".
    if not _COUNT_RE.fullmatch(text) or len(text) > _MAX_COUNT_DIGITS:
        raise ValueError(
            f"{var_name} must be a positive integer (0 disables it); got {text!r}"
        )
    value = int(text)
    if value == 0 and not allow_zero:
        raise ValueError(f"{var_name} must be a positive integer; got {text!r}")
    return value


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

    The returned address is the failed-auth bucket key exactly as resolved: the
    gate never widens it to a network block, so one caller's failures cannot
    throttle anyone else sharing its block.
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
    """The buckets of one limiter, keyed by client address or by subject.

    The map is bounded by *max_entries*. Keys are attacker-influenced (rotating
    source addresses is cheap), so an unbounded map would let the rate limiter be
    the memory-exhaustion vector it exists to prevent. When the map is full,
    buckets that have refilled are dropped first -- they are behaviourally
    identical to a fresh bucket.

    If nothing is evictable the key simply goes **untracked** for this call. It
    is never refused: refusing is what turns a full map into an outage, because
    the pre-auth check runs for every request, so refusing unknown keys would
    let one attacker who fills the map deny service to every client sharing the
    process. Losing tracking for a key costs accuracy -- that key is not
    throttled until the map drains -- while refusing costs availability. An
    availability failure reachable by the very attacker the limiter exists to
    stop is the worse of the two.
    """

    capacity: int
    clock: Callable[[], float]
    max_entries: int = DEFAULT_MAX_BUCKET_ENTRIES
    buckets: dict[str, TokenBucket] = field(default_factory=dict)
    #: Set while the map is full and nothing could be evicted, so the caller can
    #: log the condition once per episode instead of once per request.
    saturated_audited: bool = False

    def get(self, key: str) -> TokenBucket | None:
        """Return the bucket for *key* or ``None``; never allocates."""
        return self.buckets.get(key)

    def bucket(self, key: str) -> TokenBucket | None:
        """Return the bucket for *key*, allocating it, or ``None`` when full.

        ``None`` means "not tracked right now", never "refused": the caller
        allows the request. See the class docstring for why.
        """
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
        """Drop refilled buckets, scanning only the oldest ``EVICT_SCAN_LIMIT``.

        The map is attacker-fillable, so a scan proportional to its size would
        let a single request cost n units of work. Insertion order is
        oldest-first and the oldest entries are the likeliest to have refilled,
        so a constant-size prefix of the map is the right slice to inspect.
        """
        now = self.clock()
        for key in list(self.buckets)[:EVICT_SCAN_LIMIT]:
            bucket = self.buckets.get(key)
            if bucket is not None and bucket.is_refilled(now):
                del self.buckets[key]

    def note_saturated(self) -> bool:
        """Return ``True`` only the first time this saturation episode is logged."""
        if self.saturated_audited:
            return False
        self.saturated_audited = True
        return True


class RateLimiter:
    """The gate's two buckets: failed auths per client address, requests per subject.

    A capacity of ``0`` disables that bucket explicitly: every call is allowed
    and nothing is tracked. Buckets are in-process memory; a multi-replica
    deployment needs a shared limiter instead.

    Each bucket set is bounded (``max_entries``). Client addresses are
    attacker-supplied, so a full set evicts refilled buckets and then stops
    tracking new keys rather than refusing them: dropping accuracy is
    recoverable, while a full map that refuses unknown keys is a denial of
    service the attacker can trigger on purpose.
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

    def failed_auth_key(self, client: str) -> str:
        """Return the bucket key for *client*: the client's own address.

        Deliberately not a network block. One client's failures must never
        charge its neighbours: carrier NAT and corporate /24s put unrelated
        callers behind one address block, so a wider key would let ten failures
        from one caller answer ``429`` to every other caller in the block, even
        those presenting a valid token.
        """
        return client

    def peek_failed_auth(self, client: str) -> Decision:
        """Read-only check of the failed-auth bucket: consumes no token.

        The pre-auth check must not consume and must not touch the
        rejection-audit flag, because only a request actually answered ``401``
        charges the bucket. It must not allocate either: a caller that never
        fails authentication would otherwise take up slots in the map that the
        attack pressure is trying to fill. A client with no bucket, or a client
        whose key cannot be tracked because the map is full, is allowed.
        """
        if self._failed_auth.capacity <= 0:
            return Decision(True)
        bucket = self._failed_auth.get(self.failed_auth_key(client))
        if bucket is None:
            return Decision(True)
        allowed, retry_after = bucket.peek()
        return Decision(allowed, int(retry_after))

    def record_failed_auth(self, client: str) -> Decision:
        """Charge one failed-authentication token for *client*.

        Consumed only when a ``401`` is actually produced, so a request that
        succeeds never pays for its own retry. An untracked key (full map) is
        allowed to proceed: the charge is dropped, not the caller.
        """
        if self._failed_auth.capacity <= 0:
            return Decision(True)
        bucket = self._failed_auth.bucket(self.failed_auth_key(client))
        if bucket is None:
            return Decision(True)
        allowed, retry_after = bucket.consume()
        return Decision(allowed, int(retry_after))

    def note_rejection(self, client: str) -> bool:
        """Mark a rejection of *client*; ``True`` only the first one per window."""
        if self._failed_auth.capacity <= 0:
            return False
        bucket = self._failed_auth.get(self.failed_auth_key(client))
        if bucket is None:
            return False
        return bucket.mark_audited()

    def check_request(self, subject: str) -> Decision:
        """Charge one request token for *subject*, or refuse the request.

        A subject whose bucket cannot be allocated because the map is full is
        admitted and logged rather than refused, so a full subject map degrades
        to "not throttled", never to "everyone is locked out".
        """
        if self._requests.capacity <= 0:
            return Decision(True)
        bucket = self._requests.bucket(subject)
        if bucket is None:
            if self._requests.note_saturated():
                logger.warning(
                    "subject rate-limit map saturated; admitting untracked subjects",
                    extra={"outcome": "rate_limit_saturated", "bucket": "subject"},
                )
            return Decision(True)
        allowed, retry_after = bucket.consume()
        decision = Decision(allowed, int(retry_after))
        if not allowed and bucket.mark_audited():
            decision.audit_key = subject
        return decision
