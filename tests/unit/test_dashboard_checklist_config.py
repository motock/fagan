"""Tests for the dashboard's checklist endpoint (worktree
.agent_plan.md/.agent_scratchpad), progress parsing, and the /api/config
effective-configuration-snapshot endpoint.

Split out of test_dashboard.py to keep it under the project's line-count
target; shared fixtures/helpers moved to tests.unit._dashboard_helpers.
"""
import json
from pathlib import Path

import pytest

from app import dashboard as d
from pipeline import config_provenance
from tests.unit._dashboard_helpers import (  # noqa: F401
    _write_manifest,
    client,
    plan_dir,
)

# ---------- story checklist endpoint (worktree .agent_plan.md/.agent_scratchpad) ----------
#
# Surfaces the tech-lead checklist + running scratchpad the guided-
# decomposition step writes into a story's WORKTREE (not PLAN_DIR), so the
# dashboard can show how far an in-progress story's attempt has gotten.
# Rules mirror the log/journal endpoints:
#   * Returns {plan: {available, text}, scratchpad: {available, text}}.
#   * Most stories have no worktree (never dispatched, or run without
#     PIPELINE_DECOMPOSE) -> available:false, text:"" — NOT a 404 or 500.
#   * A worktree that's been deleted post-merge -> available:false (normal).
#   * The worktree path comes from the manifest only; the resolved path is
#     contain-checked under WORKTREE_ROOT so a hand-edited manifest pointing
#     outside WORKTREE_ROOT cannot read arbitrary files.
#   * 404 only for an unknown plan or story key.


@pytest.fixture
def worktree_dir(tmp_path, monkeypatch):
    """A throwaway WORKTREE_ROOT the dashboard reads worktree artifacts from.
    Mirrors the `plan_dir` fixture's monkeypatch of PLAN_DIR so tests never
    touch the real ~/.claude/worktrees."""
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    monkeypatch.setattr(d, "WORKTREE_ROOT", wt_root)
    from pipeline import server as _srv
    monkeypatch.setattr(_srv, "WORKTREE_ROOT", wt_root)
    return wt_root


def test_checklist_endpoint_returns_both_files_when_present(client, plan_dir, worktree_dir):
    """A story run under PIPELINE_DECOMPOSE has both .agent_plan.md (the
    tech-lead checklist) and .agent_scratchpad.md (running state) in its
    worktree -> both available:true with their text."""
    wt = worktree_dir / "S1"
    wt.mkdir()
    (wt / ".agent_plan.md").write_text("1. write tests\n2. implement\n")
    (wt / ".agent_scratchpad.md").write_text("done: step 1\nnext: step 2\n")
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "guided", "status": "in_progress",
               "worktree": str(wt), "dependencies": []},
    })

    res = client.get("/api/plans/demo/stories/S1/checklist")
    assert res.status_code == 200
    body = res.json()
    assert body["plan"]["available"] is True
    assert body["plan"]["text"] == "1. write tests\n2. implement\n"
    assert body["scratchpad"]["available"] is True
    assert body["scratchpad"]["text"] == "done: step 1\nnext: step 2\n"


def test_checklist_endpoint_partial_when_only_plan_present(client, plan_dir, worktree_dir):
    """The scratchpad is written incrementally by the executor; a story that
    just got its plan but hasn't checkpointed yet has plan:available but
    scratchpad:unavailable — each file is reported independently."""
    wt = worktree_dir / "S1"
    wt.mkdir()
    (wt / ".agent_plan.md").write_text("1. step\n")
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "just planned", "status": "in_progress",
               "worktree": str(wt), "dependencies": []},
    })

    body = client.get("/api/plans/demo/stories/S1/checklist").json()
    assert body["plan"]["available"] is True
    assert body["plan"]["text"] == "1. step\n"
    assert body["scratchpad"]["available"] is False
    assert body["scratchpad"]["text"] == ""


def test_checklist_endpoint_no_worktree_field_returns_unavailable(client, plan_dir, worktree_dir):
    """A story that was never dispatched (still todo) has no 'worktree' field
    — the normal case for most stories. available:false for both, never a 404
    or 500."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "fresh", "status": "todo", "dependencies": []},
    })
    body = client.get("/api/plans/demo/stories/S1/checklist").json()
    assert body == {"plan": {"available": False, "text": ""},
                    "scratchpad": {"available": False, "text": ""},
                    "progress": None}


def test_checklist_endpoint_worktree_dir_gone_returns_unavailable_not_500(client, plan_dir, worktree_dir):
    """A worktree recorded in the manifest but deleted on disk (post-merge
    cleanup) is a normal state, not an error — must degrade to available:false
    rather than 500."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "merged", "status": "done",
               "worktree": str(worktree_dir / "gone-S1"), "dependencies": []},
    })
    res = client.get("/api/plans/demo/stories/S1/checklist")
    assert res.status_code == 200
    assert res.json() == {"plan": {"available": False, "text": ""},
                          "scratchpad": {"available": False, "text": ""},
                          "progress": None}


def test_checklist_endpoint_empty_file_is_available_with_empty_text(client, plan_dir, worktree_dir):
    """Boundary: a zero-byte .agent_plan.md exists (planner wrote nothing /
    truncated) -> available:true with text:'', distinguishing 'file present
    but empty' from 'file absent'."""
    wt = worktree_dir / "S1"
    wt.mkdir()
    (wt / ".agent_plan.md").write_text("")
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "empty plan", "status": "in_progress",
               "worktree": str(wt), "dependencies": []},
    })
    body = client.get("/api/plans/demo/stories/S1/checklist").json()
    assert body["plan"]["available"] is True
    assert body["plan"]["text"] == ""


def test_checklist_endpoint_garbage_bytes_decode_replacement(client, plan_dir, worktree_dir):
    """Non-UTF-8 bytes in an agent-written artifact must not 500; replacement
    characters are accepted so the dashboard surfaces whatever's on disk."""
    wt = worktree_dir / "S1"
    wt.mkdir()
    (wt / ".agent_plan.md").write_bytes(b"good\n\xff\xfe\nmore\n")
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "binary", "status": "in_progress",
               "worktree": str(wt), "dependencies": []},
    })
    body = client.get("/api/plans/demo/stories/S1/checklist").json()
    assert body["plan"]["available"] is True
    assert body["plan"]["text"].startswith("good\n")
    assert "�" in body["plan"]["text"]


def test_checklist_endpoint_404_for_unknown_plan(client, plan_dir, worktree_dir):
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "x", "status": "todo", "dependencies": []},
    })
    assert client.get("/api/plans/nope/stories/S1/checklist").status_code == 404


def test_checklist_endpoint_404_for_unknown_story(client, plan_dir, worktree_dir):
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "x", "status": "todo", "dependencies": []},
    })
    assert client.get("/api/plans/demo/stories/NOPE/checklist").status_code == 404


def test_read_worktree_file_rejects_worktree_outside_root(client, plan_dir, worktree_dir, tmp_path):
    """Security: a manifest hand-edited to point worktree outside WORKTREE_ROOT
    (e.g. '/etc') must not let the dashboard read arbitrary files. The
    resolved path is contain-checked; an outside-root worktree degrades to
    available:false."""
    outside = tmp_path / "outside-root"
    outside.mkdir()
    (outside / ".agent_plan.md").write_text("SECRET")
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "evil", "status": "in_progress",
               "worktree": str(outside), "dependencies": []},
    })
    body = client.get("/api/plans/demo/stories/S1/checklist").json()
    assert body["plan"]["available"] is False
    assert "SECRET" not in body["plan"]["text"]


def test_read_worktree_file_rejects_relative_worktree_path(client, plan_dir, worktree_dir):
    """A non-absolute worktree (corrupt manifest) is not resolvable safely ->
    unavailable, never a 500. The orchestrator always stores an absolute path,
    so a relative one is a corruption signal we fail closed on."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "corrupt", "status": "in_progress",
               "worktree": "S1", "dependencies": []},
    })
    body = client.get("/api/plans/demo/stories/S1/checklist").json()
    assert body["plan"]["available"] is False
    assert body["scratchpad"]["available"] is False




# ---------------------------------------------------------------------------
# Per-story progress (Tier 1): _parse_progress helper + endpoint decoration.
# ---------------------------------------------------------------------------

def test_parse_progress_returns_done_and_total():
    """Plan with 3 numbered items and a scratchpad PROGRESS: 1/3 line yields
    {done: 1, total: 3}."""
    plan = "1. write tests\n2. implement\n3. refactor\n"
    scratch = "PROGRESS: 1/3\ndone: step 1\nnext: step 2\n"
    assert d._parse_progress(plan, scratch) == {"done": 1, "total": 3}


def test_parse_progress_returns_none_when_no_plan():
    """No plan text (None or empty) -> None (fail open)."""
    assert d._parse_progress(None, "PROGRESS: 1/3\n") is None
    assert d._parse_progress("", "PROGRESS: 1/3\n") is None


def test_parse_progress_returns_none_when_no_scratchpad():
    """No scratchpad text (None or empty) -> None (fail open)."""
    assert d._parse_progress("1. step\n", None) is None
    assert d._parse_progress("1. step\n", "") is None


def test_parse_progress_returns_none_when_no_progress_line():
    """A scratchpad without a PROGRESS: line (old format) -> None, never raises."""
    plan = "1. step\n2. step\n"
    scratch = "done: step 1\nnext: step 2\n"
    assert d._parse_progress(plan, scratch) is None


def test_parse_progress_returns_none_when_no_numbered_items():
    """A plan with no numbered items (total == 0) -> None."""
    plan = "Some prose without numbered items.\nMore prose.\n"
    scratch = "PROGRESS: 1/3\n"
    assert d._parse_progress(plan, scratch) is None


def test_parse_progress_never_raises_on_garbage():
    """The helper must fail open — never raise — on malformed inputs."""
    # Malformed PROGRESS line (missing total) -> None, not an exception.
    assert d._parse_progress("1. step\n", "PROGRESS: 2/\n") is None
    # Non-numeric done -> None.
    assert d._parse_progress("1. step\n", "PROGRESS: x/3\n") is None
    # PROGRESS line not anchored at line start -> None.
    assert d._parse_progress("1. step\n", "  PROGRESS: 1/1\n") is None


def test_parse_progress_counts_only_numbered_lines():
    """Total counts only lines starting with a digit followed by a period;
    prose lines and sub-bullets are ignored."""
    plan = (
        "1. first\n"
        "- a sub bullet\n"
        "2. second\n"
        "some prose\n"
        "3. third\n"
    )
    scratch = "PROGRESS: 2/3\n"
    result = d._parse_progress(plan, scratch)
    assert result == {"done": 2, "total": 3}


def test_checklist_endpoint_includes_progress_field(client, plan_dir, worktree_dir):
    """End-to-end: a worktree with a 3-item plan and PROGRESS: 1/3 scratchpad
    returns progress: {done: 1, total: 3} from the /checklist endpoint."""
    wt = worktree_dir / "S1"
    wt.mkdir()
    (wt / ".agent_plan.md").write_text("1. write tests\n2. implement\n3. refactor\n")
    (wt / ".agent_scratchpad.md").write_text("PROGRESS: 1/3\ndone: step 1\nnext: step 2\n")
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "guided", "status": "in_progress",
               "worktree": str(wt), "dependencies": []},
    })

    body = client.get("/api/plans/demo/stories/S1/checklist").json()
    assert body["progress"] is not None
    assert body["progress"]["done"] == 1
    assert body["progress"]["total"] == 3


def test_checklist_endpoint_progress_null_when_no_scratchpad(client, plan_dir, worktree_dir):
    """Plan present but no scratchpad -> progress: null (fail open)."""
    wt = worktree_dir / "S1"
    wt.mkdir()
    (wt / ".agent_plan.md").write_text("1. step\n")
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "just planned", "status": "in_progress",
               "worktree": str(wt), "dependencies": []},
    })

    body = client.get("/api/plans/demo/stories/S1/checklist").json()
    assert body["progress"] is None


def test_checklist_endpoint_progress_null_when_no_progress_line(client, plan_dir, worktree_dir):
    """Scratchpad present but without a PROGRESS: line -> progress: null."""
    wt = worktree_dir / "S1"
    wt.mkdir()
    (wt / ".agent_plan.md").write_text("1. step\n")
    (wt / ".agent_scratchpad.md").write_text("done: step 1\n")
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "old format", "status": "in_progress",
               "worktree": str(wt), "dependencies": []},
    })

    body = client.get("/api/plans/demo/stories/S1/checklist").json()
    assert body["progress"] is None


def test_checklist_endpoint_progress_null_when_no_numbered_items(client, plan_dir, worktree_dir):
    """Plan present but with no numbered items -> progress: null."""
    wt = worktree_dir / "S1"
    wt.mkdir()
    (wt / ".agent_plan.md").write_text("Some prose without numbered items.\n")
    (wt / ".agent_scratchpad.md").write_text("PROGRESS: 1/3\n")
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "prose plan", "status": "in_progress",
               "worktree": str(wt), "dependencies": []},
    })

    body = client.get("/api/plans/demo/stories/S1/checklist").json()
    assert body["progress"] is None


def test_get_plan_decorates_in_progress_story_with_progress(client, plan_dir, worktree_dir):
    """The /api/plans/{name} endpoint decorates in_progress stories with a
    progress field when the worktree has parseable plan+scratchpad."""
    wt = worktree_dir / "S1"
    wt.mkdir()
    (wt / ".agent_plan.md").write_text("1. step a\n2. step b\n")
    (wt / ".agent_scratchpad.md").write_text("PROGRESS: 1/2\n")
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "guided", "status": "in_progress",
               "worktree": str(wt), "dependencies": []},
    })

    body = client.get("/api/plans/demo").json()
    stories = body["stories"]
    assert "S1" in stories
    assert stories["S1"].get("progress") is not None
    assert stories["S1"]["progress"]["done"] == 1
    assert stories["S1"]["progress"]["total"] == 2


def test_get_plan_does_not_decorate_todo_story_with_progress(client, plan_dir, worktree_dir):
    """A todo story (no worktree) must not get a progress field."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "fresh", "status": "todo", "dependencies": []},
    })

    body = client.get("/api/plans/demo").json()
    assert "progress" not in body["stories"]["S1"]


def test_get_plan_does_not_decorate_done_story_with_progress(client, plan_dir, worktree_dir):
    """A done story must not get a progress field even if a worktree exists."""
    wt = worktree_dir / "S1"
    wt.mkdir()
    (wt / ".agent_plan.md").write_text("1. step a\n2. step b\n")
    (wt / ".agent_scratchpad.md").write_text("PROGRESS: 2/2\n")
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "merged", "status": "done",
               "worktree": str(wt), "dependencies": []},
    })

    body = client.get("/api/plans/demo").json()
    assert "progress" not in body["stories"]["S1"]


# ---------------------------------------------------------------------------
# Scratchpad prompt in pipeline/server.py must require a PROGRESS: line.
# ---------------------------------------------------------------------------

def test_server_scratchpad_prompt_requires_progress_line():
    """The scratchpad instruction in pipeline/dispatch.py must instruct the
    executor to write a PROGRESS: <done>/<total> line as the FIRST line of
    .agent_scratchpad.md."""
    import inspect

    from pipeline import dispatch

    # The instruction is built inside a function; inspect the source of the
    # module so we assert on the literal string content the implementer must
    # keep in sync with the plan.
    source = inspect.getsource(dispatch)
    assert "PROGRESS: <done>/<total>" in source, (
        "scratchpad prompt must mention 'PROGRESS: <done>/<total>'"
    )
    assert "PROGRESS: 2/5" in source, (
        "scratchpad prompt must include the 'PROGRESS: 2/5' example"
    )
    assert "The FIRST line must be" in source, (
        "scratchpad prompt must state the PROGRESS line is the FIRST line"
    )
    # The old wording that did NOT require a PROGRESS line must be gone.
    assert "keep a short running summary of what you've" not in source or (
        "PROGRESS:" in source
    )


# ---------- /api/config (effective configuration snapshot) ----------


@pytest.fixture
def isolated_config_sources(monkeypatch, tmp_path):
    """Point config_provenance's source files at nonexistent paths under
    tmp_path so /api/config tests never read this machine's real
    ~/.claude.json or launchd plist (which may hold real secrets)."""
    monkeypatch.setenv("PIPELINE_SCHEDULER_PLIST_PATH", str(tmp_path / "no-scheduler.plist"))
    monkeypatch.setenv("PIPELINE_CLAUDE_JSON_PATH", str(tmp_path / "no-claude.json"))
    return tmp_path


@pytest.fixture
def known_registry(monkeypatch):
    """A small, deterministic model_registry.json substitute (mirrors the
    fixture in tests/unit/test_get_effective_config.py) so the plan-override
    test doesn't depend on the real repo's model_registry.json contents."""
    registry = {
        "providers": {
            "claude": {"models": {"sonnet": {"tag": "sonnet"}}},
            "mlx": {"models": {"qwen": {"tag": "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"}}},
        },
        "roles": {},
    }
    monkeypatch.setattr(d.role_registry, "load_registry", lambda *a, **k: registry)
    return registry


def test_effective_config_wiring_returns_all_roles(client, plan_dir, isolated_config_sources):
    res = client.get("/api/config")
    assert res.status_code == 200
    body = res.json()
    assert set(body.keys()) == {"roles", "env", "ignored_env_vars", "sources"}
    role_names = {entry["role"] for entry in body["roles"]}
    assert role_names == set(config_provenance.PIPELINE_ROLES)


def test_effective_config_plan_override_reports_provider_and_source(
    client, plan_dir, isolated_config_sources, known_registry
):
    (plan_dir / "cfgplan.manifest.json").write_text(json.dumps({
        "epics": {}, "stories": {}, "repo_root": "/tmp",
        "role_config": {"review": {"provider": "mlx", "model": "qwen"}},
    }))

    res = client.get("/api/config?plan=cfgplan")

    assert res.status_code == 200
    roles_by_name = {entry["role"]: entry for entry in res.json()["roles"]}
    review = roles_by_name["review"]
    assert review["provider"] == "mlx"
    assert review["provider_source"] == "plan_role_config"


def test_effective_config_nonexistent_plan_resolves_with_no_overrides(
    client, plan_dir, isolated_config_sources
):
    res = client.get("/api/config?plan=nope")

    assert res.status_code == 200
    roles_by_name = {entry["role"]: entry for entry in res.json()["roles"]}
    for entry in roles_by_name.values():
        assert entry["provider_source"] != "plan_role_config"


def test_effective_config_malformed_manifest_resolves_with_no_overrides(
    client, plan_dir, isolated_config_sources
):
    (plan_dir / "broken.manifest.json").write_text("{not valid json")

    res = client.get("/api/config?plan=broken")

    assert res.status_code == 200
    roles_by_name = {entry["role"]: entry for entry in res.json()["roles"]}
    for entry in roles_by_name.values():
        assert entry["provider_source"] != "plan_role_config"


def test_effective_config_never_leaks_secret_value(
    client, plan_dir, isolated_config_sources, monkeypatch
):
    secret = "sk-super-secret-token-value-12345"
    monkeypatch.setenv("PIPELINE_TEST_API_KEY", secret)

    res = client.get("/api/config")

    assert res.status_code == 200
    assert secret not in res.text


def test_dashboard_module_imports_pipeline_service_for_scoped_write_surface():
    """W1c (docs/plans/PLATFORM_DECOUPLING_AND_SCALE_PLAN.md) deliberately
    gives app/dashboard.py write access via a `PipelineService` singleton
    (see the module docstring) - it must import pipeline.server for that,
    but the write surface stays scoped to PipelineService: it must not
    import app.pipeline_mcp_server or app.backend directly."""
    import ast

    source = Path("app/dashboard.py").read_text()
    tree = ast.parse(source)
    imported_modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)

    assert "pipeline.server" in imported_modules, imported_modules
    still_forbidden = {"app.pipeline_mcp_server", "app.backend"}
    assert not (still_forbidden & set(imported_modules)), imported_modules


def test_effective_config_endpoint_performs_no_writes(client, plan_dir, isolated_config_sources):
    _write_manifest(plan_dir, "untouched", {"S1": {"summary": "x", "status": "todo"}})
    before = {
        p.name: p.read_bytes() for p in sorted(plan_dir.iterdir())
    }

    res = client.get("/api/config?plan=untouched")

    assert res.status_code == 200
    after = {
        p.name: p.read_bytes() for p in sorted(plan_dir.iterdir())
    }
    assert after == before
