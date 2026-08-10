"""Tests for the W1a migration of ``set_story_status`` onto ``PipelineService``.

These tests verify the structural extraction of ``set_story_status`` into a
transport-agnostic ``PipelineService`` method with a module-level ``@mcp.tool()``
function that delegates to it via the ``_service`` singleton. They mirror the
established pattern in ``test_pipeline_service_seam.py`` (the W1a-01 pilot for
``pause_plan`` / ``resume_plan``).

The implementation does not exist yet, so this suite is expected to be RED
(failing on import/attribute errors) until the migration is performed.
"""

import fcntl
import inspect
import json
import os
import re
import textwrap

import pytest

import pipeline.server as p

# ---------------------------------------------------------------------------
# Shared fixtures / helpers (mirrors test_pipeline_mcp_server.py conventions).
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    return d


def _write_manifest(plan_dir, name, stories):
    (plan_dir / f"{name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories})
    )


def _read_manifest(plan_dir, name):
    return json.loads((plan_dir / f"{name}.manifest.json").read_text())


def _executable_body(src: str) -> list[str]:
    """Return the executable statement lines of a function body.

    Drops decorator lines, the ``def`` line, and any docstring block.
    """
    lines = src.splitlines()
    while lines and (lines[0].lstrip().startswith("@") or not lines[0].strip()):
        lines.pop(0)
    while lines and not lines[0].lstrip().startswith("def "):
        lines.pop(0)
    if lines:
        lines.pop(0)  # the def line itself

    body = textwrap.dedent("\n".join(lines)).splitlines()
    stripped = [ln for ln in body if ln.strip()]
    if stripped and stripped[0].lstrip().startswith(('"""', "'''")):
        quote = stripped[0].lstrip()[:3]
        if stripped[0].count(quote) >= 2 and len(stripped[0].lstrip()) > 3:
            stripped = stripped[1:]
        else:
            idx = 0
            for i, ln in enumerate(stripped):
                if quote in ln and i > 0:
                    idx = i
                    break
                if ln.lstrip().count(quote) >= 2 and i == 0:
                    idx = 0
                    break
            stripped = stripped[idx + 1:]

    return [ln.strip() for ln in stripped if ln.strip()]


def _mcp_tool_names():
    return {t.name for t in p.mcp._tool_manager.list_tools()}


# ---------------------------------------------------------------------------
# C1: PipelineService defines set_story_status as a method with self + params.
# ---------------------------------------------------------------------------


def test_pipeline_service_has_set_story_status_method():
    assert hasattr(p.PipelineService, "set_story_status"), (
        "PipelineService must define a set_story_status method"
    )
    assert callable(p.PipelineService.set_story_status)


def test_pipeline_service_set_story_status_takes_self():
    sig = inspect.signature(p.PipelineService.set_story_status)
    params = list(sig.parameters)
    assert params[0] == "self", (
        f"first parameter must be 'self', got {params[0]!r}"
    )


def test_pipeline_service_set_story_status_param_names():
    sig = inspect.signature(p.PipelineService.set_story_status)
    assert list(sig.parameters) == ["self", "plan_name", "story_key", "status"], (
        f"method params must be self,plan_name,story_key,status; got {list(sig.parameters)}"
    )


def test_pipeline_service_set_story_status_type_hints():
    sig = inspect.signature(p.PipelineService.set_story_status)
    assert sig.parameters["plan_name"].annotation is str
    assert sig.parameters["story_key"].annotation is str
    assert sig.parameters["status"].annotation is str
    assert (
        sig.return_annotation == dict[str, p.Any]
        or sig.return_annotation == "dict[str, Any]"
    ), f"return annotation must be dict[str, Any], got {sig.return_annotation!r}"


# ---------------------------------------------------------------------------
# C2 / C3: module-level @mcp.tool() function still exists & delegates.
# ---------------------------------------------------------------------------


def test_module_set_story_status_still_exists():
    assert hasattr(p, "set_story_status")
    assert callable(p.set_story_status)


def test_set_story_status_definition_count_is_two():
    # C3: exactly 2 definitions (method + tool function).
    src = inspect.getsource(p)
    assert src.count("def set_story_status") == 2, (
        "expected exactly 2 'def set_story_status' (method + tool), got "
        f"{src.count('def set_story_status')}"
    )


def test_module_set_story_status_signature_unchanged():
    sig = inspect.signature(p.set_story_status)
    params = list(sig.parameters)
    assert params == ["plan_name", "story_key", "status"], (
        f"module tool params must be plan_name,story_key,status; got {params}"
    )
    assert sig.parameters["plan_name"].annotation is str
    assert sig.parameters["story_key"].annotation is str
    assert sig.parameters["status"].annotation is str
    assert (
        sig.return_annotation == dict[str, p.Any]
        or sig.return_annotation == "dict[str, Any]"
    ), f"return annotation must be dict[str, Any], got {sig.return_annotation!r}"


def test_module_set_story_status_body_is_single_delegation():
    src = inspect.getsource(p.set_story_status)
    body = _executable_body(src)
    assert body == ["return _service.set_story_status(plan_name, story_key, status)"], (
        f"set_story_status body must be exactly one delegation, got: {body!r}"
    )


def test_module_set_story_status_docstring_preserved():
    doc = p.set_story_status.__doc__
    assert doc is not None, "set_story_status docstring must be preserved on the tool function"
    assert "Transition a story to an explicit status" in doc
    assert "Acquires _plan_lock" in doc


# ---------------------------------------------------------------------------
# C4: tool still MCP-registered (decorator stayed on module-level function).
# ---------------------------------------------------------------------------


def test_set_story_status_is_mcp_registered():
    assert "set_story_status" in _mcp_tool_names(), (
        "set_story_status must remain MCP-registered (decorator on module function)"
    )


# ---------------------------------------------------------------------------
# C5 / R1: no self. access to module globals inside the new method.
# ---------------------------------------------------------------------------


def test_set_story_status_method_has_no_self_global_access():
    src = inspect.getsource(p.PipelineService.set_story_status)
    body_lines = [ln for ln in src.splitlines() if "self." in ln]
    assert body_lines == [], (
        f"set_story_status method must not access anything via self., found: {body_lines!r}"
    )


def test_pipeline_service_class_holds_no_state_attributes():
    # R1: the class must not store copies of module globals on instances.
    svc = p.PipelineService()
    instance_attrs = vars(svc)
    assert instance_attrs == {}, (
        f"PipelineService instances must hold no state, found: {instance_attrs!r}"
    )


def test_pipeline_service_has_no_init_with_state():
    # R1: __init__ must not exist or must take nothing but self and set nothing.
    if hasattr(p.PipelineService, "__init__") and "__init__" in p.PipelineService.__dict__:
        sig = inspect.signature(p.PipelineService.__init__)
        assert list(sig.parameters) == ["self"], (
            f"PipelineService.__init__ must take only self, got {list(sig.parameters)}"
        )


# ---------------------------------------------------------------------------
# R4: entry validation stays first in the method body.
# ---------------------------------------------------------------------------


def test_set_story_status_method_validates_plan_name_first():
    src = inspect.getsource(p.PipelineService.set_story_status)
    body = _executable_body(src)
    assert body[0] == "_validate_key(plan_name)", (
        f"_validate_key(plan_name) must be the first statement, got: {body[0]!r}"
    )


def test_set_story_status_method_validates_story_key_second():
    src = inspect.getsource(p.PipelineService.set_story_status)
    body = _executable_body(src)
    assert body[1] == "_validate_key(story_key)", (
        f"_validate_key(story_key) must be the second statement, got: {body[1]!r}"
    )


# ---------------------------------------------------------------------------
# Behaviour: delegation produces identical results (happy path + negatives).
# ---------------------------------------------------------------------------


def test_set_story_status_updates_to_valid_status(plan_dir):
    _write_manifest(plan_dir, "ss1", {
        "S1": {"summary": "s", "status": "parked"},
    })
    result = p.set_story_status("ss1", "S1", "interrupted")
    assert result == {"ok": True, "story_key": "S1", "status": "interrupted"}
    manifest = _read_manifest(plan_dir, "ss1")
    assert manifest["stories"]["S1"]["status"] == "interrupted"


def test_set_story_status_via_service_updates_to_valid_status(plan_dir):
    _write_manifest(plan_dir, "ss1b", {
        "S1": {"summary": "s", "status": "parked"},
    })
    result = p._service.set_story_status("ss1b", "S1", "interrupted")
    assert result == {"ok": True, "story_key": "S1", "status": "interrupted"}
    manifest = _read_manifest(plan_dir, "ss1b")
    assert manifest["stories"]["S1"]["status"] == "interrupted"


def test_set_story_status_rejects_invalid_status(plan_dir):
    _write_manifest(plan_dir, "ss2", {
        "S1": {"summary": "s", "status": "parked"},
    })
    result = p.set_story_status("ss2", "S1", "definitely-not-a-status")
    assert result["ok"] is False
    assert "definitely-not-a-status" in result["error"]
    manifest = _read_manifest(plan_dir, "ss2")
    assert manifest["stories"]["S1"]["status"] == "parked"


def test_set_story_status_rejects_missing_story(plan_dir):
    _write_manifest(plan_dir, "ss3", {})
    result = p.set_story_status("ss3", "no-such-key", "todo")
    assert result["ok"] is False
    assert "no-such-key" in result["error"]


def test_set_story_status_missing_manifest_returns_error(plan_dir):
    result = p.set_story_status("never-ingested", "S1", "todo")
    assert result["ok"] is False


def test_set_story_status_skips_when_lock_held(plan_dir):
    _write_manifest(plan_dir, "ss4", {
        "S1": {"summary": "s", "status": "parked"},
    })
    lock_path = plan_dir / "ss4.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = p.set_story_status("ss4", "S1", "interrupted")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert result["ok"] is True
    assert result.get("skipped") == "locked"
    manifest = _read_manifest(plan_dir, "ss4")
    assert manifest["stories"]["S1"]["status"] == "parked"


def test_set_story_status_clears_parked_reason_on_unpark(plan_dir):
    _write_manifest(plan_dir, "sss", {
        "P1": {"summary": "parked", "status": "parked",
               "parked_reason": "high risk held for human review"},
    })
    result = p.set_story_status("sss", "P1", "interrupted")
    assert result["ok"] is True
    story = _read_manifest(plan_dir, "sss")["stories"]["P1"]
    assert story["status"] == "interrupted"
    assert "parked_reason" not in story


def test_set_story_status_keeps_parked_reason_when_staying_parked(plan_dir):
    _write_manifest(plan_dir, "sskeep", {
        "P1": {"summary": "parked", "status": "todo",
               "parked_reason": "high risk held for human review"},
    })
    result = p.set_story_status("sskeep", "P1", "parked")
    assert result["ok"] is True
    story = _read_manifest(plan_dir, "sskeep")["stories"]["P1"]
    assert story["status"] == "parked"
    assert story["parked_reason"] == "high risk held for human review"


# ---------------------------------------------------------------------------
# Boundary: every valid status is accepted.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [
    "todo", "in_progress", "running", "interrupted", "failed",
    "tests_passed", "pr_open", "changes_requested", "parked", "done",
])
def test_set_story_status_accepts_each_valid_status(plan_dir, status):
    _write_manifest(plan_dir, f"valid-{status}", {
        "S1": {"summary": "s", "status": "todo"},
    })
    result = p.set_story_status(f"valid-{status}", "S1", status)
    assert result["ok"] is True
    assert result["status"] == status
    assert _read_manifest(plan_dir, f"valid-{status}")["stories"]["S1"]["status"] == status


# ---------------------------------------------------------------------------
# N1: path-traversal rejection still fires through the new call path.
# ---------------------------------------------------------------------------


def test_set_story_status_rejects_traversal_plan_name(plan_dir):
    with pytest.raises(ValueError, match="invalid"):
        p.set_story_status("../evil", "S1", "todo")


def test_set_story_status_rejects_traversal_story_key(plan_dir):
    with pytest.raises(ValueError, match="invalid"):
        p.set_story_status("ss1", "../evil", "todo")


def test_set_story_status_rejects_traversal_plan_name_via_service(plan_dir):
    with pytest.raises(ValueError, match="invalid"):
        p._service.set_story_status("../evil", "S1", "todo")


def test_set_story_status_rejects_traversal_story_key_via_service(plan_dir):
    with pytest.raises(ValueError, match="invalid"):
        p._service.set_story_status("ss1", "../evil", "todo")


# ---------------------------------------------------------------------------
# N2 / R1: a monkeypatched module global still takes effect through the new
# call path. We patch _validate_key and _atomic_write_json (helpers the method
# calls as bare names) and confirm the patch lands when going through _service.
# ---------------------------------------------------------------------------


def test_set_story_status_service_uses_module_level_validate_key(plan_dir, monkeypatch):
    called = []

    def fake_validate(key):
        called.append(key)
        raise ValueError("invalid: patched")

    monkeypatch.setattr(p, "_validate_key", fake_validate)
    with pytest.raises(ValueError, match="invalid: patched"):
        p._service.set_story_status("anyplan", "S1", "todo")
    assert called == ["anyplan"]


def test_set_story_status_service_uses_module_level_atomic_write_json(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "ssatom", {
        "S1": {"summary": "s", "status": "parked"},
    })
    captured = {}

    real_write = p._atomic_write_json

    def fake_write(path, data):
        captured["path"] = path
        captured["data"] = data
        return real_write(path, data)

    monkeypatch.setattr(p, "_atomic_write_json", fake_write)
    result = p._service.set_story_status("ssatom", "S1", "interrupted")
    assert result["ok"] is True
    assert captured["data"]["stories"]["S1"]["status"] == "interrupted"


def test_set_story_status_service_uses_module_level_plan_dir(plan_dir, monkeypatch):
    # Patching the module-level PLAN_DIR must affect the service method.
    _write_manifest(plan_dir, "sspd", {
        "S1": {"summary": "s", "status": "parked"},
    })
    result = p._service.set_story_status("sspd", "S1", "interrupted")
    assert result["ok"] is True
    assert _read_manifest(plan_dir, "sspd")["stories"]["S1"]["status"] == "interrupted"


def test_set_story_status_service_uses_module_level_valid_statuses(plan_dir, monkeypatch):
    # Patching _VALID_STORY_STATUSES must affect the service method's check.
    monkeypatch.setattr(p, "_VALID_STORY_STATUSES", frozenset({"only-this"}))
    result = p._service.set_story_status("anyplan", "S1", "todo")
    assert result["ok"] is False
    assert "todo" in result["error"]


# ---------------------------------------------------------------------------
# R2: internal calls to other tools stay module-level (no self.tool calls).
# ---------------------------------------------------------------------------


def test_set_story_status_method_has_no_self_tool_calls():
    src = inspect.getsource(p.PipelineService.set_story_status)
    # No routing of other tools through self.
    forbidden = ["self.dispatch_story", "self.interrupt_story", "self.review_story",
                 "self.check_story_status", "self.advance_pipeline",
                 "self._advance_pipeline_locked", "self._original_review_story"]
    found = [f for f in forbidden if f in src]
    assert found == [], (
        f"method must not route other tools through self., found: {found!r}"
    )


# ---------------------------------------------------------------------------
# R3: the @mcp.tool() decorator stays on the module-level function, not the
# method.
# ---------------------------------------------------------------------------


def test_pipeline_service_set_story_status_is_not_mcp_tool():
    # The method should not be a wrapped tool object.
    assert not hasattr(p.PipelineService.set_story_status, "name")


# ---------------------------------------------------------------------------
# R5: no behaviour change / no opportunistic cleanup -- the method body must
# be a verbatim, re-indented copy of the original tool body.
# ---------------------------------------------------------------------------


def test_set_story_status_method_body_matches_original_statements():
    src = inspect.getsource(p.PipelineService.set_story_status)
    body = _executable_body(src)
    # The original body's key statements must all be present, in order.
    assert "_validate_key(plan_name)" in body
    assert "_validate_key(story_key)" in body
    assert body.index("_validate_key(plan_name)") < body.index("_validate_key(story_key)")
    assert any("status not in _VALID_STORY_STATUSES" in ln for ln in body)
    assert any("with _plan_lock(plan_name) as acquired:" in ln for ln in body)
    assert any('return {"ok": True, "story_key": story_key, "status": status}' in ln for ln in body)


# ---------------------------------------------------------------------------
# R7: check_story_status stays module-level and untouched (not part of epic).
# ---------------------------------------------------------------------------


def test_check_story_status_still_module_level():
    assert hasattr(p, "check_story_status")
    assert inspect.isfunction(p.check_story_status)


# ---------------------------------------------------------------------------
# R8: PipelineService defined in pipeline.server; singleton is module attr.
# ---------------------------------------------------------------------------


def test_pipeline_service_defined_in_pipeline_server():
    assert p.PipelineService.__module__ == "pipeline.server"


def test_service_singleton_is_module_attribute():
    assert "_service" in p.__dict__


def test_service_singleton_is_pipeline_service():
    assert isinstance(p._service, p.PipelineService)


# ---------------------------------------------------------------------------
# Source-text assertions: class sits above the Tools banner, singleton present.
# ---------------------------------------------------------------------------


def test_class_placed_above_tools_banner():
    src = inspect.getsource(p)
    banner = src.index("# ---------- Tools ----------")
    class_idx = src.index("class PipelineService:")
    assert class_idx < banner, "PipelineService must be defined above the Tools banner"


def test_singleton_assignment_present():
    src = inspect.getsource(p)
    assert re.search(r"^_service\s*=\s*PipelineService\(\)", src, re.MULTILINE), (
        "module-level `_service = PipelineService()` must be present"
    )


def test_set_story_status_method_above_tools_banner():
    src = inspect.getsource(p)
    banner = src.index("# ---------- Tools ----------")
    method_idx = src.index("def set_story_status")
    # The method def appears inside the class, which is above the banner.
    assert method_idx < banner or "class PipelineService:" in src[:method_idx], (
        "the method def must be within the class (above the Tools banner)"
    )


# ---------------------------------------------------------------------------
# Boundary: empty-string names are rejected by validation.
# ---------------------------------------------------------------------------


def test_set_story_status_empty_plan_name_rejected(plan_dir):
    with pytest.raises(ValueError, match="invalid"):
        p.set_story_status("", "S1", "todo")


def test_set_story_status_empty_story_key_rejected(plan_dir):
    with pytest.raises(ValueError, match="invalid"):
        p.set_story_status("ss1", "", "todo")


def test_set_story_status_empty_status_rejected(plan_dir):
    _write_manifest(plan_dir, "ssempty", {
        "S1": {"summary": "s", "status": "parked"},
    })
    result = p.set_story_status("ssempty", "S1", "")
    assert result["ok"] is False
    assert "" in result["error"]


# ---------------------------------------------------------------------------
# N3: error return shapes are byte-identical to before.
# ---------------------------------------------------------------------------


def test_set_story_status_invalid_status_error_shape(plan_dir):
    _write_manifest(plan_dir, "ssshape", {
        "S1": {"summary": "s", "status": "parked"},
    })
    result = p.set_story_status("ssshape", "S1", "bogus")
    assert result["ok"] is False
    assert "error" in result
    assert "bogus" in result["error"]
    assert "must be one of" in result["error"]


def test_set_story_status_no_such_story_error_shape(plan_dir):
    _write_manifest(plan_dir, "ssshape2", {})
    result = p.set_story_status("ssshape2", "missing", "todo")
    assert result == {"ok": False, "error": "No such story 'missing'"}