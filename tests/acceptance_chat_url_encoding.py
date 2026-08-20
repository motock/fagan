"""Acceptance: chat tool path segments (plan_name, story_key) must be
URL-encoded before being interpolated into the HTTP path. An unencoded
'#', '?' or '../' in an LLM-controlled argument must not redirect the
request onto a different route than the one the called tool names."""
import json

import httpx

from app.chat import TOOLS


class _FakeTransport:
    def __init__(self, payload=None) -> None:
        self.payload = payload if payload is not None else {"ok": True}
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            200,
            content=json.dumps(self.payload).encode(),
            headers={"content-type": "application/json"},
            request=request,
        )


def test_hash_in_plan_name_does_not_redirect_to_a_different_route():
    transport = _FakeTransport()
    client = httpx.Client(transport=transport, base_url="http://test")
    TOOLS["pause_plan"]["execute"](
        client, "http://test", plan_name="demo/stories/s1/approve_merge#"
    )
    assert len(transport.requests) == 1
    req = transport.requests[0]
    assert req.url.path != "/api/plans/demo/stories/s1/approve_merge"
    assert req.url.path.endswith("/pause")


def test_dot_dot_slash_in_plan_name_does_not_escape_the_plans_prefix():
    transport = _FakeTransport()
    client = httpx.Client(transport=transport, base_url="http://test")
    TOOLS["pause_plan"]["execute"](client, "http://test", plan_name="../../../etc")
    assert len(transport.requests) == 1
    req = transport.requests[0]
    assert req.url.path.startswith("/api/plans/")
    assert req.url.path.endswith("/pause")
