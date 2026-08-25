"""Structural-migration tests for the ``get_role_config`` MCP tool onto
``PipelineService`` (story W1a: migrate get_role_config).

These tests assert the *mechanical-move* contract described in the story:

* C1: ``PipelineService`` defines ``get_role_config`` as a method taking
  ``self`` plus the original parameters (``plan_name: str | None = None``)
  with the original type hints and the preserved default.
* C2: the module-level ``@mcp.tool()``-decorated ``def get_role_config`` still
  exists at its original position with unchanged signature/docstring and a
  one-line delegation body ``return _service.get_role_config(plan_name)``.
* C3: ``grep -c "def get_role_config" pipeline/server.py`` == 2.
* C4: the tool is still MCP-registered.
* C5: no ``self.`` access to module globals/helpers inside the new method.
* R1/R2: free variables stay free; internal calls stay module-level.
* R3: decorator/signature/docstring stay on the module-level function.
* Behaviour (happy path + plan role_config layering + monkeypatched globals)
  unchanged.

The implementation does not exist yet, so this suite is RED until the move is
performed. Run with the project venv::

    cd ~/.claude/mcp-servers/pipeline && .venv/bin/python -m pytest -q \
        tests/unit/test_get_role_config_migration.py
"""

import ast
import inspect
import json
import pathlib
import re

import pytest

from app import role_registry
from pipeline import persistence as ppers
from pipeline import persona as pper
from pipeline import server as p

# ---------- shared helpers (local copies so this file is self-contained) ----------

@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    d = tmp_path / "agents"
    d.mkdir()
    (d / "overlord.md").write_text(
        '---\nname: "overlord"\nmodel: opus\nmemory: user\n---\n\n'
        "You are the Overlord body text.\n"
    )
    (d / "code-reviewer.md").write_text(
        '---\nname: "code-reviewer"\nmodel: sonnet\n---\n\nReviewer body.\n'
    )
    (d / "product-analyst.md").write_text(
        '---\nname: "product-analyst"\nmodel: opus\n---\n\nAnalyst body.\n'
    )
    monkeypatch.setattr(p, "AGENTS_DIR", d)
    # pipeline_persona imports AGENTS_DIR from pipeline_paths at module load
    # and reads it as a free var, so patches must land on its own binding too.
    monkeypatch.setattr(pper, "AGENTS_DIR", d)
    return d


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    return d


# ---------- C1: PipelineService.get_role_config method shape ----------

def test_pipeline_service_has_get_role_config_method():
    """C1: ``PipelineService`` must define ``get_role_config`` as a method."""
    assert hasattr(p.PipelineService, "get_role_config"), (
        "PipelineService must define a `get_role_config` method"
    )
    assert inspect.isfunction(p.PipelineService.get_role_config), (
        "PipelineService.get_role_config must be a plain function/method, "
        "not a property or descriptor"
    )


def test_pipeline_service_get_role_config_takes_self_plus_original_params():
    """C1: the method signature is ``(self, plan_name: str | None = None) -> dict[str, Any]``."""
    sig = inspect.signature(p.PipelineService.get_role_config)
    params = list(sig.parameters)
    assert params == ["self", "plan_name"], (
        f"method params must be ['self', 'plan_name'], got {params}"
    )
    # self has no default; plan_name keeps the original None default.
    assert sig.parameters["self"].default is inspect.Parameter.empty, (
        "self must have no default"
    )
    assert sig.parameters["plan_name"].default is None, (
        "plan_name must keep its `= None` default (the tool's default is preserved)"
    )
    # plan_name keeps the original annotation: str | None
    ann = sig.parameters["plan_name"].annotation
    assert ann == (str | None) or str(ann) in ("str | None", "typing.Optional[str]"), (
        f"plan_name annotation must be `str | None`, got {ann!r}"
    )
    # Return annotation is dict[str, Any].
    ret = sig.return_annotation
    assert ret is not inspect.Signature.empty, (
        "method must carry the original `-> dict[str, Any]` return annotation"
    )
    assert getattr(ret, "__origin__", None) is dict or ret is dict, (
        f"return annotation must be dict[str, Any], got {ret!r}"
    )


# ---------- C2: module-level tool function is a one-line delegation ----------

def test_module_level_get_role_config_still_exists():
    """C2: the module-level ``get_role_config`` callable still exists."""
    assert hasattr(p, "get_role_config"), (
        "module-level `get_role_config` must still exist on pipeline.server"
    )
    assert callable(p.get_role_config), (
        "module-level `get_role_config` must remain callable"
    )


def test_module_level_get_role_config_signature_unchanged():
    """C2/R3: the module-level function keeps its original signature & default."""
    sig = inspect.signature(p.get_role_config)
    params = list(sig.parameters)
    assert params == ["plan_name"], (
        f"module-level tool params must be ['plan_name'], got {params}"
    )
    assert sig.parameters["plan_name"].default is None, (
        "module-level plan_name must keep its `= None` default"
    )
    ann = sig.parameters["plan_name"].annotation
    assert ann == (str | None) or str(ann) in ("str | None", "typing.Optional[str]"), (
        f"module-level plan_name annotation must be `str | None`, got {ann!r}"
    )


def test_module_level_get_role_config_docstring_unchanged():
    """C2/R3: the module-level function keeps its full docstring byte-for-byte."""
    doc = p.get_role_config.__doc__
    assert doc is not None, "module-level get_role_config must keep its docstring"
    # Distinctive phrases from the original docstring, whitespace-normalized so
    # the original's line-wrapping (a multi-line docstring, preserved
    # byte-for-byte per R3) doesn't break a phrase match at a line break.
    normalized = " ".join(doc.split())
    assert "Show the resolved (provider, model) for every pipeline role" in normalized
    assert "overlord, planner, dispatch, review, decompose" in normalized
    assert "Pure read; makes no changes." in normalized
    assert "_resolve_planner_backend applies at actual dispatch time" in normalized


def test_module_level_get_role_config_body_is_single_delegation():
    """C2: the module-level function body is exactly
    ``return _service.get_role_config(plan_name)``."""
    src = inspect.getsource(p.get_role_config)
    func = ast.parse(src).body[0]
    body = func.body
    # Skip the docstring statement (a multi-line docstring is preserved
    # byte-for-byte per R3, so it must not be mistaken for executable code).
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    assert len(body) == 1, (
        f"module-level executable body must be exactly one statement, got {len(body)}"
    )
    assert isinstance(body[0], ast.Return), (
        "module-level body must be a single return statement"
    )
    assert ast.unparse(body[0]) == "return _service.get_role_config(plan_name)", (
        f"module-level body must be `return _service.get_role_config(plan_name)`, "
        f"got {ast.unparse(body[0])!r}"
    )


# ---------- C3: exactly two definitions ----------

def test_exactly_two_get_role_config_definitions():
    """C3: exactly 2 `def get_role_config` -- method on PipelineService in
    pipeline/service.py + @mcp.tool() wrapper in pipeline/server.py."""
    server_text = pathlib.Path(p.__file__).read_text()
    service_text = pathlib.Path(p.__file__).with_name("service.py").read_text()
    count = server_text.count("def get_role_config") + service_text.count("def get_role_config")
    assert count == 2, (
        f"expected exactly 2 `def get_role_config` (method in service.py "
        f"+ tool wrapper in server.py), got {count}"
    )


# ---------- C4: still MCP-registered ----------

def test_get_role_config_is_mcp_registered():
    """C4/R3: the tool is still registered with the MCP server (decorator
    stayed on the module-level function, not the method)."""
    names = {t.name for t in p.mcp._tool_manager.list_tools()}
    assert "get_role_config" in names, (
        "get_role_config must remain MCP-registered; the @mcp.tool() decorator "
        "must stay on the module-level function, not migrate onto the method"
    )


def test_mcp_registered_fn_is_module_level_get_role_config():
    """C4/R3: the registered tool's underlying fn is the module-level function,
    not the PipelineService method."""
    tools = p.mcp._tool_manager.list_tools()
    grc = next(t for t in tools if t.name == "get_role_config")
    assert grc.fn is p.get_role_config, (
        "the MCP-registered fn must be the module-level get_role_config, "
        "not PipelineService.get_role_config"
    )


# ---------- C5: no self. access to module globals/helpers ----------

def test_method_body_has_no_self_dot_global_access():
    """C5/R1: inside the new method, no module global or helper is accessed
    through ``self.`` -- they must remain bare names so monkeypatch.setattr
    against module globals keeps landing."""
    src = inspect.getsource(p.PipelineService.get_role_config)
    body_lines = [
        ln for ln in src.splitlines()
        if ln.strip() and not ln.strip().startswith("def ")
    ]
    offenders = [ln for ln in body_lines if "self." in ln]
    assert offenders == [], (
        "no `self.` may appear in the method body (free variables must stay "
        f"free); offending lines: {offenders!r}"
    )


# ---------- R1: monkeypatched module global still takes effect ----------

def test_monkeypatched_default_model_takes_effect_through_method(agents_dir, monkeypatch):
    """R1: a monkeypatched module global (``DEFAULT_MODEL``) read by the body
    must still take effect through the new call path -- proves free variables
    stayed free and were not copied onto ``self`` at construction."""
    monkeypatch.delenv("PIPELINE_BACKEND_PLANNER", raising=False)
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: {})

    monkeypatch.setattr(p, "DEFAULT_MODEL", "weird-fallback-model")
    result = p.get_role_config()
    # planner & dispatch fall back to DEFAULT_MODEL when nothing else is set.
    assert result["roles"]["planner"]["model"] == "weird-fallback-model", (
        "monkeypatched DEFAULT_MODEL must take effect through the new call path "
        "(R1: free variable must resolve to the module global at call time)"
    )
    assert result["roles"]["dispatch"]["model"] == "weird-fallback-model", (
        "monkeypatched DEFAULT_MODEL must take effect for dispatch too"
    )


def test_monkeypatched_plan_role_config_helper_takes_effect(plan_dir, agents_dir, monkeypatch):
    """R1: a monkeypatched module-level helper (``_plan_role_config``) called by
    the body must still take effect through the new call path."""
    monkeypatch.setattr(
        role_registry,
        "load_registry",
        lambda *a, **k: {"providers": {"mlx": {"models": {"qwen": {"tag": "qwen"}}}}},
    )
    monkeypatch.setattr(
        p,
        "_plan_role_config",
        lambda plan_name: {"review": {"provider": "mlx", "model": "qwen"}},
    )

    result = p.get_role_config(plan_name="anything")

    assert result["roles"]["review"]["provider"] == "mlx", (
        "monkeypatched _plan_role_config must take effect through the new call "
        "path (R1: helper must remain a bare module-level call)"
    )
    assert result["roles"]["review"]["model"] == "qwen"


# ---------- R2: internal calls stay module-level (no self.tool calls) ----------

def test_method_body_does_not_route_through_self_for_tools():
    """R2: the body must not call other tools through ``self.<tool>(...)``;
    internal calls stay module-level."""
    src = inspect.getsource(p.PipelineService.get_role_config)
    self_calls = re.findall(r"self\.\w+\s*\(", src)
    assert self_calls == [], (
        f"no self.<tool>(...) routing allowed in the method; found {self_calls!r}"
    )


# ---------- Behaviour: happy path unchanged ----------

def test_get_role_config_reports_all_five_roles_with_no_config(agents_dir, monkeypatch):
    """Happy path: with no config, all five roles report claude as provider."""
    for var in (
        "PIPELINE_BACKEND_OVERLORD", "PIPELINE_BACKEND_PLANNER",
        "PIPELINE_BACKEND_DISPATCH", "PIPELINE_BACKEND_REVIEW",
        "PIPELINE_BACKEND_DECOMPOSE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: {})

    result = p.get_role_config()

    assert result["ok"] is True
    assert set(result["roles"]) == {
        "overlord", "planner", "dispatch", "review", "decompose",
    }
    assert result["roles"]["overlord"]["provider"] == "claude"
    assert result["roles"]["review"]["provider"] == "claude"


def test_get_role_config_reflects_registry_override(agents_dir, monkeypatch):
    """Happy path: a registry role override is reflected in the result."""
    registry = {
        "providers": {
            "mlx": {"models": {"qwen": {"tag": "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"}}}
        },
        "roles": {"review": {"provider": "mlx", "model": "qwen"}},
    }
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: registry)
    monkeypatch.delenv("PIPELINE_BACKEND_REVIEW", raising=False)
    monkeypatch.delenv("PIPELINE_BACKEND_OVERLORD", raising=False)

    result = p.get_role_config()

    assert result["roles"]["review"] == {
        "provider": "mlx", "model": "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit",
    }
    assert result["roles"]["overlord"]["provider"] == "claude"


def test_get_role_config_reflects_plan_role_config(plan_dir, agents_dir, monkeypatch):
    """Happy path: a plan's role_config layers into the resolution."""
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: {})
    (plan_dir / "cfgplan.manifest.json").write_text(json.dumps({
        "epics": {}, "stories": {}, "repo_root": "/tmp",
        "role_config": {"overlord": {"provider": "ollama"}},
    }))

    result = p.get_role_config(plan_name="cfgplan")

    assert result["roles"]["overlord"]["provider"] == "ollama"


def test_get_role_config_default_plan_name_is_none(agents_dir, monkeypatch):
    """Boundary: calling with no argument (default None) must not consult any
    plan's role_config and must still return ok."""
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: {})

    result = p.get_role_config()

    assert result["ok"] is True
    assert set(result["roles"]) == {
        "overlord", "planner", "dispatch", "review", "decompose",
    }


# ---------- Behaviour: negative / boundary cases ----------

def test_get_role_config_unknown_plan_name_still_returns_ok(plan_dir, agents_dir, monkeypatch):
    """Boundary: an unknown plan_name yields an empty plan_role_config (manifest
    missing) rather than raising; resolution proceeds with fallbacks."""
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: {})

    result = p.get_role_config(plan_name="does-not-exist")

    assert result["ok"] is True
    assert set(result["roles"]) == {
        "overlord", "planner", "dispatch", "review", "decompose",
    }


def test_get_role_config_empty_plan_name_treated_as_no_plan(agents_dir, monkeypatch):
    """Boundary: an empty-string plan_name is falsy, so the body's
    ``if plan_name`` guard skips plan role_config lookup entirely (no
    FileNotFoundError on an empty manifest path)."""
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: {})

    result = p.get_role_config(plan_name="")

    assert result["ok"] is True
    assert set(result["roles"]) == {
        "overlord", "planner", "dispatch", "review", "decompose",
    }


def test_get_role_config_returns_dict_with_ok_and_roles_keys(agents_dir, monkeypatch):
    """Shape: the return value is a dict with exactly the ``ok`` and ``roles``
    top-level keys (byte-identical shape to before)."""
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: {})

    result = p.get_role_config()

    assert isinstance(result, dict)
    assert set(result) == {"ok", "roles"}
    assert isinstance(result["roles"], dict)
    for role, entry in result["roles"].items():
        assert set(entry) == {"provider", "model"}, (
            f"role {role!r} entry must have exactly provider/model keys, got {set(entry)}"
        )


# ---------- R3: decorator stays on the module-level function ----------

def test_decorator_not_on_method():
    """R3: the ``@mcp.tool()`` decorator must NOT be on the method. The method
    is a plain function on the class; the decorator stays on the module-level
    tool function."""
    method = p.PipelineService.get_role_config
    assert method.__qualname__ == "PipelineService.get_role_config", (
        f"method qualname must be PipelineService.get_role_config, got "
        f"{method.__qualname__!r}"
    )


# ---------- PipelineService.__init__ must not take params / set state (R1) ----------

def test_pipeline_service_init_takes_only_self():
    """R1: ``PipelineService.__init__`` must not exist or take nothing but
    ``self`` and set nothing -- so module globals are not frozen onto the
    instance at construction."""
    init = getattr(p.PipelineService, "__init__", None)
    if init is None or init is object.__init__:
        return  # no custom __init__ -- allowed
    sig = inspect.signature(init)
    params = list(sig.parameters)
    assert params == ["self"], (
        f"PipelineService.__init__ must take only self, got {params}"
    )


def test_service_singleton_is_pipeline_service_instance():
    """The module-level ``_service`` singleton must be a PipelineService
    instance (the delegation target)."""
    assert hasattr(p, "_service"), "module-level `_service` singleton must exist"
    assert isinstance(p._service, p.PipelineService), (
        f"_service must be a PipelineService instance, got {type(p._service)!r}"
    )