"""Structural-migration tests for the ``list_ready_stories`` MCP tool onto
``PipelineService`` (story W1a: migrate list_ready_stories).

These tests assert the *mechanical-move* contract described in the story:

* C1: ``PipelineService`` defines ``list_ready_stories`` as a method taking
  ``self`` plus the original parameters with the original type hints.
* C2: the module-level ``@mcp.tool()``-decorated ``def list_ready_stories``
  still exists at its original position with unchanged signature/docstring and
  a one-line delegation body.
* C3: ``grep -c "def list_ready_stories" pipeline/server.py`` == 2.
* C4: the tool is still MCP-registered.
* C5: no ``self.`` access to module globals/helpers inside the new method.
* R1/R2: free variables stay free; internal calls stay module-level.
* R3: decorator/signature/docstring stay on the module-level function.
* R4: entry validation (``_validate_key``) stays first.
* Behaviour (happy path + traversal rejection + missing manifest) unchanged.

The implementation does not exist yet, so this suite is RED until the move is
performed. Run with the project venv::

    cd ~/.claude/mcp-servers/pipeline && .venv/bin/python -m pytest -q \
        tests/unit/test_list_ready_stories_migration.py
"""

import inspect
import json
import re

import pytest

from pipeline import server as p

# ---------- shared helpers (local copies so this file is self-contained) ----------

def _write_manifest(plan_dir, plan_name, stories):
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}, indent=2)
    )


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    return d


# ---------- C1: PipelineService.list_ready_stories method shape ----------

def test_pipeline_service_has_list_ready_stories_method():
    """C1: ``PipelineService`` must define ``list_ready_stories`` as a method."""
    assert hasattr(p.PipelineService, "list_ready_stories"), (
        "PipelineService must define a `list_ready_stories` method"
    )
    assert inspect.isfunction(p.PipelineService.list_ready_stories), (
        "PipelineService.list_ready_stories must be a plain function/method, "
        "not a property or descriptor"
    )


def test_pipeline_service_list_ready_stories_takes_self_plus_original_params():
    """C1: the method signature is ``(self, plan_name: str) -> list[dict]``."""
    sig = inspect.signature(p.PipelineService.list_ready_stories)
    params = list(sig.parameters)
    assert params == ["self", "plan_name"], (
        f"method params must be ['self', 'plan_name'], got {params}"
    )
    # self has no default; plan_name has no default (matches original tool).
    assert sig.parameters["self"].default is inspect.Parameter.empty
    assert sig.parameters["plan_name"].default is inspect.Parameter.empty

    # Original type hint on plan_name is `str`.
    assert sig.parameters["plan_name"].annotation is str, (
        "plan_name annotation must remain `str`"
    )
    # Original return annotation is `list[dict]`.
    assert sig.return_annotation == list[dict], (
        f"return annotation must be `list[dict]`, got {sig.return_annotation!r}"
    )


def test_pipeline_service_list_ready_stories_method_has_no_docstring():
    """R3: the docstring must NOT be duplicated onto the method -- it stays on
    the module-level tool function only."""
    method_doc = p.PipelineService.list_ready_stories.__doc__
    assert method_doc is None or method_doc.strip() == "", (
        "The method must not carry the tool's docstring (R3); it must stay on "
        "the module-level @mcp.tool() function. Got: " + repr(method_doc)
    )


# ---------- C2: module-level tool function shape ----------

def test_module_level_list_ready_stories_still_exists():
    """C2: the module-level ``def list_ready_stories`` still exists."""
    assert hasattr(p, "list_ready_stories"), (
        "module-level `list_ready_stories` must still exist on pipeline.server"
    )
    assert inspect.isfunction(p.list_ready_stories), (
        "module-level `list_ready_stories` must be a function"
    )


def test_module_level_list_ready_stories_signature_unchanged():
    """C2/R3: the module-level tool keeps its original signature
    ``(plan_name: str) -> list[dict]``."""
    sig = inspect.signature(p.list_ready_stories)
    params = list(sig.parameters)
    assert params == ["plan_name"], (
        f"module-level tool params must be ['plan_name'], got {params}"
    )
    assert sig.parameters["plan_name"].annotation is str
    assert sig.parameters["plan_name"].default is inspect.Parameter.empty
    assert sig.return_annotation == list[dict]


def test_module_level_list_ready_stories_docstring_unchanged():
    """R3: the docstring is preserved byte-for-byte on the module-level tool."""
    doc = p.list_ready_stories.__doc__
    assert doc is not None, "module-level tool must keep its docstring"
    # The original docstring text (verbatim from the pre-move function).
    expected = (
        "Return stories whose dependencies are satisfied and that are still in\n"
        "    To Do. Use this to decide what to dispatch next.\n    "
    )
    assert doc.strip() == expected.strip(), (
        "module-level list_ready_stories docstring changed:\n" + repr(doc)
    )


def test_module_level_list_ready_stories_body_is_single_delegation():
    """C2: the module-level tool body is exactly one statement delegating to
    ``_service.list_ready_stories(plan_name)``."""
    src = inspect.getsource(p.list_ready_stories)
    # Strip the def line + docstring, look at the executable body.
    # The body must contain exactly one return statement delegating.
    body_lines = [
        ln for ln in src.splitlines()
        if ln.strip() and not ln.strip().startswith('"""')
        and not ln.strip().startswith("def list_ready_stories")
        and not ln.strip().startswith("@mcp.tool")
    ]
    # Filter out the docstring continuation lines (lines that are part of the
    # triple-quoted block). Inspect.getsource keeps the docstring as a string
    # literal line; remove pure-string lines.
    non_doc = [
        ln for ln in body_lines
        if not ln.strip().startswith("Return stories")
        and not ln.strip().startswith("To Do.")
        and not ln.strip().startswith("Use this")
    ]
    # The only executable statement should be the delegation return.
    statements = [ln for ln in non_doc if ln.strip().startswith("return")]
    assert len(statements) == 1, (
        "module-level tool must have exactly one return statement, got: "
        + repr(statements)
    )
    assert "return _service.list_ready_stories(plan_name)" in src, (
        "module-level tool body must be exactly "
        "`return _service.list_ready_stories(plan_name)`"
    )


# ---------- C3: exactly two definitions ----------

def test_exactly_two_list_ready_stories_definitions():
    """C3: ``grep -c "def list_ready_stories" pipeline/server.py`` == 2."""
    server_src = inspect.getsource(p)
    count = len(re.findall(r"\bdef list_ready_stories\b", server_src))
    assert count == 2, (
        f"expected exactly 2 `def list_ready_stories` (method + tool), got {count}"
    )


# ---------- C4: still MCP-registered ----------

def test_list_ready_stories_is_a_public_mcp_tool():
    """C4: the tool is still registered with the MCP tool manager."""
    tool_names = {t.name for t in p.mcp._tool_manager.list_tools()}
    assert "list_ready_stories" in tool_names, (
        "list_ready_stories must be decorated with @mcp.tool() to be callable "
        "as an MCP tool"
    )


# ---------- C5 / R1 / R2: free variables stay free, no self. globals ----------

def test_new_method_has_no_self_access_to_module_globals_or_helpers():
    """C5/R1/R2: inside the new method, no module global or helper is accessed
    via ``self.``. The only ``self`` reference is the method's own receiver
    parameter."""
    src = inspect.getsource(p.PipelineService.list_ready_stories)
    # Remove the `def ... (self, ...)` line so the receiver param doesn't trip
    # the check.
    body = src.split(":", 1)[1]
    self_dot = re.findall(r"self\.", body)
    assert self_dot == [], (
        "no `self.` access to module globals/helpers allowed inside the moved "
        "method (R1/R2); found: " + repr(self_dot)
    )


def test_new_method_reads_plan_dir_as_free_variable():
    """R1: ``PLAN_DIR`` must remain a bare (free) name inside the method, not
    copied onto self. We assert the source references the bare global."""
    src = inspect.getsource(p.PipelineService.list_ready_stories)
    assert re.search(r"\bPLAN_DIR\b", src), (
        "method must reference the module global `PLAN_DIR` as a bare name"
    )
    assert "self.PLAN_DIR" not in src, (
        "PLAN_DIR must NOT be accessed via self (R1)"
    )


def test_new_method_calls_validate_key_as_free_function():
    """R1/R4: ``_validate_key`` must remain a bare module-level call and be the
    first executable statement (entry validation stays first)."""
    src = inspect.getsource(p.PipelineService.list_ready_stories)
    assert re.search(r"\b_validate_key\(plan_name\)", src), (
        "method must call `_validate_key(plan_name)` as a bare name"
    )
    assert "self._validate_key" not in src

    # R4: _validate_key must be the FIRST executable statement of the body.
    # Collect non-blank, non-comment, non-docstring lines after the def.
    lines = src.splitlines()
    # Find the def line index.
    def_idx = next(i for i, ln in enumerate(lines) if "def list_ready_stories" in ln)
    body_lines = []
    for ln in lines[def_idx + 1:]:
        s = ln.strip()
        if not s or s.startswith(("#", '"""', "Return stories", "To Do.", "Use this")):
            continue
        body_lines.append(s)
    assert body_lines, "method body has no executable statements"
    assert body_lines[0].startswith("_validate_key(plan_name)"), (
        "R4: `_validate_key(plan_name)` must be the FIRST executable statement; "
        "got: " + repr(body_lines[0])
    )


def test_new_method_calls_completed_dep_ids_as_free_function():
    """R1: ``_completed_dep_ids`` must remain a bare module-level call."""
    src = inspect.getsource(p.PipelineService.list_ready_stories)
    assert re.search(r"\b_completed_dep_ids\(", src), (
        "method must call `_completed_dep_ids(...)` as a bare name"
    )
    assert "self._completed_dep_ids" not in src


def test_new_method_does_not_route_other_tools_through_self():
    """R2: the body must not call other tools via ``self.<tool>(...)``."""
    src = inspect.getsource(p.PipelineService.list_ready_stories)
    forbidden = [
        "self.dispatch_story", "self.interrupt_story", "self.review_story",
        "self.check_story_status", "self.advance_pipeline",
        "self._advance_pipeline_locked", "self._original_review_story",
    ]
    hits = [name for name in forbidden if name in src]
    assert hits == [], (
        "R2: must not route other tools through self; found: " + repr(hits)
    )


# ---------- R3: decorator stays on the module-level function ----------

def test_decorator_stays_on_module_level_function_not_method():
    """R3: the ``@mcp.tool()`` decorator must be on the module-level function,
    NOT on the method. The method must have no ``@mcp.tool()`` decorator."""
    method_src = inspect.getsource(p.PipelineService.list_ready_stories)
    # The method source block (as returned by inspect.getsource on a method
    # defined inside a class) does not include preceding decorators that sit
    # above the def at the same indent -- but to be safe, assert no mcp.tool
    # decorator appears in the method's own source lines at method indent.
    assert "@mcp.tool" not in method_src, (
        "R3: the @mcp.tool() decorator must NOT be on the method"
    )

    # The module-level function must still carry the decorator. We check the
    # full module source for the decorator immediately above the module-level
    # def (top-level, no leading whitespace).
    server_src = inspect.getsource(p)
    # Find the module-level def (no leading whitespace before 'def').
    lines = server_src.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("def list_ready_stories("):
            # The immediately preceding non-blank line must be @mcp.tool().
            prev = i - 1
            while prev >= 0 and lines[prev].strip() == "":
                prev -= 1
            assert prev >= 0 and lines[prev].strip() == "@mcp.tool()", (
                "R3: module-level `def list_ready_stories` must be preceded by "
                "`@mcp.tool()` decorator; found preceding line: "
                + repr(lines[prev] if prev >= 0 else None)
            )
            break
    else:
        pytest.fail("module-level `def list_ready_stories(` not found in source")


# ---------- Behaviour: happy path (unchanged) ----------

def test_list_ready_stories_returns_stories_with_satisfied_deps(plan_dir):
    """Happy path: a todo story whose deps are all done is returned with its
    key and summary."""
    _write_manifest(plan_dir, "happy", {
        "s1": {"summary": "Foundation", "status": "done", "dependencies": []},
        "s2": {"summary": "Walls", "status": "todo", "dependencies": ["s1"]},
        "s3": {"summary": "Roof", "status": "todo", "dependencies": ["s1", "s2"]},
    })
    ready = p.list_ready_stories("happy")
    assert ready == [{"key": "s2", "summary": "Walls"}]


def test_list_ready_stories_empty_when_no_manifest(plan_dir):
    """Boundary: missing manifest returns ``[]`` (no error)."""
    assert p.list_ready_stories("nope") == []


def test_list_ready_stories_empty_when_no_todo_stories(plan_dir):
    """Boundary: all stories done -> empty list."""
    _write_manifest(plan_dir, "alldone", {
        "s1": {"summary": "A", "status": "done", "dependencies": []},
        "s2": {"summary": "B", "status": "done", "dependencies": ["s1"]},
    })
    assert p.list_ready_stories("alldone") == []


def test_list_ready_stories_empty_when_deps_unmet(plan_dir):
    """Boundary: a todo story whose deps are NOT done is excluded."""
    _write_manifest(plan_dir, "blocked", {
        "s1": {"summary": "A", "status": "todo", "dependencies": []},
        "s2": {"summary": "B", "status": "todo", "dependencies": ["s1"]},
    })
    # s1 has no deps -> ready. s2 depends on s1 which is still todo -> not ready.
    ready = p.list_ready_stories("blocked")
    assert ready == [{"key": "s1", "summary": "A"}]


def test_list_ready_stories_empty_deps_means_ready(plan_dir):
    """Boundary: a todo story with an empty dependencies list is ready."""
    _write_manifest(plan_dir, "emptydeps", {
        "s1": {"summary": "Solo", "status": "todo", "dependencies": []},
    })
    assert p.list_ready_stories("emptydeps") == [{"key": "s1", "summary": "Solo"}]


def test_list_ready_stories_resolves_summary_dependencies(plan_dir):
    """Dependencies expressed as a prerequisite's summary string must resolve
    against done stories even when the manifest is keyed by UUID."""
    _write_manifest(plan_dir, "sdep", {
        "uuid-a": {"summary": "Foundation", "status": "done", "dependencies": []},
        "uuid-b": {"summary": "Builds on foundation", "status": "todo",
                   "dependencies": ["Foundation"]},
        "uuid-c": {"summary": "Blocked", "status": "todo",
                   "dependencies": ["Builds on foundation"]},
    })
    ready = p.list_ready_stories("sdep")
    assert [r["summary"] for r in ready] == ["Builds on foundation"]


# ---------- Behaviour: negative / boundary (unchanged) ----------

def test_list_ready_stories_rejects_traversal(plan_dir):
    """N1/R4: path-traversal rejection still fires at the same point
    (``_validate_key`` is the first statement)."""
    with pytest.raises(ValueError, match="invalid"):
        p.list_ready_stories("../evil")


def test_list_ready_stories_rejects_other_traversal(plan_dir):
    """N1: another traversal variant still raises ValueError mentioning
    'invalid'."""
    with pytest.raises(ValueError, match="invalid"):
        p.list_ready_stories("..%2fevil")


# ---------- R1 regression detector: monkeypatched global takes effect ----------

def test_monkeypatched_plan_dir_takes_effect_through_method(plan_dir, tmp_path, monkeypatch):
    """N2/R1: a monkeypatched module global (``PLAN_DIR``) must still take
    effect through the new call path. If the method held a copy of PLAN_DIR on
    self, this patch would silently stop applying and the test would read the
    wrong directory."""
    # Point PLAN_DIR at a *different* directory with its own manifest.
    other = tmp_path / "other_plans"
    other.mkdir()
    _write_manifest(other, "patched", {
        "x": {"summary": "Patched-only", "status": "todo", "dependencies": []},
    })
    monkeypatch.setattr(p, "PLAN_DIR", other)

    ready = p.list_ready_stories("patched")
    assert ready == [{"key": "x", "summary": "Patched-only"}], (
        "monkeypatched PLAN_DIR did not take effect through the moved method (R1)"
    )


# ---------- C9: only pipeline/server.py changed (structural sanity) ----------

def test_service_singleton_exists():
    """The module-level ``_service = PipelineService()`` singleton must exist
    so the module-level tool can delegate to it."""
    assert isinstance(p._service, p.PipelineService), (
        "module-level `_service` must be a PipelineService instance"
    )


def test_module_level_tool_delegates_to_singleton(plan_dir):
    """C2: the module-level tool delegates to ``_service`` (the singleton),
    not to a freshly constructed instance."""
    _write_manifest(plan_dir, "deleg", {
        "d1": {"summary": "Deleg", "status": "todo", "dependencies": []},
    })
    # Spy on the singleton method to confirm the tool routes through it.
    seen = {}
    original = p._service.list_ready_stories

    def spy(plan_name):
        seen["called"] = True
        seen["arg"] = plan_name
        return original(plan_name)

    p._service.list_ready_stories = spy
    try:
        result = p.list_ready_stories("deleg")
    finally:
        p._service.list_ready_stories = original

    assert seen.get("called") is True, (
        "module-level tool must delegate to _service.list_ready_stories"
    )
    assert seen.get("arg") == "deleg"
    assert result == [{"key": "d1", "summary": "Deleg"}]