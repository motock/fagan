"""A successful full ingest consumes the staged plan draft (ADR 0012 decision 5).

The manifest is the plan record: once a full ingest has written it, the staged
draft is a second copy of the same plan that can only drift, so it is deleted.
A *partial* ingest (``only_epics``) keeps the draft, because the epics that
were not ingested this round exist nowhere else.  A refused ingest and an
ingest that was skipped because the plan is locked keep the draft too: nothing
was written, so nothing was consumed.

Re-ingesting needs a staged draft again.  An ingest of a plan with no draft is
refused with a message that names the way out (``save_plan``, or
``export_plan`` to render the current plan).

Graded here, through the public API only (``save_plan`` / ``ingest_plan`` /
``export_plan``):

* a full ingest deletes the draft and leaves the manifest;
* an ``only_epics`` ingest keeps the draft;
* a refused ingest keeps the draft;
* a ``skipped: locked`` ingest keeps the draft;
* an ingest with no staged draft is refused with the pinned message;
* ``save_plan`` -> ``ingest_plan`` -> ``export_plan`` -> ``save_plan`` ->
  ``ingest_plan`` leaves the manifest's authored content and runtime state
  unchanged.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

# pipeline.server must load before pipeline.ingest: pipeline.ingest's module
# body imports pipeline.build_detect, which imports pipeline.server, which
# imports _ingest_plan_impl back from pipeline.ingest -- so importing
# pipeline.ingest first re-enters a partially-initialized pipeline.server.
import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import ingest as ingest_mod
from pipeline import persistence as ppers
from pipeline import plan_approval as pa

#: The refusal a re-ingest without a staged draft must answer with, verbatim.
NO_DRAFT_ERROR = (
    "plan 'ap' has no staged draft: save_plan it (export_plan renders the "
    "current plan) before re-ingesting"
)


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Point every plan-directory binding at a fresh directory."""
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    # Pin the dispatch provider to "claude" so the preflight gate never fires
    # (it would otherwise consult the operator's real role registry).
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    return d


@pytest.fixture
def provider(monkeypatch):
    """A ticket provider that issues no ids, so story keys come from the plan."""

    class _Provider:
        enabled = False

        def create_epic(self, summary):
            return None

        def create_story(self, summary, description, epic_id, agent):
            return None

    prov = _Provider()
    monkeypatch.setattr(p, "get_ticket_provider", lambda: prov)
    monkeypatch.setattr(ingest_mod, "_notify_user", lambda *a, **k: None)
    return prov


class _LockedStore:
    """The real store, but its plan transaction is always held by someone else.

    ``ingest_plan`` takes the plan transaction before it writes anything and
    answers ``{"ok": True, "skipped": "locked"}`` when the lock is contended.
    Everything else is delegated to the real store, so the draft is still
    read from the real records view.
    """

    def __init__(self, real) -> None:
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    @contextmanager
    def transaction(self, plan_name, wait_seconds: float = 0.0):
        yield False


def _story(**over) -> dict:
    story = {
        "summary": "Do the thing",
        "key": "S1",
        "agent_instructions": "Do it carefully.",
    }
    story.update(over)
    return story


def _plan_document(tmp_path: Path, stories=None, **plan_extra) -> dict:
    """Return a ``save_plan``-shaped document with one epic."""
    plan = {
        "repo_root": str(tmp_path),
        "epics": [
            {"summary": "E1", "stories": stories if stories is not None else [_story()]}
        ],
    }
    plan.update(plan_extra)
    return plan


def _stage(plan_dir: Path, document: dict, name: str = "ap") -> dict:
    """Stage ``document`` as ``name``'s draft through the public tool."""
    result = p.save_plan(name, json.dumps(document))
    assert result["ok"] is True, result
    return result


def _draft_path(plan_dir: Path, name: str = "ap") -> Path:
    return plan_dir / f"{name}.json"


def _manifest_path(plan_dir: Path, name: str = "ap") -> Path:
    return plan_dir / f"{name}.manifest.json"


def _read_manifest(plan_dir: Path, name: str = "ap") -> dict:
    return json.loads(_manifest_path(plan_dir, name).read_text())


# --------------------------------------------------------------------------- #
# 1. A full ingest consumes the draft.
# --------------------------------------------------------------------------- #
def test_a_full_ingest_deletes_the_staged_draft(plan_dir, provider, tmp_path):
    _stage(plan_dir, _plan_document(tmp_path))
    assert _draft_path(plan_dir).is_file()

    result = p.ingest_plan("ap")

    assert result["ok"] is True, result
    assert not _draft_path(plan_dir).exists(), "the draft survived a full ingest"
    assert _manifest_path(plan_dir).is_file(), "the manifest was not written"


def test_a_full_ingest_leaves_the_other_drafts_alone(plan_dir, provider, tmp_path):
    """Only the ingested plan's draft is consumed."""
    _stage(plan_dir, _plan_document(tmp_path), name="ap")
    _stage(plan_dir, _plan_document(tmp_path), name="other")

    assert p.ingest_plan("ap")["ok"] is True

    assert not _draft_path(plan_dir, "ap").exists()
    assert _draft_path(plan_dir, "other").is_file()


def test_a_full_ingest_removes_only_the_draft(plan_dir, provider, tmp_path):
    """The draft is the only thing the ingest removes: nothing else is lost."""
    _stage(plan_dir, _plan_document(tmp_path))
    before = {x.name for x in plan_dir.iterdir()}
    assert "ap.json" in before

    assert p.ingest_plan("ap")["ok"] is True

    after = {x.name for x in plan_dir.iterdir()}
    assert before - after == {"ap.json"}, after
    assert "ap.manifest.json" in after


# --------------------------------------------------------------------------- #
# 2. A partial ingest keeps the draft.
# --------------------------------------------------------------------------- #
def test_an_only_epics_ingest_keeps_the_staged_draft(plan_dir, provider, tmp_path):
    """The un-ingested epics exist nowhere else, so the draft must survive."""
    document = _plan_document(
        tmp_path,
        stories=[
            _story(summary="First", key="S1"),
            _story(summary="Second", key="S2"),
        ],
    )
    _stage(plan_dir, document)

    result = p.ingest_plan("ap", only_epics=["E1"])

    assert result["ok"] is True, result
    assert _draft_path(plan_dir).is_file(), "a partial ingest consumed the draft"


def test_an_only_epics_ingest_of_a_single_epic_keeps_the_draft(
    plan_dir, provider, tmp_path
):
    document = _plan_document(
        tmp_path,
        stories=[
            _story(summary="First", key="S1"),
            _story(summary="Second", key="S2"),
        ],
    )
    document["epics"].append(
        {"summary": "E2", "stories": [_story(summary="Third", key="S3")]}
    )
    _stage(plan_dir, document)

    result = p.ingest_plan("ap", only_epics=["E2"])

    assert result["ok"] is True, result
    assert _draft_path(plan_dir).is_file()


# --------------------------------------------------------------------------- #
# 3. A refused ingest keeps the draft.
# --------------------------------------------------------------------------- #
def test_a_refused_ingest_keeps_the_staged_draft(plan_dir, provider, tmp_path):
    """A refusal writes nothing, so it must consume nothing."""
    document = _plan_document(tmp_path)
    document["repo_root"] = str(tmp_path / "does-not-exist")
    _stage(plan_dir, document)

    result = p.ingest_plan("ap")

    assert result["ok"] is False
    assert "error" in result
    assert _draft_path(plan_dir).is_file(), "a refused ingest consumed the draft"


def test_a_refused_ingest_for_an_unknown_team_keeps_the_staged_draft(
    plan_dir, provider, tmp_path
):
    document = _plan_document(tmp_path, team="no-such-team/../escape")
    _stage(plan_dir, document)

    result = p.ingest_plan("ap")

    assert result["ok"] is False, result
    assert _draft_path(plan_dir).is_file()


# --------------------------------------------------------------------------- #
# 4. A locked ingest keeps the draft.
# --------------------------------------------------------------------------- #
def test_a_locked_ingest_keeps_the_staged_draft(plan_dir, provider, tmp_path, monkeypatch):
    """``skipped: locked`` wrote no manifest, so the draft must survive."""
    _stage(plan_dir, _plan_document(tmp_path))

    real_store = p._store
    monkeypatch.setattr(p, "_store", _LockedStore(real_store))

    result = p.ingest_plan("ap")

    assert result["ok"] is True, result
    assert result.get("skipped") == "locked", result
    assert _draft_path(plan_dir).is_file(), "a locked ingest consumed the draft"


# --------------------------------------------------------------------------- #
# 5. An ingest with no staged draft is refused.
# --------------------------------------------------------------------------- #
def test_an_ingest_without_a_staged_draft_is_refused(plan_dir, provider, tmp_path):
    result = p.ingest_plan("ap")

    assert result["ok"] is False
    assert result["error"] == NO_DRAFT_ERROR


def test_a_reingest_without_a_staged_draft_is_refused(
    plan_dir, provider, tmp_path
):
    """The draft is gone after the first ingest, so the second one is refused."""
    _stage(plan_dir, _plan_document(tmp_path))
    assert p.ingest_plan("ap")["ok"] is True

    result = p.ingest_plan("ap")

    assert result["ok"] is False
    assert result["error"] == NO_DRAFT_ERROR


def test_a_refused_ingest_without_a_staged_draft_names_the_plan(
    plan_dir, provider, tmp_path
):
    result = p.ingest_plan("other")

    assert result["ok"] is False
    assert result["error"] == (
        "plan 'other' has no staged draft: save_plan it (export_plan renders "
        "the current plan) before re-ingesting"
    )


def test_the_refusal_writes_no_manifest(plan_dir, provider, tmp_path):
    p.ingest_plan("ap")

    assert not _manifest_path(plan_dir).exists()


# --------------------------------------------------------------------------- #
# 6. The round trip is stable.
# --------------------------------------------------------------------------- #
def test_save_ingest_export_save_ingest_leaves_the_manifest_unchanged(
    plan_dir, provider, tmp_path, monkeypatch
):
    """Re-staging the exported plan and ingesting it again changes nothing.

    The export renders the current plan, so the second ingest applies the same
    authored content: the manifest's authored content and its runtime state
    both survive the round trip.
    """
    monkeypatch.setenv(pa.APPROVAL_ENV, "on")
    _stage(plan_dir, _plan_document(tmp_path))
    assert p.ingest_plan("ap")["ok"] is True

    before = _read_manifest(plan_dir)

    exported = p.export_plan("ap")
    assert exported["ok"] is True, exported
    assert "plan" in exported, "export_plan must return the rendered document"

    _stage(plan_dir, exported["plan"])

    assert p.ingest_plan("ap")["ok"] is True

    after = _read_manifest(plan_dir)
    for field in ("stories", "epics", "repo_root", "team", "role_config"):
        assert after.get(field) == before.get(field), field
    # The runtime state of every story survives the round trip.
    for key, story in before["stories"].items():
        assert after["stories"][key].get("status") == story.get("status")
    # The draft is consumed again by the second full ingest.
    assert not _draft_path(plan_dir).exists()


def test_a_reingest_of_the_exported_plan_keeps_the_stories(
    plan_dir, provider, tmp_path
):
    """The exported document re-ingests into the same stories, not new ones."""
    _stage(plan_dir, _plan_document(tmp_path))
    assert p.ingest_plan("ap")["ok"] is True

    exported = p.export_plan("ap")
    _stage(plan_dir, exported["plan"])
    assert p.ingest_plan("ap")["ok"] is True

    manifest = _read_manifest(plan_dir)
    assert set(manifest["stories"]) == {"S1"}
    assert manifest["stories"]["S1"]["summary"] == "Do the thing"


def test_a_reingest_merges_a_new_story_into_the_manifest(
    plan_dir, provider, tmp_path
):
    """A new draft merges into the manifest by explicit story key."""
    _stage(plan_dir, _plan_document(tmp_path))
    assert p.ingest_plan("ap")["ok"] is True

    _stage(
        plan_dir,
        _plan_document(
            tmp_path, stories=[_story(summary="Another", key="S2")]
        ),
    )
    assert p.ingest_plan("ap")["ok"] is True

    manifest = _read_manifest(plan_dir)
    assert set(manifest["stories"]) == {"S1", "S2"}


def test_a_keyless_reingest_is_still_refused(plan_dir, provider, tmp_path):
    """The existing 'has no explicit `key`' refusal is unchanged."""
    _stage(plan_dir, _plan_document(tmp_path, stories=[_story(key=None)]))
    assert p.ingest_plan("ap")["ok"] is True

    _stage(plan_dir, _plan_document(tmp_path, stories=[_story(key=None)]))
    result = p.ingest_plan("ap")

    assert result["ok"] is False, result
    assert "has no explicit `key`" in result["error"], result["error"]


# --------------------------------------------------------------------------- #
# 7. The draft listing reflects the consumption.
# --------------------------------------------------------------------------- #
def test_the_draft_is_no_longer_listed_after_a_full_ingest(
    plan_dir, provider, tmp_path
):
    _stage(plan_dir, _plan_document(tmp_path))
    records = p._store.records()
    assert "ap" in records.list_plan_drafts("default")

    assert p.ingest_plan("ap")["ok"] is True

    assert "ap" not in records.list_plan_drafts("default")


# --------------------------------------------------------------------------- #
# 8. A crash between the manifest write and the draft delete is recoverable.
# --------------------------------------------------------------------------- #
def test_reingesting_the_same_content_after_a_crash_is_idempotent(
    plan_dir, provider, tmp_path
):
    """The same draft ingested twice yields the same manifest.

    This is the property that makes a crash between the manifest write and
    the draft delete safe: the next ingest re-applies identical content by
    key, and nothing changes.
    """
    _stage(plan_dir, _plan_document(tmp_path))
    assert p.ingest_plan("ap")["ok"] is True
    first = _read_manifest(plan_dir)

    # The draft survives (as it would after a crash) and is ingested again.
    _stage(plan_dir, _plan_document(tmp_path))
    assert p.ingest_plan("ap")["ok"] is True
    second = _read_manifest(plan_dir)

    assert second["stories"] == first["stories"]
    assert second["epics"] == first["epics"]
    assert set(second["stories"]) == {"S1"}
