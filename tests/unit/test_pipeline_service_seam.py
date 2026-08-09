"""Tests for the W1a ``PipelineService`` seam in ``pipeline/server.py``.

These tests verify the structural extraction of ``pause_plan`` / ``resume_plan``
into a transport-agnostic ``PipelineService`` class with a module-level
``_service`` singleton, while the ``@mcp.tool()``-decorated module functions
shrink to one-line delegations.

They are written to be RED until the implementation exists: the class, the
singleton, and the delegation must all be present for these to pass.
"""

import inspect
import json
import re
import textwrap

import pytest

from pipeline import server as p

# ---------------------------------------------------------------------------
# Helpers / fixtures (mirror the ones in test_pipeline_mcp_server.py so this
# file is fully self-contained).
# ---------------------------------------------------------------------------

@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    return d


def _write_manifest(plan_dir, plan_name, stories):
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}, indent=2)
    )


def _read_manifest(plan_dir, plan_name):
    return json.loads((plan_dir / f"{plan_name}.manifest.json").read_text())


# ---------------------------------------------------------------------------
# C1 / N4: PipelineService class exists, has the two methods, no __init__.
# ---------------------------------------------------------------------------

def test_pipeline_service_class_exists():
    assert hasattr(p, "PipelineService"), "pipeline.server must define PipelineService"
    assert isinstance(p.PipelineService, type)


def test_pipeline_service_has_pause_plan_method():
    assert hasattr(p.PipelineService, "pause_plan")
    method = p.PipelineService.pause_plan
    assert inspect.isfunction(method) or inspect.ismethod(method)


def test_pipeline_service_has_resume_plan_method():
    assert hasattr(p.PipelineService, "resume_plan")
    method = p.PipelineService.resume_plan
    assert inspect.isfunction(method) or inspect.ismethod(method)


def test_pipeline_service_has_no_init():
    # R1 / N4: __init__ must not exist (or take nothing but self and set nothing).
    assert "__init__" not in p.PipelineService.__dict__, (
        "PipelineService must not define __init__"
    )


def test_pipeline_service_methods_take_self():
    sig_pause = inspect.signature(p.PipelineService.pause_plan)
    params = list(sig_pause.parameters)
    assert params[0] == "self", "pause_plan method must take self as first param"
    assert list(sig_pause.parameters)[1:] == ["plan_name"]

    sig_resume = inspect.signature(p.PipelineService.resume_plan)
    params_r = list(sig_resume.parameters)
    assert params_r[0] == "self"
    assert list(sig_resume.parameters)[1:] == ["plan_name"]


def test_pipeline_service_method_type_hints():
    sig = inspect.signature(p.PipelineService.pause_plan)
    assert sig.parameters["plan_name"].annotation is str
    assert sig.return_annotation == dict[str, p.Any] or sig.return_annotation == "dict[str, Any]"


# ---------------------------------------------------------------------------
# N5: _service singleton constructed once at import, type PipelineService.
# ---------------------------------------------------------------------------

def test_service_singleton_exists_and_is_pipeline_service():
    assert hasattr(p, "_service"), "pipeline.server must define _service singleton"
    assert type(p._service).__name__ == "PipelineService"
    assert isinstance(p._service, p.PipelineService)


def test_service_singleton_is_single_instance():
    # Constructing again yields a different object, but the module attribute is
    # the one the tools delegate through.
    assert p._service is p._service  # trivially stable reference
    assert isinstance(p._service, p.PipelineService)


# ---------------------------------------------------------------------------
# C2 / C3: module-level @mcp.tool() functions still exist & delegate.
# ---------------------------------------------------------------------------

def test_module_pause_plan_still_exists():
    assert hasattr(p, "pause_plan")
    assert callable(p.pause_plan)


def test_module_resume_plan_still_exists():
    assert hasattr(p, "resume_plan")
    assert callable(p.resume_plan)


def test_pause_plan_definition_count_is_two():
    # C3: exactly 2 definitions (method + tool function).
    src = inspect.getsource(p)
    assert src.count("def pause_plan") == 2, (
        "expected exactly 2 'def pause_plan' (method + tool), got "
        f"{src.count('def pause_plan')}"
    )


def test_resume_plan_definition_count_is_two():
    src = inspect.getsource(p)
    assert src.count("def resume_plan") == 2, (
        "expected exactly 2 'def resume_plan' (method + tool), got "
        f"{src.count('def resume_plan')}"
    )


def test_module_pause_plan_signature_unchanged():
    sig = inspect.signature(p.pause_plan)
    params = list(sig.parameters)
    assert params == ["plan_name"]
    assert sig.parameters["plan_name"].annotation is str
    assert sig.return_annotation == dict[str, p.Any] or sig.return_annotation == "dict[str, Any]"


def test_module_resume_plan_signature_unchanged():
    sig = inspect.signature(p.resume_plan)
    params = list(sig.parameters)
    assert params == ["plan_name"]
    assert sig.parameters["plan_name"].annotation is str
    assert sig.return_annotation == dict[str, p.Any] or sig.return_annotation == "dict[str, Any]"


def test_module_pause_plan_body_is_single_delegation():
    src = inspect.getsource(p.pause_plan)
    # Strip the decorator line(s) and docstring; the executable body must be
    # exactly one statement delegating to _service.pause_plan.
    body = _executable_body(src)
    assert body == ["return _service.pause_plan(plan_name)"], (
        f"pause_plan body must be exactly one delegation, got: {body!r}"
    )


def test_module_resume_plan_body_is_single_delegation():
    src = inspect.getsource(p.resume_plan)
    body = _executable_body(src)
    assert body == ["return _service.resume_plan(plan_name)"], (
        f"resume_plan body must be exactly one delegation, got: {body!r}"
    )


def test_pause_plan_docstring_preserved():
    doc = p.pause_plan.__doc__
    assert doc is not None, "pause_plan docstring must be preserved on the tool function"
    assert "Stop advance_pipeline" in doc
    assert "Resume with resume_plan" in doc


def test_resume_plan_docstring_preserved():
    doc = p.resume_plan.__doc__
    assert doc is not None
    assert "Clear a pause set by pause_plan" in doc


# ---------------------------------------------------------------------------
# C4: tools still MCP-registered (decorator stayed on module-level function).
# ---------------------------------------------------------------------------

def _mcp_tool_names():
    return {t.name for t in p.mcp._tool_manager.list_tools()}


def test_pause_plan_is_mcp_registered():
    assert "pause_plan" in _mcp_tool_names(), (
        "pause_plan must remain MCP-registered (decorator on module function)"
    )


def test_resume_plan_is_mcp_registered():
    assert "resume_plan" in _mcp_tool_names(), (
        "resume_plan must remain MCP-registered (decorator on module function)"
    )


# ---------------------------------------------------------------------------
# C5 / R1: no self. access to module globals inside the new methods.
# ---------------------------------------------------------------------------

def test_pause_plan_method_has_no_self_global_access():
    src = inspect.getsource(p.PipelineService.pause_plan)
    # The only allowable `self` is the receiver parameter itself.
    # No self.<anything> in the body.
    body_lines = [ln for ln in src.splitlines() if "self." in ln]
    assert body_lines == [], (
        f"pause_plan method must not access anything via self., found: {body_lines!r}"
    )


def test_resume_plan_method_has_no_self_global_access():
    src = inspect.getsource(p.PipelineService.resume_plan)
    body_lines = [ln for ln in src.splitlines() if "self." in ln]
    assert body_lines == [], (
        f"resume_plan method must not access anything via self., found: {body_lines!r}"
    )


def test_pipeline_service_class_holds_no_state_attributes():
    # R1: the class must not store copies of module globals on instances.
    svc = p.PipelineService()
    instance_attrs = vars(svc)
    assert instance_attrs == {}, (
        f"PipelineService instances must hold no state, found: {instance_attrs!r}"
    )


# ---------------------------------------------------------------------------
# R4: entry validation stays first in the method body.
# ---------------------------------------------------------------------------

def test_pause_plan_method_validates_first():
    src = inspect.getsource(p.PipelineService.pause_plan)
    body = _executable_body(src)
    assert body[0] == "_validate_key(plan_name)", (
        f"_validate_key must be the first statement, got: {body[0]!r}"
    )


def test_resume_plan_method_validates_first():
    src = inspect.getsource(p.PipelineService.resume_plan)
    body = _executable_body(src)
    assert body[0] == "_validate_key(plan_name)", (
        f"_validate_key must be the first statement, got: {body[0]!r}"
    )


# ---------------------------------------------------------------------------
# Behaviour: delegation produces identical results (happy path + negatives).
# ---------------------------------------------------------------------------

def test_pause_plan_sets_manifest_flag(plan_dir):
    _write_manifest(plan_dir, "tobehalted", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
    })
    result = p.pause_plan("tobehalted")
    assert result == {"ok": True, "plan_name": "tobehalted", "paused": True}
    assert _read_manifest(plan_dir, "tobehalted")["paused"] is True


def test_resume_plan_clears_manifest_flag(plan_dir):
    (plan_dir / "halted3.manifest.json").write_text(json.dumps({
        "epics": {}, "paused": True,
        "stories": {"T1": {"summary": "todo", "status": "todo", "dependencies": []}},
    }))
    result = p.resume_plan("halted3")
    assert result == {"ok": True, "plan_name": "halted3", "paused": False}
    assert _read_manifest(plan_dir, "halted3")["paused"] is False


def test_pause_plan_no_such_manifest_returns_error(plan_dir):
    result = p.pause_plan("never-ingested")
    assert result == {"ok": False, "error": "No manifest for never-ingested"}


def test_resume_plan_no_such_manifest_returns_error(plan_dir):
    result = p.resume_plan("never-ingested")
    assert result == {"ok": False, "error": "No manifest for never-ingested"}


def test_resume_plan_when_not_paused_is_a_noop(plan_dir):
    _write_manifest(plan_dir, "neverhalted", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
    })
    result = p.resume_plan("neverhalted")
    assert result == {"ok": True, "plan_name": "neverhalted", "paused": False}


# ---------------------------------------------------------------------------
# N1: path-traversal rejection still fires through the new call path.
# ---------------------------------------------------------------------------

def test_pause_plan_rejects_traversal(plan_dir):
    with pytest.raises(ValueError, match="invalid"):
        p.pause_plan("../evil")


def test_resume_plan_rejects_traversal(plan_dir):
    with pytest.raises(ValueError, match="invalid"):
        p.resume_plan("../evil")


def test_pause_plan_rejects_traversal_via_service(plan_dir):
    with pytest.raises(ValueError, match="invalid"):
        p._service.pause_plan("../evil")


def test_resume_plan_rejects_traversal_via_service(plan_dir):
    with pytest.raises(ValueError, match="invalid"):
        p._service.resume_plan("../evil")


# ---------------------------------------------------------------------------
# N2 / R1: a monkeypatched module global still takes effect through the new
# call path. We patch _set_plan_paused (a helper the method calls as a bare
# name) and confirm the patch lands when going through _service.
# ---------------------------------------------------------------------------

def test_pause_plan_service_uses_module_level_set_plan_paused(plan_dir, monkeypatch):
    captured = {}

    def fake_set(plan_name, paused):
        captured["plan_name"] = plan_name
        captured["paused"] = paused
        return {"ok": True, "plan_name": plan_name, "paused": paused, "fake": True}

    monkeypatch.setattr(p, "_set_plan_paused", fake_set)
    result = p._service.pause_plan("someplan")
    assert captured == {"plan_name": "someplan", "paused": True}
    assert result == {"ok": True, "plan_name": "someplan", "paused": True, "fake": True}


def test_resume_plan_service_uses_module_level_set_plan_paused(plan_dir, monkeypatch):
    captured = {}

    def fake_set(plan_name, paused):
        captured["plan_name"] = plan_name
        captured["paused"] = paused
        return {"ok": True, "plan_name": plan_name, "paused": paused, "fake": True}

    monkeypatch.setattr(p, "_set_plan_paused", fake_set)
    result = p._service.resume_plan("someplan")
    assert captured == {"plan_name": "someplan", "paused": False}
    assert result == {"ok": True, "plan_name": "someplan", "paused": False, "fake": True}


def test_pause_plan_service_uses_module_level_validate_key(plan_dir, monkeypatch):
    # Patching the module-level _validate_key must affect the service method.
    called = []

    def fake_validate(key):
        called.append(key)
        raise ValueError("invalid: patched")

    monkeypatch.setattr(p, "_validate_key", fake_validate)
    with pytest.raises(ValueError, match="invalid: patched"):
        p._service.pause_plan("anyplan")
    assert called == ["anyplan"]


def test_resume_plan_service_uses_module_level_validate_key(plan_dir, monkeypatch):
    called = []

    def fake_validate(key):
        called.append(key)
        raise ValueError("invalid: patched")

    monkeypatch.setattr(p, "_validate_key", fake_validate)
    with pytest.raises(ValueError, match="invalid: patched"):
        p._service.resume_plan("anyplan")
    assert called == ["anyplan"]


# ---------------------------------------------------------------------------
# R3: the @mcp.tool() decorator stays on the module-level function, not the
# method. We assert the module function is registered as a tool (above) and
# that the method is NOT itself decorated/registered.
# ---------------------------------------------------------------------------

def test_pipeline_service_methods_are_not_mcp_tools():
    # The methods live on the class; they should not appear as separate tools.
    # (They share names with the module functions, so we instead assert the
    # class methods are plain functions, not wrapped tool objects.)
    assert not hasattr(p.PipelineService.pause_plan, "name")
    assert not hasattr(p.PipelineService.resume_plan, "name")


# ---------------------------------------------------------------------------
# R8: only pipeline/server.py changed is enforced at the repo level by the
# implementer; here we assert the class is defined inside pipeline.server.
# ---------------------------------------------------------------------------

def test_pipeline_service_defined_in_pipeline_server():
    assert p.PipelineService.__module__ == "pipeline.server"


def test_service_singleton_is_module_attribute():
    assert "_service" in p.__dict__


# ---------------------------------------------------------------------------
# Source-text assertions: the class sits above the Tools banner, the singleton
# assignment exists, and the tool bodies delegate.
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


def test_set_plan_paused_still_module_level_function():
    # R5: _set_plan_paused stays a module-level function, untouched.
    assert hasattr(p, "_set_plan_paused")
    obj = p._set_plan_paused
    assert inspect.isfunction(obj), "_set_plan_paused must remain a module-level function"
    sig = inspect.signature(obj)
    assert list(sig.parameters) == ["plan_name", "paused"]


# ---------------------------------------------------------------------------
# Boundary: empty-string plan name is rejected by validation.
# ---------------------------------------------------------------------------

def test_pause_plan_empty_name_rejected(plan_dir):
    with pytest.raises(ValueError, match="invalid"):
        p.pause_plan("")


def test_resume_plan_empty_name_rejected(plan_dir):
    with pytest.raises(ValueError, match="invalid"):
        p.resume_plan("")


# ---------------------------------------------------------------------------
# Utility: extract the executable (non-decorator, non-docstring) statements
# from a function's source as a list of stripped lines.
# ---------------------------------------------------------------------------

def _executable_body(src: str) -> list[str]:
    """Return the executable statement lines of a function body.

    Drops decorator lines, the ``def`` line, and any docstring block.
    """
    lines = src.splitlines()
    # Drop leading decorator lines.
    while lines and (lines[0].lstrip().startswith("@") or not lines[0].strip()):
        lines.pop(0)
    # Drop the def line.
    while lines and not lines[0].lstrip().startswith("def "):
        lines.pop(0)
    if lines:
        lines.pop(0)  # the def line itself

    # Dedent and drop blank lines.
    body = textwrap.dedent("\n".join(lines)).splitlines()
    # Remove a leading docstring (triple-quoted) block.
    stripped = [ln for ln in body if ln.strip()]
    if stripped and stripped[0].lstrip().startswith(('"""', "'''")):
        # Find the end of the docstring.
        quote = stripped[0].lstrip()[:3]
        # Single-line docstring?
        if stripped[0].count(quote) >= 2 and len(stripped[0].lstrip()) > 3:
            stripped = stripped[1:]
        else:
            # multi-line: consume until closing quote
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