"""TDD (RED) tests for WS-11: the save_plan MCP tool gains an optional
``workspace`` parameter.

Implementation contract pinned down here (pipeline/server.py, save_plan tool
only, around line 651):

    @mcp.tool()
    def save_plan(plan_name: str, plan_json: str, workspace: str | None = None) -> dict[str, Any]:
        ...
        return _service.save_plan(plan_name, plan_json, workspace)

* ``workspace`` is optional and defaults to None.
* When supplied, it is validated and (WS-11) the model-authored ``repo_root``
  in ``plan_json`` is OVERWRITTEN with the server-validated resolved path.
* When omitted, the plan's own ``repo_root`` is trusted, exactly as before.
* The tool body is a pure pass-through: it forwards ``workspace`` verbatim to
  ``_service.save_plan`` (always as the third argument, even when None) and
  returns the service's return value unchanged. Validation lives downstream
  in the service, never in the tool.

COMPAT NOTE (state this in the PR body): adding a trailing optional parameter
is backward compatible - every existing MCP call shape
(save_plan(plan_name, plan_json)) behaves identically, and the tool's input
schema gains one optional property. This is deliberate explicit-only
behavior: the MCP tool deliberately does NOT fall back to the dashboard's
persisted active workspace (the MCP server and the dashboard are separate
processes; silently coupling them through shared durable state is out of
scope - the fallback lives only in the dashboard HTTP route).

Survivors (this story must not touch them): every other MCP tool in
pipeline/server.py, the ingest_plan tool, the decompose_plan tool, and all
existing tests.
"""
import inspect
import json

import pytest

from pipeline import server as p

# Sentinel default on the recorder so we can tell "the tool did not pass a
# third argument at all" apart from "the tool passed workspace=None". The
# contract is that the tool ALWAYS forwards workspace, even when None.
_MISSING = object()

# Distinct sentinel return: the tool must return the service's return value
# verbatim (identity, not merely equality).
_RECORDER_RETURN = {"ok": True, "path": "/plans/recorder.json", "via": "test-recorder"}


class _RecordingService:
    """Stand-in for the ``_service`` binding the save_plan tool resolves
    through. Mirrors the monkeypatch-the-module-seam pattern used by
    tests/unit/test_pipeline_mcp_server_decisions_and_dispatch.py (there:
    ``monkeypatch.setattr(pt, "plane_request", _fake_plane)``; here: replace
    the module-level ``_service`` singleton so the tool's
    ``return _service.save_plan(...)`` resolves to this recorder, i.e.
    monkeypatching ``_service.save_plan`` as seen by the tool)."""

    def __init__(self, calls, exc=None):
        self._calls = calls
        self._exc = exc

    def save_plan(self, plan_name, plan_json, workspace=_MISSING, **extra):
        self._calls.append({
            "plan_name": plan_name,
            "plan_json": plan_json,
            "workspace": workspace,
            "extra": extra,
        })
        if self._exc is not None:
            raise self._exc
        return _RECORDER_RETURN


def _capture_service_save_plan(monkeypatch, exc=None):
    calls = []
    monkeypatch.setattr(p, "_service", _RecordingService(calls, exc=exc))
    return calls


def _single_call(calls):
    assert len(calls) == 1, (
        f"expected exactly one _service.save_plan call, saw {len(calls)}: {calls!r}"
    )
    return calls[0]


# ---------- signature (inspect.signature; membership assertions only) ----------

def test_save_plan_signature_has_workspace_parameter():
    sig = inspect.signature(p.save_plan)
    # Membership only - never a full-parameter-list equality; sibling stories
    # may extend this tool's parameter list further.
    assert "workspace" in sig.parameters
    assert "plan_name" in sig.parameters
    assert "plan_json" in sig.parameters


def test_save_plan_workspace_parameter_defaults_to_none():
    wp = inspect.signature(p.save_plan).parameters["workspace"]
    assert wp.default is None


def test_save_plan_workspace_parameter_is_optional_positional_or_keyword():
    wp = inspect.signature(p.save_plan).parameters["workspace"]
    assert wp.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    ann = str(wp.annotation).lower()
    assert "str" in ann
    assert "none" in ann or "optional" in ann


def test_save_plan_return_annotation_still_dict_of_str_any():
    sig = inspect.signature(p.save_plan)
    assert "dict" in str(sig.return_annotation).lower()


def test_save_plan_required_parameters_unchanged():
    # Backward compat: plan_name/plan_json stay required positional strs.
    sig = inspect.signature(p.save_plan)
    for name in ("plan_name", "plan_json"):
        param = sig.parameters[name]
        assert param.default is inspect.Parameter.empty
        assert "str" in str(param.annotation).lower()


# ---------- pass-through to _service.save_plan ----------

def test_save_plan_forwards_string_workspace_verbatim_to_service(monkeypatch):
    calls = _capture_service_save_plan(monkeypatch)
    plan_name = "ws-string-plan"
    plan_json = json.dumps({"epics": [{"summary": "E1", "stories": []}]})
    workspace = "/repo/workspaces/team-a"
    result = p.save_plan(plan_name, plan_json, workspace=workspace)
    call = _single_call(calls)
    assert call["plan_name"] is plan_name
    assert call["plan_json"] is plan_json
    assert call["workspace"] is workspace
    assert call["extra"] == {}
    assert result is _RECORDER_RETURN


def test_save_plan_forwards_none_workspace_verbatim_to_service(monkeypatch):
    calls = _capture_service_save_plan(monkeypatch)
    plan_name = "ws-none-plan"
    plan_json = "{}"
    result = p.save_plan(plan_name, plan_json, workspace=None)
    call = _single_call(calls)
    assert call["workspace"] is not _MISSING
    assert call["workspace"] is None
    assert call["plan_name"] is plan_name
    assert call["plan_json"] is plan_json
    assert call["extra"] == {}
    assert result is _RECORDER_RETURN


def test_save_plan_legacy_two_arg_shape_forwards_workspace_none(monkeypatch):
    # Backward compat: save_plan(plan_name, plan_json) behaves identically to
    # save_plan(plan_name, plan_json, workspace=None) - the tool always
    # forwards the third argument (as None) and deliberately does NOT fall
    # back to the dashboard's persisted active workspace (the MCP server and
    # the dashboard are separate processes; the fallback lives only in the
    # dashboard HTTP route).
    calls = _capture_service_save_plan(monkeypatch)
    result = p.save_plan("legacy-plan", json.dumps({"epics": []}))
    call = _single_call(calls)
    assert call["workspace"] is not _MISSING
    assert call["workspace"] is None
    assert result is _RECORDER_RETURN


def test_save_plan_accepts_workspace_as_third_positional_argument(monkeypatch):
    calls = _capture_service_save_plan(monkeypatch)
    workspace = "/repo/workspaces/positional"
    p.save_plan("positional-plan", "{}", workspace)
    call = _single_call(calls)
    assert call["workspace"] is workspace


def test_save_plan_forwards_empty_string_workspace_verbatim(monkeypatch):
    # Boundary: zero-length workspace string. The tool forwards it verbatim;
    # deciding whether "" is a valid workspace is the service's job.
    calls = _capture_service_save_plan(monkeypatch)
    p.save_plan("empty-ws-plan", "{}", workspace="")
    call = _single_call(calls)
    assert call["workspace"] == ""
    assert call["workspace"] is not None
    assert call["workspace"] is not _MISSING


def test_save_plan_forwards_malformed_non_string_workspace_verbatim(monkeypatch):
    # Validation lives downstream in the service; the tool is a pure
    # pass-through and must not pre-validate or coerce the workspace value.
    calls = _capture_service_save_plan(monkeypatch)
    malformed = 12345
    p.save_plan("bad-ws-plan", "{}", workspace=malformed)
    call = _single_call(calls)
    assert call["workspace"] is malformed


# ---------- error propagation and missing required fields ----------

def test_save_plan_propagates_service_error_type_and_message(monkeypatch):
    calls = _capture_service_save_plan(
        monkeypatch, exc=ValueError("unknown workspace 'no-such-ws'")
    )
    with pytest.raises(ValueError, match=r"unknown workspace 'no-such-ws'"):
        p.save_plan("err-plan", "{}", workspace="no-such-ws")
    assert len(calls) == 1  # the service was actually consulted


def test_save_plan_still_requires_plan_name_and_plan_json():
    # Missing required fields keep raising TypeError from the tool layer.
    with pytest.raises(TypeError) as no_args:
        p.save_plan()
    assert "plan_name" in str(no_args.value)
    with pytest.raises(TypeError) as one_arg:
        p.save_plan("only-the-name")
    assert "plan_json" in str(one_arg.value)


# ---------- docstring contract ----------

def test_save_plan_docstring_documents_optional_workspace():
    doc = (p.save_plan.__doc__ or "").lower()
    assert "workspace" in doc
    assert "optional" in doc


def test_save_plan_docstring_documents_ws11_repo_root_overwrite_when_supplied():
    doc = (p.save_plan.__doc__ or "").lower()
    assert "ws-11" in doc
    assert "repo_root" in doc
    assert "overwrite" in doc
    assert "valid" in doc  # "validated" / "server-validated"


def test_save_plan_docstring_documents_repo_root_trusted_when_omitted():
    doc = (p.save_plan.__doc__ or "").lower()
    assert "trust" in doc
    assert "repo_root" in doc


def test_save_plan_docstring_extends_rather_than_replaces_original_text():
    doc = (p.save_plan.__doc__ or "").lower()
    assert "save a generated project plan to disk" in doc


# ---------- MCP input schema ----------

def _mcp_tool_input_schema(tool_name):
    tm = getattr(p.mcp, "_tool_manager", None)
    tools = getattr(tm, "_tools", None) or {}
    tool = tools.get(tool_name)
    assert tool is not None, f"MCP tool {tool_name!r} is not registered on p.mcp"
    fn = getattr(tool, "fn", None)
    if fn is not None:
        assert fn is p.save_plan, "registered save_plan tool must wrap p.save_plan"
    params = getattr(tool, "parameters", None)
    assert isinstance(params, dict), f"MCP tool {tool_name!r} has no generated input schema"
    return params


def test_save_plan_mcp_input_schema_gains_one_optional_workspace_property():
    schema = _mcp_tool_input_schema("save_plan")
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    # Membership assertions only - never exact schema equality.
    assert "workspace" in properties
    assert "workspace" not in required
    assert {"plan_name", "plan_json"} <= set(required)


# ---------- companion server mirror (grep check) ----------

def test_companion_server_mirrors_save_plan_if_it_reexports_the_tool():
    # Mirrors `grep -n save_plan pipeline/companion_server.py`: companion_server
    # currently exposes only overlord + oracle tools, so this is expected to
    # skip as a no-op. If a save_plan tool ever appears there, it must carry
    # the identical optional workspace parameter.
    try:
        from pipeline import companion_server as cs
    except ImportError as exc:  # pragma: no cover - import environment dependent
        pytest.skip(f"pipeline.companion_server not importable here: {exc}")
    tool = getattr(cs, "save_plan", None)
    if tool is None:
        pytest.skip(
            "companion_server does not re-export save_plan - expected no-op "
            "(the grep check); nothing to mirror"
        )
    sig = inspect.signature(tool)
    assert "workspace" in sig.parameters
    assert sig.parameters["workspace"].default is None
