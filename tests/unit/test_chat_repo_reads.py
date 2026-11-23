"""Tests for chat reads that serve the repository the turn names (crw-01).

The file, directory and search tools used to read the one process-global
active workspace, which the console never sets. A chat turn that names a
repository now reads that repository's managed clone instead:

  * pipeline/service.py: ``read_workspace_file``, ``list_workspace_directory``
    and ``search_workspace`` take a keyword-only ``root``.
  * app/dashboard.py: the three ``GET /api/workspace/...`` routes take an
    optional ``repo_url`` query parameter resolved through
    ``pipeline.repo_clone.managed_path``.
  * app/chat.py: the read tools send ``repo_url`` only when the turn has one,
    and the model can never choose the repository.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import dashboard as d
from app.chat import SYSTEM_PROMPT, ChatService
from pipeline import repo_clone, repo_read_access

REPO_URL = "https://github.com/acme/widgets"
OTHER_REPO_URL = "https://github.com/evil/other"
REFUSED_DETAIL = "repo_url is not an allowed repository URL"
REPO_READS_LINE = (
    "File, directory and search tools read that repository's checkout; "
    "you do not need set_workspace."
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def repos_root(tmp_path, monkeypatch):
    """A managed-clone root holding ``github.com/acme/widgets`` with one file."""
    root = tmp_path / "repos"
    clone = root / "github.com" / "acme" / "widgets"
    (clone / "src").mkdir(parents=True)
    (clone / "README.md").write_text("hello widgets\n", encoding="utf-8")
    (clone / "src" / "app.py").write_text("needle = 1\n", encoding="utf-8")
    monkeypatch.setenv("PIPELINE_REPOS_ROOT", str(root))
    monkeypatch.delenv("PIPELINE_REPO_URL_HOSTS", raising=False)
    monkeypatch.delenv("PIPELINE_REPO_URL_ALLOW_FILE", raising=False)
    return root


@pytest.fixture
def client():
    return TestClient(d.app)


@pytest.fixture
def no_active_workspace(monkeypatch):
    """Pin the process-global active workspace to a sentinel directory."""
    monkeypatch.setattr(d._service, "get_active_workspace", lambda: None)


# --------------------------------------------------------------------------- #
# Dashboard routes: repo_url selects the managed clone
# --------------------------------------------------------------------------- #
def test_should_read_a_file_from_the_named_repository_clone(client, repos_root, no_active_workspace):
    response = client.get("/api/workspace/file", params={"path": "README.md", "repo_url": REPO_URL})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "path": "README.md", "content": "hello widgets\n"}


def test_should_list_and_search_the_named_repository_clone(client, repos_root, no_active_workspace):
    listing = client.get("/api/workspace/files", params={"path": "", "repo_url": REPO_URL})
    search = client.get("/api/workspace/search", params={"pattern": "needle", "repo_url": REPO_URL})

    assert listing.status_code == 200
    assert {entry["name"] for entry in listing.json()["entries"]} == {"README.md", "src"}
    assert search.status_code == 200
    matches = search.json()["matches"]
    assert len(matches) == 1
    assert matches[0].endswith("src/app.py:1:needle = 1")


@pytest.mark.parametrize(
    "repo_url",
    [
        "https://evil.example/acme/widgets",
        "http://github.com/acme/widgets",
        "https://github.com/acme/widgets?x=1",
        "file:///tmp/acme/widgets.git",
    ],
)
def test_should_refuse_a_repo_url_outside_the_allowlist_with_the_fixed_detail(
    client, repos_root, no_active_workspace, repo_url
):
    response = client.get("/api/workspace/file", params={"path": "README.md", "repo_url": repo_url})

    assert response.status_code == 400
    assert response.json() == {"detail": REFUSED_DETAIL}
    assert repo_url not in response.text
    assert str(repos_root) not in response.text


@pytest.mark.parametrize("path", ["../../../etc/passwd", "/etc/passwd"])
def test_should_refuse_a_path_that_escapes_the_clone(client, repos_root, no_active_workspace, path):
    response = client.get("/api/workspace/file", params={"path": path, "repo_url": REPO_URL})

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid path"}
    assert str(repos_root) not in response.text


def test_should_refuse_a_clone_path_outside_the_repos_root(client, tmp_path, monkeypatch, no_active_workspace):
    outside = tmp_path / "elsewhere" / "widgets"
    outside.mkdir(parents=True)
    (outside / "README.md").write_text("outside the root\n", encoding="utf-8")
    monkeypatch.setattr(repo_read_access, "managed_path", lambda url: outside)

    response = client.get("/api/workspace/file", params={"path": "README.md", "repo_url": REPO_URL})

    assert response.status_code == 400
    assert response.json() == {"detail": REFUSED_DETAIL}
    assert "outside the root" not in response.text
    assert str(outside) not in response.text


def test_should_answer_repository_not_cloned_yet_for_a_missing_clone(client, tmp_path, monkeypatch, no_active_workspace):
    monkeypatch.setenv("PIPELINE_REPOS_ROOT", str(tmp_path / "empty"))

    def refused_clone(url, environ=None, timeout=600):
        raise repo_clone.RepoCloneError("git clone failed")

    monkeypatch.setattr(repo_clone, "ensure_clone", refused_clone)

    response = client.get("/api/workspace/file", params={"path": "README.md", "repo_url": REPO_URL})

    assert response.status_code == 400
    assert response.json() == {"detail": "repository not cloned yet"}


def test_should_read_a_file_from_a_repository_cloned_on_its_first_read(client, tmp_path, monkeypatch, no_active_workspace):
    monkeypatch.setenv("PIPELINE_REPOS_ROOT", str(tmp_path / "fresh"))
    cloned: list[str] = []

    def write_clone(url, environ=None, timeout=600):
        cloned.append(url)
        path = repo_clone.managed_path(url)
        path.mkdir(parents=True)
        (path / "README.md").write_text("hello widgets\n", encoding="utf-8")
        return path

    monkeypatch.setattr(repo_clone, "ensure_clone", write_clone)

    response = client.get("/api/workspace/file", params={"path": "README.md", "repo_url": REPO_URL})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "path": "README.md", "content": "hello widgets\n"}
    assert cloned == [REPO_URL]


def test_should_keep_the_active_workspace_when_no_repo_url_is_given(client, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "active.txt").write_text("from the active workspace\n", encoding="utf-8")
    monkeypatch.setattr(d._service, "get_active_workspace", lambda: str(workspace))

    response = client.get("/api/workspace/file", params={"path": "active.txt"})

    assert response.status_code == 200
    assert response.json()["content"] == "from the active workspace\n"


# --------------------------------------------------------------------------- #
# Chat: the turn's repo_url reaches the read tools, and only that one
# --------------------------------------------------------------------------- #
class _ScriptedDriver:
    model = "fake-model"

    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)
        self.systems: list[str] = []

    def complete(self, *, prompt, system, model, cwd=None, **kwargs) -> str:
        self.systems.append(system)
        return self._responses.pop(0)


class _Response:
    def __init__(self, payload) -> None:
        self._payload = payload

    def json(self):
        return self._payload


class _RecordingGetHttp:
    """Fake httpx client that records every GET and answers ``{"ok": True}``."""

    def __init__(self) -> None:
        self.gets: list[tuple[str, dict]] = []

    def get(self, url, params=None, **kwargs):
        self.gets.append((url, params))
        return _Response({"ok": True})

    def post(self, url, json=None, **kwargs):
        return _Response({"ok": True})


REPLY = "done"


def _call(name: str, args: dict) -> str:
    return "[TOOL_CALL]" + json.dumps({"name": name, "args": args}) + "[/TOOL_CALL]"


def _turn(driver, http, *, repo_url=None):
    svc = ChatService(driver=driver, http_client=http, api_base_url="http://127.0.0.1:8000")
    return svc.execute_turn("hi", repo_url=repo_url)


def test_should_inject_the_turn_repo_url_into_read_tool_args():
    driver = _ScriptedDriver(_call("read_file", {"path": "README.md"}), REPLY)
    http = _RecordingGetHttp()

    _turn(driver, http, repo_url=REPO_URL)

    assert http.gets == [("/api/workspace/file", {"path": "README.md", "repo_url": REPO_URL})]


@pytest.mark.parametrize(
    "name, args, route, params",
    [
        ("read_file", {"path": "README.md", "repo_url": OTHER_REPO_URL}, "/api/workspace/file", {"path": "README.md"}),
        ("list_directory", {"path": "src", "repo_url": OTHER_REPO_URL}, "/api/workspace/files", {"path": "src"}),
        ("search_code", {"pattern": "needle", "repo_url": OTHER_REPO_URL}, "/api/workspace/search", {"pattern": "needle"}),
    ],
)
def test_should_replace_a_model_sent_repo_url_on_read_tools(name, args, route, params):
    driver = _ScriptedDriver(_call(name, args), REPLY)
    http = _RecordingGetHttp()

    _turn(driver, http, repo_url=REPO_URL)

    assert http.gets == [(route, {**params, "repo_url": REPO_URL})]


@pytest.mark.parametrize(
    "name, args, route, params",
    [
        ("read_file", {"path": "README.md", "repo_url": OTHER_REPO_URL}, "/api/workspace/file", {"path": "README.md"}),
        ("list_directory", {"path": "src", "repo_url": OTHER_REPO_URL}, "/api/workspace/files", {"path": "src"}),
        ("search_code", {"pattern": "needle", "repo_url": OTHER_REPO_URL}, "/api/workspace/search", {"pattern": "needle"}),
    ],
)
def test_should_drop_a_model_sent_repo_url_without_a_turn_repo_url(name, args, route, params):
    driver = _ScriptedDriver(_call(name, args), REPLY)
    http = _RecordingGetHttp()

    _turn(driver, http)

    assert http.gets == [(route, params)]


def test_should_send_no_repo_url_param_without_one():
    driver = _ScriptedDriver(_call("read_file", {"path": "README.md"}), REPLY)
    http = _RecordingGetHttp()

    _turn(driver, http)

    assert http.gets == [("/api/workspace/file", {"path": "README.md"})]


def test_should_tell_the_planner_reads_use_the_repository():
    driver = _ScriptedDriver(REPLY)

    _turn(driver, _RecordingGetHttp(), repo_url=REPO_URL)

    assert REPO_READS_LINE in driver.systems[0]


def test_should_not_tell_the_planner_reads_use_the_repository_without_one():
    driver = _ScriptedDriver(REPLY)

    _turn(driver, _RecordingGetHttp())

    assert REPO_READS_LINE not in driver.systems[0]
    assert driver.systems[0] == SYSTEM_PROMPT
