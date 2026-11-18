"""TEMPORARY prototype used only to validate the test suite. DELETE ME."""
from __future__ import annotations

from app.credentials import CredentialRef, resolve_for_injection
from pipeline.actor import current_namespace

GATEWAY_BASE_URL_CREDENTIAL = "gateway_base_url"
GATEWAY_API_KEY_CREDENTIAL = "gateway_api_key"


def _gateway_credentials() -> tuple[str, str]:
    namespace = current_namespace()
    base_url = resolve_for_injection(
        CredentialRef("model", GATEWAY_BASE_URL_CREDENTIAL, namespace=namespace)
    ).strip()
    api_key = resolve_for_injection(
        CredentialRef("model", GATEWAY_API_KEY_CREDENTIAL, namespace=namespace)
    ).strip()
    return base_url, api_key


def openai_chat_body(
    messages: list, *, model: str, num_ctx: int, temperature: float,
    tools: list | None = None,
) -> dict:
    body = {
        "model": model, "messages": messages, "stream": False,
        "temperature": temperature,
        "max_tokens": num_ctx,
    }
    if tools:
        body["tools"] = tools
    return body


def openai_response_envelope(payload: dict) -> dict:
    message = payload["choices"][0]["message"]
    usage = payload.get("usage") or {}
    return {
        "message": message,
        "prompt_eval_count": usage.get("prompt_tokens", 0),
        "eval_count": usage.get("completion_tokens", 0),
    }


class GatewayProvider:
    name = "gateway"
    default_endpoint = ""

    def chat(
        self, messages: list, *, model: str, num_ctx: int, temperature: float,
        tools: list | None = None, endpoint: str | None = None,
        timeout: float | None = None, think: bool | str | None = None,
    ) -> dict:
        from app import inference_providers
        from app.outbound_guard import (
            OutboundRefused,
            pinned_client,
            require_https_url,
            resolve_public_address,
        )

        base_url, api_key = _gateway_credentials()
        require_https_url(base_url)
        address = resolve_public_address(base_url)
        if not address:
            raise OutboundRefused("gateway host does not resolve to a public address")
        body = inference_providers.openai_chat_body(
            messages, model=model, num_ctx=num_ctx, temperature=temperature, tools=tools
        )
        wire_timeout = (
            timeout if timeout is not None
            else inference_providers.resolve_role_call_timeout()
        )
        with pinned_client(base_url, address, wire_timeout) as client:
            resp = client.post(
                f"{base_url.rstrip('/')}/v1/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=wire_timeout,
            )
        if resp.status_code == 429:
            raise inference_providers.RateLimitedError(
                f"Gateway at {base_url} (model={model}) returned 429 (rate limited)"
            )
        resp.raise_for_status()
        return inference_providers.openai_response_envelope(resp.json())

    def loaded_models(self, endpoint: str) -> set[str]:
        return set()

    def reachable(self, endpoint: str) -> tuple[bool, str]:
        return True, ""
