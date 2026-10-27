"""OWN-01: the plan manifest records an owner and delegates.

ADR 0006 defines plan ownership as one named owner plus delegates; ADR 0009
lists ownership records as the last unimplemented piece of the identity model.
This story *stores* ownership on the manifest at ingest and validates it --
OWN-02 reads it in the policy. Nothing here enforces anything.

Ownership defaults to today's behaviour: the ingester owns the plan and names
no delegates. The plan source file is the authority for a plan-named owner, so
re-ingesting the same plan under a different principal keeps that owner. A
malformed ownership value is a structured refusal, never a silent fallback and
never a 500. Ownership is not authored content, so it never changes the
approval digest.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# pipeline.server must load before pipeline.ingest (see
# tests/unit/test_ingest_keyless_reingest.py for the import-cycle note).
import pipeline.server as p
from app.identity import LOCAL_OPERATOR, Principal
from pipeline import concurrency as pcon
from pipeline import ingest as ingest_mod
from pipeline import persistence as ppers
from pipeline.actor import bind_principal
from pipeline.ownership import ownership_from_plan
from pipeline.plan_approval import canonical_digest

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
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("hello\n")
    return repo


def _manifest(plan_dir: Path, name: str) -> dict:
    """Read the persisted manifest file from disk (never the return value)."""
    return json.loads((plan_dir / f"{name}.manifest.json").read_text())


def _ingest(plan_dir: Path, name: str, subject: str) -> dict:
    with bind_principal(
        Principal(subject=subject, roles=frozenset({"author"}), auth_method="test")
    ):
        return ingest_mod._ingest_plan_impl(name)


def test_defaults_owner_to_ingester() -> None:
    assert ownership_from_plan({"plan_id": "p1"}, "bob") == {
        "owner": "bob",
        "delegates": [],
    }


def test_plan_named_owner_wins() -> None:
    assert ownership_from_plan({"owner": "alice"}, "bob")["owner"] == "alice"


def test_delegates_persist_in_order() -> None:
    assert ownership_from_plan({"delegates": ["bob", "carol"]}, "alice")[
        "delegates"
    ] == ["bob", "carol"]


def test_reingest_keeps_plan_named_owner(
    plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    _write_plan(plan_dir, "owned-plan", _repo_root(tmp_path), owner="alice")

    # Only the local operator may name an owner on the first ingest, so the
    # plan file's owner lands then; the later re-ingests must not displace it.
    assert (
        _ingest(plan_dir, "owned-plan", LOCAL_OPERATOR.subject)["ok"],
        _ingest(plan_dir, "owned-plan", "carol")["ok"],
        _manifest(plan_dir, "owned-plan")["owner"],
    ) == (True, True, "alice")


def test_delegates_string_rejected() -> None:
    with pytest.raises(ValueError, match="delegates"):
        ownership_from_plan({"delegates": "alice"}, "bob")


def test_empty_delegate_rejected() -> None:
    with pytest.raises(ValueError):
        ownership_from_plan({"delegates": [""]}, "bob")


def test_empty_owner_rejected() -> None:
    with pytest.raises(ValueError, match="owner"):
        ownership_from_plan({"owner": ""}, "")


def test_ingest_bad_owner_refused(
    plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    _write_plan(plan_dir, "bad-owner-plan", _repo_root(tmp_path), owner=123)

    result = _ingest(plan_dir, "bad-owner-plan", "bob")

    assert (
        result["ok"],
        (plan_dir / "bad-owner-plan.manifest.json").exists(),
    ) == (False, False)


def test_digest_ignores_ownership() -> None:
    m1 = {"plan_id": "p1", "ingested_by": "bob"}
    m2 = dict(m1, owner="alice", delegates=["carol"])
    assert canonical_digest(m1) == canonical_digest(m2)
