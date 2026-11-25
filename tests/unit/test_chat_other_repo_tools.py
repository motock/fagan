"""Tests for the chat's cross-repository planning tools (xrp-01).

The planner can list the repositories the team has connected and read any of
them, through the same routes and access check the console uses:

  * app/dashboard_routes.py: ``GET /api/repositories`` answers the listing
    ``pipeline.repo_listing_hooks.list_repositories_result`` gives for the
    namespace bound to the request.
  * app/chat_repo_tools.py: ``OTHER_REPO_TOOLS`` holds ``list_repositories``
    and three read tools that take ``repo`` as ``host/owner/name``.
  * app/chat.py: merges those tools into ``TOOLS``; ``read_file``,
    ``list_directory`` and ``search_code`` keep serving the turn's repository,
    and ``save_plan`` still writes the turn's repository onto the plan.

Only the HTTP client, the repository listing hook and the store are stubbed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import chat, dashboard_routes
from app import dashboard as d
from app.chat import SYSTEM_PROMPT
from app.chat_actions import ACTION_TOOLS, PROPOSAL_ONLY_TOOLS
from app.identity import ROLE_AUTHOR
from pipeline import namespace_access as access_module
from pipeline import repo_listing_hooks
from pipeline.namespace_access import NAMESPACE_HEADER
from pipeline.namespace_resolver import NamespaceAccess
from tests.unit.test_chat_repo_reads import (
    REPO_URL,
    _call,
    _RecordingGetHttp,
    _Response,
    _ScriptedDriver,
    _turn,
)

NAMESPACE = "team-a"
REPO = "github.com/acme/widgets"
OTHER_REPO = "github.com/other/lib"
OTHER_REPO_URL = "https://github.com/other/lib"
API_BASE = "http://127.0.0.1:8000"
NEW_TOOLS = ("list_repositories", "read_repository_file", "list_repository_directory", "search_repository")
REPO_FORMAT_ERROR = {"error": "repo must be host/owner/name"}
REPLY = "done"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _RecordingHttp(_RecordingGetHttp):
    """Records GETs and POSTs; every answer is ``{"ok": True}``."""

    def __init__(self) -> None:
        super().__init__()
        self.posts: list[tuple[str, dict]] = []

    def post(self, url, json=None, **kwargs):
        self.posts.append((url, json))
        return _Response({"ok": True})


class _Resolver:
    """The caller may reach both namespaces; the request header names one."""

    def accesses(self, principal):
        return tuple(
            NamespaceAccess(name, frozenset({ROLE_AUTHOR})) for name in (NAMESPACE, "team-b")
        )


class _ListingHook:
    """Stands in for a deployment's repository listing plugin."""

    def __init__(self, *, repos=None, error=None) -> None:
        self.repos = repos if repos is not None else []
        self.error = error
        self.namespaces: list[str] = []

    def list_repositories(self, namespace):
        self.namespaces.append(namespace)
        if self.error is not None:
            raise self.error
        return self.repos


class _StoreRef:
    """Stands in for the server's store reference; the listing never reads it."""

    def _value(self):
        return object()


@pytest.fixture
def client():
    return TestClient(d.app)


@pytest.fixture
def namespaces(monkeypatch):
    monkeypatch.setattr(access_module, "active_namespace_resolver", lambda: _Resolver())


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(dashboard_routes, "_store", _StoreRef())


@pytest.fixture
def listing(monkeypatch):
    hook = _ListingHook(repos=[{"repo": REPO, "level": None, "state": None}])
    monkeypatch.setattr(repo_listing_hooks, "_hook", lambda: hook)
    return hook


def _execute(name: str, http, **args):
    """Run one of the new tools directly, as the chat would call it."""
    from app.chat_repo_tools import OTHER_REPO_TOOLS

    return OTHER_REPO_TOOLS[name]["execute"](http, API_BASE, **args)


# --------------------------------------------------------------------------- #
# GET /api/repositories
# --------------------------------------------------------------------------- #
def test_should_list_the_repositories_of_the_bound_namespace(client, namespaces, store, listing):
    response = client.get("/api/repositories", headers={NAMESPACE_HEADER: NAMESPACE})

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "repositories": [{"repo": REPO, "level": None, "state": None}],
    }
    assert listing.namespaces == [NAMESPACE]


def test_should_answer_repositories_unavailable_without_the_listing_error_text(
    client, namespaces, store, monkeypatch
):
    hook = _ListingHook(error=RuntimeError("upstream token abc123 rejected"))
    monkeypatch.setattr(repo_listing_hooks, "_hook", lambda: hook)

    response = client.get("/api/repositories", headers={NAMESPACE_HEADER: NAMESPACE})

    body = response.json()
    assert body["ok"] is False
    assert body["reason"] == "repositories_unavailable"
    assert "correlation_id" in body
    assert "abc123" not in response.text


def test_should_ignore_a_namespace_query_parameter(client, namespaces, store, listing):
    response = client.get(
        "/api/repositories?namespace=team-b", headers={NAMESPACE_HEADER: NAMESPACE}
    )

    assert response.status_code == 200
    assert listing.namespaces == [NAMESPACE]


# --------------------------------------------------------------------------- #
# list_repositories chat tool
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("turn_repo_url", [None, REPO_URL])
def test_should_call_the_listing_route_without_a_repo_url(turn_repo_url):
    driver = _ScriptedDriver(_call("list_repositories", {}), REPLY)
    http = _RecordingGetHttp()

    _turn(driver, http, repo_url=turn_repo_url)

    assert http.gets == [("/api/repositories", None)]


# --------------------------------------------------------------------------- #
# Read tools: repo becomes repo_url, the other parameter passes through
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name, args, route, params",
    [
        ("read_repository_file", {"repo": REPO, "path": "README.md"}, "/api/workspace/file", {"path": "README.md"}),
        ("list_repository_directory", {"repo": REPO, "path": "src"}, "/api/workspace/files", {"path": "src"}),
        ("search_repository", {"repo": REPO, "pattern": "needle"}, "/api/workspace/search", {"pattern": "needle"}),
    ],
)
def test_should_send_the_repo_as_a_repo_url_to_the_matching_route(name, args, route, params):
    driver = _ScriptedDriver(_call(name, args), REPLY)
    http = _RecordingGetHttp()

    _turn(driver, http)

    assert http.gets == [(route, {**params, "repo_url": REPO_URL})]


def test_should_read_the_repository_the_model_names_even_when_the_turn_names_another():
    driver = _ScriptedDriver(
        _call("read_repository_file", {"repo": OTHER_REPO, "path": "api.py"}),
        REPLY,
    )
    http = _RecordingGetHttp()

    _turn(driver, http, repo_url=REPO_URL)

    assert http.gets == [("/api/workspace/file", {"path": "api.py", "repo_url": OTHER_REPO_URL})]


def test_should_keep_read_file_on_the_turn_repository_after_a_read_of_another_repository():
    driver = _ScriptedDriver(
        _call("read_repository_file", {"repo": OTHER_REPO, "path": "api.py"}),
        _call("read_file", {"path": "README.md"}),
        REPLY,
    )
    http = _RecordingGetHttp()

    _turn(driver, http, repo_url=REPO_URL)

    assert http.gets == [
        ("/api/workspace/file", {"path": "api.py", "repo_url": OTHER_REPO_URL}),
        ("/api/workspace/file", {"path": "README.md", "repo_url": REPO_URL}),
    ]


@pytest.mark.parametrize(
    "repo",
    [
        "github.com/acme",
        "https://github.com/acme/x",
        "github.com/acme/x/y",
        "user:tok@github.com/acme/x",
        "github.com/acme/x?ref=1",
        "github.com/acme x",
        "../x/y",
        "",
    ],
)
def test_should_refuse_a_malformed_repo_without_an_http_call(repo):
    http = _RecordingGetHttp()

    result = _execute("read_repository_file", http, repo=repo, path="README.md")

    assert result == REPO_FORMAT_ERROR
    assert http.gets == []


class _RefusingHttp(_RecordingGetHttp):
    """Answers every GET with the route's 403 body."""

    def get(self, url, params=None, **kwargs):
        self.gets.append((url, params))
        return _Response({"detail": "repository access refused"})


def test_should_return_the_route_refusal_once_without_a_retry():
    http = _RefusingHttp()

    result = _execute("read_repository_file", http, repo=REPO, path="README.md")

    assert result == {"detail": "repository access refused"}
    assert http.gets == [("/api/workspace/file", {"path": "README.md", "repo_url": REPO_URL})]


# --------------------------------------------------------------------------- #
# save_plan still writes the turn's repository
# --------------------------------------------------------------------------- #
def test_should_write_the_turn_repository_onto_a_plan_saved_after_reading_another_repository():
    plan_json = json.dumps({"epics": []})
    driver = _ScriptedDriver(
        _call("read_repository_file", {"repo": OTHER_REPO, "path": "api.py"}),
        _call("save_plan", {"plan_name": "demo", "plan_json": plan_json}),
        REPLY,
    )
    http = _RecordingHttp()

    _turn(driver, http, repo_url=REPO_URL)

    ((url, body),) = [post for post in http.posts if post[0].endswith("/save")]
    assert url == "/api/plans/demo/save"
    assert json.loads(body["plan_json"])["repo_url"] == REPO_URL


# --------------------------------------------------------------------------- #
# Prompt, registry and gating
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", NEW_TOOLS)
def test_should_name_each_new_tool_in_the_system_prompt(name):
    assert name in SYSTEM_PROMPT


def test_should_carry_the_planning_guidance_into_the_system_prompt():
    assert "search the other repositories for code that depends on it" in SYSTEM_PROMPT


def test_should_keep_the_system_prompt_assembly_unchanged():
    assert SYSTEM_PROMPT == (
        chat._SYSTEM_PROMPT_PREFIX + chat._available_tools_sentence() + chat._FINAL_SENTENCE
    )


@pytest.mark.parametrize("name", NEW_TOOLS)
def test_should_not_gate_the_new_tool_as_an_action(name):
    assert name not in ACTION_TOOLS


@pytest.mark.parametrize("name", NEW_TOOLS)
def test_should_not_list_the_new_tool_as_proposal_only(name):
    assert name not in PROPOSAL_ONLY_TOOLS


@pytest.mark.parametrize("name", NEW_TOOLS)
def test_should_not_add_the_new_tool_to_the_repo_read_tools(name):
    assert name not in chat._REPO_READ_TOOLS


def test_should_keep_the_chat_module_at_or_under_999_lines():
    chat_source = Path(chat.__file__).read_text(encoding="utf-8")

    assert len(chat_source.splitlines()) <= 999
