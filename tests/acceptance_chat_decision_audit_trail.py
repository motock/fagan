"""Acceptance: the chat answer_decision tool must stamp decided_by="chat" on
every decision it records. The server defaults decided_by to "human" when
the caller omits it, so a chat-authored decision was being recorded as if a
human had typed it directly into the dashboard - forging the audit trail
this feature explicitly claims to preserve."""
import json

import httpx

from app.chat import TOOLS


class _FakeTransport:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, content=b"{}", request=request)


def test_answer_decision_stamps_decided_by_chat():
    transport = _FakeTransport()
    client = httpx.Client(transport=transport, base_url="http://test")
    TOOLS["answer_decision"]["execute"](
        client,
        "http://test",
        plan_name="demo",
        story_key="s1",
        question="q",
        answer="a",
    )
    assert len(transport.requests) == 1
    body = json.loads(transport.requests[0].content)
    assert body["decided_by"] == "chat"
