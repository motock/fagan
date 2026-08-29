"""Tests for the workspace tool set added to the ``TOOLS`` registry in
app/chat.py.

This story adds exactly two entries to the cumulative ``TOOLS`` dict:

  * ``set_workspace``    - params ``{"path": "str", "create": "bool"}``;
    POSTs to ``/api/workspace`` with body ``{"path": path, "create": create}``
    (``create`` defaulting to ``False``).
  * ``list_workspaces``  - params ``{}``; GETs ``/api/workspaces``.

``TOOLS`` is a CUMULATIVE registry that other stories also extend, so these
tests assert ONLY what this story adds: membership of the two new names and
the per-entry structure of those two entries.  They never assert the total
contents/length of ``TOOLS`` nor an exact full-string match of the generated
prompt sentence.

The tests are self-contained: a recording fake ``http_client`` stands in for
httpx so no network is touched, and they must fail for the right reason
(missing registry entries / missing helpers) until the implementation lands.
"""
from __future__ import annotations

import pytest

import app.chat as chat_module
from app.chat import TOOLS, _available_tools_sentence

NEW_TOOLS = ("set_workspace", "list_workspaces")


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _FakeResponse:
    """Minimal stand-in for an ``httpx.Response`` exposing ``.json()``."""

    def __init__(self, payload) -> None:
        self._payload = payload

    def json(self):
        return self._payload


class _RecordingHttpClient:
    """Records every ``.get``/``.post`` call and returns a scripted payload.

    Each call is appended to ``calls`` as
    ``{"method": ..., "url": ..., <kwargs>}`` so tests can assert on the
    verb, the resolved URL, and any ``json=`` body that was sent.
    """

    def __init__(self, payload=None) -> None:
        self.calls: list[dict] = []
        self._payload = {} if payload is None else payload

    def get(self, url, **kwargs):
        self.calls.append({"method": "GET", "url": url, **kwargs})
        return _FakeResponse(self._payload)

    def post(self, url, **kwargs):
        self.calls.append({"method": "POST", "url": url, **kwargs})
        return _FakeResponse(self._payload)


def _last_call(client: _RecordingHttpClient) -> dict:
    assert client.calls, "expected the tool execute callable to issue an HTTP call"
    return client.calls[-1]


# =========================================================================== #
# Registry membership (cumulative-safe: membership only, never total shape)
# =========================================================================== #
class TestRegistryMembership:
    def test_set_workspace_is_registered(self):
        assert "set_workspace" in TOOLS

    def test_list_workspaces_is_registered(self):
        assert "list_workspaces" in TOOLS

    def test_both_names_are_distinct_entries(self):
        assert "set_workspace" in TOOLS and "list_workspaces" in TOOLS
        assert TOOLS["set_workspace"] is not TOOLS["list_workspaces"]


# =========================================================================== #
# Per-entry structure
# =========================================================================== #
class TestEntryStructure:
    @pytest.mark.parametrize("name", NEW_TOOLS)
    def test_entry_has_required_keys(self, name):
        entry = TOOLS[name]
        for key in ("description", "params", "execute"):
            assert key in entry, f"TOOLS[{name!r}] is missing the {key!r} key"

    @pytest.mark.parametrize("name", NEW_TOOLS)
    def test_description_is_a_nonempty_string(self, name):
        assert isinstance(TOOLS[name]["description"], str)
        assert TOOLS[name]["description"].strip() != ""

    def test_set_workspace_params(self):
        assert TOOLS["set_workspace"]["params"] == {"path": "str", "create": "bool"}

    def test_list_workspaces_params_empty(self):
        assert TOOLS["list_workspaces"]["params"] == {}

    @pytest.mark.parametrize("name", NEW_TOOLS)
    def test_execute_is_callable(self, name):
        assert callable(TOOLS[name]["execute"])


# =========================================================================== #
# set_workspace execute behaviour
# =========================================================================== #
class TestSetWorkspaceExecute:
    def test_posts_to_api_workspace_with_path_and_create(self):
        client = _RecordingHttpClient(payload={"ok": True})
        result = TOOLS["set_workspace"]["execute"](
            client, "http://testserver", path="/tmp/ws", create=True
        )
        call = _last_call(client)
        assert call["method"] == "POST"
        assert call["url"].endswith("/api/workspace")
        assert call.get("json") == {"path": "/tmp/ws", "create": True}
        assert result == {"ok": True}

    def test_create_defaults_to_false_when_not_supplied(self):
        client = _RecordingHttpClient()
        TOOLS["set_workspace"]["execute"](client, "http://testserver", path="/tmp/ws")
        call = _last_call(client)
        assert call["method"] == "POST"
        assert call.get("json") == {"path": "/tmp/ws", "create": False}

    def test_create_false_explicit_matches_default(self):
        client = _RecordingHttpClient()
        TOOLS["set_workspace"]["execute"](
            client, "http://testserver", path="/tmp/ws", create=False
        )
        assert _last_call(client).get("json") == {"path": "/tmp/ws", "create": False}

    def test_body_contains_exactly_path_and_create_keys(self):
        client = _RecordingHttpClient()
        TOOLS["set_workspace"]["execute"](
            client, "http://testserver", path="ws-a", create=True
        )
        body = _last_call(client).get("json")
        assert set(body) == {"path", "create"}

    def test_empty_path_is_sent_verbatim(self):
        """Boundary: an empty string path is still forwarded, not dropped."""
        client = _RecordingHttpClient()
        TOOLS["set_workspace"]["execute"](client, "http://testserver", path="", create=False)
        assert _last_call(client).get("json") == {"path": "", "create": False}

    def test_missing_path_raises_type_error(self):
        client = _RecordingHttpClient()
        with pytest.raises(TypeError) as excinfo:
            TOOLS["set_workspace"]["execute"](client, "http://testserver")
        assert "path" in str(excinfo.value)

    def test_execute_routes_through_resolve_tool_url(self, monkeypatch):
        """The execute lambda must call _resolve_tool_url(http_client, base, ...)."""
        seen: list[tuple] = []

        def _spy(http_client, api_base_url, path):
            seen.append((http_client, api_base_url, path))
            return f"{api_base_url}{path}"

        monkeypatch.setattr(chat_module, "_resolve_tool_url", _spy)
        client = _RecordingHttpClient()
        TOOLS["set_workspace"]["execute"](client, "http://base", path="/tmp/ws", create=True)
        assert seen == [(client, "http://base", "/api/workspace")]

    def test_dispatch_via_execute_tool_returns_result(self):
        client = _RecordingHttpClient(payload={"ok": True, "workspace": "/tmp/ws"})
        out = chat_module._execute_tool(
            "set_workspace", {"path": "/tmp/ws", "create": True}, client, "http://testserver"
        )
        assert out == {"result": {"ok": True, "workspace": "/tmp/ws"}}

    def test_dispatch_without_required_arg_reports_error(self):
        client = _RecordingHttpClient()
        out = chat_module._execute_tool("set_workspace", {}, client, "http://testserver")
        assert "result" not in out
        assert "error" in out and "path" in out["error"]


# =========================================================================== #
# list_workspaces execute behaviour
# =========================================================================== #
class TestListWorkspacesExecute:
    def test_gets_api_workspaces(self):
        client = _RecordingHttpClient(payload={"workspaces": ["/tmp/ws"]})
        result = TOOLS["list_workspaces"]["execute"](client, "http://testserver")
        call = _last_call(client)
        assert call["method"] == "GET"
        assert call["url"].endswith("/api/workspaces")
        assert result == {"workspaces": ["/tmp/ws"]}

    def test_get_sends_no_body(self):
        client = _RecordingHttpClient()
        TOOLS["list_workspaces"]["execute"](client, "http://testserver")
        call = _last_call(client)
        assert call["method"] == "GET"
        assert call.get("json") is None

    def test_extra_kwargs_are_tolerated(self):
        """Neighbouring entries accept **kwargs; the copied shape must too."""
        client = _RecordingHttpClient(payload={"workspaces": []})
        result = TOOLS["list_workspaces"]["execute"](
            client, "http://testserver", unexpected="x"
        )
        assert result == {"workspaces": []}
        assert _last_call(client)["url"].endswith("/api/workspaces")

    def test_execute_routes_through_resolve_tool_url(self, monkeypatch):
        seen: list[tuple] = []

        def _spy(http_client, api_base_url, path):
            seen.append((http_client, api_base_url, path))
            return f"{api_base_url}{path}"

        monkeypatch.setattr(chat_module, "_resolve_tool_url", _spy)
        client = _RecordingHttpClient()
        TOOLS["list_workspaces"]["execute"](client, "http://base")
        assert seen == [(client, "http://base", "/api/workspaces")]

    def test_dispatch_via_execute_tool_returns_result(self):
        client = _RecordingHttpClient(payload={"workspaces": ["a", "b"]})
        out = chat_module._execute_tool(
            "list_workspaces", {}, client, "http://testserver"
        )
        assert out == {"result": {"workspaces": ["a", "b"]}}


# =========================================================================== #
# Prompt sentence (membership only - never an exact full-string match)
# =========================================================================== #
class TestAvailableToolsSentence:
    def test_sentence_names_set_workspace(self):
        assert "set_workspace" in _available_tools_sentence()

    def test_sentence_names_list_workspaces(self):
        assert "list_workspaces" in _available_tools_sentence()

    def test_sentence_prefix_still_present(self):
        sentence = _available_tools_sentence()
        assert sentence.startswith("Available tools: ")

    def test_system_prompt_names_both_tools(self):
        assert "set_workspace" in chat_module.SYSTEM_PROMPT
        assert "list_workspaces" in chat_module.SYSTEM_PROMPT