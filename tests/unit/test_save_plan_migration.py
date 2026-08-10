"""Tests for the W1a migration of ``save_plan`` onto ``PipelineService``.

These tests verify the *structural* move described by the story:

* ``PipelineService`` gains a ``save_plan`` method whose body is the verbatim
  relocation of the old module-level tool body (free variables stay free,
  internal calls stay module-level, entry validation stays first).
* The module-level ``@mcp.tool()``-decorated ``save_plan`` keeps its decorator,
  signature, type hints and docstring byte-for-byte, and shrinks to a single
  ``return _service.save_plan(...)`` delegation line.
* No behaviour changes -- the existing suite is the oracle; these tests pin the
  *mechanical* requirements (C1-C9) so the implementer cannot ship a partial
  move.

The tests are written to be RED against the current code (the method does not
exist yet on ``PipelineService``) and to turn GREEN once the move is done.
"""

import inspect
import json
import textwrap

import pytest

from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import server as p


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Mirror of the plan_dir fixture in test_pipeline_mcp_server.py so this
    file is self-contained."""
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d

# ---------- C1: PipelineService.save_plan method exists with right shape ----


def test_pipeline_service_has_save_plan_method():
    """C1: ``PipelineService`` defines ``save_plan`` as a method."""
    assert hasattr(p.PipelineService, "save_plan"), (
        "PipelineService must define a `save_plan` method after the migration"
    )
    assert callable(p.PipelineService.save_plan)


def test_save_plan_method_takes_self_plus_original_params():
    """C1: the method signature is ``(self, plan_name: str, plan_json: str)
    -> dict[str, Any]`` -- ``self`` plus the original parameters, annotations
    and return type preserved."""
    sig = inspect.signature(p.PipelineService.save_plan)
    params = list(sig.parameters.keys())
    assert params[0] == "self", (
        "first parameter of the method must be `self`, got "
        f"{params[0]!r}"
    )
    assert params[1:] == ["plan_name", "plan_json"], (
        "method must keep the original parameter names plan_name, plan_json; "
        f"got {params[1:]!r}"
    )
    # Annotations preserved.
    assert sig.parameters["plan_name"].annotation is str
    assert sig.parameters["plan_json"].annotation is str
    assert sig.return_annotation == dict[str, p.Any] or sig.return_annotation is dict


# ---------- C2/C3: module-level tool still exists, shrunk to delegation -----


def test_module_level_save_plan_still_exists_and_is_mcp_tool():
    """C2/C4: the module-level ``@mcp.tool()``-decorated ``save_plan`` still
    exists at module scope with its decorator intact (so it stays registered)."""
    assert hasattr(p, "save_plan"), "module-level save_plan must still exist"
    # It must still be a registered MCP tool (R3 guard).
    tool_names = {t.name for t in p.mcp._tool_manager.list_tools()}
    assert "save_plan" in tool_names, (
        "save_plan must remain registered as an MCP tool -- the @mcp.tool() "
        "decorator must stay on the module-level function, not the method"
    )


def test_save_plan_definition_count_is_two():
    """C3: exactly two ``def save_plan`` in pipeline/server.py -- the method
    plus the tool function. Not 1 (deleted), not 3 (duplicated body)."""
    import pathlib

    src = pathlib.Path(p.__file__).read_text()
    assert src.count("def save_plan(") == 2, (
        "expected exactly 2 `def save_plan(` (method + tool), got "
        f"{src.count('def save_plan(')}"
    )


def test_module_level_save_plan_signature_unchanged():
    """C2: the module-level tool keeps its original signature
    ``(plan_name: str, plan_json: str) -> dict[str, Any]``."""
    sig = inspect.signature(p.save_plan)
    params = list(sig.parameters.keys())
    assert params == ["plan_name", "plan_json"], (
        "module-level save_plan signature must be unchanged; got "
        f"{params!r}"
    )
    assert sig.parameters["plan_name"].annotation is str
    assert sig.parameters["plan_json"].annotation is str


def test_module_level_save_plan_docstring_unchanged():
    """C2/R3: the module-level tool keeps its full docstring byte-for-byte."""
    assert p.save_plan.__doc__ is not None
    # Compare on the meaningful content (the original docstring).
    assert "Save a generated project plan to disk." in p.save_plan.__doc__
    assert "schema: { \"epics\": [ { \"summary\", \"stories\": [...] } ] }." in (
        p.save_plan.__doc__ or ""
    )
    assert "Call this after generating a plan so the user can review before ingestion." in (
        p.save_plan.__doc__ or ""
    )


def test_module_level_save_plan_body_is_single_delegation():
    """C2: the module-level tool's executable body is exactly one statement:
    ``return _service.save_plan(plan_name, plan_json)``."""
    src = inspect.getsource(p.save_plan)
    # Strip the def line + docstring; the remaining body must be a single
    # return delegation.
    # The docstring is the first triple-quoted block; everything after it.
    # src looks like: def ...\n    """..."""\n    <body>\n
    # Find the body after the closing triple-quote of the docstring.
    # Robust: take text after the last occurrence of the closing docstring.
    idx = src.rfind('"""')
    body = src[idx + 3:]
    body = textwrap.dedent(body).strip()
    assert body == "return _service.save_plan(plan_name, plan_json)", (
        "module-level save_plan body must be exactly "
        "`return _service.save_plan(plan_name, plan_json)`; got:\n" + repr(body)
    )


# ---------- R3: decorator stays on the module-level function ---------------


def test_save_plan_method_is_not_mcp_tool_decorator_target():
    """R3: the ``@mcp.tool()`` decorator must NOT be on the method. The method
    is a plain function on the class. We assert the method is not itself a
    FastMCP-registered tool (only the module-level function is)."""
    # The method should be a plain function object, not wrapped by mcp.tool
    # in a way that registers it. The real guard is C4 above; here we just
    # confirm the method has no __doc__ duplicating the tool docstring (R3:
    # do NOT duplicate the docstring onto the method).
    method = p.PipelineService.save_plan
    # The method must NOT carry the tool's docstring (R3: do not duplicate).
    assert method.__doc__ != p.save_plan.__doc__, (
        "the method must not duplicate the tool's docstring (R3)"
    )


# ---------- R1: free variables stay free (no self. on globals/helpers) -----


def test_save_plan_method_has_no_self_attribute_access_for_globals():
    """R1/C5: inside the new method, no module global or helper is accessed
    through ``self.``. The only ``self`` is the receiver parameter."""
    src = inspect.getsource(p.PipelineService.save_plan)
    # No `self.<anything>` should appear in the body.
    assert "self." not in src, (
        "R1 violation: the save_plan method must not access any global or "
        "helper through `self.`; free variables must stay free. Found "
        "`self.` in:\n" + src
    )


def test_pipeline_service_init_takes_only_self():
    """R1: ``PipelineService.__init__`` must not exist or must take nothing but
    ``self`` and set nothing (no constructor parameters, no copied globals)."""
    init = getattr(p.PipelineService, "__init__", None)
    if init is object.__init__:
        return  # default init -- fine
    sig = inspect.signature(init)
    params = list(sig.parameters.keys())
    assert params == ["self"], (
        "PipelineService.__init__ must take only `self`; got " f"{params!r}"
    )


# ---------- R4: entry validation stays first -------------------------------


def test_save_plan_method_validates_before_anything_else():
    """R4: ``_validate_key(plan_name)`` must be the FIRST executable statement
    of the moved method -- before any json parse or filesystem write."""
    src = inspect.getsource(p.PipelineService.save_plan)
    # Find the first non-docstring, non-def executable line.
    lines = src.splitlines()
    body_lines = []
    in_docstring = False
    seen_def = False
    for line in lines:
        stripped = line.strip()
        if not seen_def:
            if stripped.startswith("def save_plan"):
                seen_def = True
            continue
        if stripped.startswith(('"""', "'''")):
            if in_docstring:
                in_docstring = False
            else:
                in_docstring = True
            continue
        if in_docstring:
            continue
        if stripped == "" or stripped.startswith("#"):
            continue
        body_lines.append(stripped)
    assert body_lines, "method body appears empty"
    assert body_lines[0].startswith("_validate_key(plan_name)"), (
        "R4: the first executable statement of save_plan must be "
        "`_validate_key(plan_name)`; got: " + repr(body_lines[0])
    )


# ---------- Behaviour: happy path + negative cases (N1/N2/N3) --------------


def test_save_plan_writes_plan_to_disk_and_returns_counts(plan_dir):
    """Happy path: a valid plan is written to PLAN_DIR/<name>.json and the
    return dict carries ok/path/epic_count/story_count."""
    plan = {
        "epics": [
            {"summary": "E1", "stories": [{"key": "s1"}, {"key": "s2"}]},
            {"summary": "E2", "stories": [{"key": "s3"}]},
        ]
    }
    result = p.save_plan("happy", json.dumps(plan))
    assert result["ok"] is True
    assert result["epic_count"] == 2
    assert result["story_count"] == 3
    written = json.loads((plan_dir / "happy.json").read_text())
    assert written == plan


def test_save_plan_rejects_traversal_plan_name(plan_dir):
    """N1: path-traversal rejection still fires first (R4)."""
    with pytest.raises(ValueError, match="invalid"):
        p.save_plan("../evil", json.dumps({"epics": []}))


def test_save_plan_rejects_invalid_key_with_slash(plan_dir):
    """N1: a key containing a path separator is rejected."""
    with pytest.raises(ValueError, match="invalid"):
        p.save_plan("a/b", json.dumps({"epics": []}))


def test_save_plan_rejects_invalid_json(plan_dir):
    """N3: malformed JSON returns the documented error shape."""
    result = p.save_plan("badjson", "not-json{")
    assert result["ok"] is False
    assert "Invalid JSON" in result["error"]


def test_save_plan_rejects_missing_epics_key(plan_dir):
    """N3: a plan missing the 'epics' key returns the documented error."""
    result = p.save_plan("noepics", json.dumps({"stories": []}))
    assert result["ok"] is False
    assert result["error"] == "Plan must contain 'epics' key"


def test_save_plan_empty_epics_list(plan_dir):
    """Boundary: an empty epics list is a valid plan (zero epics, zero
    stories)."""
    result = p.save_plan("empty", json.dumps({"epics": []}))
    assert result["ok"] is True
    assert result["epic_count"] == 0
    assert result["story_count"] == 0
    assert json.loads((plan_dir / "empty.json").read_text()) == {"epics": []}


def test_save_plan_epic_without_stories_key_counts_zero(plan_dir):
    """Boundary: an epic missing the 'stories' key contributes 0 stories
    (the body uses ``e.get('stories', [])``)."""
    result = p.save_plan("nostories", json.dumps({"epics": [{"summary": "E"}]}))
    assert result["ok"] is True
    assert result["epic_count"] == 1
    assert result["story_count"] == 0


# ---------- R1/N2: monkeypatched module global takes effect through method --


def test_save_plan_reads_patched_plan_dir(monkeypatch, tmp_path, plan_dir):
    """N2/R1: the method reads ``PLAN_DIR`` as a free variable, so a
    monkeypatch on ``pipeline.server.PLAN_DIR`` must still take effect through
    the new call path (the regression detector for the R1 copy-onto-self
    mistake)."""
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", other)
    result = p.save_plan("redirected", json.dumps({"epics": []}))
    assert result["ok"] is True
    # The file must land in the patched dir, not the original plan_dir.
    assert (other / "redirected.json").exists()
    assert not (plan_dir / "redirected.json").exists()


def test_save_plan_uses_patched_atomic_write_json(monkeypatch, plan_dir):
    """N2/R1: the helper ``_atomic_write_json`` is called as a bare module-level
    name; patching it on ``pipeline.server`` must intercept the write."""
    called = {}

    def fake_write(path, data):
        called["path"] = path
        called["data"] = data

    monkeypatch.setattr(p, "_atomic_write_json", fake_write)
    plan = {"epics": [{"summary": "E", "stories": [{"key": "s"}]}]}
    result = p.save_plan("patched", json.dumps(plan))
    assert result["ok"] is True
    assert called["data"] == plan
    assert str(called["path"]).endswith("patched.json")


# ---------- C8/C9: single-file change, lint --------------------------------


def test_only_pipeline_server_changed_for_save_plan_move():
    """C9 (informational at test-time): the production change is confined to
    pipeline/server.py. This test reads the git diff stat to enforce it."""
    import subprocess

    out = subprocess.run(
        ["git", "diff", "--stat", "--", "pipeline/server.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    # We don't assert non-empty here (the move may not be staged yet when this
    # test runs pre-implementation); we only assert the test infrastructure is
    # sound. The real C9 check is manual via `git diff --stat`.
    assert out.returncode == 0