"""The direct OpenAI provider: OpenAI's Responses API with a per-namespace key.

Unlike the tenant gateway (:mod:`app.gateway_provider`), the host here is
fixed: every request goes to :data:`OPENAI_BASE_URL` unless the operator sets
``PIPELINE_OPENAI_BASE_URL`` for the whole deployment (see :func:`_base_url`).
A caller or a plan cannot change it: the ``endpoint`` argument and the
``PIPELINE_LOCAL_ENDPOINT*`` variables are ignored. Setting the override
sends every team's API key to that host, so it is an operator test switch,
not a tenant setting. The API key is a per-namespace
credential, resolved through :func:`app.credentials.resolve_for_injection`
from *inside every call* (``CredentialRef("model", "openai_api_key",
namespace=current_namespace())``). Nothing is cached on the instance or at
module scope, so one provider object reused across namespaces cannot send one
tenant's key on another tenant's call, and a call whose credential is missing
fails instead of reusing the previous value.

The request body and the response mapping come from
:mod:`app.openai_responses`. Responses are sent with ``store: false``, so
OpenAI does not keep them.

The key must never reach an exception, a log line or a returned string. A
401 body from OpenAI echoes a masked key, so error messages carry the HTTP
status and OpenAI's error code only, never the response text.
"""
from __future__ import annotations

import logging
import os
from urllib.parse import urlsplit

import httpx

from app.credentials import CredentialMissing, CredentialRef, resolve_for_injection
from app.openai_responses import responses_body, responses_envelope
from pipeline.actor import current_namespace

_LOGGER = logging.getLogger(__name__)

#: Registered provider name: PIPELINE_LOCAL_PROVIDER=openai, plan provider
#: "openai", backend driver "openai".
OPENAI_PROVIDER_NAME = "openai"

#: The default API host. The operator may replace it for the whole deployment
#: with :data:`OPENAI_BASE_URL_ENV`; nothing else can.
OPENAI_BASE_URL = "https://api.openai.com"

#: The operator-only override of the host. Read from the process environment
#: on every call, and nowhere else: not from a plan, a story, a role or
#: settings layer, or a request.
OPENAI_BASE_URL_ENV = "PIPELINE_OPENAI_BASE_URL"

#: Logged once per process when the override is in effect; carries the host only.
OPENAI_BASE_URL_OVERRIDDEN_EVENT = "openai_base_url_overridden"

#: The only hosts that may use plain ``http://``.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

#: The Responses API path, appended to :data:`OPENAI_BASE_URL`.
OPENAI_RESPONSES_PATH = "/v1/responses"

#: The per-namespace credential holding the OpenAI API key.
OPENAI_API_KEY_CREDENTIAL = "openai_api_key"

#: ``think=True`` maps to this reasoning effort; a string ``think`` is passed
#: through and validated by :func:`app.openai_responses.responses_body`.
_THINK_TRUE_EFFORT = "medium"

#: Set once the override warning has been logged in this process.
_override_warned = False


class OpenAIBaseUrlError(ValueError):
    """``PIPELINE_OPENAI_BASE_URL`` is set to a value that is not an allowed origin.

    The message names the variable and never the value, so a rejected
    override cannot leak a credential placed in it.
    """


def _override_host(value: str) -> str:
    """The host of an allowed override value, or :class:`OpenAIBaseUrlError`.

    Allowed: ``https://<host>[:port]``, or ``http://`` for a loopback host.
    No path, query, fragment, userinfo or whitespace.
    """
    refusal = (
        f"{OPENAI_BASE_URL_ENV} must be an https origin (host and optional "
        "port), or an http origin for localhost, 127.0.0.1 or ::1; the value "
        "is refused and not logged"
    )
    if any(char.isspace() for char in value) or any(char in value for char in "?#@"):
        raise OpenAIBaseUrlError(refusal)
    try:
        parts = urlsplit(value)
        _ = parts.port  # raises ValueError for a non-numeric or out-of-range port
    except ValueError as e:
        raise OpenAIBaseUrlError(refusal) from e
    host = parts.hostname
    if parts.scheme not in ("https", "http") or not host or parts.path:
        raise OpenAIBaseUrlError(refusal)
    if parts.scheme == "http" and host not in _LOOPBACK_HOSTS:
        raise OpenAIBaseUrlError(refusal)
    return host


def _base_url() -> str:
    """The API host for this call: the default, or the validated operator override.

    Unset or empty means :data:`OPENAI_BASE_URL`. A set value that is not an
    allowed origin raises :class:`OpenAIBaseUrlError`; it never falls back to
    another host. The first call that uses an override logs one warning.
    """
    global _override_warned
    value = os.environ.get(OPENAI_BASE_URL_ENV, "")
    if value == "":
        return OPENAI_BASE_URL
    host = _override_host(value)
    if not _override_warned:
        _override_warned = True
        _LOGGER.warning(
            "OpenAI API host is overridden for this deployment",
            extra={"event": OPENAI_BASE_URL_OVERRIDDEN_EVENT, "host": host},
        )
    return value


def openai_credential_ref() -> CredentialRef:
    """The API-key reference for the namespace bound right now.

    Built per call, never stored: the namespace is whatever
    :func:`pipeline.actor.current_namespace` reports at the moment of the call.
    """
    return CredentialRef(
        "model", OPENAI_API_KEY_CREDENTIAL, namespace=current_namespace()
    )


def resolve_openai_api_key() -> str:
    """Resolve the API key for the current namespace.

    Raises :class:`app.credentials.CredentialMissing` when the namespace has
    no key, and ``ValueError`` when the key is blank. Neither message contains
    the key.
    """
    api_key = (resolve_for_injection(openai_credential_ref()) or "").strip()
    if not api_key:
        raise ValueError(
            "the OpenAI API key credential resolved to an empty value; "
            "refusing to call OpenAI unauthenticated"
        )
    return api_key


def _reasoning_effort(think: bool | str | None) -> str | None:
    """Map the harness's ``think`` flag to a Responses API effort, or None."""
    if think is None or think is False:
        return None
    if think is True:
        return _THINK_TRUE_EFFORT
    return think


def _error_code(resp) -> str:
    """OpenAI's error code from a non-2xx response, or ``unknown``.

    Only the code or type field is read. The message and the raw body are
    never used: OpenAI's 401 message echoes a masked key.
    """
    try:
        error = resp.json()["error"]
        code = error.get("code") or error.get("type")
    except (ValueError, KeyError, TypeError, AttributeError):
        return "unknown"
    return str(code) if code else "unknown"


class OpenAIProvider:
    """Direct access to OpenAI's Responses API (see the module docstring)."""

    name = OPENAI_PROVIDER_NAME
    @property
    def default_endpoint(self) -> str:
        """The API host in effect now: the default, or the operator override.

        ``OllamaDriver`` reads it when it is constructed. The driver never
        sends to it through this attribute: the request URL comes from
        :func:`_base_url` in :meth:`chat`.
        """
        return _base_url()

    def chat(
        self, messages: list, *, model: str, num_ctx: int, temperature: float,
        tools: list | None = None, endpoint: str | None = None,
        timeout: float | None = None, think: bool | str | None = None,
    ) -> dict:
        """POST one non-streaming response; return Ollama's envelope shape.

        ``endpoint`` is accepted for signature parity and ignored: the host is
        :data:`OPENAI_BASE_URL` unless the operator sets
        ``PIPELINE_OPENAI_BASE_URL``, so a driver-level or
        ``PIPELINE_LOCAL_ENDPOINT*`` value cannot redirect the key.
        ``temperature`` is ignored because reasoning models reject it.
        ``num_ctx`` is ignored because it is a context size, not an output
        budget; the Responses body sets its own fixed output reservation.
        ``think`` becomes the reasoning effort.

        The key is resolved on every call - see the module docstring.
        """
        # Imported here, not at module scope: app.inference_providers imports
        # this module for its _PROVIDERS registry, so a module-level import
        # back would be circular. The shared timeout helper lives there.
        from app import inference_providers

        # Validated before the key is read: a bad override sends nothing.
        base_url = _base_url()
        api_key = resolve_openai_api_key()
        body = responses_body(
            messages, model=model, tools=tools,
            reasoning_effort=_reasoning_effort(think),
        )
        # None is coerced to the resolved role-call timeout - never an
        # unbounded read on the wire (2026-09-02 freeze).
        wire_timeout = (
            timeout if timeout is not None
            else inference_providers.resolve_role_call_timeout()
        )
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = httpx.post(
                base_url + OPENAI_RESPONSES_PATH,
                json=body,
                headers=headers,
                timeout=wire_timeout,
                follow_redirects=False,
            )
        except httpx.HTTPError as e:
            # The exception type only: its text may echo request details.
            raise RuntimeError(
                f"OpenAI endpoint unreachable: {type(e).__name__}"
            ) from e

        if resp.status_code == 429:
            raise inference_providers.RateLimitedError(
                f"OpenAI (model={model}) returned 429 (rate limited)"
            )
        if not 200 <= resp.status_code < 300:
            raise RuntimeError(
                f"OpenAI returned HTTP {resp.status_code} "
                f"(model={model}, error={_error_code(resp)})"
            )
        return responses_envelope(resp.json())

    def loaded_models(self, endpoint: str) -> set[str]:
        """Nothing to report: no probing, and the catalogue is the registry's.

        Listing models would need the key sent to the API just to enumerate
        them. The models a plan may use are declared in model_registry.json.
        """
        return set()

    def reachable(self, endpoint: str) -> tuple[bool, str]:
        """Whether this namespace's key is usable; never raises, sends nothing.

        Backs ``OllamaDriver.resource_status()``. A missing or blank key is
        reported as unreachable with the reason.
        """
        try:
            resolve_openai_api_key()
        except (CredentialMissing, ValueError) as e:
            return False, f"OpenAI API key not usable: {e}"
        return True, ""
