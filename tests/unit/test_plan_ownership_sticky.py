"""OWN-03: plan ownership is sticky across re-ingests.

OWN-01 made the plan file the authority for ownership on *every* ingest, and
``save_plan`` is unguarded, so writing ``"owner": "me"`` into someone's plan and
waiting for a re-ingest handed that caller the owner's authority. Here the plan
file may set ownership only on the first ingest: afterwards the manifest's
ownership stands unless the plan names a different one *and* the caller is the
current owner or the local operator. Every attempted change is audited.
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
from pipeline.ownership import resolve_ownership

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
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("hello\n")
    return repo


def _manifest(plan_dir: Path, name: str) -> dict:
    """Read the persisted manifest file from disk (never the return value)."""
    return json.loads((plan_dir / f"{name}.manifest.json").read_text())


def _ingest(name: str, subject: str) -> dict:
    """Call the impl directly: ``ingest_plan`` authorizes ``plan:ingest`` first,
    and LocalPolicy denies every non-operator, hiding the ownership rule."""
    with bind_principal(
        Principal(subject=subject, roles=frozenset({"author"}), auth_method="test")
    ):
        return ingest_mod._ingest_plan_impl(name)


# --- resolve_ownership: the rule matrix ----------------------------------


def test_resolve_first_ingest_defaults_to_actor(plan_dir: Path) -> None:
    assert resolve_ownership("p", {}, {}, "bob") == {"owner": "bob", "delegates": []}


def test_resolve_first_ingest_honours_plan_owner(plan_dir: Path) -> None:
    actor = LOCAL_OPERATOR.subject
    assert resolve_ownership("p", {"owner": "alice"}, {}, actor)["owner"] == "alice"


def test_resolve_later_ingest_keeps_owner_without_plan_owner(plan_dir: Path) -> None:
    assert resolve_ownership("p", {}, {"owner": "alice"}, "bob")["owner"] == "alice"


def test_resolve_stranger_cannot_take_ownership(plan_dir: Path) -> None:
    with pytest.raises(ValueError, match="current owner"):
        resolve_ownership("p", {"owner": "bob"}, {"owner": "alice"}, "bob")


def test_resolve_owner_may_reassign(plan_dir: Path) -> None:
    assert resolve_ownership("p", {"owner": "carol"}, {"owner": "alice"}, "alice")[
        "owner"
    ] == "carol"


def test_resolve_local_operator_may_reassign(plan_dir: Path) -> None:
    assert resolve_ownership(
        "p", {"owner": "carol"}, {"owner": "alice"}, LOCAL_OPERATOR.subject
    )["owner"] == "carol"


def test_resolve_delegate_cannot_take_ownership(plan_dir: Path) -> None:
    with pytest.raises(ValueError):
        resolve_ownership(
            "p",
            {"owner": "dave"},
            {"owner": "alice", "delegates": ["dave"]},
            "dave",
        )


def test_resolve_actor_cannot_delegate_to_self(plan_dir: Path) -> None:
    with pytest.raises(ValueError):
        resolve_ownership("p", {"delegates": ["bob"]}, {"owner": "alice"}, "bob")


def test_resolve_absent_delegates_key_keeps_delegates(plan_dir: Path) -> None:
    assert resolve_ownership(
        "p", {}, {"owner": "alice", "delegates": ["dave"]}, "alice"
    )["delegates"] == ["dave"]


def test_resolve_legacy_ownerless_manifest_stays_ownerless(plan_dir: Path) -> None:
    assert resolve_ownership("p", {}, {"delegates": []}, "bob")["owner"] == ""


def test_resolve_stranger_cannot_claim_legacy_manifest(plan_dir: Path) -> None:
    with pytest.raises(ValueError):
        resolve_ownership("p", {"owner": "bob"}, {"delegates": []}, "bob")


# --- wiring: ingest persists only an authorized ownership -----------------

_PLAN = "sticky-plan"


def _first_ingest_then_rewrite(plan_dir: Path, tmp_path: Path, owner: str) -> dict:
    """Ingest the plan as the operator (owner alice), then re-ingest a rewrite.

    A first ingest as the local operator is the only way to plant an owner that
    the re-ingester does not already hold, which is exactly the escalation.
    """
    repo = _repo_root(tmp_path)
    _write_plan(plan_dir, _PLAN, repo, owner="alice")
    _ingest(_PLAN, LOCAL_OPERATOR.subject)
    _write_plan(plan_dir, _PLAN, repo, owner=owner)
    return _ingest(_PLAN, owner)


def test_ingest_stranger_rewrite_refused(
    plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    assert _first_ingest_then_rewrite(plan_dir, tmp_path, "bob")["ok"] is False


def test_ingest_refusal_leaves_owner_intact(
    plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    _first_ingest_then_rewrite(plan_dir, tmp_path, "bob")
    assert _manifest(plan_dir, _PLAN)["owner"] == "alice"


def test_ingest_refusal_audited_once(
    plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    _first_ingest_then_rewrite(plan_dir, tmp_path, "bob")
    records = [
        json.loads(line)
        for line in (plan_dir / f"{_PLAN}.audit.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert [
        (r["action"], r["outcome"], r["detail"])
        for r in records
        if r["action"] == "plan:ownership_change"
    ] == [
        (
            "plan:ownership_change",
            "denied",
            {"from_owner": "alice", "to_owner": "bob", "delegates_changed": False},
        )
    ]


def test_ingest_owner_reassignment_succeeds(
    plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    repo = _repo_root(tmp_path)
    _write_plan(plan_dir, "sticky-plan", repo, owner="alice")
    _ingest("sticky-plan", LOCAL_OPERATOR.subject)
    _write_plan(plan_dir, "sticky-plan", repo, owner="carol")
    _ingest("sticky-plan", "alice")
    assert _manifest(plan_dir, "sticky-plan")["owner"] == "carol"


# --- review regression: overwrite=True must not reset ownership -----------


def _ingest_overwrite(name: str, subject: str) -> dict:
    """Re-ingest with the wholesale-replace flag under *subject*."""
    with bind_principal(
        Principal(subject=subject, roles=frozenset({"author"}), auth_method="test")
    ):
        return ingest_mod._ingest_plan_impl(name, overwrite=True)


def _owned_plan_then_rewrite_to_bob(
    plan_dir: Path, tmp_path: Path, overwrite: bool
) -> dict:
    """Plant owner alice as the operator, then rewrite the plan to name bob."""
    repo = _repo_root(tmp_path)
    _write_plan(plan_dir, _PLAN, repo, owner="alice")
    _ingest(_PLAN, LOCAL_OPERATOR.subject)
    _write_plan(plan_dir, _PLAN, repo, owner="bob")
    return _ingest_overwrite(_PLAN, "bob") if overwrite else _ingest(_PLAN, "bob")


def test_ingest_overwrite_stranger_refused(
    plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    assert _owned_plan_then_rewrite_to_bob(plan_dir, tmp_path, overwrite=True)[
        "ok"
    ] is False


def test_ingest_overwrite_refusal_leaves_owner_intact(
    plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    _owned_plan_then_rewrite_to_bob(plan_dir, tmp_path, overwrite=True)
    assert _manifest(plan_dir, _PLAN)["owner"] == "alice"


def test_ingest_overwrite_refusal_audited_once(
    plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    _owned_plan_then_rewrite_to_bob(plan_dir, tmp_path, overwrite=True)
    records = [
        json.loads(line)
        for line in (plan_dir / f"{_PLAN}.audit.jsonl").read_text().splitlines()
        if line.strip() and json.loads(line)["action"] == "plan:ownership_change"
    ]
    assert [(r["outcome"], r["detail"]["from_owner"], r["detail"]["to_owner"]) for r in records] == [
        ("denied", "alice", "bob")
    ]


def test_ingest_refusal_creates_no_plane_issues(
    plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    repo = _repo_root(tmp_path)
    _write_plan(plan_dir, _PLAN, repo, owner="alice")
    _ingest(_PLAN, LOCAL_OPERATOR.subject)
    provider.calls.clear()
    _write_plan(plan_dir, _PLAN, repo, owner="bob")
    _ingest(_PLAN, "bob")
    assert provider.calls == []


def test_resolve_strips_owner_whitespace(plan_dir: Path) -> None:
    actor = LOCAL_OPERATOR.subject
    assert resolve_ownership("p", {"owner": " alice "}, {}, actor)["owner"] == "alice"


def test_ingest_overwrite_by_owner_still_reassigns(
    plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    repo = _repo_root(tmp_path)
    _write_plan(plan_dir, _PLAN, repo, owner="alice")
    _ingest(_PLAN, LOCAL_OPERATOR.subject)
    _write_plan(plan_dir, _PLAN, repo, owner="carol")
    _ingest_overwrite(_PLAN, "alice")
    assert _manifest(plan_dir, _PLAN)["owner"] == "carol"
