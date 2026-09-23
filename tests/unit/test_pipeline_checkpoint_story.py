"""Tests for the ``checkpoint_story`` MCP tool (MCPHYG-4).

The story renames the primary checkpoint tool to ``checkpoint_story`` while
keeping ``checkpoint`` available as a deprecated alias. The guarantee this
suite makes is *behavioural identity*: ``checkpoint_story`` is a second name
for the exact same code path, so every behavioural assertion is parametrized
over both callables.

Structural requirements:

* ``checkpoint_story`` exists as a module-level function.
* It carries the ``@mcp.tool()`` decorator.
* It is registered with the MCP server.
* ``checkpoint_story`` carries the full behavioral docstring; ``checkpoint``
  carries a short deprecation pointer to it (documentation-quality pass,
  2026-09-23 -- the two names are no longer required to share one docstring,
  only one implementation).

Behavioural requirements (parametrized over ``checkpoint`` and
``checkpoint_story``):

* happy path commits WIP work and records a journal entry;
* multiple entries append in order;
* an unknown ``story_key`` returns the same error shape;
* path traversal is rejected in ``plan_name`` and in ``story_key``;
* empty ``step``/``summary`` are accepted (the alias is not stricter).

``checkpoint_story`` is a thin alias: it delegates to the same
``PipelineService.checkpoint`` call as ``checkpoint``, so the two names are
interchangeable.
"""

import inspect
import json

import pytest

from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import server as p


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Mirror of the plan_dir fixture in test_pipeline_mcp_server.py: patches
    PLAN_DIR on pipeline.server and on the persistence/concurrency modules
    that read it as a free var."""
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


def _write_manifest(plan_dir, plan_name, stories):
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}, indent=2)
    )


# ---------- Structural requirements ----------

def test_module_level_checkpoint_story_exists():
    """The module-level ``checkpoint_story`` function exists and is callable."""
    assert hasattr(p, "checkpoint_story"), (
        "module-level checkpoint_story must exist"
    )
    assert callable(p.checkpoint_story)


def test_module_level_checkpoint_story_is_decorated_with_mcp_tool():
    """``checkpoint_story`` carries its own ``@mcp.tool()`` decorator."""
    src = inspect.getsource(p.checkpoint_story)
    assert "@mcp.tool()" in src, (
        "the @mcp.tool() decorator must be on the module-level checkpoint_story"
    )


def test_module_level_checkpoint_story_signature_matches_checkpoint():
    """The alias keeps the exact same signature as ``checkpoint``."""
    sig = inspect.signature(p.checkpoint_story)
    params = list(sig.parameters)
    assert params == ["plan_name", "story_key", "step", "summary", "next_hint"], (
        "checkpoint_story signature must match checkpoint's"
    )
    assert sig.parameters["next_hint"].default == ""
    annotations = {k: v.annotation for k, v in sig.parameters.items()}
    assert annotations["plan_name"] is str
    assert annotations["story_key"] is str
    assert annotations["step"] is str
    assert annotations["summary"] is str
    assert annotations["next_hint"] is str
    assert inspect.signature(p.checkpoint_story) == inspect.signature(p.checkpoint)


def test_module_level_checkpoint_story_docstring_matches_checkpoint():
    """``checkpoint_story`` carries the full docstring; ``checkpoint`` carries
    a short pointer back to it rather than duplicating the text."""
    assert p.checkpoint_story.__doc__ is not None, (
        "checkpoint_story must carry a docstring"
    )
    assert p.checkpoint.__doc__ is not None, "checkpoint must carry a docstring"
    assert p.checkpoint_story.__doc__ != p.checkpoint.__doc__, (
        "checkpoint's docstring should point to checkpoint_story's, not "
        "duplicate it verbatim"
    )
    assert "checkpoint_story" in p.checkpoint.__doc__, (
        "checkpoint's docstring must name checkpoint_story as the preferred name"
    )
    assert "deprecat" in p.checkpoint.__doc__.lower(), (
        "checkpoint's docstring must say it is deprecated"
    )


def test_module_level_checkpoint_story_body_is_single_delegation():
    """The body is exactly one statement delegating to the same service call."""
    import ast

    src = inspect.getsource(p.checkpoint_story)
    fn_def = ast.parse(src).body[0]
    assert isinstance(fn_def, ast.FunctionDef)
    body = fn_def.body
    # A docstring surfaces as a leading ast.Expr(ast.Constant(str)) node;
    # strip it (however many source lines it spans) before counting
    # executable statements.
    if body and isinstance(body[0], ast.Expr) and isinstance(
        getattr(body[0], "value", None), ast.Constant
    ) and isinstance(body[0].value.value, str):
        body = body[1:]
    assert len(body) == 1, (
        f"checkpoint_story body must be exactly one statement, got {len(body)}"
    )
    stmt = ast.unparse(body[0])
    assert stmt == (
        "return _service.checkpoint(plan_name, story_key, step, summary, next_hint)"
    ), f"body must delegate to _service.checkpoint, got: {stmt}"


def test_checkpoint_story_is_mcp_registered():
    """The alias is registered with the MCP server alongside ``checkpoint``."""
    tool_names = {t.name for t in p.mcp._tool_manager.list_tools()}
    assert "checkpoint_story" in tool_names, (
        "checkpoint_story must be registered as an MCP tool"
    )
    assert "checkpoint" in tool_names, (
        "checkpoint must remain registered as a deprecated alias"
    )


def test_checkpoint_remains_a_deprecated_delegating_alias():
    """The original tool still exists, still delegates to the same service
    call, and its docstring now points to checkpoint_story instead of
    duplicating it (documentation-quality pass, 2026-09-23 -- see the
    module docstring). It is no longer required to be byte-identical to
    its pre-alias text."""
    assert hasattr(p, "checkpoint")
    assert callable(p.checkpoint)
    src = inspect.getsource(p.checkpoint)
    assert "@mcp.tool()" in src
    assert "def checkpoint(" in src
    assert "def checkpoint_story(" not in src, (
        "checkpoint's source must not be altered by the alias addition"
    )
    assert "return _service.checkpoint(plan_name, story_key, step, summary, next_hint)" in src
    # The mcp SDK's docstring dedenting differs between CI's Python 3.12
    # and 3.13+ (see test_module_level_list_ready_stories_docstring_present_
    # and_accurate in test_list_ready_stories_migration.py for the same
    # note), so compare whitespace-collapsed text, not raw bytes.
    expected = (
        "Deprecated alias for checkpoint_story — identical behavior and "
        "parameters (see checkpoint_story's docstring for the full description "
        "of plan_name/story_key/step/summary/next_hint). Kept only for backward "
        "compatibility with agents/prompts still calling the old name; prefer "
        "checkpoint_story in new code."
    )
    assert " ".join(p.checkpoint.__doc__.split()) == " ".join(expected.split())


# ---------- Behavioural requirements (parametrized over both names) ----------

BOTH_NAMES = pytest.mark.parametrize(
    "checkpoint_fn", [p.checkpoint, p.checkpoint_story], ids=["checkpoint", "checkpoint_story"]
)


@BOTH_NAMES
def test_checkpoint_happy_path(checkpoint_fn, plan_dir, tmp_path, monkeypatch):
    """Both names commit WIP work and record a journal entry identically."""
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

    result = checkpoint_fn(
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


@BOTH_NAMES
def test_checkpoint_appends_multiple_entries_in_order(checkpoint_fn, plan_dir, tmp_path, monkeypatch):
    """Entries are appended in order for both names."""
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

    checkpoint_fn("ck2", "S1", "step-1", "first")
    checkpoint_fn("ck2", "S1", "step-2", "second")

    journal = json.loads((plan_dir / "ck2.S1.journal.json").read_text())
    assert [e["step"] for e in journal] == ["step-1", "step-2"]
    assert [e["commit"] for e in journal] == ["sha-1", "sha-2"]


def test_both_names_share_one_store(plan_dir, tmp_path, monkeypatch):
    """The alias writes to the SAME store as the primary: entries interleave in
    call order regardless of which name is used."""
    worktree = str(tmp_path / "wt")
    _write_manifest(plan_dir, "alpha", {
        "s1": {"summary": "thing", "status": "in_progress", "worktree": worktree},
    })

    shas = iter(["sha-1", "sha-2", "sha-3"])

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = (next(shas) + "\n") if cmd[:2] == ["git", "rev-parse"] else ""
            stderr = ""
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    assert p.checkpoint_story("alpha", "s1", "step-1", "did A")["ok"] is True
    assert p.checkpoint_story("alpha", "s1", "step-2", "did B")["ok"] is True
    assert p.checkpoint("alpha", "s1", "step-3", "did C")["ok"] is True

    journal = json.loads((plan_dir / "alpha.s1.journal.json").read_text())
    assert [e["step"] for e in journal] == ["step-1", "step-2", "step-3"]
    assert [e["commit"] for e in journal] == ["sha-1", "sha-2", "sha-3"]


@BOTH_NAMES
def test_checkpoint_unknown_story_returns_error(checkpoint_fn, plan_dir):
    """Unknown-story error shape is identical for both names."""
    _write_manifest(plan_dir, "ck3", {})
    result = checkpoint_fn("ck3", "NOPE", "step-1", "summary")
    assert result["ok"] is False
    assert "NOPE" in result["error"]


def test_unknown_story_error_shape_is_identical(plan_dir):
    """Both names produce the same error shape for an unknown story_key."""
    _write_manifest(plan_dir, "ck3b", {})
    primary = p.checkpoint("ck3b", "ghost", "step-1", "x")
    alias = p.checkpoint_story("ck3b", "ghost", "step-1", "x")
    assert primary["ok"] is False
    assert alias["ok"] is False
    assert primary["error"] == alias["error"]


def test_failed_alias_call_does_not_corrupt_state(plan_dir, tmp_path, monkeypatch):
    """A failed alias call must not create a spurious journal entry, and a
    subsequent successful call records exactly one entry."""
    worktree = str(tmp_path / "wt")
    _write_manifest(plan_dir, "alpha", {
        "s1": {"summary": "thing", "status": "in_progress", "worktree": worktree},
    })

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = "sha-1\n" if cmd[:2] == ["git", "rev-parse"] else ""
            stderr = ""
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    failed = p.checkpoint_story("alpha", "ghost", "step-1", "x")
    assert failed["ok"] is False
    assert not (plan_dir / "alpha.ghost.journal.json").exists(), (
        "a failed call must not create a journal for the unknown story"
    )

    assert p.checkpoint_story("alpha", "s1", "step-1", "x")["ok"] is True
    journal = json.loads((plan_dir / "alpha.s1.journal.json").read_text())
    assert len(journal) == 1
    assert journal[0]["step"] == "step-1"


@BOTH_NAMES
def test_checkpoint_rejects_traversal_plan_name(checkpoint_fn, plan_dir, monkeypatch):
    """Path-traversal rejection fires for plan_name on both names."""
    with pytest.raises(ValueError, match="invalid"):
        checkpoint_fn("../evil", "S1", "step-1", "summary")


@BOTH_NAMES
def test_checkpoint_rejects_traversal_story_key(checkpoint_fn, plan_dir, monkeypatch):
    """Path-traversal rejection fires for story_key on both names."""
    with pytest.raises(ValueError, match="invalid"):
        checkpoint_fn("myplan", "../evil", "step-1", "summary")


@BOTH_NAMES
def test_checkpoint_accepts_empty_step_and_summary(checkpoint_fn, plan_dir, tmp_path, monkeypatch):
    """Boundary: empty step/summary are accepted -- the alias is not stricter."""
    worktree = str(tmp_path / "wt")
    _write_manifest(plan_dir, "ck6", {
        "S1": {"summary": "thing", "status": "in_progress", "worktree": worktree},
    })

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = "sha-empty\n" if cmd[:2] == ["git", "rev-parse"] else ""
            stderr = ""
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    result = checkpoint_fn("ck6", "S1", "", "")
    assert result["ok"] is True
    assert result["step"] == ""

    journal = json.loads((plan_dir / "ck6.S1.journal.json").read_text())
    assert len(journal) == 1
    assert journal[0]["step"] == ""
    assert journal[0]["summary"] == ""
