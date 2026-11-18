"""The tenant "Customer AI gateway" provider.

Some tenants front their models with their own OpenAI-compatible AI gateway
(LiteLLM proxy, Portkey, an in-house router, ...) instead of exposing a model
server on the pipeline host. One deployment therefore serves several tenants
whose endpoints and keys differ, so - unlike every other provider in
:mod:`app.inference_providers` - this one has no process-wide endpoint and no
process-wide key:

* ``default_endpoint`` is empty. ``OllamaDriver``'s
  ``PIPELINE_LOCAL_ENDPOINT[_<PROVIDER>]`` fallback is skipped for this
  provider (the environment is per *process*, the tenant is per *namespace*),
  so the base URL can only come from the namespace's own credential.
* Both the base URL and the API key are resolved through
  :func:`app.credentials.resolve_for_injection` from *inside every call*
  (``CredentialRef("model", ..., namespace=current_namespace())``). Nothing is
  cached on the instance or at module scope: one provider object is reused
  across namespaces, so a cached ``(base_url, key)`` pair would send one
  tenant's key to another tenant's host - and a call whose credential is
  missing must fail rather than reuse the previous call's value.
* The base URL must be ``https`` and resolve to a *public* address
  (:func:`app.outbound_guard.require_https_url` /
  :func:`app.outbound_guard.resolve_public_address`), and the request is sent
  through :func:`app.outbound_guard.pinned_client`, which connects to the
  address that was actually validated (no DNS rebinding between the check and
  the request).

The wire format is OpenAI's ``/v1/chat/completions``; the request body and the
response mapping are shared with ``LMStudioProvider`` through
:func:`app.inference_providers.openai_chat_body` /
:func:`app.inference_providers.openai_response_envelope`, so the two can never
drift apart.
"""
from __future__ import annotations

import httpx

from app.credentials import CredentialMissing, CredentialRef, resolve_for_injection
from app.outbound_guard import (
    OutboundRefused,
    pinned_client,
    require_https_url,
    resolve_public_address,
)
from pipeline.actor import current_namespace

#: Registered provider name: PIPELINE_LOCAL_PROVIDER=gateway, plan provider
#: "gateway", backend driver "gateway".
GATEWAY_PROVIDER_NAME = "gateway"

#: The credential holding the tenant gateway's base URL (e.g.
#: ``https://gw.acme.example`` - the ``/v1/chat/completions`` path is added
#: here). Deliberately absent from app.credentials' env allowlist: a
#: deployment-wide fallback would route every tenant to one host.
GATEWAY_BASE_URL_CREDENTIAL = "gateway_base_url"

#: The credential holding the tenant gateway's API key. Also deliberately
#: absent from app.credentials' env allowlist - see above.
GATEWAY_API_KEY_CREDENTIAL = "gateway_api_key"

#: OpenAI-compatible chat path, appended to the credential's base URL.
GATEWAY_CHAT_PATH = "/v1/chat/completions"


def gateway_credential_ref(name: str) -> CredentialRef:
    """The tenant-scoped credential reference for ``name``.

    Built per call, never stored: the namespace is whatever
    :func:`pipeline.actor.current_namespace` reports *now*, so a provider
    instance shared by two namespaces reads each tenant's own credential.
    """
    return CredentialRef("model", name, namespace=current_namespace())


def resolve_gateway_credentials() -> tuple[str, str]:
    """Resolve ``(base_url, api_key)`` for the current namespace.

    Called from inside every request. Raises
    :class:`app.credentials.CredentialMissing` (from
    ``resolve_for_injection``) when the namespace has not configured a
    gateway - the correct outcome for a tenant without one, and never a
    silent fallback to another tenant's values or to a local endpoint.
    """
    base_url = (
        resolve_for_injection(gateway_credential_ref(GATEWAY_BASE_URL_CREDENTIAL))
        or ""
    ).strip()
    api_key = (
        resolve_for_injection(gateway_credential_ref(GATEWAY_API_KEY_CREDENTIAL))
        or ""
    ).strip()
    if not api_key:
        raise ValueError(
            "the gateway API key credential resolved to an empty value; "
            "refusing to call the tenant gateway unauthenticated"
        )
    return base_url.rstrip("/"), api_key


def validate_gateway_base_url(base_url: str) -> str:
    """Validate ``base_url`` and return the public address to pin.

    HTTPS only, and the name must resolve to a public address: a tenant config
    pointing at ``http://``, loopback, a private network, or the cloud
    metadata service is refused before anything is sent. Nothing is cached, so
    a refused address on one call cannot leak into the next one.
    """
    require_https_url(base_url)
    address = resolve_public_address(base_url)
    if not address:
        raise OutboundRefused(
            "gateway endpoint does not resolve to a public address; "
            "refusing to send a tenant key there"
        )
    return address


class GatewayProvider:
    """A tenant-owned OpenAI-compatible gateway (see the module docstring)."""

    name = GATEWAY_PROVIDER_NAME
    #: No process-wide default: the base URL is a per-namespace credential.
    default_endpoint = ""

    def chat(
        self, messages: list, *, model: str, num_ctx: int, temperature: float,
        tools: list | None = None, endpoint: str | None = None,
        timeout: float | None = None, think: bool | str | None = None,
    ) -> dict:
        """POST one non-streaming completion; return Ollama's envelope shape.

        ``endpoint`` is accepted for signature parity with
        :class:`app.inference_providers.LocalInferenceProvider` but ignored:
        the base URL always comes from the namespace's own credential, so a
        stale driver-level endpoint (or a ``PIPELINE_LOCAL_ENDPOINT*`` value)
        can never redirect a tenant's key. ``think`` likewise has no
        equivalent field in the OpenAI-compatible protocol.

        Everything is resolved fresh on every call - see the module docstring
        for why caching it would leak one tenant's key to another.
        """
        # Imported here, not at module scope: app.inference_providers imports
        # this module for its _PROVIDERS registry, so a module-level import
        # back would be circular. The shared wire helpers live there.
        from app import inference_providers

        base_url, api_key = resolve_gateway_credentials()
        address = validate_gateway_base_url(base_url)

        body = inference_providers.openai_chat_body(
            messages, model=model, num_ctx=num_ctx, temperature=temperature,
            tools=tools,
        )
        # None (defaulted or explicit) is coerced to the resolved role-call
        # timeout - never an unbounded read on the wire (2026-09-02 freeze).
        wire_timeout = (
            timeout if timeout is not None
            else inference_providers.resolve_role_call_timeout()
        )
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        try:
            # Pinned to ``address``: the configured hostname is kept for the
            # Host header and TLS SNI, but the connection goes to the address
            # that was validated a moment ago.
            with pinned_client(base_url, address, wire_timeout) as client:
                resp = client.post(
                    f"{base_url}{GATEWAY_CHAT_PATH}", json=body, headers=headers
                )
        except httpx.HTTPError as e:
            raise RuntimeError(
                f"gateway endpoint {base_url} unreachable: {e}"
            ) from e

        if resp.status_code == 429:
            raise inference_providers.RateLimitedError(
                f"gateway endpoint {base_url} (model={model}) returned 429 "
                f"(rate limited)"
            )
        resp.raise_for_status()
        return inference_providers.openai_response_envelope(resp.json())

    def loaded_models(self, endpoint: str) -> set[str]:
        """Nothing to report: the gateway owns the tenant's model catalogue.

        Which models a tenant gateway serves is the tenant's own routing
        decision, and probing with a tenant key just to enumerate them is not
        something the pipeline should do. Same shape as MLXProvider and
        LiteLLMProvider, which have no loaded-model concept either.
        """
        return set()

    def reachable(self, endpoint: str) -> tuple[bool, str]:
        """Whether this namespace's gateway config is usable; never raises.

        Backs ``OllamaDriver.resource_status()``, so a missing or refused
        credential is reported as unreachable with the reason instead of
        raising - and nothing is sent.
        """
        try:
            base_url, _api_key = resolve_gateway_credentials()
            validate_gateway_base_url(base_url)
        except (CredentialMissing, ValueError, OutboundRefused) as e:
            # Missing/blank credential, or a URL/address the guard refused.
            return False, f"gateway endpoint not usable: {e}"
        return True, ""
