"""ChatService's default httpx.Client must carry a timeout long enough for
the slowest tool it invokes.

The `decompose` tool resolves (synchronously, over this same client) to a
real product-analyst LLM call that routinely takes 15-90s+; httpx's library
default timeout is 5.0s, so a client constructed without an explicit timeout
times out on essentially every decompose call (observed live 2026-09-08:
"the decompose call timed out" from the chat dashboard). This test pins the
default client's timeout at the ChatService constructor, the one layer that
owns the client's lifetime.
"""

from __future__ import annotations

import httpx

from app.chat import ChatService


def test_default_http_client_read_timeout_exceeds_a_real_decompose_call():
    svc = ChatService(api_key=None)
    timeout = svc._http_client.timeout
    assert isinstance(timeout, httpx.Timeout)
    # httpx's library default is 5.0s; a decompose turn routinely takes 15-90s+
    assert timeout.read >= 300, (
        f"default ChatService http client read timeout is {timeout.read}s - "
        "too short for the decompose tool's synchronous LLM call"
    )


def test_injected_http_client_is_used_unchanged():
    # Callers that inject their own client (tests, TestClient) keep full
    # control - ChatService must not override their timeout or wrap the client.
    injected = httpx.Client(timeout=httpx.Timeout(2.0))
    svc = ChatService(http_client=injected)
    assert svc._http_client is injected
    assert svc._http_client.timeout.read == 2.0
