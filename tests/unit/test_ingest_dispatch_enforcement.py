"""AUTH-07: ``plan:ingest`` and ``plan:dispatch`` are authorised.

Both actions are in ``app.policy.Action`` and both were unenforced: one writes a
plan into the registry, the other starts an agent. The check lives in
``PipelineService.ingest_plan`` and in ``_dispatch_story_impl`` -- not in the
transport -- so the MCP tool, the dashboard route and the chat adapter all pass
through it.

The local single-operator path must stay byte-identical: with nothing bound (or
``local-operator`` bound) both calls behave exactly as they did before the
check existed.

Fixtures are copied from ``tests/unit/test_authorization.py`` (``plan_dir``) and
``tests/unit/test_ingest_team.py`` (the ingest plan/provider harness) per this
repo's convention -- there is no shared conftest.py for these.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

# pipeline.server must load before pipeline.ingest (see
# tests/unit/test_ingest_keyless_reingest.py for the import-cycle note).
import pipeline.server as p
from app.identity import LOCAL_OPERATOR
from pipeline import concurrency as pcon
from pipeline import dispatch as dispatch_mod
from pipeline import ingest as ingest_mod
from pipeline import parsers
from pipeline import persistence as ppers
from pipeline.actor import ANONYMOUS, bind_principal
from pipeline.authorization import ENFORCE_ENV, authorize

#: A principal the local policy denies for every action.
DENIED = ANONYMOUS


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Point every PLAN_DIR binding at a throwaway directory.

    ``pipeline_persistence`` and ``pipeline_concurrency`` import ``PLAN_DIR``
    from ``pipeline_paths`` at module load and read it as a free variable, so
    the patch has to land on their own bindings too.
    """
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    monkeypatch.setenv(ENFORCE_ENV, "")
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    return d


class _RecordingProvider:
    """A ticket provider that records instead of calling Plane."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def create_epic(self, summary: str):
        self.calls.append(("create_epic", summary))

    def create_story(self, summary, description, epic_id, agent):
        self.calls.append(("create_story", summary))


@pytest.fixture
def provider(monkeypatch):
    prov = _RecordingProvider()
    monkeypatch.setattr(p, "get_ticket_provider", lambda: prov)
    monkeypatch.setattr(ingest_mod, "_notify_user", lambda *a, **k: None)
    # Pin the dispatch provider to "claude" so the non-Claude preflight gate
    # never fires (it would consult the operator's real role registry).
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    return prov


def _write_plan(plan_dir: Path, name: str, repo_root: Path) -> None:
    plan = {
        "name": name,
        "repo_root": str(repo_root),
        "epics": [
            {
                "summary": "E1",
                "stories": [
                    {
                        "key": "s1",
                        "summary": "S1",
                        "agent_instructions": "Do the thing.",
                    }
                ],
            }
        ],
    }
    (plan_dir / f"{name}.json").write_text(json.dumps(plan))


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("hello\n")
    return repo


def _write_manifest(plan_dir: Path, name: str, repo_root: Path) -> None:
    """Write a dispatchable, approved manifest so dispatch gets past its gates."""
    manifest = {
        "name": name,
        "repo_root": str(repo_root),
        "approval": {"status": "approved"},
        "epics": {"e1": {"id": "e1", "summary": "E1"}},
        "stories": {
            "s-1": {
                "summary": "S1",
                "agent_instructions": "Do the thing.",
                "status": "todo",
                "epic": "e1",
                "agent": "software-engineer",
            }
        },
    }
    path = p._store.manifest_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest))


class _LockedStore:
    """A store whose plan transaction is always held by someone else.

    Dispatch's first act after the authorization check is to enter the plan
    transaction; when the lock yields False it returns ``{"ok": True,
    "skipped": "locked"}`` before any worktree, git or agent work. That makes
    it a side-effect-free probe of "the gate let the call through" -- exactly
    the behaviour the pre-check path had when the lock was contended.
    """

    @contextmanager
    def transaction(self, plan_name):
        yield False


def test_ingest_plan_is_allowed_for_the_local_operator(
    plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    _write_plan(plan_dir, "plan-a", _repo(tmp_path))

    with bind_principal(LOCAL_OPERATOR):
        result = p.ingest_plan("plan-a")

    assert result["ok"] is True, result


def test_dispatch_story_is_allowed_for_the_local_operator(
    plan_dir: Path, tmp_path: Path, monkeypatch
) -> None:
    _write_manifest(plan_dir, "plan-alpha", _repo(tmp_path))
    monkeypatch.setattr(p, "_store", _LockedStore())

    with bind_principal(LOCAL_OPERATOR):
        result = p.dispatch_story("plan-alpha", "s-1")

    assert result["ok"] is True, result


def test_ingest_plan_denies_a_denied_principal(
    plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    _write_plan(plan_dir, "plan-a", _repo(tmp_path))

    with bind_principal(DENIED):
        result = p.ingest_plan("plan-a")

    assert result["ok"] is False, result


def test_ingest_plan_denial_writes_no_manifest(
    plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    _write_plan(plan_dir, "plan-a", _repo(tmp_path))

    with bind_principal(DENIED):
        p.ingest_plan("plan-a")

    assert not p._store.manifest_path("plan-a").exists()


def test_dispatch_story_denies_a_denied_principal(plan_dir: Path, tmp_path: Path) -> None:
    _write_manifest(plan_dir, "plan-alpha", _repo(tmp_path))

    with bind_principal(DENIED):
        result = p.dispatch_story("plan-alpha", "s-1")

    assert "plan:dispatch" in result["error"], result


def test_dispatch_story_denial_creates_no_worktree(
    plan_dir: Path, tmp_path: Path, monkeypatch
) -> None:
    _write_manifest(plan_dir, "plan-alpha", _repo(tmp_path))
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    monkeypatch.setattr(p, "WORKTREE_ROOT", worktrees)

    # The worktree seam is the only thing that creates an entry under
    # WORKTREE_ROOT. Record that it ran by creating the entry, then stop
    # before any agent subprocess is launched.
    def _create_fresh_worktree(plan_name, branch, worktree_path, story_key=None):
        worktree_path.mkdir(parents=True, exist_ok=True)
        raise RuntimeError("stop: reached worktree creation")

    monkeypatch.setattr(dispatch_mod, "_create_fresh_worktree", _create_fresh_worktree)

    with bind_principal(DENIED):
        p.dispatch_story("plan-alpha", "s-1")

    assert list(worktrees.iterdir()) == []


def test_ingest_plan_with_enforcement_disabled_proceeds(
    plan_dir: Path, provider: _RecordingProvider, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(ENFORCE_ENV, "0")
    _write_plan(plan_dir, "plan-a", _repo(tmp_path))

    with bind_principal(DENIED):
        result = p.ingest_plan("plan-a")

    assert result["ok"] is True, result


def test_dispatch_story_with_enforcement_disabled_proceeds(
    plan_dir: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(ENFORCE_ENV, "0")
    _write_manifest(plan_dir, "plan-alpha", _repo(tmp_path))
    monkeypatch.setattr(p, "_store", _LockedStore())

    with bind_principal(DENIED):
        result = p.dispatch_story("plan-alpha", "s-1")

    assert result["ok"] is True, result


def test_traversal_plan_name_is_refused_by_key_validation(plan_dir: Path) -> None:
    with pytest.raises(ValueError) as refusal:
        p.dispatch_story("../../etc/passwd", "s-1")
    with pytest.raises(ValueError) as expected:
        parsers._validate_key("../../etc/passwd")

    assert str(refusal.value) == str(expected.value)


def test_traversal_plan_name_records_no_authorization_denial(
    plan_dir: Path, monkeypatch
) -> None:
    notified: list[tuple] = []
    monkeypatch.setattr(
        ppers, "_notify_user", lambda *a, **k: notified.append((a, k))
    )

    with bind_principal(DENIED), pytest.raises(ValueError):
        p.dispatch_story("../../etc/passwd", "s-1")

    assert notified == []


def test_traversal_plan_name_is_refused_by_key_validation_on_ingest(
    plan_dir: Path,
) -> None:
    """Ingest validates the key before ``authorize`` reads the manifest.

    ``authorize`` builds paths as ``PLAN_DIR / f"{plan_name}.<suffix>"``; a
    traversal name must die in ``_validate_key`` first, so the refusal has to be
    the key-validation one and not an authorization refusal.
    """
    with pytest.raises(ValueError) as refusal:
        p.ingest_plan("../../etc/passwd")
    with pytest.raises(ValueError) as expected:
        parsers._validate_key("../../etc/passwd")

    assert str(refusal.value) == str(expected.value)


def test_ingest_traversal_plan_name_records_no_authorization_denial(
    plan_dir: Path, provider: _RecordingProvider, monkeypatch
) -> None:
    notified: list[tuple] = []
    monkeypatch.setattr(
        ppers, "_notify_user", lambda *a, **k: notified.append((a, k))
    )

    with bind_principal(DENIED), pytest.raises(ValueError):
        p.ingest_plan("../../etc/passwd")

    assert notified == []


def test_ingest_traversal_plan_name_writes_nothing_into_the_registry(
    plan_dir: Path, provider: _RecordingProvider
) -> None:
    with bind_principal(DENIED), pytest.raises(ValueError):
        p.ingest_plan("../../etc/passwd")

    assert list(plan_dir.iterdir()) == []


def test_denied_principal_is_denied_the_ingest_action(plan_dir: Path) -> None:
    """Guard: the fixture's denied principal really is denied ``plan:ingest``."""
    with bind_principal(DENIED):
        refusal = authorize("plan:ingest", plan_name="plan-a")

    assert refusal is not None
