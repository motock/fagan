"""Dispatch helpers no longer take ``manifest_path``.

ADR 0012 stage C removes ``manifest_path`` from the Store Protocol. These
helpers receive the manifest dict and persist through the record store, so
the path parameter must not exist on their signatures and the removed keyword
must be rejected.
"""

import inspect
from contextlib import contextmanager
from pathlib import Path

import pytest

from pipeline import dispatch, dispatch_context, dispatch_phases, dispatch_worktree


def _parameter_names(func) -> list[str]:
    return list(inspect.signature(func).parameters)


def test_should_not_declare_manifest_path_on_resolve_context():
    assert "manifest_path" not in _parameter_names(dispatch_context._step_resolve_context)


def test_should_not_declare_manifest_path_on_detached_test_author():
    assert "manifest_path" not in _parameter_names(
        dispatch_phases._step_detached_test_author
    )


def test_should_not_declare_manifest_path_on_refuse_if_remote_branch_taken():
    assert "manifest_path" not in _parameter_names(
        dispatch_worktree._refuse_if_remote_branch_taken
    )


def test_should_reject_manifest_path_keyword_on_resolve_context(tmp_path):
    with pytest.raises(TypeError, match="manifest_path"):
        dispatch_context._step_resolve_context(
            "plan", "S1", {}, {"stories": {}}, manifest_path=tmp_path / "m.json"
        )


def test_should_reject_manifest_path_keyword_on_detached_test_author(tmp_path):
    with pytest.raises(TypeError, match="manifest_path"):
        dispatch_phases._step_detached_test_author(
            {},
            story_key="S1",
            worktree_path=tmp_path,
            dispatch_backend="claude",
            local_model="m",
            plan_name="plan",
            plan_role_config=None,
            manifest={"stories": {}},
            manifest_path=tmp_path / "m.json",
            marker_path=tmp_path / "marker",
            resuming=False,
        )


def test_should_reject_manifest_path_keyword_on_refuse_if_remote_branch_taken(
    tmp_path, monkeypatch
):
    @contextmanager
    def scoped_repo_root(_plan_name):
        yield tmp_path

    monkeypatch.setattr(dispatch, "_scoped_repo_root", scoped_repo_root)
    monkeypatch.setattr(dispatch, "remote_branch_exists", lambda root, branch: False)

    story: dict = {}
    with pytest.raises(TypeError, match="manifest_path"):
        dispatch_worktree._refuse_if_remote_branch_taken(
            "plan",
            "S1",
            story,
            "agent/S1",
            manifest_path=Path("manifest.json"),
            manifest={"stories": {"S1": story}},
        )
