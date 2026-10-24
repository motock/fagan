"""dispatch_story resolves dispatch-phase context sources, records
context_digests, and passes the rendered block to the implementer prompt."""
import json

import pytest

from app import backend
from pipeline import concurrency as pcon
from pipeline import context_sources as cs
from pipeline import paths
from pipeline import persistence as ppers
from pipeline import server as p
from pipeline import ticketing as pt


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    for mod in (p, ppers, pcon, paths):
        monkeypatch.setattr(mod, "PLAN_DIR", d)
    return d


@pytest.fixture
def env(plan_dir, tmp_path, monkeypatch):
    wt = tmp_path / "worktrees"
    wt.mkdir()
    monkeypatch.setattr(p, "WORKTREE_ROOT", wt)
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(
        backend.subprocess, "Popen",
        lambda cmd, **kw: type("P", (), {"pid": 1})(),
    )
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    notes, builds = [], []
    monkeypatch.setattr(
        p, "_notify_user", lambda plan, msg, **kw: notes.append((msg, kw)))
    real_build = p._build_dispatch_command

    def spy(*a, **kw):
        spec = real_build(*a, **kw)
        builds.append(spec)
        return spec

    monkeypatch.setattr(p, "_build_dispatch_command", spy)
    # An existing worktree dir makes this a resume, so no git work is needed.
    (wt / "CTX-1").mkdir()
    return notes, builds


def _source(uri, name="ux", required=True):
    return {
        "name": name, "provider": "dir", "uri": str(uri), "phases": ["dispatch"],
        "mode": "prompt", "max_chars": 20000, "authority": "reference",
        "priority": 0, "required": required,
    }


def _write(plan_dir, sources, story_extra=None):
    story = {"summary": "x", "agent_instructions": "BRIEF-TEXT", "status": "todo",
             "dependencies": [], **(story_extra or {})}
    context = {"max_total_chars": 60000, "sources": sources} if sources else None
    manifest = {"epics": {}, "stories": {"CTX-1": story}}
    if context:
        manifest["context"] = context
    (plan_dir / "plan.manifest.json").write_text(json.dumps(manifest))


def _story(plan_dir):
    return json.loads((plan_dir / "plan.manifest.json").read_text())["stories"]["CTX-1"]


@pytest.fixture
def ctx_dir(tmp_path):
    d = tmp_path / "ctx"
    d.mkdir()
    (d / "guide.md").write_text("USE-TABS-ALWAYS\n")
    return d


def test_should_inject_block_once_after_brief(env, plan_dir, ctx_dir):
    _, builds = env
    _write(plan_dir, [_source(ctx_dir)])

    p.dispatch_story("plan", "CTX-1")

    prompt = builds[0]["prompt"]
    assert prompt.count("USE-TABS-ALWAYS") == 1
    assert prompt.index("BRIEF-TEXT") < prompt.index("USE-TABS-ALWAYS")


def test_should_record_context_digests(env, plan_dir, ctx_dir):
    _write(plan_dir, [_source(ctx_dir)])

    p.dispatch_story("plan", "CTX-1")

    entry = _story(plan_dir)["context_digests"]["ux"]
    assert set(entry) == {
        "provider", "uri", "digest", "bytes", "files", "truncated", "fetched_at"}
    assert entry["digest"].startswith("sha256:") and entry["files"] == 1


def test_should_reuse_snapshot_without_refetching(env, plan_dir, ctx_dir, monkeypatch):
    _, builds = env
    _write(plan_dir, [_source(ctx_dir)])
    p.dispatch_story("plan", "CTX-1")
    manifest = json.loads((plan_dir / "plan.manifest.json").read_text())
    manifest["stories"]["CTX-1"]["status"] = "todo"
    (plan_dir / "plan.manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(cs, "resolve_dir_source",
        lambda s: (_ for _ in ()).throw(AssertionError("refetched")))

    p.dispatch_story("plan", "CTX-1")

    assert "USE-TABS-ALWAYS" in builds[1]["prompt"]


def test_should_block_dispatch_when_required_source_fails(env, plan_dir, tmp_path):
    _, builds = env
    _write(plan_dir, [_source(tmp_path / "missing")])

    result = p.dispatch_story("plan", "CTX-1")

    assert result["ok"] is False and "context source" in result["error"]
    assert builds == []
    assert _story(plan_dir)["status"] == "todo"


def test_should_continue_when_optional_source_fails(env, plan_dir, tmp_path):
    notes, builds = env
    _write(plan_dir, [_source(tmp_path / "missing", required=False)])

    p.dispatch_story("plan", "CTX-1")

    assert len(builds) == 1
    assert [kw["event"] for _, kw in notes if kw.get("event") == "context_missing"] == [
        "context_missing"]


def test_should_leave_prompt_unchanged_without_context(env, plan_dir):
    _, builds = env
    _write(plan_dir, None)

    p.dispatch_story("plan", "CTX-1")

    assert "REFERENCE DATA" not in builds[0]["prompt"]
    assert "context_digests" not in _story(plan_dir)
