"""Tests for the W1a migration of the ``checkpoint`` MCP tool onto
``PipelineService``.

These tests verify the *structural* move described by the story:

* ``PipelineService.checkpoint`` exists as a method taking ``self`` plus the
  original parameters, with the original type hints and defaults.
* The module-level ``@mcp.tool()``-decorated ``def checkpoint(...)`` still
  exists at its original position with an unchanged signature and docstring,
  and its body is exactly one delegation statement.
* The free variables inside the method stay free (no ``self.`` access to
  module globals/helpers), so ``monkeypatch.setattr(pipeline.server, ...)``
  patches keep landing.
* Entry validation (``_validate_key``) stays first.
* The tool stays MCP-registered.
* Behaviour is unchanged: happy path, unknown-story error, nothing-to-commit
  no-op, real commit failure, and path-traversal rejection all behave as
  before.

The implementation does not exist yet, so this suite is expected to be RED
until the migration is performed.
"""

import inspect
import json

import pytest

from pipeline import server as p

# ---------- Structural requirements (the mechanical move) ----------
# ---------- Structural requirements (the mechanical move) ----------

def test_pipeline_service_has_checkpoint_method():
    """C1: PipelineService defines ``checkpoint`` as a method."""
    assert hasattr(p.PipelineService, "checkpoint"), (
        "PipelineService must define a `checkpoint` method"
    )
    assert callable(p.PipelineService.checkpoint)


def test_pipeline_service_checkpoint_takes_self_and_original_params():
    """C1: the method takes ``self`` plus the original parameters with the
    original annotations and defaults."""
    sig = inspect.signature(p.PipelineService.checkpoint)
    params = list(sig.parameters)
    assert params[0] == "self", "first parameter must be self"
    assert params[1:] == ["plan_name", "story_key", "step", "summary", "next_hint"], (
        "method parameters must match the original tool signature in order"
    )

    annotations = {k: v.annotation for k, v in sig.parameters.items() if k != "self"}
    assert annotations["plan_name"] is str
    assert annotations["story_key"] is str
    assert annotations["step"] is str
    assert annotations["summary"] is str
    assert annotations["next_hint"] is str

    # next_hint has a default of ""
    assert sig.parameters["next_hint"].default == ""
    # the required params have no default
    for name in ("plan_name", "story_key", "step", "summary"):
        assert sig.parameters[name].default is inspect.Parameter.empty, (
            f"{name} must remain required (no default)"
        )


def test_pipeline_service_checkpoint_return_annotation():
    """C1: the method preserves the original return type hint."""
    sig = inspect.signature(p.PipelineService.checkpoint)
    # dict[str, Any] stringifies to "dict[str, Any]"
    assert "dict" in str(sig.return_annotation), (
        "return annotation must be preserved as a dict type"
    )


def test_module_level_checkpoint_tool_still_exists():
    """C2/C3: the module-level @mcp.tool() checkpoint function still exists."""
    assert hasattr(p, "checkpoint"), "module-level checkpoint must still exist"
    assert callable(p.checkpoint)


def test_module_level_checkpoint_is_decorated_with_mcp_tool():
    """C2/R3: the @mcp.tool() decorator stays on the module-level function."""
    src = inspect.getsource(p.checkpoint)
    # The decorator must appear immediately above the module-level def. We
    # check the source of the function object, which includes its decorator.
    assert "@mcp.tool()" in src, (
        "the @mcp.tool() decorator must remain on the module-level checkpoint"
    )


def test_module_level_checkpoint_signature_unchanged():
    """C2/R3: the module-level function keeps the original signature."""
    sig = inspect.signature(p.checkpoint)
    params = list(sig.parameters)
    assert params == ["plan_name", "story_key", "step", "summary", "next_hint"], (
        "module-level checkpoint signature must be unchanged"
    )
    assert sig.parameters["next_hint"].default == ""
    annotations = {k: v.annotation for k, v in sig.parameters.items()}
    assert annotations["plan_name"] is str
    assert annotations["story_key"] is str
    assert annotations["step"] is str
    assert annotations["summary"] is str
    assert annotations["next_hint"] is str


def test_module_level_checkpoint_docstring_unchanged():
    """C2/R3: the module-level function keeps its full docstring."""
    doc = p.checkpoint.__doc__
    assert doc is not None, "module-level checkpoint must keep its docstring"
    assert "Record a durable checkpoint" in doc
    assert "journal" in doc
    assert "resume from the last checkpoint" in doc


def test_module_level_checkpoint_body_is_single_delegation():
    """C2: the module-level body is exactly one statement delegating to the
    service method, passing the same args in the same order."""
    src = inspect.getsource(p.checkpoint)
    # Strip the decorator, signature and docstring; the remaining executable
    # body must be a single return statement delegating to _service.checkpoint.
    lines = src.splitlines()
    body = []
    in_body = False
    for line in lines:
        stripped = line.strip()
        if not in_body:
            if stripped.startswith("def checkpoint("):
                in_body = True
            continue
        # skip the docstring
        if stripped.startswith(('"""', "'''")):
            continue
        if stripped == "":
            continue
        body.append(stripped)
    # The only executable statement should be the delegation return.
    non_doc = [b for b in body if not b.startswith('"""') and not b.startswith("'''")]
    assert len(non_doc) == 1, (
        f"module-level checkpoint body must be exactly one statement, got {non_doc}"
    )
    stmt = non_doc[0]
    assert stmt.startswith("return _service.checkpoint("), (
        f"body must delegate to _service.checkpoint, got: {stmt}"
    )
    # All five args must be passed through, in order.
    assert "plan_name" in stmt
    assert "story_key" in stmt
    assert "step" in stmt
    assert "summary" in stmt
    assert "next_hint" in stmt


def test_exactly_two_checkpoint_definitions():
    """C3: exactly two `def checkpoint` definitions (method + tool)."""
    import re
    text = open(p.__file__).read()
    count = len(re.findall(r"\bdef checkpoint\b", text))
    assert count == 2, (
        f"expected exactly 2 `def checkpoint` definitions, found {count}"
    )


def test_checkpoint_is_mcp_registered():
    """C4: the tool is still registered with the MCP server."""
    tool_names = {t.name for t in p.mcp._tool_manager.list_tools()}
    assert "checkpoint" in tool_names, (
        "checkpoint must remain registered as an MCP tool"
    )


def test_pipeline_service_checkpoint_has_no_docstring():
    """R3: the method must NOT duplicate the docstring (it stays on the tool)."""
    assert p.PipelineService.checkpoint.__doc__ in (None, ""), (
        "the method must not carry a docstring; the docstring stays on the "
        "module-level @mcp.tool() function"
    )


def test_pipeline_service_checkpoint_uses_no_self_for_globals():
    """C5/R1: no module global or helper is accessed through ``self`` inside
    the new method."""
    src = inspect.getsource(p.PipelineService.checkpoint)
    for line in src.splitlines():
        stripped = line.strip()
        # The only allowed `self.` is the method's own receiver parameter.
        if stripped.startswith("def checkpoint(self"):
            continue
        assert "self." not in stripped, (
            f"no `self.` access to globals/helpers allowed in method body: {stripped}"
        )


def test_pipeline_service_has_no_init_with_state():
    """R1: PipelineService.__init__ must not copy globals onto self."""
    init = getattr(p.PipelineService, "__init__", None)
    if init is not None and init is not object.__init__:
        src = inspect.getsource(init)
        assert "self." not in src.replace("def __init__(self", ""), (
            "__init__ must not set any state onto self"
        )


# ---------- Behavioural requirements (unchanged by the move) ----------

def _write_manifest(plan_dir, plan_name, stories):
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}, indent=2)
    )


def test_checkpoint_happy_path(plan_dir, tmp_path, monkeypatch):
    """The moved tool still commits WIP work and records a journal entry."""
    worktree = str(tmp_path / "wt")
    _write_manifest(plan_dir, "ck", {
        "S1": {"summary": "thing", "status": "in_progress", "worktree": worktree},
    })

    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)

        class Result:
            returncode = 0
            stdout = "abc123\n" if cmd[:2] == ["git", "rev-parse"] else ""
            stderr = ""
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    result = p.checkpoint(
        "ck", "S1", "step-1", "Implemented the parser",
        next_hint="write tests for edge cases",
    )

    assert result["ok"] is True
    assert result["commit"] == "abc123"
    assert result["step"] == "step-1"

    assert ["git", "add", "-A"] in calls
    assert ["git", "reset", "-q", "--", "agent.log"] in calls
    commit_calls = [c for c in calls if c[:2] == ["git", "commit"]]
    assert commit_calls and commit_calls[0][-1] == "wip(S1): step-1"

    journal = json.loads((plan_dir / "ck.S1.journal.json").read_text())
    assert len(journal) == 1
    assert journal[0]["step"] == "step-1"
    assert journal[0]["summary"] == "Implemented the parser"
    assert journal[0]["next_hint"] == "write tests for edge cases"
    assert journal[0]["commit"] == "abc123"
    assert "ts" in journal[0]


def test_checkpoint_appends_multiple_entries_in_order(plan_dir, tmp_path, monkeypatch):
    """N2: a monkeypatched module global (subprocess.run) still takes effect
    through the new call path, and entries are appended in order."""
    worktree = str(tmp_path / "wt")
    _write_manifest(plan_dir, "ck2", {
        "S1": {"summary": "thing", "status": "in_progress", "worktree": worktree},
    })

    shas = iter(["sha-1", "sha-2"])

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = (next(shas) + "\n") if cmd[:2] == ["git", "rev-parse"] else ""
            stderr = ""
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    p.checkpoint("ck2", "S1", "step-1", "first")
    p.checkpoint("ck2", "S1", "step-2", "second")

    journal = json.loads((plan_dir / "ck2.S1.journal.json").read_text())
    assert [e["step"] for e in journal] == ["step-1", "step-2"]
    assert [e["commit"] for e in journal] == ["sha-1", "sha-2"]


def test_checkpoint_unknown_story_returns_error(plan_dir):
    """N3: unknown-story error shape is byte-identical to before."""
    _write_manifest(plan_dir, "ck3", {})
    result = p.checkpoint("ck3", "NOPE", "step-1", "summary")
    assert result["ok"] is False
    assert "NOPE" in result["error"]


def test_checkpoint_nothing_to_commit_still_records_journal(plan_dir, tmp_path, monkeypatch):
    """Boundary: nothing staged still succeeds and records HEAD sha."""
    worktree = str(tmp_path / "wt")
    _write_manifest(plan_dir, "ck4", {
        "S1": {"summary": "thing", "status": "in_progress", "worktree": worktree},
    })

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = ""
            stderr = ""
        if cmd[:2] == ["git", "commit"]:
            Result.returncode = 1
            Result.stdout = "nothing to commit, working tree clean\n"
        elif cmd[:2] == ["git", "rev-parse"]:
            Result.stdout = "existing-sha\n"
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    result = p.checkpoint("ck4", "S1", "step-1", "no new changes")
    assert result["ok"] is True
    assert result["commit"] == "existing-sha"


def test_checkpoint_raises_on_real_commit_failure(plan_dir, tmp_path, monkeypatch):
    """Expected exception: a real git commit failure raises RuntimeError."""
    worktree = str(tmp_path / "wt")
    _write_manifest(plan_dir, "ck5", {
        "S1": {"summary": "thing", "status": "in_progress", "worktree": worktree},
    })

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = ""
            stderr = ""
        if cmd[:2] == ["git", "commit"]:
            Result.returncode = 1
            Result.stderr = "fatal: unable to write new index file"
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    with pytest.raises(RuntimeError):
        p.checkpoint("ck5", "S1", "step-1", "summary")


def test_checkpoint_rejects_traversal_plan_name(plan_dir, monkeypatch):
    """N1/R4: path-traversal rejection still fires first for plan_name."""
    with pytest.raises(ValueError, match="invalid"):
        p.checkpoint("../evil", "S1", "step-1", "summary")


def test_checkpoint_rejects_traversal_story_key(plan_dir, monkeypatch):
    """N1/R4: path-traversal rejection still fires first for story_key."""
    with pytest.raises(ValueError, match="invalid"):
        p.checkpoint("myplan", "../evil", "step-1", "summary")


def test_checkpoint_validation_runs_before_any_filesystem_access(plan_dir, monkeypatch):
    """R4: validation must be the FIRST statement -- before any manifest read.
    A traversal plan_name must raise before the manifest file is ever opened."""
    opened = []

    real_read_text = p.Path.read_text

    def _spy_read_text(self, *args, **kwargs):
        opened.append(str(self))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(p.Path, "read_text", _spy_read_text)

    with pytest.raises(ValueError, match="invalid"):
        p.checkpoint("../evil", "S1", "step-1", "summary")

    assert opened == [], (
        "validation must run before any filesystem read; manifest was opened: "
        f"{opened}"
    )


def test_checkpoint_default_next_hint_is_empty_string(plan_dir, tmp_path, monkeypatch):
    """Boundary: next_hint defaults to '' and is recorded as such."""
    worktree = str(tmp_path / "wt")
    _write_manifest(plan_dir, "ckd", {
        "S1": {"summary": "thing", "status": "in_progress", "worktree": worktree},
    })

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = "sha-d\n" if cmd[:2] == ["git", "rev-parse"] else ""
            stderr = ""
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    result = p.checkpoint("ckd", "S1", "step-1", "summary")
    assert result["ok"] is True
    assert result["next_hint"] == ""

    journal = json.loads((plan_dir / "ckd.S1.journal.json").read_text())
    assert journal[0]["next_hint"] == ""


def test_checkpoint_empty_step_and_summary_are_accepted(plan_dir, tmp_path, monkeypatch):
    """Boundary: empty-string step and summary are accepted (no min-length
    guard in the original behaviour)."""
    worktree = str(tmp_path / "wt")
    _write_manifest(plan_dir, "cke", {
        "S1": {"summary": "thing", "status": "in_progress", "worktree": worktree},
    })

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = "sha-e\n" if cmd[:2] == ["git", "rev-parse"] else ""
            stderr = ""
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    result = p.checkpoint("cke", "S1", "", "")
    assert result["ok"] is True
    assert result["step"] == ""
    assert result["summary"] == ""


def test_service_checkpoint_delegates_same_as_tool(plan_dir, tmp_path, monkeypatch):
    """C2: the module-level tool delegates to _service.checkpoint, so calling
    the tool and calling the service method directly must produce the same
    result for the same inputs."""
    worktree = str(tmp_path / "wt")
    _write_manifest(plan_dir, "ckm", {
        "S1": {"summary": "thing", "status": "in_progress", "worktree": worktree},
    })

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = "sha-m\n" if cmd[:2] == ["git", "rev-parse"] else ""
            stderr = ""
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    tool_result = p.checkpoint("ckm", "S1", "step-1", "summary", next_hint="hint")
    # The journal now has one entry; reset by reading and comparing structure.
    assert tool_result["ok"] is True

    # Calling the service method directly must also work and produce the
    # same shape (commit/step/summary/next_hint/ts keys).
    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    _write_manifest(plan_dir, "ckm2", {
        "S1": {"summary": "thing", "status": "in_progress", "worktree": worktree},
    })
    method_result = p._service.checkpoint("ckm2", "S1", "step-1", "summary", next_hint="hint")
    assert method_result["ok"] is True
    assert set(method_result.keys()) == set(tool_result.keys())