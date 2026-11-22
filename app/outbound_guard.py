"""Outbound request guard for tenant-supplied URLs (ADR 0009, hosted providers).

One module, one set of rules, for every outbound request the service makes to a
URL that a tenant - not the operator - supplied: a model endpoint, an OIDC
issuer's JWKS document, an OIDC token or userinfo endpoint. Keeping the rules
in a single place is the point: a second copy that drifts is how one caller
ends up less strict than the other.

Free-standing like ``app.identity``: no imports from other ``app.*`` modules, so
anything may depend on it without a cycle.

The guard has two halves, and both are load-bearing:

* the **syntactic** half (:func:`require_https_url`, :func:`is_internal_target`)
  runs without any network access, so it can be applied at configuration time on
  a host that is offline;
* the **resolving** half (:func:`resolve_public_address`, :func:`pinned_client`)
  runs per request, and the address it validates is the address the connection
  is then *pinned* to. Validating a name and then letting the HTTP client
  resolve it again is a TOCTOU hole (DNS rebinding): the name can answer
  publicly for the check and ``127.0.0.1`` for the request.

Every refusal carries a fixed reason token, never the URL: the reason lands in
logs and error responses, and the URL may contain a token or a query string.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import urllib.parse
from typing import Any

import httpx

#: Bound on name resolution. ``socket.getaddrinfo`` has no timeout of its own
#: and runs while a request is in flight, so a slow or black-holed resolver
#: would otherwise stall every caller behind one lookup.
DNS_TIMEOUT_S = 5.0

#: Schemes a tenant-supplied URL may use. Plain ``http`` is absent deliberately:
#: the address checks below would still stop an internal target, but the
#: credentials and the response would travel in the clear.
REQUIRED_SCHEME = "https"

#: Reason tokens. Fixed strings, so a caller can branch on the cause without
#: parsing a message, and so no URL fragment leaks into a log line.
REASON_SCHEME = "url must use https"
REASON_NO_HOST = "url must have a host"
REASON_USERINFO = "url must not contain credentials"
REASON_FRAGMENT = "url must not contain a fragment"


class OutboundRefused(Exception):
    """A tenant-supplied URL failed the outbound guard.

    ``reason`` is a fixed token (see the ``REASON_*`` constants); it never
    contains the URL itself.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def embedded_ipv4(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """Return the IPv4 address tunnelled inside ``address``, if any.

    Covers IPv4-mapped (``::ffff:127.0.0.1``), 6to4 (``2002:7f00:1::``) and the
    NAT64 well-known prefix (``64:ff9b::7f00:1``). Judging the wrapper alone
    would let a loopback or metadata target through in a form the outer address
    makes look public.
    """
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.sixtofour is not None:
        return address.sixtofour
    if address in ipaddress.IPv6Network("64:ff9b::/96"):
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return None


def is_internal_address(address: Any) -> bool:
    """Return True for any IP an outbound request must never be sent to.

    This is an allowlist, not a denylist: an address is acceptable only when
    :attr:`is_global` is true. Enumerating "bad" ranges instead is how the
    carrier-grade NAT block ``100.64.0.0/10`` slipped through - CPython reports
    it as neither private nor global, so a metadata endpoint that lives there
    (``100.100.100.200``) read as public. IPv4 addresses tunnelled inside IPv6
    are unwrapped first, for the same reason.
    """
    if isinstance(address, ipaddress.IPv6Address):
        embedded = embedded_ipv4(address)
        if embedded is not None:
            return is_internal_address(embedded)
    return not address.is_global


def is_internal_target(url: str) -> bool:
    """Return True if ``url`` names a loopback/private/link-local host.

    The URL may come from a tenant, so a value that points inward would
    otherwise turn the shared server into an SSRF probe against loopback or a
    cloud metadata service.

    Case and a trailing dot are folded first: ``LOCALHOST`` and ``localhost.``
    both resolve to 127.0.0.1 but neither compares equal to ``localhost``.
    This is only the syntactic half of the guard - it runs at configuration
    time, when resolving a name would fail closed on an offline host - and
    :func:`resolve_public_address` judges the actual address on every request.
    """
    try:
        host = urllib.parse.urlsplit(url).hostname
    except ValueError:
        return True
    if not host:
        return True
    host = host.casefold().rstrip(".")
    try:
        return is_internal_address(ipaddress.ip_address(host))
    except ValueError:
        return host == "localhost" or host.endswith(".localhost")


def require_https_url(url: str) -> None:
    """Raise :class:`OutboundRefused` unless ``url`` is a safe https URL.

    The negative list, each entry load-bearing:

    * the scheme must be exactly ``https`` - ``http`` sends credentials in the
      clear, and ``file``/``gopher``/``data`` are not outbound requests at all;
    * a host must be present - ``https:///path`` has none, and different HTTP
      clients disagree about what they then connect to;
    * no userinfo - ``https://user:pass@host`` hides the real host from a human
      reading the URL, and clients differ on whether they send the credentials,
      which is how "``@``-confusion" redirects to an attacker-chosen host;
    * no fragment - it is never sent to the server, so a URL carrying one means
      the caller is treating a browser-shaped URL as an endpoint, and any check
      done on the full string disagrees with what is actually requested.

    The host is *not* resolved here: this function performs no network access,
    so it is safe to call at configuration time. Use
    :func:`resolve_public_address` for the address judgement.
    """
    if not isinstance(url, str):
        # A non-string is not a URL at all; "has no host" is the honest reason
        # and keeps the token set closed.
        raise OutboundRefused(REASON_NO_HOST)
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        raise OutboundRefused(REASON_SCHEME) from None
    if parts.scheme != REQUIRED_SCHEME:
        raise OutboundRefused(REASON_SCHEME)
    if not parts.hostname:
        raise OutboundRefused(REASON_NO_HOST)
    if parts.username is not None or parts.password is not None:
        raise OutboundRefused(REASON_USERINFO)
    if parts.fragment:
        raise OutboundRefused(REASON_FRAGMENT)


def _resolve_host(
    host: str, *, allow_private: bool = False, timeout: float | None = None
) -> str | None:
    """Resolve ``host`` with a bounded deadline, returning a public address.

    ``getaddrinfo`` blocks with no timeout of its own. The lookup therefore runs
    on a short-lived daemon thread and the caller gives up after ``timeout``
    (``DNS_TIMEOUT_S`` by default); a resolver that never answers fails closed
    instead of pinning a worker until it does.

    With ``allow_private`` the name is still resolved - the address is pinned
    for the connection - but an internal result is accepted instead of refused.
    """
    deadline = DNS_TIMEOUT_S if timeout is None else timeout
    infos: list[Any] = []

    def lookup() -> None:
        try:
            infos.extend(socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP))
        except (OSError, UnicodeError):
            return

    worker = threading.Thread(target=lookup, name="outbound-resolve", daemon=True)
    worker.start()
    worker.join(deadline)
    if not infos:
        return None
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except (IndexError, TypeError, ValueError):
            return None
        if not allow_private and is_internal_address(address):
            return None
    return str(infos[0][4][0])


def resolve_public_address(
    url: str, *, allow_private: bool = False, timeout: float | None = None
) -> str | None:
    """Return the public address ``url``'s host resolves to, or None.

    None means "do not fetch": either the URL has no usable host, the name does
    not resolve at all, or any of its addresses is loopback/private/link-local.
    Judging the resolved addresses rather than the name is what stops
    ``127-0-0-1.nip.io`` (or any other public name pointing back inside) from
    reaching loopback or the cloud metadata service. Resolving again on every
    request also means a name that starts resolving inward after a cached
    answer expires is refused instead of trusted on the strength of its first
    answer.

    ``allow_private`` is the operator's opt-in for a self-hosted endpoint on a
    private network: the name is still resolved and the resulting address is
    still returned for pinning, but the internal-address rejection is skipped.
    """
    try:
        host = urllib.parse.urlsplit(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    return _resolve_host(host, allow_private=allow_private, timeout=timeout)


class PinnedTransport(httpx.HTTPTransport):
    """HTTPS transport that connects to one already-validated address.

    :func:`resolve_public_address` validates every address a name resolves to,
    but ``httpx`` would then resolve that name a second time at connect time. A
    name that answers publicly for the check and ``127.0.0.1`` for the request
    (DNS rebinding) would still be fetched, so the validated address is pinned
    here rather than re-derived: the request goes to that IP, while the
    configured hostname is kept for the ``Host`` header and for TLS SNI.
    httpcore uses the ``sni_hostname`` extension as ``server_hostname``, so the
    certificate is still verified against the name and never against the IP.
    """

    def __init__(self, address: str, hostname: str) -> None:
        super().__init__()
        self._address = address
        self._hostname = hostname

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        request.headers["Host"] = self._hostname
        request.extensions = {**request.extensions, "sni_hostname": self._hostname}
        request.url = request.url.copy_with(host=self._address)
        return super().handle_request(request)


def pinned_client(url: str, address: str, timeout: float) -> httpx.Client:
    """A client whose connections go to the already-validated ``address``.

    Redirects stay disabled: a ``30x`` from a tenant endpoint to an internal
    address would otherwise be followed to a URL that never passed the guard.
    """
    try:
        hostname = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        hostname = ""
    return httpx.Client(
        transport=PinnedTransport(address, hostname),
        timeout=httpx.Timeout(timeout),
        follow_redirects=False,
    )
