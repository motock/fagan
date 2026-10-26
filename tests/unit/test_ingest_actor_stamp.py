"""ACT-03: the manifest records who ingested the plan.

A manifest must say who ingested it so a later approval can tell "the person
who wrote this also approved it" from "someone else approved it", and so the
audit stream has a first-class actor per plan.

The stamp is *current provenance*, not the original author: re-ingesting the
same plan under a different bound principal refreshes ``ingested_by``. It
belongs to the manifest only -- the plan source file (``<plan>.json``, written
by ``save_plan``) must stay byte-identical, and the field must never
participate in the approval digest.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# pipeline.server must load before pipeline.ingest (see
# tests/unit/test_ingest_keyless_reingest.py for the import-cycle note).
import pipeline.server as p
from app.identity import Principal
from pipeline import concurrency as pcon
from pipeline import ingest as ingest_mod
from pipeline import persistence as ppers
from pipeline.actor import bind_principal

_PREFLIGHT = "Preflight: test fixture - not a real plan\n"


class _RecordingProvider:
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


@pytest.fixture
def actor_plan_dir(tmp_path, monkeypatch):
    """Point every PLAN_DIR binding at ``tmp_path``.

    pipeline_persistence and pipeline_concurrency import PLAN_DIR from
    pipeline_paths at module load and read it as a free var, so patches must
    land on their own bindings too.
    """
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


def _write_plan(plan_dir: Path, name: str, repo_root: Path) -> Path:
    plan = {
        "name": name,
        "repo_root": str(repo_root),
        "epics": [
            {
                "summary": "E1",
                "stories": [
                    {
                        # An explicit key is required to re-ingest a plan:
                        # without one, ingest refuses to mint a duplicate.
                        "key": "issue-1",
                        "summary": "S1",
                        "agent_instructions": _PREFLIGHT + "Do the thing.",
                    }
                ],
            }
        ],
    }
    plan_path = plan_dir / f"{name}.json"
    plan_path.write_text(json.dumps(plan))
    return plan_path


def _repo_root(tmp_path: Path) -> Path:
    """A README-only repo_root: no ecosystem marker, so ingest accepts it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("hello\n")
    return repo


def _manifest(plan_dir: Path, name: str) -> dict:
    """Read the persisted manifest file from disk (never the return value)."""
    return json.loads((plan_dir / f"{name}.manifest.json").read_text())


def test_ingest_stamps_bound_principal_as_ingested_by(
    actor_plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    _write_plan(actor_plan_dir, "actor-plan", _repo_root(tmp_path))

    with bind_principal(
        Principal(subject="alice", roles=frozenset({"author"}), auth_method="test")
    ):
        result = ingest_mod._ingest_plan_impl("actor-plan")

    assert result["ok"] is True, f"ingest failed: {result!r}"
    manifest = _manifest(actor_plan_dir, "actor-plan")
    assert manifest["ingested_by"] == "alice"


def test_ingest_with_nothing_bound_stamps_local_operator(
    actor_plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    _write_plan(actor_plan_dir, "local-plan", _repo_root(tmp_path))

    result = ingest_mod._ingest_plan_impl("local-plan")

    assert result["ok"] is True, f"ingest failed: {result!r}"
    manifest = _manifest(actor_plan_dir, "local-plan")
    assert manifest["ingested_by"] == "local-operator"


def test_reingest_refreshes_ingested_by_to_the_current_principal(
    actor_plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    _write_plan(actor_plan_dir, "refresh-plan", _repo_root(tmp_path))

    with bind_principal(
        Principal(subject="alice", roles=frozenset({"author"}), auth_method="test")
    ):
        first = ingest_mod._ingest_plan_impl("refresh-plan")
    assert first["ok"] is True, f"first ingest failed: {first!r}"
    assert _manifest(actor_plan_dir, "refresh-plan")["ingested_by"] == "alice"

    with bind_principal(
        Principal(subject="bob", roles=frozenset({"author"}), auth_method="test")
    ):
        second = ingest_mod._ingest_plan_impl("refresh-plan")
    assert second["ok"] is True, f"second ingest failed: {second!r}"
    assert _manifest(actor_plan_dir, "refresh-plan")["ingested_by"] == "bob"

    with bind_principal(
        Principal(subject="alice", roles=frozenset({"author"}), auth_method="test")
    ):
        third = ingest_mod._ingest_plan_impl("refresh-plan")
    assert third["ok"] is True, f"third ingest failed: {third!r}"
    assert _manifest(actor_plan_dir, "refresh-plan")["ingested_by"] == "alice"


def test_ingest_leaves_the_plan_source_file_byte_identical(
    actor_plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    plan_path = _write_plan(actor_plan_dir, "source-plan", _repo_root(tmp_path))
    before = plan_path.read_bytes()

    with bind_principal(
        Principal(subject="alice", roles=frozenset({"author"}), auth_method="test")
    ):
        result = ingest_mod._ingest_plan_impl("source-plan")

    assert result["ok"] is True, f"ingest failed: {result!r}"
    after = plan_path.read_bytes()
    assert before == after, "ingest rewrote the plan source file"
