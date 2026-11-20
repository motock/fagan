"""Tests for crr-01: the chat and /api/decompose accept ``repo_url``.

A signed-in author drafts a plan with the chat. The server, not the model,
names the repository: a request ``repo_url`` is threaded through the chat
turn, injected into the ``decompose`` and ``save_plan`` tool calls, and
written onto the decomposed plan by ``PipelineService.decompose_plan``. A
request naming both ``repo_url`` and ``workspace`` is refused.

Every backend is stubbed: the clone helper (``repo_clone.ensure_clone``) and
the decompose backend (``pipeline.server._run_decompose_detailed``). No test
clones a repository or calls a model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.chat as chat_module
import app.dashboard_routes_plan as routes_plan
from app import dashboard as d
from app.chat import SYSTEM_PROMPT, ChatRequest, ChatService
from pipeline import repo_clone
from pipeline import server as p

REPO_URL = "https://github.com/acme/widgets"
OTHER_URL = "https://github.com/other/thing"
REPLY = "All done - no tool calls needed."


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _ScriptedDriver:
    """Fake backend driver: returns scripted responses and records prompts."""

    model = "fake-model"

    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)
        self.systems: list[str] = []

    def complete(self, *, prompt, system, model, cwd=None, **kwargs) -> str:
        self.systems.append(system)
        return self._responses.pop(0)


class _Response:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _RecordingHttp:
    """Fake httpx client: records every POST and answers with ``{"ok": True}``."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, Any]] = []

    def post(self, url, json=None, **kwargs):
        self.posts.append((url, json))
        return _Response({"ok": True})

    def get(self, url, **kwargs):
        return _Response({"ok": True})


def _call(name: str, args: dict) -> str:
    return "[TOOL_CALL]" + json.dumps({"name": name, "args": args}) + "[/TOOL_CALL]"


def _turn(driver, http, *, repo_url=None, workspace=None, message="hi", service=None):
    svc = service or ChatService(driver=driver, http_client=http, api_base_url="http://127.0.0.1:8000")
    return svc.execute_turn(message, repo_url=repo_url, workspace=workspace)


def _sent_plan_json(http: _RecordingHttp) -> dict:
    """Return the plan object the save_plan tool posted, parsed."""
    (_, body), = [post for post in http.posts if post[0].endswith("/save")]
    return json.loads(body["plan_json"])


# --------------------------------------------------------------------------- #
# 1. ChatRequest model
# --------------------------------------------------------------------------- #
def test_chat_request_accepts_repo_url_and_defaults_to_none():
    assert ChatRequest(message="hi").repo_url is None
    assert ChatRequest(message="hi", repo_url=REPO_URL).repo_url == REPO_URL


# --------------------------------------------------------------------------- #
# 2-3. System prompt
# --------------------------------------------------------------------------- #
def test_system_prompt_names_the_active_repository_when_repo_url_is_set():
    driver = _ScriptedDriver(REPLY)
    _turn(driver, _RecordingHttp(), repo_url=REPO_URL)

    system = driver.systems[0]
    assert f"The active repository is {REPO_URL}." in system
    assert (
        "Plans you draft and save target that repository; the server records it "
        "on the plan, so do not add repo_root or repo_url to plan JSON yourself."
    ) in system


def test_system_prompt_is_unchanged_without_repo_url():
    driver = _ScriptedDriver(REPLY, REPLY)
    _turn(driver, _RecordingHttp())
    _turn(driver, _RecordingHttp(), repo_url="")

    assert driver.systems == [SYSTEM_PROMPT, SYSTEM_PROMPT]


# --------------------------------------------------------------------------- #
# 4. Chat endpoint refusal (before any model call)
# --------------------------------------------------------------------------- #
class _NeverConstructed:
    def __init__(self, *args, **kwargs):
        raise AssertionError("ChatService must not be built for a refused request")


@pytest.mark.parametrize("path", ["/api/chat", "/api/chat/stream"])
def test_chat_refuses_repo_url_and_workspace_together(monkeypatch, path):
    monkeypatch.setattr(chat_module, "ChatService", _NeverConstructed)
    client = TestClient(d.app)

    res = client.post(
        path, json={"message": "hi", "repo_url": REPO_URL, "workspace": "/tmp/ws"}
    )

    assert res.status_code == 400
    assert res.json()["detail"] == "name repo_url or workspace, not both"


def test_chat_endpoint_forwards_repo_url_to_the_turn(monkeypatch):
    captured: list[dict] = []

    class _Spy:
        def __init__(self, *args, **kwargs):
            pass

        def execute_turn(self, message, *, plan_name=None, history=None, workspace=None, repo_url=None):
            captured.append({"repo_url": repo_url, "workspace": workspace})
            return {"reply": "hi", "tool_calls": [], "turns": 1}

    monkeypatch.setattr(chat_module, "ChatService", _Spy)

    res = TestClient(d.app).post("/api/chat", json={"message": "hi", "repo_url": REPO_URL})

    assert res.status_code == 200
    assert captured == [{"repo_url": REPO_URL, "workspace": None}]


# --------------------------------------------------------------------------- #
# 5-6. decompose tool body
# --------------------------------------------------------------------------- #
def test_decompose_tool_sends_repo_url_when_set():
    driver = _ScriptedDriver(_call("decompose", {"goal": "build it"}), REPLY)
    http = _RecordingHttp()

    _turn(driver, http, repo_url=REPO_URL)

    assert http.posts == [("/api/decompose", {"request": "build it", "repo_url": REPO_URL})]


def test_decompose_tool_body_is_unchanged_without_repo_url():
    driver = _ScriptedDriver(_call("decompose", {"goal": "build it"}), REPLY)
    http = _RecordingHttp()

    _turn(driver, http)

    assert http.posts == [("/api/decompose", {"request": "build it"})]


def test_decompose_tool_drops_a_model_sent_repo_url_when_none_is_set():
    driver = _ScriptedDriver(
        _call("decompose", {"goal": "build it", "repo_url": OTHER_URL}), REPLY
    )
    http = _RecordingHttp()

    _turn(driver, http)

    assert http.posts == [("/api/decompose", {"request": "build it"})]


# --------------------------------------------------------------------------- #
# 7-8. save_plan tool body
# --------------------------------------------------------------------------- #
def test_save_plan_tool_overwrites_model_repo_root_with_the_request_repo_url():
    plan = {
        "repo_root": "/srv/evil",
        "repo_url": OTHER_URL,
        "workspace": "/srv/evil",
        "epics": [{"summary": "E", "stories": [{"summary": "S"}]}],
    }
    driver = _ScriptedDriver(_call("save_plan", {"plan_name": "demo", "plan_json": json.dumps(plan)}), REPLY)
    http = _RecordingHttp()

    _turn(driver, http, repo_url=REPO_URL)

    sent = _sent_plan_json(http)
    assert sent == {"repo_url": REPO_URL, "epics": plan["epics"]}


def test_save_plan_tool_passes_unparseable_plan_json_through_unchanged():
    driver = _ScriptedDriver(_call("save_plan", {"plan_name": "demo", "plan_json": "not json"}), REPLY)
    http = _RecordingHttp()

    _turn(driver, http, repo_url=REPO_URL)

    assert http.posts == [("/api/plans/demo/save", {"plan_json": "not json"})]


@pytest.mark.parametrize("plan_json", ["[1, 2]", '"a string"', "42"])
def test_save_plan_tool_passes_a_non_object_plan_json_through_unchanged(plan_json):
    driver = _ScriptedDriver(_call("save_plan", {"plan_name": "demo", "plan_json": plan_json}), REPLY)
    http = _RecordingHttp()

    _turn(driver, http, repo_url=REPO_URL)

    assert http.posts == [("/api/plans/demo/save", {"plan_json": plan_json})]


def test_injected_args_are_the_ones_recorded_in_tool_calls():
    driver = _ScriptedDriver(_call("decompose", {"goal": "g"}), REPLY)
    http = _RecordingHttp()

    result = _turn(driver, http, repo_url=REPO_URL)

    assert result["tool_calls"][0]["args"] == {"goal": "g", "repo_url": REPO_URL}


# --------------------------------------------------------------------------- #
# 9. No state leaks between turns
# --------------------------------------------------------------------------- #
def test_two_turns_do_not_leak_repo_url():
    driver = _ScriptedDriver(
        _call("decompose", {"goal": "first"}),
        REPLY,
        _call("decompose", {"goal": "second"}),
        REPLY,
    )
    http = _RecordingHttp()
    svc = ChatService(driver=driver, http_client=http, api_base_url="http://127.0.0.1:8000")

    svc.execute_turn("one", repo_url=REPO_URL)
    svc.execute_turn("two")

    assert http.posts == [
        ("/api/decompose", {"request": "first", "repo_url": REPO_URL}),
        ("/api/decompose", {"request": "second"}),
    ]
    assert driver.systems[2] == SYSTEM_PROMPT


# --------------------------------------------------------------------------- #
# 10-11. /api/decompose route
# --------------------------------------------------------------------------- #
class _FakeDecomposeService:
    """Stands in for the dashboard's service. Reading the active workspace fails."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def get_active_workspace(self):
        raise AssertionError("decompose must not read the active workspace")

    def decompose_plan(self, request, workspace=None, **kwargs):
        self.calls.append((request, workspace, kwargs))
        return {"ok": True, "plan": {"repo_url": REPO_URL, "epics": []}}


@pytest.fixture
def fake_service(monkeypatch):
    fake = _FakeDecomposeService()
    monkeypatch.setattr(routes_plan, "_service", fake)
    return fake


def test_decompose_route_with_repo_url_does_not_read_the_active_workspace(fake_service):
    res = TestClient(d.app).post("/api/decompose", json={"request": "goal", "repo_url": REPO_URL})

    assert res.status_code == 200
    assert fake_service.calls == [("goal", None, {"repo_url": REPO_URL})]


def test_decompose_route_refuses_repo_url_and_workspace_together(fake_service):
    res = TestClient(d.app).post(
        "/api/decompose",
        json={"request": "goal", "repo_url": REPO_URL, "workspace": "/tmp/ws"},
    )

    assert res.status_code == 400
    assert res.json()["detail"] == "name repo_url or workspace, not both"
    assert fake_service.calls == []


# --------------------------------------------------------------------------- #
# 12-14. PipelineService.decompose_plan
# --------------------------------------------------------------------------- #
@pytest.fixture
def plan_dir(tmp_path, monkeypatch) -> Path:
    """Keep the active-workspace store hermetic: no host state is read."""
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", directory)
    return directory


@pytest.fixture
def clone_calls(tmp_path, monkeypatch) -> list[str]:
    """Stub the clone helper to return a fixed managed path, recording URLs."""
    calls: list[str] = []

    def _ensure_clone(url, *args, **kwargs):
        calls.append(url)
        return tmp_path / "clones" / "widgets"

    monkeypatch.setattr(repo_clone, "ensure_clone", _ensure_clone)
    return calls


@pytest.fixture
def backend(monkeypatch):
    """Stub the decompose backend. The model's plan names a server repo_root,
    so any repo_root in a result must have been stripped by the service."""
    seen: list[dict] = []
    plan = {"repo_root": "/srv/evil", "epics": [{"summary": "E", "stories": []}]}

    def _run(request, **kwargs):
        seen.append({"request": request, **kwargs})
        return ("```json\n" + json.dumps(plan) + "\n```", None)

    monkeypatch.setattr(p, "_run_decompose_detailed", _run)
    return seen


def test_decompose_plan_with_repo_url_returns_repo_url_and_no_repo_root(
    clone_calls, backend, plan_dir
):
    result = p.PipelineService().decompose_plan("goal", repo_url=REPO_URL)

    assert clone_calls == [REPO_URL]
    assert result["ok"] is True
    assert result["plan"]["repo_url"] == REPO_URL
    assert "repo_root" not in result["plan"]


def test_decompose_plan_with_repo_url_uses_the_clone_as_decompose_context(
    clone_calls, backend, plan_dir, tmp_path
):
    p.PipelineService().decompose_plan("goal", repo_url=REPO_URL)

    assert backend[0]["repo_root"] == str(tmp_path / "clones" / "widgets")


def test_decompose_plan_refuses_a_repo_url_the_clone_helper_refuses(monkeypatch, backend, plan_dir):
    def _refuse(url, *args, **kwargs):
        raise repo_clone.RepoUrlError("repository host is not allowed")

    monkeypatch.setattr(repo_clone, "ensure_clone", _refuse)

    result = p.PipelineService().decompose_plan("goal", repo_url=OTHER_URL)

    assert result == {"ok": False, "error": "repository host is not allowed"}
    assert backend == []


def test_decompose_plan_without_repo_url_is_unchanged(monkeypatch, clone_calls, backend, plan_dir):
    result = p.PipelineService().decompose_plan("goal")

    assert clone_calls == []
    assert "repo_url" not in result["plan"]
    assert result == {"ok": True, "plan": {"repo_root": "/srv/evil", "epics": [{"summary": "E", "stories": []}]}}
