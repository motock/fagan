"""AUTH-02: ingest records the plan's team on the manifest.

ADR 0009's chain is principal -> team -> repo -> plan -> story. Manifests
already carry ``repo_root``; this pins the ``team`` field that gives a policy
decision something to evaluate the plan against. Until the shared control
plane exists there is exactly one implicit team, so a plan that omits ``team``
records ``DEFAULT_TEAM`` ("local").

Re-ingesting a plan that omits ``team`` must preserve the team the manifest
already had - that is why ``prior`` is consulted before the default. A
``plan.get("team") or DEFAULT_TEAM`` implementation passes the first
re-ingest but silently downgrades the persisted team, so the state left on
disk is asserted here, not just the return value.

Malformed values are rejected at ingest, before any manifest is written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# pipeline.server must load before pipeline.ingest (see
# tests/unit/test_ingest_keyless_reingest.py for the import-cycle note).
import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import ingest as ingest_mod
from pipeline import persistence as ppers
from pipeline.authorization import DEFAULT_TEAM

_TEAM_RULE = "^[a-z0-9][a-z0-9-]{0,31}$"


class _RecordingProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def create_epic(self, summary: str):
        self.calls.append(("create_epic", summary))

    def create_story(self, summary, description, epic_id, agent):
        self.calls.append(("create_story", summary))


@pytest.fixture
def team_plan_dir(tmp_path, monkeypatch):
    """Local copy of the shared ``plan_dir`` fixture.

    Importing the shared fixture and using it as a same-named test parameter
    is the standard pytest pattern, but ruff's F811 false positive is only
    ignored for the ``test_pipeline_mcp_server_*.py`` file pattern, so this
    file keeps its own copy under a distinct name.
    """
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    # pipeline_persistence and pipeline_concurrency import PLAN_DIR from
    # pipeline_paths at module load and read it as a free var, so patches
    # must land on their own bindings too.
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


@pytest.fixture
def provider(monkeypatch):
    prov = _RecordingProvider()
    monkeypatch.setattr(p, "get_ticket_provider", lambda: prov)
    monkeypatch.setattr(ingest_mod, "_notify_user", lambda *a, **k: None)
    # Pin the dispatch provider to "claude" so the non-Claude preflight gate
    # never fires (it would consult the operator's real role registry).
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    return prov


def _write_plan(plan_dir: Path, name: str, repo_root: Path, **extra) -> dict:
    plan = {
        "name": name,
        "repo_root": str(repo_root),
        "epics": [
            {
                "summary": "E1",
                "stories": [
                    {
                        # An explicit `key` is required to re-ingest a plan
                        # (see _keyless_reingest_error); without it ingest
                        # would mint a duplicate story.
                        "key": "s1",
                        "summary": "S1",
                        "agent_instructions": "Do the thing.",
                    }
                ],
            }
        ],
    }
    plan.update(extra)
    (plan_dir / f"{name}.json").write_text(json.dumps(plan))
    return plan


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("hello\n")
    return repo


def _manifest(plan_name: str) -> dict:
    return json.loads(ingest_mod._store.manifest_path(plan_name).read_text())


def test_plan_without_team_records_default_team(
    team_plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    _write_plan(team_plan_dir, "no-team", _repo(tmp_path))

    result = ingest_mod._ingest_plan_impl("no-team")

    assert result["ok"] is True, result
    assert DEFAULT_TEAM == "local"
    assert _manifest("no-team")["team"] == DEFAULT_TEAM


def test_plan_with_team_records_that_team(
    team_plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    _write_plan(team_plan_dir, "core-team", _repo(tmp_path), team="core")

    result = ingest_mod._ingest_plan_impl("core-team")

    assert result["ok"] is True, result
    assert _manifest("core-team")["team"] == "core"


@pytest.mark.parametrize(
    "bad_team",
    ["Core", "core team", "-core", "a" * 33, 123],
    ids=["uppercase", "space", "leading-dash", "too-long", "not-a-string"],
)
def test_malformed_team_is_rejected_before_any_manifest_is_written(
    team_plan_dir: Path,
    provider: _RecordingProvider,
    tmp_path: Path,
    bad_team,
) -> None:
    _write_plan(team_plan_dir, "bad-team", _repo(tmp_path), team=bad_team)

    result = ingest_mod._ingest_plan_impl("bad-team")

    assert result["ok"] is False, f"team {bad_team!r} was accepted"
    error = result["error"]
    assert _TEAM_RULE in error, f"error does not name the rule: {error!r}"
    assert "team" in error, f"error does not name the field: {error!r}"
    # Rejected before any manifest was written.
    assert not ingest_mod._store.manifest_path("bad-team").exists()
    assert provider.calls == [], f"side effects ran before rejection: {provider.calls!r}"


def test_reingest_without_team_preserves_recorded_team(
    team_plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    _write_plan(team_plan_dir, "keep-team", repo, team="core")
    assert ingest_mod._ingest_plan_impl("keep-team")["ok"] is True
    assert _manifest("keep-team")["team"] == "core"

    # Re-ingest the same plan with the `team` key absent.
    _write_plan(team_plan_dir, "keep-team", repo)
    result = ingest_mod._ingest_plan_impl("keep-team")

    assert result["ok"] is True, result
    assert _manifest("keep-team")["team"] == "core"


def test_reingest_with_different_team_updates_it(
    team_plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    _write_plan(team_plan_dir, "switch-team", repo, team="core")
    assert ingest_mod._ingest_plan_impl("switch-team")["ok"] is True
    assert _manifest("switch-team")["team"] == "core"

    _write_plan(team_plan_dir, "switch-team", repo, team="design")
    result = ingest_mod._ingest_plan_impl("switch-team")

    assert result["ok"] is True, result
    assert _manifest("switch-team")["team"] == "design"

    # Call 4: omit `team` again. `prior` now says "design", so the persisted
    # team must stay "design" - an implementation that fell back to
    # DEFAULT_TEAM on call 2 would have written "local" and stayed wrong here.
    _write_plan(team_plan_dir, "switch-team", repo)
    result = ingest_mod._ingest_plan_impl("switch-team")

    assert result["ok"] is True, result
    assert _manifest("switch-team")["team"] == "design"
