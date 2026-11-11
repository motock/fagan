"""ADR 0012 decision 5: the plan document is rendered from the manifest on demand.

The plan document (the ``save_plan`` schema: ``{"epics": [{"summary",
"stories": [...]}]}``) is rendered from an ingested manifest by
``pipeline.plan_export.render_plan_document`` and exposed as the read tool
``export_plan``. What is graded here:

* the render: the document's shape, the story fields it carries, the runtime
  state it must never carry, the context sources it keeps (plan-origin only)
  and the legacy-manifest shape;
* the round trip: rendering an ingested manifest and ingesting the result
  again yields the same canonical digest, so an approval still binds to the
  content it approved;
* the tool: the service method, the MCP registration, the remote read scoping
  (a plan the caller may not see answers exactly like a plan that is not
  there, so the reply is no existence oracle) and the documentation pins.

``render_plan_document`` is imported defensively so that a missing module
fails an *assertion* (one per requirement) instead of breaking collection for
the whole suite.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest

# pipeline.server must load before pipeline.ingest (import-cycle note, see
# tests/unit/test_ingest_keyless_reingest.py).
import pipeline.server as p
from app.identity import Principal
from pipeline import concurrency as pcon
from pipeline import ingest as ingest_mod
from pipeline import persistence as ppers
from pipeline import remote_tool_policy
from pipeline.actor import bind_principal
from pipeline.plan_approval import canonical_digest
from pipeline.service import PipelineService

try:  # the module under test does not exist yet
    from pipeline.plan_export import render_plan_document
except ImportError as error:  # pragma: no cover - the red state
    render_plan_document = None
    _IMPORT_ERROR = error
else:
    _IMPORT_ERROR = None

REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE = REPO_ROOT / "REFERENCE.md"

#: The unknown-plan refusals the existing plan read/write paths use. The
#: export must reuse one of them instead of inventing a new wording.
UNKNOWN_PLAN_ERRORS = {
    "Plan not found",
    "unknown plan/story",
    "unknown plan or story",
}

#: A non-local principal: the local operator is refused every remote tool.
STRANGER = Principal(subject="https://idp#eve", auth_method="oidc")
OTHER = Principal(subject="https://idp#alice", auth_method="oidc")

_PREFLIGHT = "Preflight: test fixture - not a real plan"


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def plan_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every PLAN_DIR binding at a throwaway directory."""
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", directory)
    monkeypatch.setattr(ppers, "PLAN_DIR", directory)
    monkeypatch.setattr(pcon, "PLAN_DIR", directory)
    return directory


class _RecordingProvider:
    """A ticket provider that issues its own ids, so the manifest key and the
    authored (plan) key differ."""

    def create_epic(self, summary: str) -> str:
        return f"epic-{summary}"

    def create_story(self, summary, description, epic_id, agent) -> str:
        return f"issue-{summary}"


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> _RecordingProvider:
    prov = _RecordingProvider()
    monkeypatch.setattr(p, "get_ticket_provider", lambda: prov)
    monkeypatch.setattr(ingest_mod, "_notify_user", lambda *a, **k: None)
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    return prov


def _render(manifest: dict | None) -> dict:
    """Render *manifest*, failing an assertion when the module is missing."""
    assert render_plan_document is not None, (
        "pipeline.plan_export.render_plan_document is missing: "
        f"{_IMPORT_ERROR}"
    )
    return render_plan_document(manifest)


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    (repo / "README.md").write_text("hello\n")
    return repo


def _story(summary: str, **extra) -> dict:
    story = {
        "summary": summary,
        "agent_instructions": _PREFLIGHT + "\nImplement it.",
    }
    story.update(extra)
    return story


def _write_plan(plan_dir: Path, name: str, repo_root: Path, epics, **extra) -> dict:
    plan = {"name": name, "repo_root": str(repo_root), "epics": epics}
    plan.update(extra)
    (plan_dir / f"{name}.json").write_text(json.dumps(plan))
    return plan


def _manifest_of(plan_name: str) -> dict:
    return json.loads(ingest_mod._store.manifest_path(plan_name).read_text())


def _fixture_epics() -> list[dict]:
    return [
        {
            "summary": "First epic",
            "stories": [
                _story(
                    "Alpha story",
                    key="A1",
                    description="Alpha does a thing.",
                    dependencies=["B1"],
                    persona="software-engineer",
                    model="opus",
                    acceptance=[{"path": "tests/test_alpha.py", "source": "passes"}],
                    risk="medium",
                    backend="claude",
                    tdd_split=True,
                ),
                _story("Beta story", key="B1", description="Beta does a thing."),
            ],
        },
        {
            "summary": "Second epic",
            "stories": [
                _story("Gamma story", key="G1", description="Gamma does a thing."),
            ],
        },
    ]


def _ingest(plan_dir: Path, name: str, repo_root: Path, epics, **extra) -> dict:
    _write_plan(plan_dir, name, repo_root, epics, **extra)
    result = ingest_mod._ingest_plan_impl(name)
    assert result.get("ok") is True, result
    return _manifest_of(name)


def _exported_stories(document: dict) -> dict[str, dict]:
    """Return the exported stories keyed by their exported key."""
    return {
        story["key"]: story
        for epic in document["epics"]
        for story in epic["stories"]
    }


# --------------------------------------------------------------------------- #
# The render
# --------------------------------------------------------------------------- #
def test_render_produces_a_save_plan_shaped_document(plan_dir, provider, tmp_path):
    repo = _repo(tmp_path)
    manifest = _ingest(plan_dir, "shape", repo, _fixture_epics())

    document = _render(manifest)

    assert isinstance(document, dict)
    assert document["repo_root"] == str(repo)
    assert document["team"] == manifest["team"]
    assert document.get("role_config", {}) == manifest.get("role_config", {})
    assert [epic["summary"] for epic in document["epics"]] == [
        "First epic",
        "Second epic",
    ]
    first = document["epics"][0]["stories"][0]
    assert first["key"] == "A1"
    assert first["summary"] == "Alpha story"
    assert first["description"] == "Alpha does a thing."
    assert first["agent_instructions"].startswith(_PREFLIGHT)
    assert first["persona"] == "software-engineer"
    assert first["model"] == "opus"
    assert first["acceptance"] == [{"path": "tests/test_alpha.py", "source": "passes"}]
    assert first["risk"] == "medium"
    assert first["backend"] == "claude"
    assert first["tdd_split"] is True


def test_render_translates_dependencies_back_to_plan_keys(plan_dir, provider, tmp_path):
    repo = _repo(tmp_path)
    manifest = _ingest(plan_dir, "deps", repo, _fixture_epics())
    # The manifest keys are the provider's issue ids, the plan keys are A1/B1.
    assert set(manifest["stories"]) == {
        "issue-Alpha story",
        "issue-Beta story",
        "issue-Gamma story",
    }

    document = _render(manifest)

    stories = _exported_stories(document)
    assert set(stories) == {"A1", "B1", "G1"}
    assert stories["A1"]["dependencies"] == ["B1"]


def test_render_keeps_the_manifest_key_when_the_story_has_no_plan_key(
    plan_dir, provider, tmp_path
):
    repo = _repo(tmp_path)
    manifest = _ingest(plan_dir, "nokey", repo, _fixture_epics())
    manifest["stories"]["issue-Gamma story"].pop("plan_key", None)

    document = _render(manifest)

    keys = [story["key"] for epic in document["epics"] for story in epic["stories"]]
    assert "issue-Gamma story" in keys


def test_render_never_exports_runtime_state(plan_dir, provider, tmp_path):
    repo = _repo(tmp_path)
    manifest = _ingest(plan_dir, "runtime", repo, _fixture_epics())
    story = manifest["stories"]["issue-Alpha story"]
    story.update(
        {
            "status": "in_progress",
            "attempts": 3,
            "pr_url": "https://example.test/pr/1",
            "worktree": "/tmp/wt",
            "review_verdict": "approved",
            "review_score": 9,
            "log": "some log",
            "approval": {"status": "approved"},
            "owner": "https://idp#alice",
            "delegates": ["https://idp#dee"],
            "ingested_by": "someone",
            "sizing_auto_routed": True,
            "pid": 4242,
        }
    )

    document = _render(manifest)

    exported = _exported_stories(document)["A1"]
    for runtime_field in (
        "status",
        "attempts",
        "pr_url",
        "worktree",
        "review_verdict",
        "review_score",
        "log",
        "approval",
        "owner",
        "delegates",
        "ingested_by",
        "sizing_auto_routed",
        "pid",
    ):
        assert runtime_field not in exported, runtime_field
    for plan_field in ("approval", "owner", "delegates", "ingested_by", "status"):
        assert plan_field not in document, plan_field


def test_render_keeps_only_plan_origin_context_sources(plan_dir, provider, tmp_path):
    repo = _repo(tmp_path)
    (tmp_path / "plan-spec").mkdir()
    plan_context = {
        "sources": [
            {
                "name": "plan-spec",
                "provider": "dir",
                "uri": str(tmp_path / "plan-spec"),
                "phases": ["dispatch"],
            }
        ]
    }
    manifest = _ingest(
        plan_dir, "ctx", repo, _fixture_epics(), context=plan_context
    )
    # The manifest's context is the merged one: plan + repo + org sources.
    manifest["context"]["sources"].append(
        {
            "name": "repo-spec",
            "provider": "dir",
            "uri": str(tmp_path / "repo-spec"),
            "phases": ["dispatch"],
            "origin": "repo",
        }
    )
    manifest["context"]["sources"].append(
        {
            "name": "org-spec",
            "provider": "dir",
            "uri": str(tmp_path / "org-spec"),
            "phases": ["dispatch"],
            "origin": "org",
        }
    )

    document = _render(manifest)

    sources = document.get("context", {}).get("sources", [])
    names = [source["name"] for source in sources]
    assert "plan-spec" in names
    assert "repo-spec" not in names
    assert "org-spec" not in names


def test_render_of_a_legacy_manifest_uses_one_ingested_stories_epic():
    legacy = {
        "repo_root": "/tmp/repo",
        "team": "local",
        "stories": {
            "S1": {"summary": "One", "status": "todo"},
            "S2": {"summary": "Two", "status": "todo"},
        },
    }

    document = _render(legacy)

    assert [epic["summary"] for epic in document["epics"]] == ["Ingested stories"]
    stories = document["epics"][0]["stories"]
    assert [story["key"] for story in stories] == ["S1", "S2"]


def test_render_refuses_a_manifest_without_stories():
    with pytest.raises(ValueError) as none_error:
        _render(None)
    assert "manifest has no stories" in str(none_error.value)

    with pytest.raises(ValueError) as empty_error:
        _render({"repo_root": "/tmp/repo"})
    assert "manifest has no stories" in str(empty_error.value)


def test_render_does_not_mutate_the_manifest(plan_dir, provider, tmp_path):
    repo = _repo(tmp_path)
    manifest = _ingest(plan_dir, "nomutate", repo, _fixture_epics())
    before = copy.deepcopy(manifest)

    _render(manifest)

    assert manifest == before


def test_rendered_document_passes_the_save_plan_validations(
    plan_dir, provider, tmp_path
):
    repo = _repo(tmp_path)
    manifest = _ingest(plan_dir, "saveable", repo, _fixture_epics())

    document = _render(manifest)

    assert "epics" in document
    assert isinstance(document["epics"], list)
    result = PipelineService().save_plan("exported-plan", json.dumps(document))
    assert result.get("ok") is True, result


# --------------------------------------------------------------------------- #
# The round trip
# --------------------------------------------------------------------------- #
def test_render_then_ingest_keeps_the_canonical_digest(plan_dir, provider, tmp_path):
    repo = _repo(tmp_path)
    manifest = _ingest(plan_dir, "first", repo, _fixture_epics())
    document = _render(manifest)

    # Stage the rendered document as a draft plan and ingest it under a fresh
    # name: the second manifest must be the same authored content.
    (plan_dir / "second.json").write_text(json.dumps(document))
    second_result = ingest_mod._ingest_plan_impl("second")
    assert second_result.get("ok") is True, second_result
    second = _manifest_of("second")

    assert second["repo_root"] == manifest["repo_root"]
    assert canonical_digest(second) == canonical_digest(manifest)
    for key, story in manifest["stories"].items():
        other = second["stories"][key]
        assert other["description"] == story["description"]
        assert other["epic"] == story["epic"]
        assert other.get("plan_key") == story.get("plan_key")
    assert second["plan_epics"] == manifest["plan_epics"]


# --------------------------------------------------------------------------- #
# The tool
# --------------------------------------------------------------------------- #
def test_export_plan_returns_the_rendered_document(plan_dir, provider, tmp_path):
    repo = _repo(tmp_path)
    manifest = _ingest(plan_dir, "exported", repo, _fixture_epics())
    service = PipelineService()

    assert hasattr(service, "export_plan"), "PipelineService.export_plan is missing"
    result = service.export_plan("exported")

    assert result.get("ok") is True, result
    document = result["plan"]
    assert document["repo_root"] == str(repo)
    assert [epic["summary"] for epic in document["epics"]] == [
        "First epic",
        "Second epic",
    ]
    assert document == _render(manifest)


def test_export_plan_of_an_unknown_plan_is_a_refusal(plan_dir, provider, tmp_path):
    repo = _repo(tmp_path)
    _ingest(plan_dir, "known", repo, _fixture_epics())
    service = PipelineService()
    assert hasattr(service, "export_plan"), "PipelineService.export_plan is missing"

    result = service.export_plan("no-such-plan")

    assert result.get("ok") is False, result
    assert isinstance(result.get("error"), str) and result["error"]
    assert result["error"] in UNKNOWN_PLAN_ERRORS | {
        f"No such plan {'no-such-plan'!r}"
    }


def test_export_plan_refuses_an_invalid_plan_name_before_any_read(
    plan_dir, provider, tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    _ingest(plan_dir, "valid", repo, _fixture_epics())
    service = PipelineService()
    assert hasattr(service, "export_plan"), "PipelineService.export_plan is missing"

    def _boom(*args, **kwargs):
        raise AssertionError("the manifest was read before the plan name was validated")

    class _Records:
        def get_manifest(self, *args, **kwargs):
            _boom()

    monkeypatch.setattr(p._store, "get_manifest", _boom)
    monkeypatch.setattr(p._store, "get_manifest_or_none", _boom)
    monkeypatch.setattr(p._store, "records", lambda: _Records())

    try:
        result = service.export_plan("../escape")
    except ValueError as error:
        message = str(error)
    else:
        assert isinstance(result, dict), result
        assert result.get("ok") is False, result
        message = str(result.get("error", ""))
    assert "invalid" in message.lower() or "escape" in message


def test_export_plan_is_registered_as_an_mcp_tool():
    names = [tool.name for tool in p.mcp._tool_manager.list_tools()]
    assert "export_plan" in names, "export_plan is not registered as an MCP tool"
    assert names.count("export_plan") == 1


def test_export_plan_is_a_remote_read_tool():
    """``export_plan`` is a plan-scoped remote read, never an existence oracle.

    A read tool listed as a remote read must be plan-scoped, or it would answer
    differently for a plan that exists and one that does not. The assertion
    pins the pair together so an allowlisting that half-lands fails here.
    """
    remote_read = remote_tool_policy.REMOTE_READ
    plan_scoped = remote_tool_policy.PLAN_SCOPED_READ

    assert "export_plan" in remote_read, "export_plan must be a remote read tool"
    assert "export_plan" in plan_scoped, (
        "export_plan must be plan-scoped: an unscoped read is an existence oracle"
    )
    assert "export_plan" not in remote_tool_policy.REMOTE_ENFORCED
    assert remote_tool_policy.remote_tool_allowed("export_plan") is True


def test_remote_caller_cannot_export_a_plan_it_may_not_see(plan_dir, tmp_path):
    """A hidden plan answers exactly like a plan that is not there."""
    assert hasattr(p, "export_plan"), "export_plan is not exposed on the server"
    (plan_dir / "alpha.manifest.json").write_text(
        json.dumps(
            {
                "epics": {},
                "stories": {
                    "S1": {
                        "summary": "Secret summary",
                        "status": "todo",
                        "owner": OTHER.subject,
                    }
                },
                "owner": OTHER.subject,
            }
        )
    )

    calls: list[str] = []

    async def _call(plan_name: str):
        async def _call_next(ctx):
            calls.append(plan_name)
            return p.export_plan(plan_name)

        class _Ctx:
            method = "tools/call"

            def __init__(self) -> None:
                self.params = {
                    "name": "export_plan",
                    "arguments": {"plan_name": plan_name},
                }

        with bind_principal(STRANGER):
            return await remote_tool_policy.remote_tool_policy(_Ctx(), _call_next)

    hidden = asyncio.run(_call("alpha"))
    missing = asyncio.run(_call("ghost"))

    assert hidden == missing, "a hidden plan must answer like a missing one"
    assert _is_refusal(hidden)
    assert "Secret summary" not in json.dumps(hidden, default=str)
    # The refusal comes from the plan-scoped check, before the handler runs, so
    # a remote caller reaches neither the hidden plan nor the missing one and
    # the reply is no existence oracle.
    assert calls == [], "the tool ran for a remote caller the policy refuses"


def _is_refusal(reply: Any) -> bool:
    if isinstance(reply, dict):
        if reply.get("isError") is True:
            return True
        if "ok" in reply:
            return reply.get("ok") is False
    return bool(getattr(reply, "isError", False))


# --------------------------------------------------------------------------- #
# The documentation pins
# --------------------------------------------------------------------------- #
def test_reference_documents_export_plan_as_a_tool():
    # The "MCP tools reference" section is byte-pinned against the pre-split
    # README (tests/unit/test_readme_reference_split.py), so the tool is
    # documented beside the remote read allowlist instead.
    text = REFERENCE.read_text(encoding="utf-8")
    blocks = [block for block in text.split("\n\n") if block.startswith("`export_plan(plan_name)`")]

    assert blocks, "REFERENCE.md must describe export_plan in its own paragraph"
    assert "never exported" in blocks[0], "the paragraph must say runtime state is not exported"


def test_reference_documents_export_plan_in_the_remote_read_scoping_list():
    text = REFERENCE.read_text(encoding="utf-8")
    paragraphs = [block for block in text.split("\n\n") if "read-only tools" in block]

    assert paragraphs, "REFERENCE.md must document the remote read allowlist"
    documented = any("export_plan" in block for block in paragraphs)
    # Documentation may not promise more than the policy delivers: the tool is
    # listed in that paragraph exactly when it is on the remote read allowlist.
    assert documented == ("export_plan" in remote_tool_policy.REMOTE_READ), (
        "REFERENCE.md's remote read-scoping list must agree with "
        "pipeline/remote_tool_policy.py"
    )
