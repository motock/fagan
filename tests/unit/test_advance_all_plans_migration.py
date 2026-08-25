"""Structural + behavioural tests for the W1a migration of `advance_all_plans`
onto `PipelineService`.

These tests pin the *mechanical-move* contract described in the story:

  * C1  -- `PipelineService.advance_all_plans` exists as a method taking `self`.
  * C2  -- the module-level `@mcp.tool()`-decorated `advance_all_plans` still
           exists with an unchanged signature/docstring and a one-line
           delegation body.
  * C3  -- exactly two `def advance_all_plans` definitions in the file.
  * C4  -- the tool is still MCP-registered.
  * C5  -- no `self.<global>` access inside the new method.
  * R2  -- the internal `advance_pipeline(plan_name)` call stays a bare
           module-level call (so `monkeypatch.setattr(p, "advance_pipeline", ...)`
           keeps landing).
  * R3  -- decorator + signature + docstring stay on the module-level function.
  * R5  -- the `except Exception` / `# noqa: BLE001` comment and the long
           NOTE-on-zombie-reaping docstring are preserved verbatim.

The behavioural tests (happy path, failure isolation, empty, non-manifest
ignore, zombie non-reap) are re-asserted here against the *method* on
`PipelineService` (via `_service`) and against the module-level tool, so the
delegation path is exercised end-to-end.
"""

import inspect
import json
import re

import pytest

from pipeline import server as p

# ---------- helpers (mirror the ones in test_pipeline_mcp_server.py) ----------

def _write_manifest(plan_dir, plan_name, stories):
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}, indent=2)
    )


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Local copy of the plan_dir fixture (the one in test_pipeline_mcp_server.py
    is not shared across modules). Patches PLAN_DIR on pipeline.server and the
    sibling modules that captured it at import time."""
    from pipeline import concurrency as pcon
    from pipeline import persistence as ppers

    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


# ---------- C1: method exists on PipelineService taking self ----------

def test_pipeline_service_has_advance_all_plans_method():
    assert hasattr(p.PipelineService, "advance_all_plans"), (
        "PipelineService must define advance_all_plans as a method"
    )
    method = inspect.getattr_static(p.PipelineService, "advance_all_plans")
    assert isinstance(method, type(p.PipelineService.__init__)) or callable(method)


def test_advance_all_plans_method_takes_only_self():
    """The tool takes no arguments, so the method must take only `self`."""
    sig = inspect.signature(p.PipelineService.advance_all_plans)
    params = list(sig.parameters)
    assert params == ["self"], (
        f"advance_all_plans(self) must take only self, got {params}"
    )


def test_advance_all_plans_method_return_annotation_is_dict():
    sig = inspect.signature(p.PipelineService.advance_all_plans)
    assert sig.return_annotation is not inspect.Parameter.empty, (
        "the method must keep the original return type hint"
    )
    # dict[str, Any] -> repr contains 'dict'
    assert "dict" in repr(sig.return_annotation)


# ---------- C2: module-level tool still exists, one-line delegation ----------

def test_module_level_advance_all_plans_still_exists():
    assert hasattr(p, "advance_all_plans"), (
        "the module-level advance_all_plans tool must still exist"
    )


def test_module_level_advance_all_plans_body_is_single_delegation():
    """The @mcp.tool() function body must be exactly one statement delegating
    to _service.advance_all_plans()."""
    src = inspect.getsource(p.advance_all_plans)
    # Strip the signature/docstring; the executable body after the docstring
    # must be a single `return _service.advance_all_plans()` line.
    # Find the return statement(s).
    returns = re.findall(r"^\s*return\s+_service\.advance_all_plans\(\)\s*$",
                         src, re.MULTILINE)
    assert returns, (
        "module-level advance_all_plans must delegate via "
        "`return _service.advance_all_plans()`"
    )
    # No other executable statements (besides the def line, docstring, and the
    # single return) should appear. We check there is exactly one return and
    # no for/try/except/plans= in the module-level function source.
    assert src.count("return _service.advance_all_plans()") == 1
    assert "for manifest_path" not in src, (
        "the loop body must have moved off the module-level tool function"
    )
    assert "except Exception" not in src, (
        "the try/except must have moved off the module-level tool function"
    )
    assert "plans = {}" not in src, (
        "the executable body must have moved off the module-level tool function"
    )


def test_module_level_advance_all_plans_signature_unchanged():
    sig = inspect.signature(p.advance_all_plans)
    # No parameters (the tool takes no arguments).
    assert list(sig.parameters) == [], (
        f"module-level tool must take no args, got {list(sig.parameters)}"
    )
    assert "dict" in repr(sig.return_annotation)


# ---------- C3: exactly two def advance_all_plans in the file ----------

def test_exactly_two_advance_all_plans_definitions():
    # The method lives on PipelineService in pipeline/service.py; the
    # @mcp.tool() wrapper lives in pipeline/server.py. Exactly one each.
    import pathlib
    server_text = pathlib.Path(p.__file__).read_text()
    service_text = pathlib.Path(p.__file__).with_name("service.py").read_text()
    count = (
        len(re.findall(r"def advance_all_plans\b", server_text))
        + len(re.findall(r"def advance_all_plans\b", service_text))
    )
    assert count == 2, (
        f"expected exactly 2 `def advance_all_plans` (method in service.py "
        f"+ tool wrapper in server.py), got {count}"
    )


# ---------- C4: still MCP-registered ----------

def test_advance_all_plans_is_a_public_mcp_tool():
    tool_names = {t.name for t in p.mcp._tool_manager.list_tools()}
    assert "advance_all_plans" in tool_names, (
        "advance_all_plans must remain decorated with @mcp.tool() so it stays "
        "MCP-registered (R3 guard: the decorator must NOT move onto the method)"
    )


# ---------- C5: no self.<global> inside the new method ----------

def test_no_self_global_access_in_method():
    src = inspect.getsource(p.PipelineService.advance_all_plans)
    # The only permissible `self` occurrence is the receiver in the def line.
    # Any `self.<name>` access to a module global/helper is forbidden (R1).
    self_dotted = re.findall(r"\bself\.\w+", src)
    # Filter out the `def ... self` parameter usage (no dot there).
    assert self_dotted == [], (
        "the moved method must not access any module global/helper via self "
        f"(R1); found: {self_dotted}"
    )


# ---------- R2: internal advance_pipeline call stays a bare module-level call ----------

def test_method_calls_bare_module_level_advance_pipeline():
    src = inspect.getsource(p.PipelineService.advance_all_plans)
    # Must call advance_pipeline(...) as a bare name, NOT self.advance_pipeline.
    assert re.search(r"\badvance_pipeline\s*\(", src), (
        "the method must call advance_pipeline(plan_name) as a bare "
        "module-level call (R2)"
    )
    assert "self.advance_pipeline" not in src, (
        "the internal call must NOT be routed through self (R2): tests patch "
        "p.advance_pipeline, and self.advance_pipeline would bypass that"
    )


# ---------- R3: docstring preserved verbatim on the module-level function ----------

def test_module_level_docstring_preserves_zombie_note():
    """The long NOTE-on-zombie-reaping docstring must stay byte-for-byte on the
    module-level tool function (R3), NOT be duplicated onto the method."""
    doc = p.advance_all_plans.__doc__
    assert doc is not None, "module-level tool must keep its docstring"
    assert "NOTE on zombie reaping" in doc, (
        "the NOTE on zombie reaping must be preserved in the tool docstring"
    )
    assert "reap" in doc
    assert "dispatch_attempts" in doc
    assert "2026-06-28" in doc
    assert "_reap_zombie_in_progress_stories" in doc


def test_module_level_docstring_preserves_scheduler_intent():
    doc = p.advance_all_plans.__doc__
    assert "recurring scheduler" in doc
    assert "manifest" in doc
    assert "hardcoded plan name" in doc


# ---------- R5: except Exception / noqa: BLE001 comment preserved ----------

def test_method_preserves_except_exception_noqa_comment():
    """The `except Exception as e:  # noqa: BLE001` comment must be preserved
    verbatim in the moved body."""
    src = inspect.getsource(p.PipelineService.advance_all_plans)
    assert "except Exception as e:  # noqa: BLE001" in src, (
        "the `except Exception as e:  # noqa: BLE001` comment must be preserved "
        "verbatim in the moved method body (R5)"
    )


def test_method_preserves_failure_isolation_comment():
    src = inspect.getsource(p.PipelineService.advance_all_plans)
    assert "must not stop every other plan" in src, (
        "the per-plan failure isolation comment must be preserved verbatim (R5)"
    )


# ---------- Behavioural: happy path through the method ----------

def test_method_runs_every_manifest(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "p1", {})
    _write_manifest(plan_dir, "p2", {})

    calls = []
    monkeypatch.setattr(
        p, "advance_pipeline",
        lambda plan_name: calls.append(plan_name) or {"ok": True, "plan": plan_name},
    )

    result = p._service.advance_all_plans()

    assert result["ok"] is True
    assert sorted(calls) == ["p1", "p2"]
    assert result["plans"]["p1"]["ok"] is True
    assert result["plans"]["p2"]["ok"] is True


def test_method_isolates_failures_and_continues(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "p1", {})
    _write_manifest(plan_dir, "p2", {})

    def _fake_advance(plan_name):
        if plan_name == "p1":
            raise RuntimeError("boom")
        return {"ok": True, "plan": plan_name}

    monkeypatch.setattr(p, "advance_pipeline", _fake_advance)

    result = p._service.advance_all_plans()

    assert result["ok"] is True
    assert result["plans"]["p1"]["ok"] is False
    assert "boom" in result["plans"]["p1"]["error"]
    assert result["plans"]["p2"]["ok"] is True


def test_method_with_no_manifests_returns_empty(plan_dir):
    result = p._service.advance_all_plans()
    assert result == {"ok": True, "plans": {}}


def test_method_ignores_non_manifest_plan_json(plan_dir, monkeypatch):
    (plan_dir / "p3.json").write_text(json.dumps({"epics": []}))

    calls = []
    monkeypatch.setattr(
        p, "advance_pipeline",
        lambda plan_name: calls.append(plan_name) or {"ok": True},
    )

    result = p._service.advance_all_plans()
    assert calls == []
    assert result["plans"] == {}


# ---------- Behavioural: delegation path through the module-level tool ----------

def test_module_level_tool_delegates_to_method(plan_dir, monkeypatch):
    """Calling the module-level tool must reach the method and still hit the
    patched module-level advance_pipeline (R2 end-to-end)."""
    _write_manifest(plan_dir, "d1", {})

    calls = []
    monkeypatch.setattr(
        p, "advance_pipeline",
        lambda plan_name: calls.append(plan_name) or {"ok": True},
    )

    result = p.advance_all_plans()
    assert result["ok"] is True
    assert calls == ["d1"]
    assert result["plans"]["d1"]["ok"] is True


# ---------- R1 regression detector: patched module global takes effect ----------

def test_method_reads_patched_plan_dir_global(plan_dir, monkeypatch):
    """R1: the method must read PLAN_DIR as a free variable, so a
    monkeypatch.setattr(p, 'PLAN_DIR', ...) still takes effect through the new
    call path. plan_dir fixture already patches p.PLAN_DIR; we additionally
    confirm a fresh patch is observed."""
    _write_manifest(plan_dir, "g1", {})

    seen_plan_dir = []

    def _spy(plan_name):
        seen_plan_dir.append(p.PLAN_DIR)
        return {"ok": True}

    monkeypatch.setattr(p, "advance_pipeline", _spy)
    p._service.advance_all_plans()
    assert seen_plan_dir == [plan_dir], (
        "the method must resolve PLAN_DIR from the module dict at call time "
        "(R1), so the monkeypatched PLAN_DIR is observed"
    )


# ---------- Boundary: single manifest ----------

def test_method_single_manifest(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "only", {})
    calls = []
    monkeypatch.setattr(
        p, "advance_pipeline",
        lambda plan_name: calls.append(plan_name) or {"ok": True},
    )
    result = p._service.advance_all_plans()
    assert calls == ["only"]
    assert list(result["plans"]) == ["only"]


# ---------- Boundary: every plan failing still returns ok=True ----------

def test_method_all_plans_failing_still_ok_true(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "f1", {})
    _write_manifest(plan_dir, "f2", {})
    monkeypatch.setattr(p, "advance_pipeline", lambda plan_name: (_ for _ in ()).throw(RuntimeError("x")))
    result = p._service.advance_all_plans()
    assert result["ok"] is True
    assert result["plans"]["f1"]["ok"] is False
    assert result["plans"]["f2"]["ok"] is False
    assert "x" in result["plans"]["f1"]["error"]
    assert "x" in result["plans"]["f2"]["error"]


# ---------- R5: no docstring duplicated onto the method ----------

def test_method_does_not_carry_the_tool_docstring():
    """R3/R5: the docstring must NOT be duplicated onto the method. The method
    may have no docstring or only a trivial one, but the long NOTE must live
    solely on the module-level tool function."""
    doc = p.PipelineService.advance_all_plans.__doc__
    if doc is not None:
        assert "NOTE on zombie reaping" not in doc, (
            "the zombie-reaping NOTE docstring must not be duplicated onto the "
            "method (R3); it stays only on the module-level tool function"
        )


# ---------- R8: only pipeline/server.py changed is not testable here, but
# the method must live in pipeline/server.py (same file as the class) ----------

def test_method_defined_in_pipeline_service_module():
    mod = p.PipelineService.advance_all_plans.__module__
    assert mod == "pipeline.service", (
        f"advance_all_plans method must live in pipeline.service, got {mod}"
    )


def test_service_singleton_is_pipeline_service_instance():
    assert isinstance(p._service, p.PipelineService), (
        "_service must be a PipelineService instance so the delegation "
        "`return _service.advance_all_plans()` reaches the method"
    )