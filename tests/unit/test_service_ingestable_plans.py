import json
from pathlib import Path

import pytest

from pipeline import server
from pipeline.service import PipelineService

# Helper to write a JSON file

def write_json(path: Path, data):
    path.write_text(json.dumps(data))


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    # Patch server.PLAN_DIR to the tmp_path
    monkeypatch.setattr(server, "PLAN_DIR", tmp_path)
    return tmp_path


def test_list_ingestable_plans_basic(plan_dir):
    write_json(plan_dir / "anagram.json", {"epics": {}})
    service = PipelineService()
    assert service.list_ingestable_plans() == ["anagram"]


def test_excludes_non_ingestable_files(plan_dir):
    write_json(plan_dir / "anagram.json", {"epics": {}})
    # Bookkeeping files deliberately use an UNRELATED stem ('other'), so this
    # test keeps verifying the file-shape rule (multi-dot stems are never plan
    # sources) without colliding with the separate already-ingested rule, which
    # would otherwise filter 'anagram' out via its own manifest.
    write_json(plan_dir / "other.manifest.json", {"epics": {}})
    write_json(plan_dir / "other.decisions.json", {"epics": {}})
    write_json(plan_dir / "other.storykey.journal.json", {"epics": {}})
    # Bookkeeping single-dot files are excluded because they lack 'epics'.
    write_json(plan_dir / "recent_workspaces.json", {"workspaces": []})
    write_json(plan_dir / "active_workspace.json", {"path": "/tmp/w"})
    service = PipelineService()
    assert service.list_ingestable_plans() == ["anagram"]


def test_excludes_file_without_epics(plan_dir):
    write_json(plan_dir / "foo.json", {"not_epics": 1})
    service = PipelineService()
    assert service.list_ingestable_plans() == []


def test_empty_dir_returns_empty(plan_dir):
    service = PipelineService()
    assert service.list_ingestable_plans() == []


def test_malformed_json_skipped(plan_dir):
    (plan_dir / "bad.json").write_text("{ not json }")
    service = PipelineService()
    assert service.list_ingestable_plans() == []


def test_sorted_output(plan_dir):
    write_json(plan_dir / "b.json", {"epics": {}})
    write_json(plan_dir / "a.json", {"epics": {}})
    write_json(plan_dir / "c.json", {"epics": {}})
    service = PipelineService()
    assert service.list_ingestable_plans() == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Already-ingested exclusion: a plan with a companion '<stem>.manifest.json'
# (written by pipeline/ingest.py via _store.manifest_path(plan_name)) has
# already been ingested and must not be offered as an ingest target again.
# ---------------------------------------------------------------------------


def test_excludes_already_ingested_plans(plan_dir):
    """Headline case: 'anagram' has a manifest (already ingested) -> excluded;
    'beta' has no manifest (not yet ingested) -> included."""
    write_json(plan_dir / "anagram.json", {"epics": {}})
    write_json(plan_dir / "anagram.manifest.json", {})
    write_json(plan_dir / "beta.json", {"epics": {}})
    service = PipelineService()
    assert service.list_ingestable_plans() == ["beta"]


def test_manifest_exclusion_is_by_existence_not_content(plan_dir):
    """The check is on the manifest file's existence, not its contents: even a
    manifest that is not valid JSON still marks the plan as already ingested."""
    write_json(plan_dir / "anagram.json", {"epics": {}})
    (plan_dir / "anagram.manifest.json").write_text("not json at all")
    service = PipelineService()
    assert service.list_ingestable_plans() == []


def test_empty_manifest_file_still_excludes(plan_dir):
    """A zero-byte manifest file still excludes the plan (existence check)."""
    write_json(plan_dir / "anagram.json", {"epics": {}})
    (plan_dir / "anagram.manifest.json").write_text("")
    service = PipelineService()
    assert service.list_ingestable_plans() == []


def test_all_plans_ingested_returns_empty(plan_dir):
    """Boundary: every candidate plan has a manifest -> empty list."""
    write_json(plan_dir / "a.json", {"epics": {}})
    write_json(plan_dir / "a.manifest.json", {})
    write_json(plan_dir / "b.json", {"epics": {}})
    write_json(plan_dir / "b.manifest.json", {"ingested_at": "2024-01-01"})
    service = PipelineService()
    assert service.list_ingestable_plans() == []


def test_no_manifests_returns_all_plans(plan_dir):
    """Boundary: no manifests at all -> unchanged pre-existing behavior."""
    write_json(plan_dir / "a.json", {"epics": {}})
    write_json(plan_dir / "b.json", {"epics": {}})
    service = PipelineService()
    assert service.list_ingestable_plans() == ["a", "b"]


def test_manifest_for_unrelated_stem_does_not_exclude(plan_dir):
    """A manifest whose stem does not match the plan must not exclude it."""
    write_json(plan_dir / "anagram.json", {"epics": {}})
    write_json(plan_dir / "other.manifest.json", {})
    service = PipelineService()
    assert service.list_ingestable_plans() == ["anagram"]


def test_manifest_exclusion_preserves_sorting(plan_dir):
    """Sorting still applies after already-ingested plans are filtered out."""
    write_json(plan_dir / "c.json", {"epics": {}})
    write_json(plan_dir / "a.json", {"epics": {}})
    write_json(plan_dir / "b.json", {"epics": {}})
    write_json(plan_dir / "b.manifest.json", {})
    service = PipelineService()
    assert service.list_ingestable_plans() == ["a", "c"]


def test_manifest_does_not_rescue_non_ingestable_file(plan_dir):
    """A file lacking 'epics' stays excluded whether or not it has a manifest,
    and a manifest for it must not make it appear as a plan."""
    write_json(plan_dir / "foo.json", {"not_epics": 1})
    write_json(plan_dir / "foo.manifest.json", {})
    service = PipelineService()
    assert service.list_ingestable_plans() == []


def test_malformed_plan_with_manifest_is_skipped_without_error(plan_dir):
    """A malformed plan file is skipped as before; its manifest presence must
    not turn the skip into an exception."""
    (plan_dir / "bad.json").write_text("{ not json }")
    write_json(plan_dir / "bad.manifest.json", {})
    write_json(plan_dir / "good.json", {"epics": {}})
    service = PipelineService()
    assert service.list_ingestable_plans() == ["good"]


def test_manifest_only_file_is_not_a_plan(plan_dir):
    """A lone '<stem>.manifest.json' with no '<stem>.json' yields nothing."""
    write_json(plan_dir / "anagram.manifest.json", {"epics": {}})
    service = PipelineService()
    assert service.list_ingestable_plans() == []


def test_docstring_documents_manifest_exclusion():
    """The docstring must document the new already-ingested exclusion."""
    import inspect

    from pipeline.service import PipelineService as _Service

    doc = inspect.getdoc(_Service.list_ingestable_plans) or ""
    lowered = doc.lower()
    assert "manifest" in lowered, doc
    assert any(
        phrase in lowered for phrase in ("ingested", "already", "companion")
    ), f"docstring does not describe manifest-based exclusion: {doc!r}"

