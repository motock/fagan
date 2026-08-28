"""Tests for threading a per-request ``workspace`` value through the chat path.

The story copies EXACTLY how ``plan_name`` already works in ``app/chat.py``:

1. ``ChatRequest`` gains ``workspace: str | None = None`` beside ``plan_name``.
2. ``ChatService.execute_turn`` gains a keyword-only ``workspace=None`` param.
3. ``chat_endpoint`` forwards ``workspace=req.workspace`` to ``execute_turn``.
4. When ``workspace`` is set, the *system prompt* for that turn gains a
   sentence stating the active workspace absolute path and instructing the
   model to use it as ``repo_root`` when authoring a plan.  When it is None
   (or an empty string), the system prompt must be unchanged from today -
   i.e. equal to the module-level ``SYSTEM_PROMPT`` constant.

Constraints graded here:
- No server-side session state / global / cache: the workspace arrives
  per-request, so two consecutive turns must not leak workspace state in
  either direction.
- The tool list must stay generated from the ``TOOLS`` registry via
  ``_available_tools_sentence()`` - asserted by MEMBERSHIP of a registered
  tool name in the prompt, never by exact count or exact full-prompt string.

The implementation does not exist yet; these tests must fail for the right
reason (TypeError on the missing kwarg / AttributeError on the missing
field) until it lands.
"""
from __future__ import annotations

import inspect
import os
import re
from typing import get_args

import pytest
from fastapi.testclient import TestClient

import app.chat as chat_module
from app.chat import TOOLS, ChatRequest, ChatService


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _RecordingDriver:
    """Fake backend driver: records every complete() call, emits no tool calls.

    ``execute_turn`` stops after the first driver response that parses to zero
    tool calls, so a single ``complete`` recording is enough to inspect the
    system prompt for the turn.
    """

    model = "fake-model"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def complete(self, *, prompt, system, model, cwd=None, **kwargs) -> str:
        self.calls.append({"prompt": prompt, "system": system, "model": model, "cwd": cwd})
        return "All done - no tool calls needed."


def _capture_system_prompt(workspace, *, message="hello", history=None, plan_name=None):
    """Run one REAL ChatService turn against a recording driver.

    Returns the ``system`` prompt the service handed to the driver, so tests
    can assert on the workspace sentence without a live model.
    """
    driver = _RecordingDriver()
    svc = ChatService(driver=driver, http_client=object(), api_base_url="http://127.0.0.1:8000")
    svc.execute_turn(message, plan_name=plan_name, history=history, workspace=workspace)
    assert driver.calls, "driver.complete was never called"
    return driver.calls[0]["system"]


def _make_spy_service(captured):
    """Build a ChatService stand-in that records execute_turn kwargs."""

    class _SpyChatService:
        def __init__(self, *args, **kwargs):
            pass

        def execute_turn(self, message, *, plan_name=None, history=None, workspace=None) -> dict:
            captured.append(
                {"message": message, "plan_name": plan_name, "history": history, "workspace": workspace}
            )
            return {"reply": "hi", "tool_calls": [], "turns": 1}

    return _SpyChatService


@pytest.fixture
def client():
    from app import dashboard as d

    return TestClient(d.app)


# --------------------------------------------------------------------------- #
# 1. ChatRequest model
# --------------------------------------------------------------------------- #
def test_chat_request_accepts_workspace_and_defaults_to_none():
    """ChatRequest accepts a workspace value and defaults it to None."""
    assert ChatRequest(message="hi").workspace is None
    assert ChatRequest(message="hi", workspace="/tmp/demo-repo").workspace == "/tmp/demo-repo"
    validated = ChatRequest.model_validate({"message": "hi", "workspace": "/tmp/demo-repo"})
    assert validated.workspace == "/tmp/demo-repo"
    assert ChatRequest.model_validate({"message": "hi", "workspace": None}).workspace is None


def test_chat_request_workspace_field_is_optional_str_or_none_beside_plan_name():
    """workspace must be declared ``str | None = None`` beside plan_name."""
    fields = ChatRequest.model_fields
    assert "workspace" in fields, "ChatRequest is missing the workspace field"
    assert not fields["workspace"].is_required(), "workspace must be optional"
    assert fields["workspace"].default is None, "workspace must default to None"
    union_args = get_args(fields["workspace"].annotation)
    assert str in union_args and type(None) in union_args, (
        f"workspace must be typed 'str | None', got {fields['workspace'].annotation!r}"
    )
    names = list(fields)
    assert abs(names.index("workspace") - names.index("plan_name")) == 1, (
        "workspace must be declared beside plan_name, got field order: "
        f"{names}"
    )


# --------------------------------------------------------------------------- #
# 2. Endpoint forwarding (spy on a fake service)
# --------------------------------------------------------------------------- #
def test_endpoint_forwards_workspace_to_execute_turn(client, monkeypatch):
    """A workspace in the request body must reach execute_turn verbatim."""
    captured: list[dict] = []
    monkeypatch.setattr(chat_module, "ChatService", _make_spy_service(captured))

    resp = client.post(
        "/api/chat",
        json={
            "message": "hello",
            "plan_name": "demo",
            "history": [{"role": "user", "content": "prev"}],
            "workspace": "/tmp/demo-repo",
        },
    )

    assert resp.status_code == 200
    assert captured == [
        {
            "message": "hello",
            "plan_name": "demo",
            "history": [{"role": "user", "content": "prev"}],
            "workspace": "/tmp/demo-repo",
        }
    ], "endpoint must forward workspace (alongside plan_name/history) to execute_turn"


def test_endpoint_forwards_none_when_workspace_omitted(client, monkeypatch):
    """Omitting workspace from the body must forward None, not raise."""
    captured: list[dict] = []
    monkeypatch.setattr(chat_module, "ChatService", _make_spy_service(captured))

    resp = client.post("/api/chat", json={"message": "hello"})

    assert resp.status_code == 200
    assert captured == [
        {"message": "hello", "plan_name": None, "history": None, "workspace": None}
    ]


def test_endpoint_forwards_none_for_explicit_null_workspace(client, monkeypatch):
    """An explicit JSON null workspace is forwarded as None."""
    captured: list[dict] = []
    monkeypatch.setattr(chat_module, "ChatService", _make_spy_service(captured))

    resp = client.post("/api/chat", json={"message": "hello", "workspace": None})

    assert resp.status_code == 200
    assert captured[0]["workspace"] is None


@pytest.mark.parametrize("bad_workspace", [123, 4.5, ["not", "a", "string"], {"k": "v"}])
def test_endpoint_rejects_non_string_workspace(client, monkeypatch, bad_workspace):
    """A non-string workspace is a pydantic validation error (422) naming the
    field, and the service must never be invoked."""
    captured: list[dict] = []
    monkeypatch.setattr(chat_module, "ChatService", _make_spy_service(captured))

    resp = client.post("/api/chat", json={"message": "hello", "workspace": bad_workspace})

    assert resp.status_code == 422
    assert captured == [], "execute_turn must not run when validation fails"
    error_locs = [tuple(err.get("loc", [])) for err in resp.json()["detail"]]
    assert any("workspace" in loc for loc in error_locs), (
        f"the 422 detail must name the offending field 'workspace', got {resp.json()['detail']}"
    )


def test_endpoint_workspace_not_sticky_across_requests(client, monkeypatch):
    """No server-side session state: a workspace sent on one request must not
    appear on a subsequent request that omits it."""
    captured: list[dict] = []
    monkeypatch.setattr(chat_module, "ChatService", _make_spy_service(captured))

    first = client.post("/api/chat", json={"message": "m1", "workspace": "/tmp/repo-a"})
    second = client.post("/api/chat", json={"message": "m2"})

    assert first.status_code == 200 and second.status_code == 200
    assert captured[0]["workspace"] == "/tmp/repo-a"
    assert captured[1]["workspace"] is None, (
        "workspace must arrive per-request only - it must not persist "
        "server-side between requests (no session state / global / cache)"
    )


# --------------------------------------------------------------------------- #
# 3. Workspace sentence in the system prompt
# --------------------------------------------------------------------------- #
def test_workspace_sentence_contains_absolute_path(tmp_path):
    """When workspace is set, the system prompt names the absolute path."""
    workspace = str(tmp_path.resolve())
    assert os.path.isabs(workspace)

    system = _capture_system_prompt(workspace)

    assert workspace in system, (
        "the system prompt for the turn must state the active workspace "
        f"absolute path {workspace!r}"
    )


def test_workspace_sentence_instructs_repo_root_usage(tmp_path):
    """The workspace sentence must tell the model to use it as repo_root."""
    workspace = str(tmp_path.resolve())

    system = _capture_system_prompt(workspace)

    assert "repo_root" in system, (
        "the workspace sentence must instruct the model to use the workspace "
        "as repo_root when authoring a plan"
    )


def test_workspace_none_leaves_system_prompt_unchanged():
    """With workspace=None the system prompt is exactly today's SYSTEM_PROMPT."""
    system = _capture_system_prompt(None)

    assert system == chat_module.SYSTEM_PROMPT, (
        "workspace=None must not alter the system prompt"
    )
    # The workspace sentence must be conditional, not baked into the base
    # prompt: the unchanged prompt must contain neither a repo_root
    # instruction nor any workspace path.
    assert "repo_root" not in system, (
        "the base SYSTEM_PROMPT must not mention repo_root; the workspace "
        "sentence is per-turn only"
    )


def test_workspace_none_leaves_user_prompt_unchanged():
    """With workspace=None the user prompt is still the bare message."""
    driver = _RecordingDriver()
    svc = ChatService(driver=driver, http_client=object(), api_base_url="http://127.0.0.1:8000")
    svc.execute_turn("hello", workspace=None)

    assert driver.calls[0]["prompt"] == "hello"


def test_empty_string_workspace_treated_as_absent():
    """An empty-string workspace counts as absent: no workspace sentence."""
    system = _capture_system_prompt("")

    assert system == chat_module.SYSTEM_PROMPT, (
        "workspace='' must be treated as absent and leave the system prompt unchanged"
    )


def test_workspace_path_with_spaces_included_verbatim(tmp_path):
    """Boundary: a workspace path containing spaces is embedded unmodified."""
    ws_dir = tmp_path / "my repo"
    ws_dir.mkdir()
    workspace = str(ws_dir.resolve())

    system = _capture_system_prompt(workspace)

    assert workspace in system, "the workspace path must appear verbatim (no quoting/mangling)"


# --------------------------------------------------------------------------- #
# 4. Per-request only: no session state, no global, no cache
# --------------------------------------------------------------------------- #
def test_workspace_is_per_request_no_stale_state(tmp_path):
    """Consecutive turns on one service must not leak workspace either way."""
    ws_a = str((tmp_path / "repo-a").resolve())
    ws_b = str((tmp_path / "repo-b").resolve())
    driver = _RecordingDriver()
    svc = ChatService(driver=driver, http_client=object(), api_base_url="http://127.0.0.1:8000")

    svc.execute_turn("m1", workspace=ws_a)
    svc.execute_turn("m2")  # no workspace this turn
    svc.execute_turn("m3", workspace=ws_b)

    assert len(driver.calls) == 3
    assert ws_a in driver.calls[0]["system"]
    assert driver.calls[1]["system"] == chat_module.SYSTEM_PROMPT, (
        "a workspace from a previous turn must not leak into a later turn "
        "(no session state / global / cache)"
    )
    assert ws_b in driver.calls[2]["system"]
    assert ws_a not in driver.calls[2]["system"], (
        "a new workspace must fully replace the previous one"
    )


# --------------------------------------------------------------------------- #
# 5. Prompt still generated from the TOOLS registry (membership only)
# --------------------------------------------------------------------------- #
def test_prompt_still_generated_from_tools_registry(tmp_path):
    """A registered tool name appears in the prompt - by MEMBERSHIP only.

    Deliberately never asserts the total tool list, an exact count, or an
    exact full-prompt string: SYSTEM_PROMPT is a shared artifact assembled
    from TOOLS via _available_tools_sentence() and must stay that way.
    """
    assert TOOLS, "TOOLS registry must not be empty"
    tool_name = min(TOOLS)

    system_with_workspace = _capture_system_prompt(str(tmp_path.resolve()))
    system_without_workspace = _capture_system_prompt(None)

    assert tool_name in system_with_workspace, (
        f"registered tool {tool_name!r} must still be named in the system prompt"
    )
    assert tool_name in system_without_workspace
    assert tool_name in chat_module.SYSTEM_PROMPT


def test_system_prompt_assignment_still_uses_available_tools_sentence():
    """SYSTEM_PROMPT must still be assembled via _available_tools_sentence().

    Guards against a hand-written frozen tool list being pasted into the
    prompt (the exact drift bug _available_tools_sentence was written to fix).
    """
    source = inspect.getsource(chat_module)
    match = re.search(r"^SYSTEM_PROMPT\s*=", source, re.MULTILINE)
    assert match, "SYSTEM_PROMPT assignment not found in app/chat.py"
    window = source[match.start() : match.start() + 300]
    assert "_available_tools_sentence()" in window, (
        "SYSTEM_PROMPT must keep being built from _available_tools_sentence() "
        "(the TOOLS registry), not from a hand-written tool list"
    )


# --------------------------------------------------------------------------- #
# 6. Structural / mechanically-checkable requirements
# --------------------------------------------------------------------------- #
def test_execute_turn_signature_has_keyword_only_workspace():
    """execute_turn must accept a keyword-only workspace defaulting to None."""
    sig = inspect.signature(ChatService.execute_turn)
    assert "workspace" in sig.parameters, (
        "ChatService.execute_turn is missing the workspace parameter"
    )
    param = sig.parameters["workspace"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
        "workspace must be keyword-only (like plan_name)"
    )
    assert param.default is None, "workspace must default to None"


def test_chat_endpoint_source_forwards_workspace():
    """chat_endpoint must pass workspace=req.workspace to execute_turn."""
    source = inspect.getsource(chat_module.chat_endpoint)
    assert "workspace=req.workspace" in source, (
        "chat_endpoint must forward workspace=req.workspace to execute_turn"
    )