from unittest.mock import MagicMock

import pytest

from pipeline import server, server_tools_read


@pytest.fixture
def fake_service(monkeypatch):
    service = MagicMock()
    service.get_manifest_or_none.return_value = {"stories": {"S-1": {}}}
    service.get_story_diff.return_value = {"available": True, "files": []}
    monkeypatch.setattr(server, "_service", service)
    return service


def test_should_register_get_story_diff_on_the_mcp_tool_manager():
    registered = server.mcp._tool_manager.get_tool("get_story_diff")

    assert registered.fn is server_tools_read.get_story_diff


def test_should_delegate_summary_request_to_the_service(fake_service):
    result = server_tools_read.get_story_diff("plan", "S-1")

    assert result == {"available": True, "files": []}
    fake_service.get_story_diff.assert_called_once_with(
        "plan", "S-1", {"stories": {"S-1": {}}}, path=None
    )


def test_should_pass_path_through_to_the_service(fake_service):
    server_tools_read.get_story_diff("plan", "S-1", path="pipeline/a.py")

    assert fake_service.get_story_diff.call_args.kwargs["path"] == "pipeline/a.py"


def test_should_clamp_overlong_path_to_the_cap(fake_service):
    server_tools_read.get_story_diff("plan", "S-1", path="a" * (server_tools_read.MAX_PATH_LENGTH + 50))

    assert fake_service.get_story_diff.call_args.kwargs["path"] == "a" * server_tools_read.MAX_PATH_LENGTH


def test_should_not_alter_path_at_the_cap(fake_service):
    path = "a" * server_tools_read.MAX_PATH_LENGTH

    server_tools_read.get_story_diff("plan", "S-1", path=path)

    assert fake_service.get_story_diff.call_args.kwargs["path"] == path


def test_should_return_not_found_when_plan_has_no_manifest(fake_service):
    fake_service.get_manifest_or_none.return_value = None
    fake_service.get_story_diff.return_value = {"ok": False, "status": 404, "error": "unknown plan/story"}

    result = server_tools_read.get_story_diff("missing", "S-1")

    assert result["status"] == 404
    assert fake_service.get_story_diff.call_args.args[2] == {}


def test_should_report_no_worktree_without_raising(monkeypatch):
    service = server.PipelineService()
    monkeypatch.setattr(service, "get_manifest_or_none", lambda _p: {"stories": {"S-1": {"summary": "s"}}})
    monkeypatch.setattr(server, "_service", service)

    result = server_tools_read.get_story_diff("plan", "S-1", path="x.py")

    assert result == {"available": False, "reason": "no_worktree"}
