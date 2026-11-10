"""ADR 0012 decision 5: the manifest must stand alone as the plan record.

``ingest_plan`` drops authored, descriptive content today: each story's
``description``, the epic a story was authored under, the authored ``key``
(when a ticketing provider issues the story id), and the plan-level
``preflight_override`` / ``repo_url``. Without them the plan document cannot
be rendered from the manifest alone.

This story records them:

- per story: ``description`` (str, default ``""``), ``epic`` (the epic summary
  the story was authored under) and ``plan_key`` (the authored key, when the
  plan story had one);
- per plan: ``plan_epics`` (the epic summaries, in authored order),
  ``preflight_override`` and ``repo_url`` (only when the plan names them).

``epic``, ``plan_key``, ``plan_epics``, ``preflight_override`` and
``repo_url`` are recorded but are NOT plan-defining: they never enter
``canonical_digest``, so recording them cannot reset an approval.
``description`` is recorded but is likewise not a digest member *at this
point* - the later description/team story owns ``description``'s digest
membership together with the digest-version bump, so this story must not
assert either way about it.

A non-string ``description`` is refused at ingest, naming the story, and no
manifest is written.
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
from pipeline.plan_approval import canonical_digest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLAN_APPROVAL_SPEC = _REPO_ROOT / "docs" / "specs" / "PLAN_APPROVAL.md"

# A non-Claude dispatch provider is gated on a real `Preflight:` line
# (.claude/rules/local-dispatch-preflight.md), so every fixture story carries
# one as its first line.
_PREFLIGHT = "Preflight: test fixture - not a real plan"


class _RecordingProvider:
    """A ticket provider that issues its own ids (so the authored key and the
    manifest key differ) and records the calls it received."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def create_epic(self, summary: str) -> str:
        self.calls.append(("create_epic", summary))
        return f"epic-{summary}"

    def create_story(self, summary, description, epic_id, agent) -> str:
        self.calls.append(("create_story", summary))
        return f"issue-{summary}"


class _NullProvider:
    """A ticket provider that issues no ids (the no-backend case): the
    manifest key is then the plan's own story key."""

    def create_epic(self, summary: str) -> None:
        return None

    def create_story(self, summary, description, epic_id, agent) -> None:
        return None


@pytest.fixture
def manifest_plan_dir(tmp_path, monkeypatch):
    """Local copy of the shared ``plan_dir`` fixture (distinct name so ruff's
    F811 false positive does not fire - see tests/unit/test_ingest_team.py)."""
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


@pytest.fixture
def provider(monkeypatch):
    prov = _RecordingProvider()
    monkeypatch.setattr(p, "get_ticket_provider", lambda: prov)
    monkeypatch.setattr(ingest_mod, "_notify_user", lambda *a, **k: None)
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    return prov


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
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
    plan = {
        "name": name,
        "repo_root": str(repo_root),
        "epics": epics,
    }
    plan.update(extra)
    (plan_dir / f"{name}.json").write_text(json.dumps(plan))
    return plan


def _manifest(plan_name: str) -> dict:
    return json.loads(ingest_mod._store.manifest_path(plan_name).read_text())


def _two_epic_plan(repo_root: Path) -> list[dict]:
    return [
        {
            "summary": "First epic",
            "stories": [
                _story("Alpha story", key="A1", description="Alpha does a thing."),
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


def test_manifest_records_description_epic_plan_key_and_plan_epics(
    manifest_plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    _write_plan(
        manifest_plan_dir, "descriptive", _repo(tmp_path), _two_epic_plan(_repo(tmp_path))
    )

    result = ingest_mod._ingest_plan_impl("descriptive")

    assert result["ok"] is True, result
    manifest = _manifest("descriptive")
    stories = manifest["stories"]
    # The provider issues its own ids, so the manifest keys are the issued
    # ids and the authored key is recorded separately as ``plan_key``.
    assert set(stories) == {"issue-Alpha story", "issue-Beta story", "issue-Gamma story"}
    alpha = stories["issue-Alpha story"]
    assert alpha["description"] == "Alpha does a thing."
    assert alpha["epic"] == "First epic"
    assert alpha["plan_key"] == "A1"
    beta = stories["issue-Beta story"]
    assert beta["description"] == "Beta does a thing."
    assert beta["epic"] == "First epic"
    assert beta["plan_key"] == "B1"
    gamma = stories["issue-Gamma story"]
    assert gamma["description"] == "Gamma does a thing."
    assert gamma["epic"] == "Second epic"
    assert gamma["plan_key"] == "G1"
    assert manifest["plan_epics"] == ["First epic", "Second epic"]


def test_plan_epics_are_in_authored_order(
    manifest_plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    epics = [
        {"summary": "Zeta epic", "stories": [_story("Zeta story")]},
        {"summary": "Alpha epic", "stories": [_story("Alpha story")]},
        {"summary": "Mu epic", "stories": [_story("Mu story")]},
    ]
    _write_plan(manifest_plan_dir, "ordered", repo, epics)

    result = ingest_mod._ingest_plan_impl("ordered")

    assert result["ok"] is True, result
    assert _manifest("ordered")["plan_epics"] == ["Zeta epic", "Alpha epic", "Mu epic"]


def test_plan_with_zero_epics_ingests_with_empty_plan_epics(
    manifest_plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    _write_plan(manifest_plan_dir, "empty", _repo(tmp_path), [])

    result = ingest_mod._ingest_plan_impl("empty")

    assert result["ok"] is True, result
    assert _manifest("empty")["plan_epics"] == []


def test_story_without_description_gets_empty_string(
    manifest_plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    epics = [{"summary": "An epic", "stories": [_story("No description story")]}]
    _write_plan(manifest_plan_dir, "nodesc", repo, epics)

    result = ingest_mod._ingest_plan_impl("nodesc")

    assert result["ok"] is True, result
    story = _manifest("nodesc")["stories"]["issue-No description story"]
    assert story["description"] == ""


def test_story_without_authored_key_has_no_plan_key(
    manifest_plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    epics = [{"summary": "An epic", "stories": [_story("Keyless story")]}]
    _write_plan(manifest_plan_dir, "nokey", repo, epics)

    result = ingest_mod._ingest_plan_impl("nokey")

    assert result["ok"] is True, result
    story = _manifest("nokey")["stories"]["issue-Keyless story"]
    assert "plan_key" not in story


def test_authored_key_is_kept_when_the_provider_issues_no_id(
    manifest_plan_dir: Path, monkeypatch, tmp_path: Path
) -> None:
    """With no ticketing backend the manifest key IS the authored key; the
    authored key is still recorded as ``plan_key``."""
    monkeypatch.setattr(p, "get_ticket_provider", lambda: _NullProvider())
    monkeypatch.setattr(ingest_mod, "_notify_user", lambda *a, **k: None)
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    repo = _repo(tmp_path)
    epics = [{"summary": "An epic", "stories": [_story("Local story", key="L1")]}]
    _write_plan(manifest_plan_dir, "local", repo, epics)

    result = ingest_mod._ingest_plan_impl("local")

    assert result["ok"] is True, result
    stories = _manifest("local")["stories"]
    assert set(stories) == {"L1"}
    assert stories["L1"]["plan_key"] == "L1"
    assert stories["L1"]["epic"] == "An epic"


@pytest.mark.parametrize(
    "bad_description",
    [123, 4.5, True, ["a", "b"], {"text": "x"}, None],
    ids=["int", "float", "bool", "list", "dict", "null"],
)
def test_non_string_description_is_refused_and_writes_no_manifest(
    manifest_plan_dir: Path, provider: _RecordingProvider, tmp_path: Path, bad_description
) -> None:
    repo = _repo(tmp_path)
    epics = [
        {
            "summary": "An epic",
            "stories": [_story("Bad description story", description=bad_description)],
        }
    ]
    _write_plan(manifest_plan_dir, "baddesc", repo, epics)

    result = ingest_mod._ingest_plan_impl("baddesc")

    assert result["ok"] is False, f"description {bad_description!r} was accepted"
    error = result["error"]
    assert "Bad description story" in error, f"error does not name the story: {error!r}"
    assert not ingest_mod._store.manifest_path("baddesc").exists()


def test_reingest_refreshes_description_and_keeps_runtime_state(
    manifest_plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    epics = [
        {
            "summary": "First epic",
            "stories": [_story("Alpha story", key="A1", description="Old text.")],
        }
    ]
    _write_plan(manifest_plan_dir, "refresh", repo, epics)
    assert ingest_mod._ingest_plan_impl("refresh")["ok"] is True

    # Simulate runtime state a re-ingest must not clobber.
    manifest_path = ingest_mod._store.manifest_path("refresh")
    manifest = json.loads(manifest_path.read_text())
    manifest["stories"]["issue-Alpha story"]["status"] = "in_progress"
    manifest["stories"]["issue-Alpha story"]["pr_url"] = "https://example.test/pr/1"
    manifest_path.write_text(json.dumps(manifest))

    _write_plan(
        manifest_plan_dir,
        "refresh",
        repo,
        [
            {
                "summary": "First epic",
                "stories": [
                    _story("Alpha story", key="A1", description="New text."),
                ],
            }
        ],
    )
    result = ingest_mod._ingest_plan_impl("refresh")

    assert result["ok"] is True, result
    story = _manifest("refresh")["stories"]["issue-Alpha story"]
    assert story["description"] == "New text."
    assert story["epic"] == "First epic"
    assert story["plan_key"] == "A1"
    assert story["status"] == "in_progress"
    assert story["pr_url"] == "https://example.test/pr/1"


def test_reingest_refreshes_epic_membership_when_the_story_moved(
    manifest_plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    _write_plan(
        manifest_plan_dir,
        "moved",
        repo,
        [
            {
                "summary": "First epic",
                "stories": [_story("Alpha story", key="A1", description="Text.")],
            }
        ],
    )
    assert ingest_mod._ingest_plan_impl("moved")["ok"] is True
    assert _manifest("moved")["stories"]["issue-Alpha story"]["epic"] == "First epic"

    _write_plan(
        manifest_plan_dir,
        "moved",
        repo,
        [
            {
                "summary": "Second epic",
                "stories": [_story("Alpha story", key="A1", description="Text.")],
            }
        ],
    )
    result = ingest_mod._ingest_plan_impl("moved")

    assert result["ok"] is True, result
    manifest = _manifest("moved")
    assert manifest["stories"]["issue-Alpha story"]["epic"] == "Second epic"
    assert manifest["plan_epics"] == ["Second epic"]


def test_descriptive_fields_are_outside_the_digest(
    manifest_plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    _write_plan(
        manifest_plan_dir,
        "digest",
        repo,
        _two_epic_plan(repo),
        preflight_override="emergency: pinned by the operator",
        repo_url="https://example.test/repo.git",
    )
    assert ingest_mod._ingest_plan_impl("digest")["ok"] is True
    manifest = _manifest("digest")

    assert manifest["preflight_override"] == "emergency: pinned by the operator"
    assert manifest["repo_url"] == "https://example.test/repo.git"

    stripped = json.loads(json.dumps(manifest))
    stripped.pop("plan_epics", None)
    stripped.pop("preflight_override", None)
    stripped.pop("repo_url", None)
    for story in stripped["stories"].values():
        story.pop("epic", None)
        story.pop("plan_key", None)

    assert canonical_digest(stripped) == canonical_digest(manifest)


def test_preflight_override_and_repo_url_absent_from_the_plan_are_not_recorded(
    manifest_plan_dir: Path, provider: _RecordingProvider, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    epics = [{"summary": "An epic", "stories": [_story("Plain story")]}]
    _write_plan(manifest_plan_dir, "plain", repo, epics)

    result = ingest_mod._ingest_plan_impl("plain")

    assert result["ok"] is True, result
    manifest = _manifest("plain")
    assert "preflight_override" not in manifest
    assert "repo_url" not in manifest


def test_descriptive_fields_are_not_ingest_authored_story_fields() -> None:
    """The descriptive fields are recorded, but they are not part of the
    shared authored-field tuple the re-ingest merge is driven by (that tuple
    lives in pipeline.server and drives its docstring/schema-doc coverage)."""
    import pipeline.server as server_mod

    authored = tuple(server_mod._INGEST_AUTHORED_STORY_FIELDS)
    for field in ("description", "epic", "plan_key"):
        assert field not in authored, f"{field!r} must not be an ingest authored field"


def test_plan_approval_spec_records_that_descriptive_fields_are_not_plan_defining() -> None:
    text = _PLAN_APPROVAL_SPEC.read_text(encoding="utf-8")
    assert "`epic`" in text
    assert "`plan_key`" in text
    assert "`plan_epics`" in text
    assert "`preflight_override`" in text
    assert "`repo_url`" in text
    assert "not plan-defining" in text
    assert "do not reset approval" in text
