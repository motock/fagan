"""Structural tests for the W1a migration of ``mark_story_in_progress`` onto
``PipelineService``.

These tests assert the *mechanical* invariants of the move described in the
story spec, NOT new behaviour (the move is behaviour-preserving). They fail
before the implementation exists (the method is absent / the tool body is not
a single delegation line) and pass once the move is done correctly.

Run with the project venv:
    cd ~/.claude/mcp-servers/pipeline && .venv/bin/python -m pytest -q \
        tests/unit/test_mark_story_in_progress_migration.py
"""

import inspect
import json
import textwrap

import pytest

from pipeline import server as p


# ---------- C1: the method exists on PipelineService with the right shape ----


def test_pipelineservice_has_mark_story_in_progress_method():
    """C1: PipelineService must define mark_story_in_progress as a method."""
    assert hasattr(p.PipelineService, "mark_story_in_progress"), (
        "PipelineService must define a mark_story_in_progress method"
    )
    assert callable(p.PipelineService.mark_story_in_progress)


def test_method_takes_self_plus_original_params():
    """C1: the method signature is (self, plan_name: str, story_key: str)."""
    sig = inspect.signature(p.PipelineService.mark_story_in_progress)
    params = list(sig.parameters.keys())
    assert params == ["self", "plan_name", "story_key"], (
        f"method params must be [self, plan_name, story_key], got {params}"
    )
    plan_hint = sig.parameters["plan_name"].annotation
    story_hint = sig.parameters["story_key"].annotation
    assert plan_hint is str, f"plan_name annotation must be str, got {plan_hint!r}"
    assert story_hint is str, f"story_key annotation must be str, got {story_hint!r}"
    assert sig.parameters["plan_name"].default is inspect.Parameter.empty
    assert sig.parameters["story_key"].default is inspect.Parameter.empty


def test_method_return_annotation_is_dict_of_str_any():
    """C1: return annotation is dict[str, Any]."""
    sig = inspect.signature(p.PipelineService.mark_story_in_progress)
    ret = sig.return_annotation
    assert str(ret) == "dict[str, Any]", f"return annotation wrong: {ret!r}"


def test_method_has_no_docstring():
    """R3: the docstring stays on the module-level tool function, NOT on the
    method. A duplicated docstring on the method is a regression signal."""
    method_doc = p.PipelineService.mark_story_in_progress.__doc__
    assert method_doc is None, (
        "the method must NOT carry a docstring; the docstring stays on the "
        "module-level @mcp.tool() function"
    )


# ---------- C2: the module-level tool is a one-line delegation ---------------


def test_module_level_tool_function_still_exists():
    """C2: the module-level @mcp.tool() def mark_story_in_progress exists."""
    assert hasattr(p, "mark_story_in_progress"), (
        "module-level mark_story_in_progress must still exist"
    )
    assert callable(p.mark_story_in_progress)


def test_module_level_tool_signature_unchanged():
    """R3/C2: the tool function's signature, type hints and defaults are
    byte-for-byte unchanged."""
    sig = inspect.signature(p.mark_story_in_progress)
    params = list(sig.parameters.keys())
    assert params == ["plan_name", "story_key"], (
        f"tool params must be [plan_name, story_key], got {params}"
    )
    assert sig.parameters["plan_name"].annotation is str
    assert sig.parameters["story_key"].annotation is str
    assert str(sig.return_annotation) == "dict[str, Any]"


def test_module_level_tool_docstring_unchanged():
    """R3: the tool function's full docstring is preserved verbatim."""
    doc = p.mark_story_in_progress.__doc__
    assert doc is not None, "tool function must keep its docstring"
    assert "Transition the ticket to In Progress" in doc
    assert "Use this before writing any code for a story." in doc


def test_module_level_tool_body_is_single_delegation():
    """C2: the tool function's executable body is exactly one statement."""
    src = inspect.getsource(p.mark_story_in_progress)
    assert "return _service.mark_story_in_progress(plan_name, story_key)" in src, (
        "tool body must delegate via: return _service.mark_story_in_progress("
        "plan_name, story_key)"
    )
    forbidden_in_tool = [
        "_validate_key(",
        "get_ticket_provider(",
        "manifest_path",
        "json.loads",
        "_atomic_write_json(",
        "manifest[",
        "PLAN_DIR",
    ]
    for token in forbidden_in_tool:
        assert token not in src, (
            f"forbidden token {token!r} must not remain in the module-level "
            "tool function body -- it should live only in the method"
        )


# ---------- C3: exactly two definitions --------------------------------------


def test_exactly_two_definitions_of_mark_story_in_progress():
    """C3: grep -c 'def mark_story_in_progress' pipeline/server.py == 2."""
    src = inspect.getsource(p)
    count = src.count("def mark_story_in_progress")
    assert count == 2, (
        f"expected exactly 2 'def mark_story_in_progress' (method + tool), "
        f"got {count}"
    )


# ---------- C4: still MCP-registered ----------------------------------------


def test_tool_still_mcp_registered():
    """C4: the tool is still registered with FastMCP (R3 guard)."""
    tool_names = {t.name for t in p.mcp._tool_manager.list_tools()}
    assert "mark_story_in_progress" in tool_names, (
        "mark_story_in_progress must remain registered as an MCP tool; the "
        "@mcp.tool() decorator must stay on the module-level function"
    )


# ---------- C5: no self.<global/helper> inside the method --------------------


def test_method_body_uses_no_self_attribute_access_for_globals():
    """C5/R1: inside the method, no module global or helper is accessed via
    self. (self may appear only as the receiver parameter.)"""
    src = inspect.getsource(p.PipelineService.mark_story_in_progress)
    lines = src.splitlines()
    body_lines = [ln for ln in lines if "def mark_story_in_progress" not in ln]
    body = "\n".join(body_lines)
    assert "self." not in body, (
        "method body must not access any module global/helper through self; "
        f"found 'self.' in:\n{body}"
    )


def test_method_body_keeps_validate_key_as_free_name():
    """R1: _validate_key stays a bare module-level name."""
    src = inspect.getsource(p.PipelineService.mark_story_in_progress)
    assert "_validate_key(plan_name)" in src
    assert "_validate_key(story_key)" in src
    assert "self._validate_key" not in src


def test_method_body_keeps_globals_as_free_names():
    """R1: PLAN_DIR, get_ticket_provider, _atomic_write_json stay bare names."""
    src = inspect.getsource(p.PipelineService.mark_story_in_progress)
    assert "PLAN_DIR" in src
    assert "get_ticket_provider()" in src
    assert "_atomic_write_json" in src
    for g in ["self.PLAN_DIR", "self.get_ticket_provider", "self._atomic_write_json"]:
        assert g not in src, f"{g!r} must not appear in the method body"


# ---------- R4: entry validation stays first --------------------------------


def test_validate_key_calls_are_first_statements_of_method():
    """R4: _validate_key calls must be the FIRST statements of the method."""
    src = inspect.getsource(p.PipelineService.mark_story_in_progress)
    lines = textwrap.dedent(src).splitlines()
    body_stmts = []
    in_docstring = False
    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith("def mark_story_in_progress"):
            continue
        if not stripped:
            continue
        if stripped.startswith('"""') or stripped.startswith("'''"):
            in_docstring = not in_docstring
            continue
        if in_docstring:
            continue
        if stripped.startswith("#"):
            continue
        body_stmts.append(stripped)
    assert len(body_stmts) >= 2, f"expected at least 2 body statements, got {body_stmts}"
    assert body_stmts[0] == "_validate_key(plan_name)", (
        f"first statement must be _validate_key(plan_name), got {body_stmts[0]!r}"
    )
    assert body_stmts[1] == "_validate_key(story_key)", (
        f"second statement must be _validate_key(story_key), got {body_stmts[1]!r}"
    )


# ---------- R2: internal calls stay module-level (no self.<tool>) -----------


def test_method_does_not_route_other_tools_through_self():
    """R2: no other tool function is rewritten to self.<tool>(...)."""
    src = inspect.getsource(p.PipelineService.mark_story_in_progress)
    for tool in [
        "self.dispatch_story",
        "self.interrupt_story",
        "self.review_story",
        "self.check_story_status",
        "self.advance_pipeline",
        "self._advance_pipeline_locked",
    ]:
        assert tool not in src, f"{tool!r} must not appear in the method body"


# ---------- Behaviour preservation: happy path + negatives ------------------


@pytest.fixture
def _null_provider(monkeypatch):
    monkeypatch.setattr(p, "get_ticket_provider", lambda: _NullSetStateProvider())


def test_tool_delegates_to_service_and_sets_in_progress(plan_dir, _null_provider):
    """Happy path through the module-level tool still flips status to
    in_progress (exercises the delegation _service.mark_story_in_progress)."""
    (plan_dir / "happy.manifest.json").write_text(
        json.dumps({"stories": {"S1": {"status": "todo"}}})
    )
    result = p.mark_story_in_progress("happy", "S1")
    assert result == {"ok": True}
    manifest = json.loads((plan_dir / "happy.manifest.json").read_text())
    assert manifest["stories"]["S1"]["status"] == "in_progress"


def test_service_method_sets_in_progress_directly(plan_dir, _null_provider):
    """Happy path calling the service method directly."""
    (plan_dir / "svc.manifest.json").write_text(
        json.dumps({"stories": {"S1": {"status": "todo"}}})
    )
    result = p._service.mark_story_in_progress("svc", "S1")
    assert result == {"ok": True}
    manifest = json.loads((plan_dir / "svc.manifest.json").read_text())
    assert manifest["stories"]["S1"]["status"] == "in_progress"


def test_unknown_story_returns_error(plan_dir, _null_provider):
    """N3: unknown-story error shape is byte-identical to before."""
    (plan_dir / "unk.manifest.json").write_text(
        json.dumps({"stories": {"S1": {"status": "todo"}}})
    )
    result = p.mark_story_in_progress("unk", "NOPE")
    assert result == {"ok": False, "error": "No such story NOPE"}


def test_missing_manifest_raises_filenotfound(plan_dir, _null_provider):
    """N3: missing manifest still raises (json read of a missing file)."""
    with pytest.raises(FileNotFoundError):
        p.mark_story_in_progress("no-such-plan", "S1")


def test_rejects_traversal_plan_name(plan_dir, _null_provider):
    """N1: path-traversal rejection still fires FIRST for plan_name."""
    with pytest.raises(ValueError, match="invalid"):
        p.mark_story_in_progress("../evil", "S1")


def test_rejects_traversal_story_key(plan_dir, _null_provider):
    """N1: path-traversal rejection still fires FIRST for story_key."""
    (plan_dir / "trav.manifest.json").write_text(
        json.dumps({"stories": {"S1": {"status": "todo"}}})
    )
    with pytest.raises(ValueError, match="invalid"):
        p.mark_story_in_progress("trav", "../evil")


def test_monkeypatched_global_takes_effect_through_method(plan_dir, monkeypatch):
    """N2/R1: a monkeypatched module global (get_ticket_provider) still takes
    effect through the new method call path -- the R1 regression detector."""
    calls = []

    class _FakeProvider:
        def set_state(self, story_key, state, plan_name=None):
            calls.append((story_key, state, plan_name))
            return True

    monkeypatch.setattr(p, "get_ticket_provider", lambda: _FakeProvider())
    (plan_dir / "patch.manifest.json").write_text(
        json.dumps({"stories": {"S1": {"status": "todo"}}})
    )
    result = p.mark_story_in_progress("patch", "S1")
    assert result == {"ok": True}
    assert calls == [("S1", p.LogicalState.IN_PROGRESS, "patch")]


def test_monkeypatched_plan_dir_takes_effect_through_method(tmp_path, monkeypatch):
    """N2/R1: a monkeypatched PLAN_DIR still takes effect through the method."""
    d = tmp_path / "altplans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(p, "get_ticket_provider", lambda: _NullSetStateProvider())
    (d / "alt.manifest.json").write_text(
        json.dumps({"stories": {"S1": {"status": "todo"}}})
    )
    result = p.mark_story_in_progress("alt", "S1")
    assert result == {"ok": True}
    manifest = json.loads((d / "alt.manifest.json").read_text())
    assert manifest["stories"]["S1"]["status"] == "in_progress"


# ---------- R8: only pipeline/server.py changed (no new files in prod) -------


def test_service_singleton_is_pipeline_service_instance():
    """The module-level _service is a PipelineService instance (delegation
    target exists)."""
    assert isinstance(p._service, p.PipelineService)


# ---------- helpers ---------------------------------------------------------


class _NullSetStateProvider:
    """A ticket provider whose set_state is a no-op, so the manifest write is
    the only observable effect."""

    def set_state(self, story_key, state, plan_name=None):
        return True