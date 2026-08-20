"""Acceptance: approve_merge and set_story_status must not be reachable as
chat tools. approve_merge is the human-confirmation override for a parked
risk=high story; set_story_status can move a story to pr_open, a status the
unattended scheduler will merge on its own next tick. Exposing either to an
LLM whose output is attacker-controlled (prompt injection) hands the human
confirmation step to the attacker."""
import httpx

from app.chat import _execute_tool


class _FakeTransport:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, content=b"{}", request=request)


def test_approve_merge_is_not_a_callable_chat_tool():
    transport = _FakeTransport()
    client = httpx.Client(transport=transport, base_url="http://test")
    result = _execute_tool(
        "approve_merge", {"plan_name": "demo", "story_key": "s1"}, client, "http://test"
    )
    assert "error" in result
    assert transport.requests == []


def test_set_story_status_is_not_a_callable_chat_tool():
    transport = _FakeTransport()
    client = httpx.Client(transport=transport, base_url="http://test")
    result = _execute_tool(
        "set_story_status",
        {"plan_name": "demo", "story_key": "s1", "status": "pr_open"},
        client,
        "http://test",
    )
    assert "error" in result
    assert transport.requests == []
