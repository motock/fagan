"""Decompose appends the selected workspace's repo-level context block."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

import pipeline.server  # noqa: F401
from pipeline import context_sources as ctxsrc
from pipeline import planner
from pipeline.service import PipelineService

GOAL = "Build a CLI todo app."
GUIDE = "Repo guidance for decomposition."


@pytest.fixture(autouse=True)
def _allow_decompose_phase(monkeypatch):
    # The phase gate is stubbed so the resolution logic is tested independent
    # of which phases the validator currently accepts.
    monkeypatch.setattr(ctxsrc, "SUPPORTED_PHASES", (*ctxsrc.SUPPORTED_PHASES, "decompose"))


def _workspace(tmp_path, *, required=True, uri=None, phases=("decompose",)):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text(GUIDE)
    source = {
        "name": "guide", "provider": "dir", "uri": uri or str(docs),
        "phases": list(phases), "required": required,
    }
    (tmp_path / ".fagan.json").write_text(json.dumps({"context": {"sources": [source]}}))
    return tmp_path


def _sent_request(**kwargs):
    backend = MagicMock()
    backend.complete.return_value = "{}"
    with patch.object(planner.backend, "get_backend", return_value=backend):
        result = planner._run_decompose_detailed(GOAL, **kwargs)
    return backend, result


def test_should_append_block_after_goal_when_repo_has_context(tmp_path):
    backend, _ = _sent_request(repo_root=str(_workspace(tmp_path)))
    sent = backend.complete.call_args.args[0]
    assert sent.startswith(GOAL)
    assert sent.index(GOAL) < sent.index(GUIDE)


def test_should_leave_request_unchanged_without_repo_root():
    backend, _ = _sent_request()
    assert backend.complete.call_args.args[0] == GOAL


def test_should_leave_request_unchanged_when_repo_has_no_config(tmp_path):
    backend, _ = _sent_request(repo_root=str(tmp_path))
    assert backend.complete.call_args.args[0] == GOAL


def test_should_omit_sources_for_other_phases(tmp_path):
    ws = _workspace(tmp_path, phases=("dispatch",))
    backend, _ = _sent_request(repo_root=str(ws))
    assert backend.complete.call_args.args[0] == GOAL


def test_should_fail_decompose_when_required_source_fails(tmp_path):
    ws = _workspace(tmp_path, uri=str(tmp_path / "missing"))
    backend, (text, error) = _sent_request(repo_root=str(ws))
    assert text is None
    assert error.startswith("context error: required context source failed")
    backend.complete.assert_not_called()


def test_should_skip_optional_source_that_fails(tmp_path):
    ws = _workspace(tmp_path, required=False, uri=str(tmp_path / "missing"))
    backend, _ = _sent_request(repo_root=str(ws))
    assert backend.complete.call_args.args[0] == GOAL


def test_should_fail_decompose_on_invalid_repo_config(tmp_path):
    (tmp_path / ".fagan.json").write_text("{not json")
    backend, (text, error) = _sent_request(repo_root=str(tmp_path))
    assert text is None
    assert "invalid JSON" in error
    backend.complete.assert_not_called()


def test_should_surface_context_error_from_service(tmp_path):
    ws = _workspace(tmp_path, uri=str(tmp_path / "missing"))
    with patch.object(planner.backend, "get_backend"):
        result = PipelineService().decompose_plan(GOAL, str(ws))
    assert result["ok"] is False
    assert "required context source failed" in result["error"]


def test_should_pass_active_workspace_when_none_given(tmp_path):
    svc = PipelineService()
    seen = {}

    def fake(request, **kwargs):
        seen.update(kwargs)
        return None, "boom"

    with (
        patch("pipeline.service._store.get_active_workspace", return_value=str(tmp_path)),
        patch("pipeline.service._run_decompose_detailed", fake),
    ):
        svc.decompose_plan(GOAL)
    assert seen["repo_root"] == str(tmp_path)
