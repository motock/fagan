"""Tests for W1a story: migrate ``decompose_plan`` MCP tool onto ``PipelineService``.

These tests assert the *structural* requirements of the migration described in the
story spec. They are intentionally self-contained and runnable against code that has
not yet been modified: before the move, ``PipelineService`` has no ``decompose_plan``
method, so the structural assertions fail for the right reason (the implementation is
missing), not because of a bug in the test logic.

The existing behavioural tests in ``test_pipeline_mcp_server.py`` remain the oracle
for behaviour; this file grades the *mechanics* of the move (method exists, tool is
still registered, free variables stay free, the tool body is a one-line delegation,
no behaviour change, only one file changed, etc.).
"""

import inspect
import json
import textwrap

import pytest

from pipeline import persona as pper
from pipeline import server as p

# ---------------------------------------------------------------------------
# Fixtures (local copies so this file is self-contained and does not depend on
# fixtures defined in test_pipeline_mcp_server.py).
# ---------------------------------------------------------------------------


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    d = tmp_path / "agents"
    d.mkdir()
    (d / "overlord.md").write_text(
        '---\nname: "overlord"\nmodel: opus\nmemory: user\n---\n\n'
        "You are the Overlord body text.\n"
    )
    (d / "software-engineer.md").write_text(
        '---\nname: "software-engineer"\nmodel: sonnet\n---\n\nEngineer body.\n'
    )
    (d / "code-reviewer.md").write_text(
        '---\nname: "code-reviewer"\nmodel: sonnet\n---\n\nReviewer body.\n'
    )
    (d / "product-analyst.md").write_text(
        '---\nname: "product-analyst"\nmodel: opus\n---\n\nAnalyst body.\n'
    )
    monkeypatch.setattr(p, "AGENTS_DIR", d)
    monkeypatch.setattr(pper, "AGENTS_DIR", d)
    return d
# ---------------------------------------------------------------------------
# C1: PipelineService defines decompose_plan as a method taking self + original
#     params, with the original type hints.
# ---------------------------------------------------------------------------


def _method_on_class():
    """Return the decompose_plan defined directly on PipelineService, or None."""
    return getattr(p.PipelineService, "decompose_plan", None)


def _tool_function():
    """Return the module-level decompose_plan callable, or None."""
    return getattr(p, "decompose_plan", None)


def test_method_exists_on_pipeline_service_class():
    """C1: PipelineService.decompose_plan must exist as a method on the class."""
    method = _method_on_class()
    assert method is not None, "PipelineService has no decompose_plan method"


def test_method_takes_self_and_request_with_original_signature():
    """C1: the method signature is (self, request: str) -> dict[str, Any]."""
    method = _method_on_class()
    if method is None:
        pytest.fail("PipelineService.decompose_plan does not exist yet")
    sig = inspect.signature(method)
    params = list(sig.parameters.keys())
    assert params[0] == "self", f"first param must be self, got {params[0]!r}"
    assert "request" in params, "method must keep the original 'request' parameter"
    assert params == ["self", "request"], f"unexpected params: {params}"
    # Original return annotation is dict[str, Any].
    ret = sig.return_annotation
    assert ret is not inspect.Signature.empty, "return annotation must be preserved"
    # typing.Any always stringifies with its module-qualified name at runtime.
    assert str(ret) == "dict[str, typing.Any]", f"return annotation changed: {ret!r}"
    # request param keeps its str annotation.
    req_ann = sig.parameters["request"].annotation
    assert req_ann is str or str(req_ann) == "str", (
        f"request annotation changed: {req_ann!r}"
    )


# ---------------------------------------------------------------------------
# C2 / C3: the module-level @mcp.tool() function still exists, unchanged
# signature + docstring, body is exactly one delegation statement.
# ---------------------------------------------------------------------------


def test_module_level_tool_function_still_exists():
    """C2: the module-level decompose_plan callable still exists."""
    assert _tool_function() is not None, "module-level decompose_plan disappeared"


def test_exactly_two_definitions_of_decompose_plan():
    """C3: exactly 2 'def decompose_plan' -- method on PipelineService in
    pipeline/service.py + @mcp.tool() wrapper in pipeline/server.py."""
    import pathlib
    server_text = pathlib.Path(p.__file__).read_text()
    service_text = pathlib.Path(p.__file__).with_name("service.py").read_text()
    count = server_text.count("def decompose_plan") + service_text.count("def decompose_plan")
    assert count == 2, (
        f"expected exactly 2 'def decompose_plan' (method in service.py "
        f"+ tool wrapper in server.py), got {count}"
    )


def test_tool_function_body_is_single_delegation():
    """C2: the module-level tool body is exactly
    ``return _service.decompose_plan(request)``."""
    fn = _tool_function()
    if fn is None:
        pytest.fail("module-level decompose_plan does not exist")
    source = inspect.getsource(fn)
    # Strip the decorator line(s) and the def line + docstring to isolate the body.
    # The body must contain exactly one executable statement delegating to _service.
    assert "_service.decompose_plan(request)" in source, (
        "tool body must delegate to _service.decompose_plan(request)"
    )
    # Count executable (non-docstring, non-decorator, non-def) statements.
    tree = inspect.getsource(fn)
    # Remove the docstring block (triple-quoted) so it isn't counted as a statement.
    import ast

    mod = ast.parse(textwrap.dedent(tree))
    func = mod.body[0]
    assert isinstance(func, ast.FunctionDef)
    # Collect statements that are not the docstring.
    stmts = []
    for node in func.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            # docstring expression
            continue
        stmts.append(node)
    assert len(stmts) == 1, (
        f"tool body must be exactly one statement, got {len(stmts)}: "
        f"{[ast.dump(s) for s in stmts]}"
    )
    only = stmts[0]
    assert isinstance(only, ast.Return), "the single statement must be a return"
    assert isinstance(only.value, ast.Call), "the return must be a call"
    # _service.decompose_plan(request)
    call = only.value
    assert isinstance(call.func, ast.Attribute), "must call an attribute (_service.x)"
    assert call.func.attr == "decompose_plan", "must call .decompose_plan"
    assert isinstance(call.func.value, ast.Name), "receiver must be a bare name"
    assert call.func.value.id == "_service", "receiver must be _service"
    assert len(call.args) == 1, "delegation must pass exactly one positional arg"
    assert isinstance(call.args[0], ast.Name), "arg must be a bare name"
    assert call.args[0].id == "request", "arg must be 'request'"


def test_tool_function_signature_unchanged():
    """C2: the module-level tool keeps its original signature (request: str) -> dict."""
    fn = _tool_function()
    if fn is None:
        pytest.fail("module-level decompose_plan does not exist")
    sig = inspect.signature(fn)
    params = list(sig.parameters.keys())
    assert params == ["request"], f"tool signature changed: {params}"
    assert sig.parameters["request"].annotation is str or str(
        sig.parameters["request"].annotation
    ) in ("str", "<class 'str'>")
    assert str(sig.return_annotation) in ("dict[str, Any]", "dict[str, typing.Any]")


def test_tool_function_docstring_preserved_byte_for_byte():
    """C3: the tool's full docstring is preserved unchanged."""
    fn = _tool_function()
    if fn is None:
        pytest.fail("module-level decompose_plan does not exist")
    doc = fn.__doc__
    assert doc is not None, "tool docstring was dropped"
    # Key phrases that must survive verbatim (from the original docstring).
    assert "Turn a raw goal/feature request into epics/stories JSON" in doc
    assert "product-analyst persona" in doc
    assert "Does NOT call save_plan itself" in doc
    assert 'Returns {"ok": True, "plan": {...}} on success' in doc
    assert "never\nraises" in doc or "never raises" in doc


def test_tool_function_still_decorated_with_mcp_tool():
    """C3 / C4: the @mcp.tool() decorator stays on the module-level function."""
    source = inspect.getsource(_tool_function())
    # inspect.getsource includes preceding decorators.
    assert "@mcp.tool()" in source, "@mcp.tool() decorator missing from tool function"


# ---------------------------------------------------------------------------
# C4: the tool is still MCP-registered.
# ---------------------------------------------------------------------------


def test_tool_is_mcp_registered():
    """C4: 'decompose_plan' is in the registered MCP tool set."""
    names = {t.name for t in p.mcp._tool_manager.list_tools()}
    assert "decompose_plan" in names, "decompose_plan is not MCP-registered"


# ---------------------------------------------------------------------------
# C5 / R1: no self.<global> access inside the new method.
# ---------------------------------------------------------------------------


def test_method_has_no_self_attribute_access_to_globals():
    """C5 / R1: the new method must not access module globals/helpers via self."""
    method = _method_on_class()
    if method is None:
        pytest.fail("PipelineService.decompose_plan does not exist yet")
    source = inspect.getsource(method)
    # The only legitimate `self` usage is the receiver parameter itself.
    # No `self.<anything>` should appear in the body.
    # Remove the def line first so the `self` parameter isn't matched.
    lines = source.splitlines()
    body_lines = [ln for ln in lines if not ln.lstrip().startswith("def ")]
    body = "\n".join(body_lines)
    assert "self." not in body, (
        "method must not access globals/helpers through self; found 'self.' in body:\n"
        + body
    )


def test_method_calls_run_decompose_as_bare_name():
    """R1: _run_decompose must be called as a bare name, not self._run_decompose."""
    method = _method_on_class()
    if method is None:
        pytest.fail("PipelineService.decompose_plan does not exist yet")
    source = inspect.getsource(method)
    assert "_run_decompose(request)" in source, (
        "method must call _run_decompose(request) as a bare name"
    )
    assert "self._run_decompose" not in source, (
        "method must NOT call self._run_decompose (R1: free variables stay free)"
    )


def test_method_calls_extract_json_block_as_bare_name():
    """R1: _extract_json_block must remain a bare name."""
    method = _method_on_class()
    if method is None:
        pytest.fail("PipelineService.decompose_plan does not exist yet")
    source = inspect.getsource(method)
    assert "_extract_json_block" in source, "_extract_json_block call missing"
    assert "self._extract_json_block" not in source, (
        "_extract_json_block must stay a bare name, not self._extract_json_block"
    )


def test_pipeline_service_init_takes_no_extra_params():
    """R1: PipelineService.__init__ must not take params beyond self / set nothing."""
    init = getattr(p.PipelineService, "__init__", None)
    if init is None:
        # No custom __init__ -> object.__init__, which is fine.
        return
    sig = inspect.signature(init)
    params = list(sig.parameters.keys())
    # object.__init__ has (self, /, *args, **kwargs); a custom one must be just self.
    if init is object.__init__:
        return
    assert params == ["self"], (
        f"PipelineService.__init__ must take only self, got {params}"
    )


# ---------------------------------------------------------------------------
# R3: the decorator is NOT on the method.
# ---------------------------------------------------------------------------


def test_method_is_not_decorated_with_mcp_tool():
    """R3: the @mcp.tool() decorator must not be on the method."""
    method = _method_on_class()
    if method is None:
        pytest.fail("PipelineService.decompose_plan does not exist yet")
    source = inspect.getsource(method)
    assert "@mcp.tool()" not in source, (
        "@mcp.tool() must stay on the module-level function, not the method"
    )


def test_method_has_no_docstring():
    """R3: the docstring must NOT be duplicated onto the method."""
    method = _method_on_class()
    if method is None:
        pytest.fail("PipelineService.decompose_plan does not exist yet")
    assert method.__doc__ in (None, ""), (
        "docstring must not be duplicated onto the method (R3)"
    )


# ---------------------------------------------------------------------------
# R4: entry validation ordering. decompose_plan has no _validate_key call (it
#     takes free-text `request`, not a plan_name), so this is N/A -- but we
#     assert the body's first executable statement is the _run_decompose call,
#     preserving the original ordering.
# ---------------------------------------------------------------------------


def test_method_first_statement_is_run_decompose_call():
    """R4/R5: the first executable statement must be `text = _run_decompose(request)`,
    exactly as in the original body (no reordering)."""
    method = _method_on_class()
    if method is None:
        pytest.fail("PipelineService.decompose_plan does not exist yet")
    import ast

    source = inspect.getsource(method)
    mod = ast.parse(textwrap.dedent(source))
    func = mod.body[0]
    assert isinstance(func, ast.FunctionDef)
    first = func.body[0]
    # First statement: text = _run_decompose(request)
    assert isinstance(first, ast.Assign), (
        f"first statement must be an assignment, got {type(first).__name__}"
    )
    assert len(first.targets) == 1
    assert isinstance(first.targets[0], ast.Name)
    assert first.targets[0].id == "text", "first assignment target must be 'text'"
    assert isinstance(first.value, ast.Call)
    assert isinstance(first.value.func, ast.Name)
    assert first.value.func.id == "_run_decompose", (
        "first statement must call bare _run_decompose"
    )


# ---------------------------------------------------------------------------
# R5: no behaviour change -- the method body must be the original body,
#     re-indented by one level. We compare the AST of the method body against
#     the AST of the original tool body (minus the delegation line).
# ---------------------------------------------------------------------------


def test_method_body_matches_original_logic():
    """R5: the moved body must be byte-identical logic to the original (no
    rewrites). Compares the method's AST against a frozen dump of the
    pre-migration module-level body (the tool body is now just a one-line
    delegation, so it can no longer serve as the comparison target)."""
    method = _method_on_class()
    if method is None:
        pytest.fail("PipelineService.decompose_plan does not exist yet")
    import ast

    method_src = textwrap.dedent(inspect.getsource(method))
    method_tree = ast.parse(method_src).body[0]
    method_stmts = [
        n for n in method_tree.body
        if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str))
    ]
    method_dump = "\n".join(ast.dump(s) for s in method_stmts)

    original_src = textwrap.dedent('''
        text = _run_decompose(request)
        if not text:
            return {"ok": False, "error": "decompose backend returned no output"}
        candidate = _extract_json_block(text)
        try:
            plan = json.loads(candidate)
        except json.JSONDecodeError as e:
            return {"ok": False, "error": f"invalid JSON: {e}", "raw": text}
        if not isinstance(plan, dict) or not isinstance(plan.get("epics"), list):
            return {
                "ok": False,
                "error": "response JSON is missing an 'epics' list",
                "raw": text,
            }
        return {"ok": True, "plan": plan}
    ''')
    original_stmts = ast.parse(original_src).body
    original_dump = "\n".join(ast.dump(s) for s in original_stmts)

    assert method_dump == original_dump, (
        "method body logic differs from the original tool body (R5: move verbatim)\n"
        f"--- method ---\n{method_dump}\n--- original ---\n{original_dump}\n"
    )


# ---------------------------------------------------------------------------
# Behaviour preserved through the new call path (N2: monkeypatched module
# global still takes effect). These mirror the existing behavioural tests but
# route through the *method* on the singleton to prove the patch lands.
# ---------------------------------------------------------------------------


def _patch_run_decompose(monkeypatch, return_value):
    monkeypatch.setattr(p, "_run_decompose", lambda request, **k: return_value)


def test_method_happy_path_through_singleton(agents_dir, monkeypatch):
    """N2/R1: a monkeypatched module global (_run_decompose) takes effect when
    called through _service.decompose_plan -- proving free-variable resolution."""
    plan_json = json.dumps({"epics": [{"summary": "E1", "stories": []}]})
    _patch_run_decompose(monkeypatch, f"```json\n{plan_json}\n```")

    result = p._service.decompose_plan("Build a CLI todo app.")

    assert result["ok"] is True
    assert result["plan"]["epics"][0]["summary"] == "E1"


def test_method_malformed_json_preserves_raw(agents_dir, monkeypatch):
    """N3: error shape byte-identical through the method path."""
    _patch_run_decompose(monkeypatch, "not json at all")

    result = p._service.decompose_plan("Build a CLI todo app.")

    assert result["ok"] is False
    assert "raw" in result
    assert result["raw"] == "not json at all"


def test_method_rejects_missing_epics_list(agents_dir, monkeypatch):
    """N3: missing-epics error shape preserved through the method path."""
    _patch_run_decompose(monkeypatch, '{"not_epics": []}')

    result = p._service.decompose_plan("Build a CLI todo app.")

    assert result["ok"] is False
    assert "epics" in result["error"]


def test_method_fails_open_on_none_backend(agents_dir, monkeypatch):
    """N3: None backend -> ok=False, never raises, through the method path."""
    _patch_run_decompose(monkeypatch, None)

    result = p._service.decompose_plan("Build a CLI todo app.")

    assert result["ok"] is False


def test_tool_delegates_to_method_result(agents_dir, monkeypatch):
    """C2: the module-level tool and the method produce identical results
    (the tool is a pure delegation)."""
    plan_json = json.dumps({"epics": [{"summary": "E1", "stories": []}]})
    _patch_run_decompose(monkeypatch, f"```json\n{plan_json}\n```")

    via_tool = p.decompose_plan("Build a CLI todo app.")
    via_method = p._service.decompose_plan("Build a CLI todo app.")

    assert via_tool == via_method


# ---------------------------------------------------------------------------
# R8 / C9: only pipeline/server.py changed (this is a static check on the
# working tree at test time; the implementer's diff is what matters, but we
# assert the production file is the one that carries the method).
# ---------------------------------------------------------------------------


def test_method_defined_in_pipeline_service_module():
    """R8: the method is defined in pipeline/service.py (PipelineService's home)."""
    method = _method_on_class()
    if method is None:
        pytest.fail("PipelineService.decompose_plan does not exist yet")
    assert method.__module__ == "pipeline.service", (
        f"method must live in pipeline.service, got {method.__module__!r}"
    )


def test_singleton_is_pipeline_service_instance():
    """The module-level _service is a PipelineService (delegation target)."""
    assert isinstance(p._service, p.PipelineService)