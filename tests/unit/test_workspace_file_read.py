"""Tests for ``PipelineService.read_workspace_file`` (pipeline/service.py).

Story: add exactly one new method to ``PipelineService``, placed near the
existing ``get_active_workspace`` / ``resolve_workspace`` accessors:

    read_workspace_file(self, relative_path: str, *, max_bytes: int = 200_000) -> dict

Behavior contract (every branch returns a dict, never raises):

- no active workspace            -> ``{'ok': False, 'error': 'no active workspace'}``
- path rejected by
  ``pipeline.workspace_fs.resolve_within_workspace``
  (``WorkspaceSecurityError`` / ``ValueError``)
                                 -> ``{'ok': False, 'error': 'invalid path'}``
  -- a fixed generic message; the resolved or rejected path must NEVER be
  echoed back to the caller.
- missing / non-regular-file     -> ``{'ok': False, 'error': 'not found'}``
- ``os.path.getsize`` > max_bytes-> ``{'ok': False, 'error': 'file too large'}``
  -- checked BEFORE any content is read into memory.
- invalid UTF-8 payload          -> ``{'ok': False, 'error': 'not a text file'}``
- success                        -> ``{'ok': True, 'path': <relative_path>,
                                     'content': <utf-8 text>}``

The active workspace is stubbed to a ``tmp_path`` directory both at the
instance level (``service.get_active_workspace``) and at the module seam
(``pipeline.service._store.get_active_workspace``), so the tests pass whether
the implementation dispatches through ``self.get_active_workspace()`` (as the
brief mandates) or reads the store directly. Path rejection exercises the REAL
``resolve_within_workspace``; only the workspace selection is faked.

Every test here fails with ``AttributeError`` (method does not exist yet) or
an assertion until the implementation lands -- that is the correct RED state.
"""

from __future__ import annotations

import inspect
import os
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

    Patches both seams the implementation could plausibly read:
    the instance method (the brief's mandated ``self.get_active_workspace()``
    dispatch) and the module-level ``_store`` free variable.
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
    """Service whose active workspace is a real tmp directory."""
    _stub_active_workspace(monkeypatch, service, tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Existence, signature, and placement (RED until the method exists)
# ---------------------------------------------------------------------------


def test_read_workspace_file_method_exists():
    assert hasattr(PipelineService, "read_workspace_file")


def test_read_workspace_file_signature():
    sig = inspect.signature(PipelineService.read_workspace_file)
    assert list(sig.parameters) == ["self", "relative_path", "max_bytes"]
    assert sig.parameters["relative_path"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    # max_bytes must be keyword-only with the 200_000 default.
    assert sig.parameters["max_bytes"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["max_bytes"].default == 200_000
    assert sig.return_annotation is dict or sig.return_annotation == "dict"


def test_read_workspace_file_placed_near_workspace_accessors():
    """The new method must sit next to get_active_workspace/resolve_workspace."""
    source = inspect.getsource(PipelineService)
    lines = source.splitlines()
    positions = {}
    for index, line in enumerate(lines):
        match = re.match(r"\s*def (\w+)\(", line)
        if match:
            positions.setdefault(match.group(1), index)
    for anchor in ("get_active_workspace", "resolve_workspace"):
        assert anchor in positions, f"expected existing accessor {anchor!r}"
    assert "read_workspace_file" in positions
    nearest = min(
        abs(positions["read_workspace_file"] - positions[anchor])
        for anchor in ("get_active_workspace", "resolve_workspace")
    )
    assert nearest <= 120, "read_workspace_file is not placed near the accessors"


# ---------------------------------------------------------------------------
# No active workspace
# ---------------------------------------------------------------------------


def test_no_active_workspace_returns_error(monkeypatch, service):
    _stub_active_workspace(monkeypatch, service, None)
    result = service.read_workspace_file("notes.txt")
    assert result == {"ok": False, "error": "no active workspace"}


def test_no_active_workspace_short_circuits_before_path_checks(
    monkeypatch, service
):
    """Even a traversal attempt reports the workspace error, not 'invalid path'."""
    _stub_active_workspace(monkeypatch, service, None)
    result = service.read_workspace_file("../secret")
    assert result == {"ok": False, "error": "no active workspace"}


# ---------------------------------------------------------------------------
# Path rejection (real resolve_within_workspace against a tmp workspace)
# ---------------------------------------------------------------------------


def test_traversal_path_rejected(active_workspace, service):
    result = service.read_workspace_file("../secret")
    assert result == {"ok": False, "error": "invalid path"}


def test_invalid_path_error_never_echoes_the_path(active_workspace, service):
    """The fixed generic message must not contain the rejected or resolved path."""
    result = service.read_workspace_file("../secret")
    serialized = repr(result)
    assert "../secret" not in serialized
    assert str(active_workspace) not in serialized
    assert "secret" not in serialized


def test_absolute_path_rejected(active_workspace, service):
    result = service.read_workspace_file("/etc/passwd")
    assert result == {"ok": False, "error": "invalid path"}


def test_encoded_traversal_rejected(active_workspace, service):
    result = service.read_workspace_file("%2e%2e%2fsecret")
    assert result == {"ok": False, "error": "invalid path"}


def test_empty_relative_path_rejected(active_workspace, service):
    result = service.read_workspace_file("")
    assert result == {"ok": False, "error": "invalid path"}


def test_non_string_path_rejected(active_workspace, service):
    """ValueError generally is caught, so junk types map to 'invalid path'."""
    for bad in (None, 123, b"notes.txt"):
        result = service.read_workspace_file(bad)
        assert result == {"ok": False, "error": "invalid path"}


@pytest.mark.skipif(os.name != "posix", reason="symlink escape needs POSIX")
def test_symlink_escape_rejected(active_workspace, service, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    secret = outside / "secret.txt"
    secret.write_text("top secret", encoding="utf-8")
    link = active_workspace / "link.txt"
    link.symlink_to(secret)
    result = service.read_workspace_file("link.txt")
    assert result == {"ok": False, "error": "invalid path"}


# ---------------------------------------------------------------------------
# Not found / not a regular file
# ---------------------------------------------------------------------------


def test_missing_file_returns_not_found(active_workspace, service):
    result = service.read_workspace_file("does_not_exist.txt")
    assert result == {"ok": False, "error": "not found"}


def test_directory_path_returns_not_found(active_workspace, service):
    (active_workspace / "subdir").mkdir()
    result = service.read_workspace_file("subdir")
    assert result == {"ok": False, "error": "not found"}


def test_not_found_error_does_not_leak_resolved_path(active_workspace, service):
    result = service.read_workspace_file("does_not_exist.txt")
    assert str(active_workspace) not in repr(result)


# ---------------------------------------------------------------------------
# Size guard (max_bytes supplied by the test, not the 200_000 default)
# ---------------------------------------------------------------------------


def test_file_larger_than_max_bytes_rejected(active_workspace, service):
    (active_workspace / "big.txt").write_text("x" * 64, encoding="utf-8")
    result = service.read_workspace_file("big.txt", max_bytes=8)
    assert result == {"ok": False, "error": "file too large"}


def test_size_check_happens_before_decoding(active_workspace, service):
    """An oversized NON-UTF-8 file must report size, proving no read-then-check."""
    (active_workspace / "blob.bin").write_bytes(b"\xff\xfe" * 64)
    result = service.read_workspace_file("blob.bin", max_bytes=8)
    assert result == {"ok": False, "error": "file too large"}


def test_file_exactly_max_bytes_is_allowed(active_workspace, service):
    """Boundary: size == max_bytes does not 'exceed' it."""
    (active_workspace / "exact.txt").write_text("12345", encoding="utf-8")
    result = service.read_workspace_file("exact.txt", max_bytes=5)
    assert result == {"ok": True, "path": "exact.txt", "content": "12345"}


def test_empty_file_is_within_max_bytes(active_workspace, service):
    """Boundary: zero-byte file reads back as an empty string."""
    (active_workspace / "empty.txt").write_text("", encoding="utf-8")
    result = service.read_workspace_file("empty.txt", max_bytes=1)
    assert result == {"ok": True, "path": "empty.txt", "content": ""}


# ---------------------------------------------------------------------------
# Decoding guard
# ---------------------------------------------------------------------------


def test_invalid_utf8_returns_not_a_text_file(active_workspace, service):
    (active_workspace / "binary.bin").write_bytes(b"\xff\xfe\x00\x81bad")
    result = service.read_workspace_file("binary.bin")
    assert result == {"ok": False, "error": "not a text file"}


def test_lone_surrogate_utf8_returns_not_a_text_file(active_workspace, service):
    (active_workspace / "bad.bin").write_bytes(b"\xed\xa0\x80")
    result = service.read_workspace_file("bad.bin")
    assert result == {"ok": False, "error": "not a text file"}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_valid_text_file_round_trips(active_workspace, service):
    (active_workspace / "notes.txt").write_text("hello world", encoding="utf-8")
    result = service.read_workspace_file("notes.txt")
    assert result == {"ok": True, "path": "notes.txt", "content": "hello world"}


def test_valid_file_content_is_str_and_preserves_utf8(active_workspace, service):
    (active_workspace / "uni.txt").write_text("héllo wörld\n", encoding="utf-8")
    result = service.read_workspace_file("uni.txt")
    assert result["ok"] is True
    assert isinstance(result["content"], str)
    assert result["content"] == "héllo wörld\n"


def test_success_path_key_is_the_relative_path_not_resolved(
    active_workspace, service
):
    (active_workspace / "nested").mkdir()
    (active_workspace / "nested" / "a.txt").write_text("A", encoding="utf-8")
    result = service.read_workspace_file("nested/a.txt")
    assert result["ok"] is True
    assert result["path"] == "nested/a.txt"
    assert str(active_workspace) not in repr(result["path"])


def test_success_honors_explicit_max_bytes(active_workspace, service):
    (active_workspace / "ok.txt").write_text("abc", encoding="utf-8")
    result = service.read_workspace_file("ok.txt", max_bytes=3)
    assert result == {"ok": True, "path": "ok.txt", "content": "abc"}


# ---------------------------------------------------------------------------
# Route level: GET /api/workspace/file (app/dashboard.py)
# ---------------------------------------------------------------------------
#
# Follows the EXACT pattern of set_workspace_route: on a not-ok result raise
# HTTPException(status_code=400, detail=result.get('error', ...)); otherwise
# return the result dict. The conftest autouse fixture injects the correct
# X-Pipeline-Api-Key header into every TestClient, so these requests carry
# valid auth; the service is mocked so only the route wiring is under test.


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app import dashboard

    return TestClient(dashboard.app)


def test_route_exists_and_maps_path_query_to_service(client, monkeypatch):
    """GET /api/workspace/file?path=... delegates to _service.read_workspace_file."""
    from app import dashboard

    captured = {}

    def fake_read(relative_path, **kwargs):
        captured["path"] = relative_path
        return {"ok": True, "path": relative_path, "content": "hi"}

    monkeypatch.setattr(dashboard._service, "read_workspace_file", fake_read)
    response = client.get("/api/workspace/file", params={"path": "notes.txt"})
    assert response.status_code == 200
    assert captured["path"] == "notes.txt"
    assert response.json() == {"ok": True, "path": "notes.txt", "content": "hi"}


def test_route_returns_400_with_service_error(client, monkeypatch):
    """ok False -> HTTP 400 whose detail is the service's error string."""
    from app import dashboard

    monkeypatch.setattr(
        dashboard._service,
        "read_workspace_file",
        lambda *a, **k: {"ok": False, "error": "invalid path"},
    )
    response = client.get("/api/workspace/file", params={"path": "../secret"})
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid path"


def test_route_400_detail_is_verbatim_service_error(client, monkeypatch):
    from app import dashboard

    monkeypatch.setattr(
        dashboard._service,
        "read_workspace_file",
        lambda *a, **k: {"ok": False, "error": "file too large"},
    )
    response = client.get("/api/workspace/file", params={"path": "big.txt"})
    assert response.status_code == 400
    assert response.json()["detail"] == "file too large"


def test_route_requires_path_query_param(client):
    """Missing required 'path' query parameter is a validation error, not 200."""
    response = client.get("/api/workspace/file")
    assert response.status_code == 422


def test_route_rejects_missing_auth_header(monkeypatch):
    """The global require_api_key dependency covers the new route too."""
    from fastapi.testclient import TestClient

    from app import dashboard

    monkeypatch.setattr(
        dashboard._service,
        "read_workspace_file",
        lambda *a, **k: {"ok": True, "path": "x", "content": ""},
    )
    with TestClient(dashboard.app, headers={"X-Pipeline-Api-Key": "wrong-key"}) as bad:
        response = bad.get("/api/workspace/file", params={"path": "x"})
    assert response.status_code == 401