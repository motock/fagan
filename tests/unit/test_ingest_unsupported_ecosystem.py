"""LAG-5: ingest must reject a plan whose repo_root is an unsupported ecosystem.

A Go/.NET repo now gets a real test command, but a Ruby/PHP/Swift/Elixir repo
has no detectable command - ``detect_test_command`` returns a command that
exits non-zero telling the operator to declare ``test_cmd`` in ``.fagan.json``.
Ingesting such a plan would burn dispatch/rework budget on a gate no executor
can satisfy, so ``_ingest_plan_impl`` must reject it right after the existing
``repo_root`` missing/not-a-directory check, in the same
``{"ok": False, "error": ...}`` shape.

A README-only repo_root (the smoke-test scratch repo) has no marker at all and
must still ingest.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# pipeline.server must load before pipeline.ingest (see
# tests/unit/test_ingest_keyless_reingest.py for the import-cycle note).
import pipeline.server as p
from pipeline import ingest as ingest_mod

_UNSUPPORTED_MARKERS = ("Gemfile", "composer.json", "Package.swift", "mix.exs")


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


def _write_plan(plan_dir: Path, name: str, repo_root: Path) -> dict:
    plan = {
        "name": name,
        "repo_root": str(repo_root),
        "epics": [
            {
                "summary": "E1",
                "stories": [
                    {"summary": "S1", "agent_instructions": "Do the thing."}
                ],
            }
        ],
    }
    (plan_dir / f"{name}.json").write_text(json.dumps(plan))
    return plan


def _repo_with(tmp_path: Path, name: str, marker: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    (repo / marker).write_text("x\n")
    return repo


@pytest.mark.parametrize("marker", _UNSUPPORTED_MARKERS)
def test_plan_with_unsupported_repo_root_is_rejected(
    plan_dir: Path, provider: _RecordingProvider, tmp_path: Path, marker: str
) -> None:
    repo = _repo_with(tmp_path, "unsupported_repo", marker)
    _write_plan(plan_dir, "unsupported-plan", repo)

    result = ingest_mod._ingest_plan_impl("unsupported-plan")

    assert result["ok"] is False, f"plan with {marker} repo_root was not rejected"
    error = result["error"]
    assert marker in error, f"error does not name the marker {marker!r}: {error!r}"
    assert ".fagan.json" in error, f"error does not mention .fagan.json: {error!r}"
    assert "test_cmd" in error, f"error does not mention test_cmd: {error!r}"
    # Rejected before any ticket-provider side effect.
    assert provider.calls == [], f"side effects ran before rejection: {provider.calls!r}"


def test_readme_only_repo_root_is_not_rejected(
    plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    repo = tmp_path / "readme_repo"
    repo.mkdir()
    (repo / "README.md").write_text("hello\n")
    _write_plan(plan_dir, "readme-plan", repo)

    result = ingest_mod._ingest_plan_impl("readme-plan")

    error = result.get("error", "")
    assert ".fagan.json" not in error, (
        f"README-only repo_root was rejected by the unsupported-ecosystem check: {error!r}"
    )
    assert result["ok"] is True, f"README-only repo_root was rejected: {result!r}"


# Reviewer follow-up: is_unsupported_ecosystem_command is also True for
# repo_config.invalid_config_command, so a repo_root with a MALFORMED
# .fagan.json must be rejected naming the actual RepoConfigError reason - not
# "unsupported ecosystem" with a marker that does not exist.
def test_plan_with_malformed_fagan_json_names_the_real_reason(
    plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    repo = tmp_path / "broken_config_repo"
    repo.mkdir()
    (repo / "Gemfile").write_text("source 'https://rubygems.org'\n")
    (repo / ".fagan.json").write_text('{"test_cmd": "not-a-list"}\n')
    _write_plan(plan_dir, "broken-config-plan", repo)

    result = ingest_mod._ingest_plan_impl("broken-config-plan")

    assert result["ok"] is False, f"malformed .fagan.json plan was not rejected: {result!r}"
    error = result["error"]
    assert "unsupported ecosystem" not in error, (
        f"malformed .fagan.json misreported as an unsupported ecosystem: {error!r}"
    )
    assert "invalid .fagan.json" in error, (
        f"error does not name the actual .fagan.json problem: {error!r}"
    )
    assert "must be a non-empty list" in error, (
        f"error drops the real RepoConfigError text: {error!r}"
    )
    assert provider.calls == [], f"side effects ran before rejection: {provider.calls!r}"
