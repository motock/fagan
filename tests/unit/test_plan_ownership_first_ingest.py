"""OWN-04: only the local operator names an owner on the first ingest.

ADR 0006 decision 1 says a plan's owner is "taken from the authenticated MCP
caller at ingest". ``save_plan`` is unguarded, so a plan file -- writable by any
caller -- cannot be what grants ownership: on a first ingest only the local
operator may name an owner that differs from the caller or supply delegates.
Everyone else owns the plan they created, delegates nothing, and any naming
they attempted is recorded as ``plan:ownership_change``/``ignored``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# pipeline.server must load before pipeline.ingest (see
# tests/unit/test_ingest_keyless_reingest.py for the import-cycle note).
import pipeline.server as p
from app.identity import LOCAL_OPERATOR, Principal
from pipeline import audit as audit_mod
from pipeline import concurrency as pcon
from pipeline import ingest as ingest_mod
from pipeline import persistence as ppers
from pipeline.actor import bind_principal

_PREFLIGHT = "Preflight: test fixture - not a real plan\n"
_PLAN = "first-ingest-plan"


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
def plan_dir(tmp_path, monkeypatch):
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
    monkeypatch.setattr(audit_mod, "PLAN_DIR", d, raising=False)
    return d


def _write_plan(plan_dir: Path, name: str, repo_root: Path, **ownership) -> Path:
    """Write a one-story plan, spreading *ownership* onto the plan source."""
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
        **ownership,
    }
    plan_path = plan_dir / f"{name}.json"
    plan_path.write_text(json.dumps(plan))
    return plan_path


def _repo_root(tmp_path: Path) -> Path:
    """A README-only repo_root: no ecosystem marker, so ingest accepts it."""
    # ``exist_ok``: a test that re-ingests builds the same repo_root twice.
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "README.md").write_text("hello\n")
    return repo


def _manifest(plan_dir: Path, name: str) -> dict:
    """Read the persisted manifest file from disk (never the return value)."""
    return json.loads((plan_dir / f"{name}.manifest.json").read_text())


def _ingest(name: str, subject: str) -> dict:
    """Call the impl directly: ``ingest_plan`` authorizes ``plan:ingest`` first
    and LocalPolicy denies every non-operator, hiding the ownership rule."""
    with bind_principal(
        Principal(subject=subject, roles=frozenset({"author"}), auth_method="test")
    ):
        return ingest_mod._ingest_plan_impl(name)


def _audit_records(plan_dir: Path, name: str) -> list[dict]:
    """Read the plan's audit stream from disk, ignoring blank lines."""
    path = plan_dir / f"{name}.audit.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text().splitlines() if line.strip()
    ]


def _ownership_changes(plan_dir: Path, name: str) -> list[dict]:
    return [
        r
        for r in _audit_records(plan_dir, name)
        if r["action"] == "plan:ownership_change"
    ]


def _first_ingest_owned_by_plan_file(
    plan_dir: Path, tmp_path: Path, subject: str
) -> dict:
    """Ingest a plan that names ``alice`` with delegate ``bob`` as *subject*."""
    repo = _repo_root(tmp_path)
    _write_plan(plan_dir, _PLAN, repo, owner="alice", delegates=["bob"])
    return _ingest(_PLAN, subject)


# --- first ingest: a non-operator owns what they create, delegates nothing --


def test_non_operator_first_ingest_owner_is_caller(
    plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    res = _first_ingest_owned_by_plan_file(plan_dir, tmp_path, "carol")
    assert (res["ok"], _manifest(plan_dir, _PLAN)["owner"]) == (True, "carol")


def test_non_operator_first_ingest_drops_delegates(
    plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    _first_ingest_owned_by_plan_file(plan_dir, tmp_path, "carol")
    assert _manifest(plan_dir, _PLAN)["delegates"] == []


def test_non_operator_first_ingest_audits_ignored_naming(
    plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    _first_ingest_owned_by_plan_file(plan_dir, tmp_path, "carol")
    assert [
        (r["outcome"], r["detail"]) for r in _ownership_changes(plan_dir, _PLAN)
    ] == [("ignored", {"ignored_owner": "alice", "ignored_delegates": 1})]


def test_non_operator_naming_self_audits_nothing(
    plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    repo = _repo_root(tmp_path)
    _write_plan(plan_dir, _PLAN, repo, owner="carol")
    _ingest(_PLAN, "carol")
    assert _ownership_changes(plan_dir, _PLAN) == []


# --- first ingest: the operator path is unchanged ---------------------------


def test_operator_first_ingest_keeps_plan_owner_and_delegates(
    plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    _first_ingest_owned_by_plan_file(plan_dir, tmp_path, LOCAL_OPERATOR.subject)
    manifest = _manifest(plan_dir, _PLAN)
    assert (manifest["owner"], manifest["delegates"]) == ("alice", ["bob"])


# --- later ingests: OWN-03 is unchanged -------------------------------------


def test_later_ingest_non_owner_keeps_existing_owner(
    plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    _first_ingest_owned_by_plan_file(plan_dir, tmp_path, "carol")
    _ingest(_PLAN, "dave")
    manifest = _manifest(plan_dir, _PLAN)
    assert (manifest["owner"], manifest["delegates"]) == ("carol", [])


def test_later_ingest_current_owner_may_change_owner(
    plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    _first_ingest_owned_by_plan_file(plan_dir, tmp_path, "carol")
    repo = _repo_root(tmp_path)
    _write_plan(plan_dir, _PLAN, repo, owner="dave")
    _ingest(_PLAN, "carol")
    assert _manifest(plan_dir, _PLAN)["owner"] == "dave"
