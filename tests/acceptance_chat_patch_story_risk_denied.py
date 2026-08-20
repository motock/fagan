"""Acceptance: the chat patch_story tool must refuse to forward a 'risk'
field. patch_story's general HTTP API legitimately allows a plan author to
edit risk, but the chat tool's caller is an LLM whose output is
attacker-controlled; risk feeds PIPELINE_RISK_THRESHOLD, so a prompt-injected
risk downgrade lets the unattended scheduler auto-merge a story that was
parked specifically for human review. The call must fail closed - reject the
whole patch (no HTTP request at all) rather than silently drop the field."""
import json

import httpx

from app.chat import TOOLS


class _FakeTransport:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, content=b"{}", request=request)


def test_patch_story_rejects_a_risk_field_without_any_http_call():
    transport = _FakeTransport()
    client = httpx.Client(transport=transport, base_url="http://test")
    result = TOOLS["patch_story"]["execute"](
        client,
        "http://test",
        plan_name="demo",
        story_key="s1",
        fields={"risk": "low"},
    )
    assert "error" in result
    assert transport.requests == []


def test_patch_story_still_allows_non_risk_fields():
    transport = _FakeTransport()
    client = httpx.Client(transport=transport, base_url="http://test")
    TOOLS["patch_story"]["execute"](
        client,
        "http://test",
        plan_name="demo",
        story_key="s1",
        fields={"agent_instructions": "do X"},
    )
    assert len(transport.requests) == 1
    body = json.loads(transport.requests[0].content)
    assert body == {"agent_instructions": "do X"}
