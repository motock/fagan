"""Classify an Ollama model tag as cloud-served or on-device.

Historically every "served by someone else's hardware" decision in this
codebase keyed off the literal ``:cloud`` tag suffix (ollama.com's proxied
frontier models: ``glm-5.2:cloud``, ``minimax-m3:cloud``, ...). That suffix is
only one of three ways a model can turn out to be cloud-served:

1. the ``:cloud`` tag itself (still honoured -- it is the tag ollama.com
   documents and existing operators have pinned in their configs);
2. the model registry entry's explicit ``class`` -- a ``capable`` entry means
   the weights run somewhere other than this box (see
   ``pipeline.strength_class``), so a plain-named alias of a hosted model
   classifies the same way its ``:cloud`` sibling does;
3. the endpoint serving the Ollama API. Dialling ``https://ollama.com``
   directly (per-team API key, no local proxy) means *every* tag on that
   connection is cloud-served, plain name or not. A localhost /
   ``host.docker.internal`` / private-IP endpoint means none of them are.

The endpoint test is a pure string/address classification: it parses the URL
and asks whether the host is a loopback/docker/private address or a routable
one. It deliberately does NOT resolve DNS -- this decides how much context to
grant a model and whether to skip a local memory floor, not whether to trust
a peer, and a DNS lookup in the dispatch path would add latency (and a
failure mode) to every call.

Split into its own module because the four call sites (``_tuned_num_ctx``,
the dispatch step cap, the memory floor, strength classification, and the
local-success ledger) live in three packages and must agree exactly.
"""

from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit

CAPABLE = "capable"
ON_DEVICE = "on-device"

CLOUD_TAG_SUFFIX = ":cloud"

# Ollama's default host is a loopback address; a tag with no endpoint
# configured therefore keeps today's on-device classification.
_LOCAL_HOSTNAMES = frozenset({"localhost", "host.docker.internal"})

# Same precedence the dispatch path's own endpoint lookup uses: the
# provider-scoped variable wins over the provider-agnostic one, so an
# operator running a local Ollama and a hosted one side by side does not
# silently classify both by one value.
_ENDPOINT_ENV_VARS = ("PIPELINE_LOCAL_ENDPOINT_OLLAMA", "PIPELINE_LOCAL_ENDPOINT")


def endpoint_from_env() -> str:
    """Return the configured Ollama endpoint, or "" when none is set."""
    for name in _ENDPOINT_ENV_VARS:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return ""


def _endpoint_host(endpoint: str) -> str:
    """Return the lowercased hostname of *endpoint*, or "" when it has none.

    Accepts what operators actually put in PIPELINE_LOCAL_ENDPOINT*: a full
    URL (``http://localhost:11434``, ``https://ollama.com``), a bare
    host:port (``localhost:11434``) or a bare hostname. An IPv6 literal keeps
    its brackets when schemeless, so ``urlsplit("//" + endpoint)`` is retried
    on the first parse returning no host.
    """
    value = (endpoint or "").strip()
    if not value:
        return ""
    for candidate in (value, f"//{value}"):
        try:
            host = urlsplit(candidate).hostname
        except ValueError:
            continue
        if host:
            return host.lower().rstrip(".")
    return ""


def _is_local_host(host: str) -> bool:
    """True when *host* is loopback/docker-internal/private, else False.

    A non-global IP literal (RFC1918, CGNAT, link-local, loopback) is local:
    it cannot be reached from outside this network, which is the only claim
    this function makes. Hostname resolution is intentionally out of scope.
    """
    if not host:
        # No endpoint at all: today's behavior, i.e. a local Ollama.
        return True
    if host in _LOCAL_HOSTNAMES:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # A real hostname. "ollama", "gpu-box.lan", "api.example.com" and
        # "ollama.com" all land here and are treated as remote; without DNS
        # there is no truthful way to tell a LAN name from a public one, and
        # guessing local would under-size a hosted model.
        return False
    return not address.is_global


def endpoint_is_remote(endpoint: str) -> bool:
    """True when *endpoint* points at a host other than this box."""
    return not _is_local_host(_endpoint_host(endpoint))


def is_cloud_served(
    provider: str,
    tag: str,
    *,
    endpoint: str = "",
    entry_class: str | None = None,
) -> bool:
    """True when *tag* on *provider* runs on hardware that is not this box.

    Precedence:

    1. a ``:cloud`` tag suffix is cloud-served regardless of endpoint or
       provider -- local ``ollama serve`` proxies those to ollama.com, so the
       suffix survives a loopback endpoint, and it is the one rule that
       predates this module;
    2. any provider other than ``ollama`` is not classified beyond that
       (``claude`` and ``litellm`` are handled by the strength classifier);
    3. an explicit registry ``entry_class`` of ``capable``/``on-device``
       decides, in both directions (mirrors ``pipeline.strength_class``);
    4. otherwise a remote endpoint makes a plain-named tag cloud-served.

    Never raises: a malformed endpoint degrades to the tag/class rules rather
    than taking down the dispatch path.
    """
    model_tag = str(tag or "").strip().lower()
    if model_tag.endswith(CLOUD_TAG_SUFFIX):
        return True
    provider_name = str(provider or "").strip().lower()
    if provider_name != "ollama":
        return False
    if entry_class is not None:
        candidate = str(entry_class).strip().lower()
        if candidate == CAPABLE:
            return True
        if candidate == ON_DEVICE:
            return False
    if not str(endpoint or "").strip():
        return False
    try:
        return endpoint_is_remote(endpoint)
    except ValueError:  # pragma: no cover - defensive, parse is total today
        return False
