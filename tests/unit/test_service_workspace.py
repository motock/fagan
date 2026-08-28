"""Failing tests for two new ``PipelineService`` methods (pipeline/service.py):

- ``resolve_workspace(path, create=False)`` delegates to
  ``pipeline.workspace.validate_workspace`` (create=False) or
  ``pipeline.workspace.create_workspace`` (create=True), records the
  resolved path via ``_store.add_recent_workspace`` on an ``ok=True``
  result, and returns the underlying dict unchanged.
- ``list_workspaces()`` returns the store's recent-workspaces list merged
  with every existing manifest's ``repo_root``, most-recent-first,
  de-duplicated by resolved path, each entry shaped as
  ``{"path": str, "exists": bool, "valid": bool}``.

Neither method exists yet on ``PipelineService`` (class begins at
pipeline/service.py:163), so every test here is expected to fail with an
AttributeError until the implementation lands. That is the correct RED
state, not a bug in these tests.

Uses the real ``FileStore`` against a tmp ``PLAN_DIR`` (via the shared
``plan_dir`` fixture from tests/unit/conftest.py) rather than a hand-rolled
fake store, so this suite is agnostic to whether the implementation
discovers manifests via ``self.list_plans()`` + ``self.get_manifest_or_none()``
(PipelineService's existing pattern) or ``_store.list_manifests()`` directly
-- both read from the same real, tmp-isolated PLAN_DIR. All manifest/recents
content is synthetic, written under ``tmp_path``; nothing here reads or
asserts against the operator's real ``~/.claude/plans``.
"""

import json
import subprocess

import pytest

import pipeline.workspace as workspace_mod
from pipeline import server as p
from pipeline.store import FileStore

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _init_git_repo(path):
    """Create a real, minimal git repo with one commit at ``path``."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(path), capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path), capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=str(path), capture_output=True, check=True,
    )
    return path


def _write_manifest(plan_dir, plan_name, repo_root=None, stories=None):
    manifest = {"epics": {}, "stories": stories or {}}
    if repo_root is not None:
        manifest["repo_root"] = repo_root
    (plan_dir / f"{plan_name}.manifest.json").write_text(json.dumps(manifest))


@pytest.fixture
def store():
    return FileStore()


# ---------------------------------------------------------------------------
# resolve_workspace -- delegation (create=False -> validate_workspace)
# ---------------------------------------------------------------------------


def test_resolve_workspace_create_false_delegates_to_validate_workspace(
    plan_dir, monkeypatch
):
    calls = []

    def fake_validate(raw):
        calls.append(raw)
        return {"ok": True, "path": "/resolved/from/validate", "error": None}

    monkeypatch.setattr(workspace_mod, "validate_workspace", fake_validate)

    result = p._service.resolve_workspace("/some/input", create=False)

    assert calls == ["/some/input"], (
        f"validate_workspace must be called exactly once with the raw path, got {calls!r}"
    )
    assert result == {"ok": True, "path": "/resolved/from/validate", "error": None}


def test_resolve_workspace_create_false_does_not_call_create_workspace(
    plan_dir, monkeypatch
):
    create_calls = []
    monkeypatch.setattr(workspace_mod, "validate_workspace", lambda raw: {"ok": False, "path": "", "error": "x"})
    monkeypatch.setattr(
        workspace_mod, "create_workspace", lambda raw: create_calls.append(raw) or {"ok": True, "path": raw, "error": None}
    )

    p._service.resolve_workspace("/some/input", create=False)

    assert create_calls == [], "create=False must never call create_workspace"


# ---------------------------------------------------------------------------
# resolve_workspace -- delegation (create=True -> create_workspace)
# ---------------------------------------------------------------------------


def test_resolve_workspace_create_true_delegates_to_create_workspace(
    plan_dir, monkeypatch
):
    calls = []

    def fake_create(raw):
        calls.append(raw)
        return {"ok": True, "path": "/resolved/from/create", "error": None}

    monkeypatch.setattr(workspace_mod, "create_workspace", fake_create)

    result = p._service.resolve_workspace("/new/ws", create=True)

    assert calls == ["/new/ws"], (
        f"create_workspace must be called exactly once with the raw path, got {calls!r}"
    )
    assert result == {"ok": True, "path": "/resolved/from/create", "error": None}


def test_resolve_workspace_create_true_does_not_call_validate_workspace(
    plan_dir, monkeypatch
):
    validate_calls = []
    monkeypatch.setattr(
        workspace_mod, "validate_workspace", lambda raw: validate_calls.append(raw) or {"ok": True, "path": raw, "error": None}
    )
    monkeypatch.setattr(workspace_mod, "create_workspace", lambda raw: {"ok": True, "path": raw, "error": None})

    p._service.resolve_workspace("/new/ws", create=True)

    assert validate_calls == [], "create=True must never call validate_workspace"


# ---------------------------------------------------------------------------
# resolve_workspace -- return value pass-through ("unchanged")
# ---------------------------------------------------------------------------


def test_resolve_workspace_returns_underlying_dict_unchanged_on_success(
    plan_dir, monkeypatch
):
    stub_result = {"ok": True, "path": "/exact/pass/through", "error": None}
    monkeypatch.setattr(workspace_mod, "validate_workspace", lambda raw: dict(stub_result))

    result = p._service.resolve_workspace("/input", create=False)

    assert result == stub_result


def test_resolve_workspace_returns_underlying_dict_unchanged_on_failure(
    plan_dir, monkeypatch
):
    stub_result = {"ok": False, "path": "", "error": "boundary case: whatever validate_workspace said"}
    monkeypatch.setattr(workspace_mod, "validate_workspace", lambda raw: dict(stub_result))

    result = p._service.resolve_workspace("/input", create=False)

    assert result == stub_result


# ---------------------------------------------------------------------------
# resolve_workspace -- recording on success, never on failure
# ---------------------------------------------------------------------------


def test_resolve_workspace_records_resolved_path_on_ok_true(plan_dir, store, monkeypatch):
    # Input path and the stub's returned (resolved) path deliberately differ,
    # so the assertion is unambiguous about which one gets recorded: the
    # value from the ok=True *result*, not the caller's raw input string.
    monkeypatch.setattr(
        workspace_mod,
        "validate_workspace",
        lambda raw: {"ok": True, "path": "/resolved/canonical/path", "error": None},
    )

    p._service.resolve_workspace("~/raw/unresolved/input", create=False)

    assert store.get_recent_workspaces() == ["/resolved/canonical/path"]


def test_resolve_workspace_does_not_record_on_ok_false(plan_dir, store, monkeypatch):
    monkeypatch.setattr(
        workspace_mod,
        "validate_workspace",
        lambda raw: {"ok": False, "path": "", "error": "not a git repository"},
    )

    p._service.resolve_workspace("/bad/path", create=False)

    assert store.get_recent_workspaces() == []


def test_resolve_workspace_create_true_records_on_ok_true(plan_dir, store, monkeypatch):
    monkeypatch.setattr(
        workspace_mod,
        "create_workspace",
        lambda raw: {"ok": True, "path": "/newly/created/repo", "error": None},
    )

    p._service.resolve_workspace("/new/ws", create=True)

    assert store.get_recent_workspaces() == ["/newly/created/repo"]


def test_resolve_workspace_create_true_does_not_record_on_ok_false(plan_dir, store, monkeypatch):
    monkeypatch.setattr(
        workspace_mod,
        "create_workspace",
        lambda raw: {"ok": False, "path": "/attempted", "error": "path is not empty"},
    )

    p._service.resolve_workspace("/attempted", create=True)

    assert store.get_recent_workspaces() == []


# ---------------------------------------------------------------------------
# resolve_workspace -- real integration (no workspace-module mocking)
# ---------------------------------------------------------------------------


def test_resolve_workspace_valid_repo_ok_true_and_recorded(plan_dir, store, tmp_path):
    repo = _init_git_repo(tmp_path / "real-repo")

    result = p._service.resolve_workspace(str(repo), create=False)

    assert result["ok"] is True
    assert store.get_recent_workspaces() == [result["path"]]


def test_resolve_workspace_invalid_path_ok_false_and_nothing_recorded(plan_dir, store, tmp_path):
    missing = tmp_path / "does-not-exist"

    result = p._service.resolve_workspace(str(missing), create=False)

    assert result["ok"] is False
    assert store.get_recent_workspaces() == []


def test_resolve_workspace_create_true_creates_and_records(plan_dir, store, tmp_path):
    new_dir = tmp_path / "brand-new-workspace"
    assert not new_dir.exists()

    result = p._service.resolve_workspace(str(new_dir), create=True)

    assert result["ok"] is True
    assert new_dir.exists()
    assert (new_dir / ".git").exists()
    assert store.get_recent_workspaces() == [result["path"]]


# ---------------------------------------------------------------------------
# resolve_workspace -- negative / boundary inputs
# ---------------------------------------------------------------------------


def test_resolve_workspace_none_path_is_ok_false_and_not_recorded(plan_dir, store):
    result = p._service.resolve_workspace(None, create=False)

    assert result["ok"] is False
    assert store.get_recent_workspaces() == []


def test_resolve_workspace_empty_string_path_is_ok_false_and_not_recorded(plan_dir, store):
    result = p._service.resolve_workspace("", create=False)

    assert result["ok"] is False
    assert store.get_recent_workspaces() == []


def test_resolve_workspace_traversal_path_is_ok_false_and_not_recorded(plan_dir, store):
    result = p._service.resolve_workspace("/tmp/../evil", create=False)

    assert result["ok"] is False
    assert store.get_recent_workspaces() == []


def test_resolve_workspace_relative_path_is_ok_false_and_not_recorded(plan_dir, store):
    result = p._service.resolve_workspace("relative/path", create=False)

    assert result["ok"] is False
    assert store.get_recent_workspaces() == []


def test_resolve_workspace_default_create_is_false(plan_dir, store, monkeypatch):
    # Boundary: omitting `create` entirely must behave exactly like create=False
    # (validate_workspace only, never create_workspace).
    create_calls = []
    monkeypatch.setattr(workspace_mod, "validate_workspace", lambda raw: {"ok": True, "path": raw, "error": None})
    monkeypatch.setattr(workspace_mod, "create_workspace", lambda raw: create_calls.append(raw) or {"ok": True, "path": raw, "error": None})

    p._service.resolve_workspace("/some/path")

    assert create_calls == [], "omitting create= must default to False (validate, not create)"


# ---------------------------------------------------------------------------
# list_workspaces -- empty / boundary
# ---------------------------------------------------------------------------


def test_list_workspaces_returns_empty_list_when_no_manifests_and_no_recents(plan_dir):
    result = p._service.list_workspaces()

    assert result == []


def test_list_workspaces_return_type_is_list(plan_dir):
    result = p._service.list_workspaces()

    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# list_workspaces -- merging store recents with manifest repo_roots
# ---------------------------------------------------------------------------


def test_list_workspaces_merges_store_recents_with_manifest_repo_roots(
    plan_dir, store, tmp_path
):
    recent_repo = _init_git_repo(tmp_path / "recent-only")
    manifest_repo = _init_git_repo(tmp_path / "manifest-only")

    store.add_recent_workspace(str(recent_repo))
    _write_manifest(plan_dir, "planA", repo_root=str(manifest_repo))

    result = p._service.list_workspaces()

    paths = {entry["path"] for entry in result}
    assert paths == {str(recent_repo), str(manifest_repo)}


def test_list_workspaces_deduplicates_path_present_in_both_sources(
    plan_dir, store, tmp_path
):
    shared_repo = _init_git_repo(tmp_path / "shared")

    store.add_recent_workspace(str(shared_repo))
    _write_manifest(plan_dir, "planB", repo_root=str(shared_repo))

    result = p._service.list_workspaces()

    matching = [entry for entry in result if entry["path"] == str(shared_repo)]
    assert len(matching) == 1, (
        f"a path present in both recents and a manifest must appear exactly once, got {matching!r}"
    )


def test_list_workspaces_preserves_most_recent_first_ordering(plan_dir, store, tmp_path):
    ws1 = _init_git_repo(tmp_path / "ws1")
    ws2 = _init_git_repo(tmp_path / "ws2")
    ws3 = _init_git_repo(tmp_path / "ws3")

    # add_recent_workspace prepends, so recency order is ws3, ws2, ws1.
    store.add_recent_workspace(str(ws1))
    store.add_recent_workspace(str(ws2))
    store.add_recent_workspace(str(ws3))

    result = p._service.list_workspaces()
    ordered_recent_paths = [
        entry["path"] for entry in result
        if entry["path"] in {str(ws1), str(ws2), str(ws3)}
    ]

    assert ordered_recent_paths == [str(ws3), str(ws2), str(ws1)]


def test_list_workspaces_only_manifests_no_recents(plan_dir, tmp_path):
    manifest_repo = _init_git_repo(tmp_path / "only-manifest")
    _write_manifest(plan_dir, "planC", repo_root=str(manifest_repo))

    result = p._service.list_workspaces()

    assert [entry["path"] for entry in result] == [str(manifest_repo)]


def test_list_workspaces_only_recents_no_manifests(plan_dir, store, tmp_path):
    recent_repo = _init_git_repo(tmp_path / "only-recent")
    store.add_recent_workspace(str(recent_repo))

    result = p._service.list_workspaces()

    assert [entry["path"] for entry in result] == [str(recent_repo)]


def test_list_workspaces_manifest_without_repo_root_is_skipped(plan_dir):
    # Boundary: a manifest with no repo_root key at all must not produce a
    # bogus entry (e.g. a None/empty-string path).
    _write_manifest(plan_dir, "planD", repo_root=None)

    result = p._service.list_workspaces()

    assert result == []


# ---------------------------------------------------------------------------
# list_workspaces -- entry shape: {"path": str, "exists": bool, "valid": bool}
# ---------------------------------------------------------------------------


def test_list_workspaces_entry_shape_for_valid_existing_repo(plan_dir, store, tmp_path):
    repo = _init_git_repo(tmp_path / "shape-valid")
    store.add_recent_workspace(str(repo))

    result = p._service.list_workspaces()

    assert len(result) == 1
    entry = result[0]
    assert set(entry.keys()) == {"path", "exists", "valid"}
    assert entry["path"] == str(repo)
    assert entry["exists"] is True
    assert entry["valid"] is True


def test_list_workspaces_deleted_recorded_path_still_listed_as_invalid(
    plan_dir, store, tmp_path
):
    repo = _init_git_repo(tmp_path / "will-be-deleted")
    store.add_recent_workspace(str(repo))

    # Remove the .git directory (still exists on disk, but no longer a repo)
    # is one failure mode; fully removing the directory (gone entirely) is
    # the other. Test the stronger case: the directory itself is gone.
    import shutil
    shutil.rmtree(repo)

    result = p._service.list_workspaces()

    assert len(result) == 1, (
        "a recorded-but-deleted workspace must still be LISTED, never silently dropped"
    )
    entry = result[0]
    assert entry["path"] == str(repo)
    assert entry["exists"] is False
    assert entry["valid"] is False


def test_list_workspaces_recorded_path_with_git_dir_removed_is_invalid_but_exists(
    plan_dir, store, tmp_path
):
    repo = _init_git_repo(tmp_path / "git-dir-removed")
    store.add_recent_workspace(str(repo))

    import shutil
    shutil.rmtree(repo / ".git")

    result = p._service.list_workspaces()

    assert len(result) == 1
    entry = result[0]
    assert entry["path"] == str(repo)
    assert entry["exists"] is True
    assert entry["valid"] is False
