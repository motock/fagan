"""Tests for the W1a migration of the ``list_plans`` MCP tool onto ``PipelineService``.

These tests verify the *structural* move described by the story
"W1a: migrate list_plans onto PipelineService" -- they assert the
mechanically-checkable requirements (C1-C9, R1-R8) without inventing new
behavioural tests for logic that already has coverage elsewhere.

The implementation does not exist yet, so this file is expected to be RED
(import/attribute errors) until a later dispatch adds
``PipelineService.list_plans`` and shrinks the module-level tool to a
one-line delegation.
"""

import inspect

from pipeline import server as p


# ---------------------------------------------------------------------------
# C1 / C3 -- PipelineService.list_plans method exists and is unique
# ---------------------------------------------------------------------------

def test_pipelineservice_has_list_plans_method():
    """C1: ``PipelineService`` must define ``list_plans`` as a method."""
    assert hasattr(p.PipelineService, "list_plans"), (
        "PipelineService must define a `list_plans` method"
    )
    method = inspect.getattr_static(p.PipelineService, "list_plans")
    assert inspect.isfunction(method), (
        "PipelineService.list_plans must be a plain function/method, not a descriptor"
    )


def test_list_plans_method_takes_only_self():
    """C1: the method takes ``self`` plus the original parameters. The original
    tool takes no arguments, so the method signature is exactly ``(self)``."""
    sig = inspect.signature(p.PipelineService.list_plans)
    params = list(sig.parameters)
    assert params == ["self"], (
        f"PipelineService.list_plans must take only `self`, got {params}"
    )


def test_list_plans_method_return_annotation_is_list_of_str():
    """C1: the method preserves the original ``-> list[str]`` return type."""
    sig = inspect.signature(p.PipelineService.list_plans)
    assert sig.return_annotation is list[str] or sig.return_annotation == list[str], (
        f"PipelineService.list_plans return annotation must be list[str], "
        f"got {sig.return_annotation!r}"
    )


def test_exactly_two_list_plans_definitions():
    """C3: ``grep -c 'def list_plans'`` must return exactly 2 -- the method on
    PipelineService plus the module-level @mcp.tool() wrapper."""
    source = inspect.getsource(p)
    count = source.count("def list_plans")
    assert count == 2, (
        f"Expected exactly 2 `def list_plans` definitions (method + tool), "
        f"found {count}"
    )


# ---------------------------------------------------------------------------
# C2 -- module-level @mcp.tool() wrapper still exists, unchanged signature
# ---------------------------------------------------------------------------

def test_module_level_list_plans_still_exists():
    """C2: the module-level ``list_plans`` callable must still exist."""
    assert hasattr(p, "list_plans"), "module-level list_plans must still exist"
    assert callable(p.list_plans)


def test_module_level_list_plans_signature_unchanged():
    """C2: the module-level tool keeps its original no-arg signature and
    ``-> list[str]`` return annotation."""
    sig = inspect.signature(p.list_plans)
    assert list(sig.parameters) == [], (
        f"module-level list_plans must take no parameters, got {list(sig.parameters)}"
    )
    assert sig.return_annotation is list[str] or sig.return_annotation == list[str], (
        f"module-level list_plans return annotation must be list[str], "
        f"got {sig.return_annotation!r}"
    )


def test_module_level_list_plans_docstring_unchanged():
    """C2/R3: the module-level tool's docstring must be byte-for-byte
    unchanged: ``List saved plans available for ingestion.``"""
    assert p.list_plans.__doc__ == "List saved plans available for ingestion.", (
        f"list_plans docstring changed: {p.list_plans.__doc__!r}"
    )


def test_module_level_list_plans_body_is_single_delegation():
    """C2: the module-level tool's executable body must be exactly one
    statement delegating to ``_service.list_plans()``."""
    src = inspect.getsource(p.list_plans)
    # Strip the decorator line(s), def line and docstring, leaving the body.
    lines = src.splitlines()
    body_lines = []
    seen_docstring = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("@"):
            continue
        if stripped.startswith("def list_plans"):
            continue
        if stripped.startswith('"""') or stripped.startswith("'''"):
            seen_docstring = not seen_docstring if not seen_docstring else seen_docstring
            continue
        if seen_docstring:
            continue
        if stripped == "":
            continue
        body_lines.append(stripped)
    assert body_lines == ["return _service.list_plans()"], (
        f"module-level list_plans body must be exactly "
        f"`return _service.list_plans()`, got {body_lines}"
    )


# ---------------------------------------------------------------------------
# C4 / R3 -- the tool is still MCP-registered via @mcp.tool() on the wrapper
# ---------------------------------------------------------------------------

def test_list_plans_is_a_public_mcp_tool():
    """C4/R3: ``list_plans`` must remain registered as an MCP tool. This fails
    if the ``@mcp.tool()`` decorator migrated onto the method instead of
    staying on the module-level wrapper."""
    tool_names = {t.name for t in p.mcp._tool_manager.list_tools()}
    assert "list_plans" in tool_names, (
        "list_plans must be decorated with @mcp.tool() on the module-level "
        "function to remain a public MCP tool"
    )


# ---------------------------------------------------------------------------
# R3 -- the decorator stays on the module-level function, NOT the method
# ---------------------------------------------------------------------------

def test_method_is_not_decorated_with_mcp_tool():
    """R3: the ``@mcp.tool()`` decorator must NOT be on the method. The method
    source must not contain an ``@mcp.tool()`` line."""
    src = inspect.getsource(p.PipelineService.list_plans)
    assert "@mcp.tool()" not in src, (
        "PipelineService.list_plans must not carry the @mcp.tool() decorator; "
        "it belongs on the module-level wrapper"
    )


def test_module_level_list_plans_is_decorated_with_mcp_tool():
    """R3: the module-level wrapper must still carry ``@mcp.tool()``."""
    src = inspect.getsource(p.list_plans)
    assert "@mcp.tool()" in src, (
        "module-level list_plans must retain the @mcp.tool() decorator"
    )


# ---------------------------------------------------------------------------
# R1 / R5 -- free variables stay free; no self.<global> in the method
# ---------------------------------------------------------------------------

def test_method_body_has_no_self_attribute_access_to_globals():
    """R1/C5: inside the new method, no module global or helper may be
    accessed through ``self.``. The only ``self`` allowed is the receiver
    parameter itself."""
    src = inspect.getsource(p.PipelineService.list_plans)
    # Remove the `def ... self ...` signature line before scanning so the
    # receiver parameter does not count.
    body = "\n".join(
        line for line in src.splitlines()
        if "def list_plans" not in line
    )
    assert "self." not in body, (
        "PipelineService.list_plans must not access any global/helper via "
        "`self.` -- free variables must stay free (R1)"
    )


def test_method_reads_plan_dir_as_free_variable():
    """R1/R5: the moved body must still reference ``PLAN_DIR`` as a bare name
    (so monkeypatch.setattr(p, 'PLAN_DIR', ...) keeps landing)."""
    src = inspect.getsource(p.PipelineService.list_plans)
    assert "PLAN_DIR" in src, (
        "PipelineService.list_plans must reference PLAN_DIR as a bare free variable"
    )
    assert "self.PLAN_DIR" not in src, (
        "PLAN_DIR must be a free variable, not self.PLAN_DIR (R1)"
    )


def test_pipelineservice_init_takes_only_self():
    """R1: ``PipelineService.__init__`` must not exist or must take nothing
    but ``self`` and set nothing (no constructor copies of globals)."""
    init = getattr(p.PipelineService, "__init__", None)
    if init is None or init is object.__init__:
        return
    sig = inspect.signature(init)
    assert list(sig.parameters) == ["self"], (
        f"PipelineService.__init__ must take only `self`, got {list(sig.parameters)}"
    )


# ---------------------------------------------------------------------------
# R5 -- the moved body is verbatim (no behaviour change / opportunistic edits)
# ---------------------------------------------------------------------------

def test_method_body_matches_original_logic():
    """R5: the moved body must be the original statement, re-indented by one
    level. The original body is::

        return [p.stem for p in PLAN_DIR.glob("*.json")]

    so the method body must contain that exact expression."""
    src = inspect.getsource(p.PipelineService.list_plans)
    assert "[p.stem for p in PLAN_DIR.glob(\"*.json\")]" in src, (
        "PipelineService.list_plans body must be moved verbatim: "
        "`return [p.stem for p in PLAN_DIR.glob('*.json')]`"
    )


def test_method_has_no_docstring():
    """R3/R5: the docstring must NOT be duplicated onto the method -- it stays
    on the module-level wrapper only."""
    method = p.PipelineService.list_plans
    assert method.__doc__ in (None, ""), (
        f"PipelineService.list_plans must not carry a docstring (R3), "
        f"got {method.__doc__!r}"
    )


# ---------------------------------------------------------------------------
# Behavioural parity -- the delegation actually works end-to-end
# ---------------------------------------------------------------------------

def test_list_plans_empty_when_no_manifests(plan_dir):
    """Happy path / boundary (empty collection): with no plan files, the tool
    returns an empty list through the new delegation path."""
    assert p.list_plans() == []


def test_list_plans_returns_plan_stems(plan_dir):
    """Happy path: with plan files on disk, the tool returns their stems via
    the delegation to ``_service.list_plans()``."""
    (plan_dir / "alpha.json").write_text("{}")
    (plan_dir / "beta.json").write_text("{}")

    result = p.list_plans()

    assert sorted(result) == ["alpha", "beta"]


def test_list_plans_ignores_non_json_files(plan_dir):
    """Boundary: only ``*.json`` files are globbed; other files are ignored."""
    (plan_dir / "real.json").write_text("{}")
    (plan_dir / "notes.md").write_text("not a plan")
    (plan_dir / "ignore.txt").write_text("nope")

    assert p.list_plans() == ["real"]


def test_list_plans_single_file(plan_dir):
    """Boundary (one element): a single plan file yields a one-element list."""
    (plan_dir / "solo.json").write_text("{}")
    assert p.list_plans() == ["solo"]


def test_list_plans_takes_no_arguments():
    """The tool takes no arguments, so the delegation is
    ``return _service.list_plans()`` with no args passed through."""
    sig = inspect.signature(p.list_plans)
    assert list(sig.parameters) == [], (
        "list_plans takes no arguments; the wrapper must call "
        "_service.list_plans() with no args"
    )


# ---------------------------------------------------------------------------
# R1 regression detector -- a monkeypatched module global takes effect
# ---------------------------------------------------------------------------

def test_list_plans_reads_monkeypatched_plan_dir(plan_dir, tmp_path, monkeypatch):
    """R1/N2: patching ``pipeline.server.PLAN_DIR`` to a different directory
    must still take effect through the new call path (the method reads
    ``PLAN_DIR`` as a free variable)."""
    other = tmp_path / "other_plans"
    other.mkdir()
    (other / "patched.json").write_text("{}")
    monkeypatch.setattr(p, "PLAN_DIR", other)

    assert p.list_plans() == ["patched"]


def test_service_list_plans_reads_monkeypatched_plan_dir(plan_dir, tmp_path, monkeypatch):
    """R1/N2: calling the method directly on the singleton ``_service`` must
    also honour a monkeypatched ``PLAN_DIR`` -- proving the free-variable
    binding resolves to the module dict at call time, not at construction."""
    other = tmp_path / "other_plans2"
    other.mkdir()
    (other / "direct.json").write_text("{}")
    monkeypatch.setattr(p, "PLAN_DIR", other)

    assert p._service.list_plans() == ["direct"]


# ---------------------------------------------------------------------------
# C9 -- only pipeline/server.py changed (structural guard via source shape)
# ---------------------------------------------------------------------------

def test_service_singleton_is_pipelineservice_instance():
    """The module-level ``_service`` must be a ``PipelineService`` instance so
    the delegation ``_service.list_plans()`` resolves to the new method."""
    assert isinstance(p._service, p.PipelineService), (
        "_service must be a PipelineService instance"
    )


def test_service_singleton_list_plans_callable():
    """The singleton must expose ``list_plans`` as a bound method."""
    assert callable(getattr(p._service, "list_plans", None)), (
        "_service.list_plans must be callable"
    )