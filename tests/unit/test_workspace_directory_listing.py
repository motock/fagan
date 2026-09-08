"""Tests for ``PipelineService.list_workspace_directory`` (pipeline/service.py)
and its route ``GET /api/workspace/files`` (app/dashboard.py).

Story: add exactly one new method to ``PipelineService``, placed near the
existing workspace accessors / ``read_workspace_file``:

    list_workspace_directory(self, relative_path: str = '') -> dict

Behavior contract (mirrors ``read_workspace_file``'s error-shape conventions;
every branch returns a dict, never raises):

- no active workspace            -> ``{'ok': False, 'error': 'no active workspace'}``
- path rejected by
  ``pipeline.workspace_fs.resolve_within_workspace``
  (``WorkspaceSecurityError`` / ``ValueError``)
                                 -> ``{'ok': False, 'error': 'invalid path'}``
  -- a fixed generic message; the resolved or rejected path must NEVER be
  echoed back to the caller.
- path does not exist            -> ``{'ok': False, 'error': 'not found'}``
- path exists but is not a
  directory                      -> ``{'ok': False, 'error': 'not a directory'}``
- success                        -> ``{'ok': True, 'path': <relative_path>,
                                     'entries': [{'name': <basename>,
                                                  'type': 'file'|'dir'},
                                                 ...]}``
  sorted by name ascending.  ONLY immediate children are listed: the
  implementation must NOT recurse into subdirectories (unbounded recursion is
  a resource-exhaustion risk), so a nested sub-subdirectory's contents never
  appear in the result.

Note on the empty path: ``resolve_within_workspace`` rejects ``''`` outright,
but the brief mandates that ``relative_path=''`` lists the workspace ROOT, so
the implementation must map the empty path onto the root itself (e.g. treat
``''`` as ``'.'``) rather than passing it through unchanged.

Route: ``GET /api/workspace/files`` with an OPTIONAL ``path`` query parameter
(default ``''``).  On a not-ok service result it raises HTTP 400 whose detail
is the service's error string, exactly like ``GET /api/workspace/file``.

The active workspace is stubbed to a ``tmp_path`` directory both at the
instance level (``service.get_active_workspace``) and at the module seam
(``pipeline.service._store.get_active_workspace``), so the tests pass whether
the implementation dispatches through ``self.get_active_workspace()`` (as
``read_workspace_file`` does) or reads the store directly.  Path rejection
exercises the REAL ``resolve_within_workspace``; only the workspace selection
is faked.

Every test here fails with ``AttributeError`` (method does not exist yet) or
an assertion until the implementation lands -- that is the correct RED state.
"""

from __future__ import annotations

import inspect
import re
import types

import pytest

import pipeline.service as service_mod
from pipeline.service import PipelineService

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def service():
    """A bare PipelineService (the project's established construction)."""
    return PipelineService()


def _stub_active_workspace(monkeypatch, service, path):
    """Force ``get_active_workspace`` to report *path* (str) or None.

    Patches both seams the implementation could plausibly read: the instance
    method (the ``self.get_active_workspace()`` dispatch that
    ``read_workspace_file`` uses) and the module-level ``_store`` free
    variable.
    """
    value = None if path is None else str(path)
    monkeypatch.setattr(service, "get_active_workspace", lambda: value)
    monkeypatch.setattr(
        service_mod,
        "_store",
        types.SimpleNamespace(get_active_workspace=lambda: value),
        raising=False,
    )
    return value


@pytest.fixture
def active_workspace(tmp_path, monkeypatch, service):
    """A PipelineService whose active workspace is a real tmp_path directory."""
    _stub_active_workspace(monkeypatch, service, tmp_path)
    return tmp_path


def _names(result):
    """The entry names of a successful listing, in returned order."""
    return [entry["name"] for entry in result["entries"]]


# ---------------------------------------------------------------------------
# Existence, signature, and placement (RED until the method exists)
# ---------------------------------------------------------------------------


def test_list_workspace_directory_method_exists():
    assert hasattr(PipelineService, "list_workspace_directory")


def test_list_workspace_directory_signature():
    sig = inspect.signature(PipelineService.list_workspace_directory)
    assert list(sig.parameters) == ["self", "relative_path"]
    assert sig.parameters["relative_path"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    # relative_path must be a plain parameter with the '' default -- NOT
    # keyword-only, and not required.
    assert sig.parameters["relative_path"].default == ""
    assert sig.return_annotation is dict or sig.return_annotation == "dict"


def test_list_workspace_directory_placed_near_workspace_accessors():
    """The new method must sit next to the workspace accessors/read helper."""
    source = inspect.getsource(PipelineService)
    lines = source.splitlines()
    positions = {}
    for index, line in enumerate(lines):
        match = re.match(r"\s*def (\w+)\(", line)
        if match:
            positions.setdefault(match.group(1), index)
    for anchor in ("get_active_workspace", "resolve_workspace"):
        assert anchor in positions, f"expected existing accessor {anchor!r}"
    assert "list_workspace_directory" in positions
    nearest = min(
        abs(positions["list_workspace_directory"] - positions[anchor])
        for anchor in ("get_active_workspace", "resolve_workspace")
    )
    assert nearest <= 120, "list_workspace_directory is not placed near the accessors"


# ---------------------------------------------------------------------------
# No active workspace
# ---------------------------------------------------------------------------


def test_no_active_workspace_returns_error(monkeypatch, service):
    _stub_active_workspace(monkeypatch, service, None)
    result = service.list_workspace_directory("sub")
    assert result == {"ok": False, "error": "no active workspace"}


def test_no_active_workspace_default_path(monkeypatch, service):
    """Calling with no argument at all still reports the workspace error."""
    _stub_active_workspace(monkeypatch, service, None)
    result = service.list_workspace_directory()
    assert result == {"ok": False, "error": "no active workspace"}


def test_no_active_workspace_short_circuits_before_path_checks(
    monkeypatch, service
):
    """Even a traversal attempt reports the workspace error, not 'invalid path'."""
    _stub_active_workspace(monkeypatch, service, None)
    result = service.list_workspace_directory("../secret")
    assert result == {"ok": False, "error": "no active workspace"}


# ---------------------------------------------------------------------------
# Workspace root listing (relative_path == '')
# ---------------------------------------------------------------------------


def test_root_listing_with_empty_path_lists_all_top_level_entries(
    active_workspace, service
):
    """path='' addresses the workspace ROOT and lists its immediate children."""
    ws = active_workspace
    (ws / "alpha.txt").write_text("a")
    (ws / "beta.txt").write_text("b")
    (ws / "zdir").mkdir()
    (ws / "adir").mkdir()

    result = service.list_workspace_directory("")

    assert result["ok"] is True
    assert result["path"] == ""
    assert _names(result) == ["adir", "alpha.txt", "beta.txt", "zdir"]
    types_by_name = {e["name"]: e["type"] for e in result["entries"]}
    assert types_by_name == {
        "alpha.txt": "file",
        "beta.txt": "file",
        "zdir": "dir",
        "adir": "dir",
    }


def test_root_listing_via_no_argument(active_workspace, service):
    """The default parameter value ('') also addresses the workspace root."""
    ws = active_workspace
    (ws / "only.txt").write_text("x")

    result = service.list_workspace_directory()

    assert result == {
        "ok": True,
        "path": "",
        "entries": [{"name": "only.txt", "type": "file"}],
    }


def test_root_listing_with_dot_path(active_workspace, service):
    """'.' is the resolver's canonical spelling for the root; it must work."""
    ws = active_workspace
    (ws / "f.txt").write_text("x")
    (ws / "d").mkdir()

    result = service.list_workspace_directory(".")

    assert result["ok"] is True
    assert _names(result) == ["d", "f.txt"]
    types_by_name = {e["name"]: e["type"] for e in result["entries"]}
    assert types_by_name == {"f.txt": "file", "d": "dir"}


def test_empty_workspace_root_returns_empty_entries(active_workspace, service):
    """An empty directory is a successful listing with zero entries."""
    result = service.list_workspace_directory("")
    assert result == {"ok": True, "path": "", "entries": []}


def test_root_listing_does_not_recurse(active_workspace, service):
    """Only immediate children: a nested sub-subdirectory's contents are absent."""
    ws = active_workspace
    (ws / "top.txt").write_text("t")
    (ws / "sub").mkdir()
    (ws / "sub" / "mid.txt").write_text("m")
    (ws / "sub" / "deeper").mkdir()
    (ws / "sub" / "deeper" / "deep.txt").write_text("d")

    result = service.list_workspace_directory("")

    assert result["ok"] is True
    # Exactly the two immediate children, nothing from sub/ or sub/deeper/.
    assert _names(result) == ["sub", "top.txt"]
    assert len(result["entries"]) == 2
    types_by_name = {e["name"]: e["type"] for e in result["entries"]}
    assert types_by_name == {"sub": "dir", "top.txt": "file"}
    all_names = {e["name"] for e in result["entries"]}
    assert "mid.txt" not in all_names
    assert "deeper" not in all_names
    assert "deep.txt" not in all_names


# ---------------------------------------------------------------------------
# Path rejection (real resolve_within_workspace against a tmp workspace)
# ---------------------------------------------------------------------------


def test_traversal_path_rejected(active_workspace, service):
    result = service.list_workspace_directory("../secret")
    assert result == {"ok": False, "error": "invalid path"}


def test_encoded_traversal_rejected(active_workspace, service):
    """Percent-encoded traversal must not slip past the raw-spelling check."""
    result = service.list_workspace_directory("%2e%2e%2fsecret")
    assert result == {"ok": False, "error": "invalid path"}


def test_invalid_path_error_never_echoes_the_path(active_workspace, service):
    """The rejection message is fixed and generic; the path is not echoed."""
    result = service.list_workspace_directory("../secret")
    assert result["error"] == "invalid path"
    assert "../secret" not in str(result)


def test_non_string_path_rejected(active_workspace, service):
    """A non-str relative_path is a resolver rejection, not a crash."""
    result = service.list_workspace_directory(None)  # type: ignore[arg-type]
    assert result == {"ok": False, "error": "invalid path"}


def test_absolute_path_rejected(active_workspace, service):
    result = service.list_workspace_directory("/etc/passwd")
    assert result == {"ok": False, "error": "invalid path"}


# ---------------------------------------------------------------------------
# Not found / not a directory
# ---------------------------------------------------------------------------


def test_nonexistent_subdirectory_returns_not_found(active_workspace, service):
    result = service.list_workspace_directory("does-not-exist")
    assert result == {"ok": False, "error": "not found"}


def test_nonexistent_nested_path_returns_not_found(active_workspace, service):
    result = service.list_workspace_directory("real-dir/nope")
    assert result == {"ok": False, "error": "not found"}


def test_not_found_error_does_not_leak_resolved_path(active_workspace, service):
    result = service.list_workspace_directory("does-not-exist")
    assert result["error"] == "not found"
    assert str(active_workspace) not in str(result)


def test_file_path_returns_not_a_directory(active_workspace, service):
    """A path pointing at an existing FILE is 'not a directory', not 'not found'."""
    ws = active_workspace
    (ws / "plain.txt").write_text("hello")

    result = service.list_workspace_directory("plain.txt")

    assert result == {"ok": False, "error": "not a directory"}


def test_not_a_directory_error_does_not_leak_resolved_path(
    active_workspace, service
):
    ws = active_workspace
    (ws / "plain.txt").write_text("hello")

    result = service.list_workspace_directory("plain.txt")

    assert result["error"] == "not a directory"
    assert str(ws) not in str(result)


# ---------------------------------------------------------------------------
# Subdirectory listing: sorting, types, no recursion
# ---------------------------------------------------------------------------


def test_subdirectory_entries_sorted_by_name_with_types(active_workspace, service):
    """Entries are sorted by name ascending and typed 'file' / 'dir'."""
    ws = active_workspace
    sub = ws / "sub"
    sub.mkdir()
    (sub / "zeta.txt").write_text("z")
    (sub / "alpha.txt").write_text("a")
    (sub / "mdir").mkdir()
    (sub / "adir").mkdir()
    (sub / "mid.txt").write_text("m")

    result = service.list_workspace_directory("sub")

    assert result == {
        "ok": True,
        "path": "sub",
        "entries": [
            {"name": "adir", "type": "dir"},
            {"name": "alpha.txt", "type": "file"},
            {"name": "mdir", "type": "dir"},
            {"name": "mid.txt", "type": "file"},
            {"name": "zeta.txt", "type": "file"},
        ],
    }


def test_subdirectory_listing_does_not_recurse(active_workspace, service):
    """A subdirectory's own subdirectories are listed as 'dir', not descended."""
    ws = active_workspace
    sub = ws / "sub"
    sub.mkdir()
    (sub / "keep.txt").write_text("k")
    nested = sub / "nested"
    nested.mkdir()
    (nested / "inner.txt").write_text("i")
    (nested / "inner_dir").mkdir()
    (nested / "inner_dir" / "leaf.txt").write_text("l")

    result = service.list_workspace_directory("sub")

    assert result["ok"] is True
    assert _names(result) == ["keep.txt", "nested"]
    types_by_name = {e["name"]: e["type"] for e in result["entries"]}
    assert types_by_name == {"keep.txt": "file", "nested": "dir"}
    all_names = {e["name"] for e in result["entries"]}
    assert "inner.txt" not in all_names
    assert "inner_dir" not in all_names
    assert "leaf.txt" not in all_names
    assert len(result["entries"]) == 2


def test_empty_subdirectory_returns_empty_entries(active_workspace, service):
    ws = active_workspace
    (ws / "void").mkdir()

    result = service.list_workspace_directory("void")

    assert result == {"ok": True, "path": "void", "entries": []}


def test_success_path_key_is_the_relative_path_not_resolved(
    active_workspace, service
):
    """The echoed 'path' is the caller's relative spelling, never absolute."""
    ws = active_workspace
    (ws / "sub").mkdir()

    result = service.list_workspace_directory("sub")

    assert result["ok"] is True
    assert result["path"] == "sub"
    assert str(ws) not in str(result["path"])


def test_entry_dicts_have_exactly_name_and_type_keys(active_workspace, service):
    """Each entry carries exactly 'name' and 'type' -- no extra leakage."""
    ws = active_workspace
    (ws / "f.txt").write_text("x")
    (ws / "d").mkdir()

    result = service.list_workspace_directory("")

    assert result["ok"] is True
    for entry in result["entries"]:
        assert set(entry.keys()) == {"name", "type"}


# ---------------------------------------------------------------------------
# Route level: GET /api/workspace/files (app/dashboard.py)
# ---------------------------------------------------------------------------
#
# Follows the EXACT pattern of read_workspace_file_route: on a not-ok result
# raise HTTPException(status_code=400, detail=result.get('error', ...));
# otherwise return the result dict.  The conftest autouse fixture injects the
# correct X-Pipeline-Api-Key header into every TestClient, so these requests
# carry valid auth; the service is mocked so only the route wiring is under
# test.


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app import dashboard

    return TestClient(dashboard.app)


def test_route_exists_and_maps_path_query_to_service(client, monkeypatch):
    """GET /api/workspace/files?path=... delegates to _service.list_workspace_directory."""
    from app import dashboard

    captured = {}

    def fake_list(relative_path="", **kwargs):
        captured["path"] = relative_path
        return {"ok": True, "path": relative_path, "entries": []}

    monkeypatch.setattr(
        dashboard._service, "list_workspace_directory", fake_list
    )
    response = client.get("/api/workspace/files", params={"path": "sub"})
    assert response.status_code == 200
    assert captured["path"] == "sub"
    assert response.json() == {"ok": True, "path": "sub", "entries": []}


def test_route_path_query_is_optional_and_defaults_to_empty(client, monkeypatch):
    """No query parameter at all -> the service is called with '' (the root)."""
    from app import dashboard

    captured = {}

    def fake_list(relative_path="", **kwargs):
        captured["path"] = relative_path
        return {"ok": True, "path": relative_path, "entries": []}

    monkeypatch.setattr(
        dashboard._service, "list_workspace_directory", fake_list
    )
    response = client.get("/api/workspace/files")
    assert response.status_code == 200
    assert captured["path"] == ""
    assert response.json() == {"ok": True, "path": "", "entries": []}


def test_route_empty_path_query_reaches_service_as_empty_string(
    client, monkeypatch
):
    """An explicitly empty ?path= is forwarded as '' (falsy-or-'' is fine)."""
    from app import dashboard

    captured = {}

    def fake_list(relative_path="", **kwargs):
        captured["path"] = relative_path
        return {"ok": True, "path": relative_path, "entries": []}

    monkeypatch.setattr(
        dashboard._service, "list_workspace_directory", fake_list
    )
    response = client.get("/api/workspace/files", params={"path": ""})
    assert response.status_code == 200
    assert captured["path"] == ""


def test_route_returns_400_with_service_error(client, monkeypatch):
    """ok False -> HTTP 400 whose detail is the service's error string."""
    from app import dashboard

    monkeypatch.setattr(
        dashboard._service,
        "list_workspace_directory",
        lambda *a, **k: {"ok": False, "error": "invalid path"},
    )
    response = client.get("/api/workspace/files", params={"path": "../secret"})
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid path"


def test_route_400_detail_is_verbatim_service_error(client, monkeypatch):
    from app import dashboard

    for error in ("not found", "not a directory", "no active workspace"):
        def fake_list(*a, _error=error, **k):
            return {"ok": False, "error": _error}

        monkeypatch.setattr(
            dashboard._service,
            "list_workspace_directory",
            fake_list,
        )
        response = client.get("/api/workspace/files", params={"path": "x"})
        assert response.status_code == 400
        assert response.json()["detail"] == error


def test_route_rejects_missing_auth_header(monkeypatch):
    """The global require_api_key dependency covers the new route too."""
    from fastapi.testclient import TestClient

    from app import dashboard

    monkeypatch.setattr(
        dashboard._service,
        "list_workspace_directory",
        lambda *a, **k: {"ok": True, "path": "", "entries": []},
    )
    with TestClient(dashboard.app, headers={"X-Pipeline-Api-Key": "wrong-key"}) as bad:
        response = bad.get("/api/workspace/files")
    assert response.status_code == 401