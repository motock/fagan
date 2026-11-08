"""The patch APPLY engine reads its target manifest through the record store.

``pipeline.worktree_patch.apply_patch`` used to load
``pipeline.server.PLAN_DIR / "<plan>.manifest.json"`` itself, so under
``PIPELINE_STORE=sqlite`` a patch proposed against a plan whose manifest lives
in the database was refused with "unknown plan or story" even though the store
knew the story.  It now reads
``_store.records().get_manifest("default", plan_name)`` -- mirroring
:func:`pipeline.checkpoint._plan_manifest` and
:func:`pipeline.cross_plan.neighbour_manifests`, both ADR 0012 stage B -- and
keeps the legacy plan-directory read as the fallback for a store with no
record view, so in-process store stand-ins keep working unchanged.

The failures still fail CLOSED: a manifest neither source can produce, or one
that lacks the story, is the same ``404 unknown plan or story`` and never
touches the worktree (no ``git apply`` call at all).
"""

from __future__ import annotations

import difflib
import json
import subprocess
from pathlib import Path

import pytest

from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import server as pserver
from pipeline import worktree_patch
from pipeline.worktree_patch import apply_patch, create_patch_record

PLAN_NAME = "wapstoreplan"
STORY_KEY = "WAP-STORE"
OTHER_STORY_KEY = "WAP-OTHER"

ORIGINAL_TEXT = "alpha\ngamma\n"
PATCHED_TEXT = "alpha\nBETA\ngamma\n"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _unified(rel: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
        )
    )


def _pending_record(worktree: Path) -> dict:
    """Store a pending patch record that rewrites ``app.py`` in *worktree*."""
    return create_patch_record(
        PLAN_NAME,
        STORY_KEY,
        _unified("app.py", ORIGINAL_TEXT, PATCHED_TEXT),
        ["app.py"],
        1,
    )


def _manifest(status: str, worktree: str) -> dict:
    return {
        "epics": {},
        "stories": {STORY_KEY: {"status": status, "worktree": worktree}},
    }


def _write_manifest(plan_dir: Path, manifest: dict) -> Path:
    path = plan_dir / f"{PLAN_NAME}.manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _assert_refusal(result: dict, *, error: str, status_code: int) -> None:
    assert result["ok"] is False
    assert result.get("status_code") == status_code, result
    assert result.get("error") == error, result


class _FakeRecordStore:
    """A store whose only useful surface is the single-manifest record read."""

    def __init__(self, manifests=None, *, raises: Exception | None = None):
        self._manifests = manifests or {}
        self._raises = raises
        self.requested: list[tuple[str, str]] = []

    def records(self):
        return self

    def get_manifest(self, namespace, plan_name):
        self.requested.append((namespace, plan_name))
        if self._raises is not None:
            raise self._raises
        return self._manifests.get(plan_name)


class _LegacyStore:
    """A store with no record view at all (an in-process stand-in)."""

    def manifest_path(self, plan_name):
        return pserver.PLAN_DIR / f"{plan_name}.manifest.json"


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """A private PLAN_DIR shared by the store, the engine and the journal."""
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(pserver, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


@pytest.fixture
def repo(tmp_path):
    """A real, committed, symlink-free git repo standing in for a worktree."""
    root = (tmp_path / "worktree").resolve()
    root.mkdir()
    (root / "app.py").write_text(ORIGINAL_TEXT, encoding="utf-8")
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test Author"),
        ("add", "-A"),
        ("commit", "-q", "-m", "initial"),
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


@pytest.fixture
def store(monkeypatch):
    """Install a fake record-store surface as ``pipeline.server._store``."""

    def install(fake) -> _FakeRecordStore:
        monkeypatch.setattr(pserver, "_store", fake, raising=False)
        return fake

    return install


# --------------------------------------------------------------------------
# the manifest comes from the store
# --------------------------------------------------------------------------


def test_should_apply_a_patch_when_the_manifest_lives_only_in_the_store(
    plan_dir, repo, store
):
    """No manifest on disk at all: the record store alone must satisfy apply."""
    store(_FakeRecordStore({PLAN_NAME: _manifest("failed", str(repo))}))
    rec = _pending_record(repo)

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    assert result["ok"] is True, result
    assert (repo / "app.py").read_text(encoding="utf-8") == PATCHED_TEXT


def test_should_prefer_the_store_manifest_over_the_plan_dir(
    plan_dir, repo, store
):
    """The record view is the source of truth: disk must not override it."""
    # On disk the story is ACTIVE (gate 3 would refuse it); the store says the
    # story is failed/repaired.  A store-first read succeeds; a disk-first one
    # returns 409.
    _write_manifest(plan_dir, _manifest("in_progress", str(repo)))
    store(_FakeRecordStore({PLAN_NAME: _manifest("failed", str(repo))}))
    rec = _pending_record(repo)

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    assert result["ok"] is True, result


def test_should_read_the_default_namespace_for_the_requested_plan(
    plan_dir, repo, store
):
    fake = store(_FakeRecordStore({}))
    _write_manifest(plan_dir, _manifest("in_progress", str(repo)))

    apply_patch(PLAN_NAME, STORY_KEY, "wp-not-a-real-id", "whatever")

    assert fake.requested == [("default", PLAN_NAME)]


# --------------------------------------------------------------------------
# the legacy plan-directory read is kept as the fallback
# --------------------------------------------------------------------------


def test_should_fall_back_to_the_plan_dir_when_the_store_has_no_manifest(
    plan_dir, repo, store
):
    store(_FakeRecordStore({}))
    _write_manifest(plan_dir, _manifest("in_progress", str(repo)))

    result = apply_patch(PLAN_NAME, STORY_KEY, "wp-not-a-real-id", "whatever")

    _assert_refusal(result, error="story is active", status_code=409)


def test_should_fall_back_to_the_plan_dir_when_the_store_raises(
    plan_dir, repo, store
):
    store(_FakeRecordStore(raises=RuntimeError("store unavailable")))
    _write_manifest(plan_dir, _manifest("in_progress", str(repo)))

    result = apply_patch(PLAN_NAME, STORY_KEY, "wp-not-a-real-id", "whatever")

    _assert_refusal(result, error="story is active", status_code=409)


def test_should_fall_back_to_the_plan_dir_for_a_store_without_a_record_view(
    plan_dir, repo, store
):
    store(_LegacyStore())
    _write_manifest(plan_dir, _manifest("in_progress", str(repo)))

    result = apply_patch(PLAN_NAME, STORY_KEY, "wp-not-a-real-id", "whatever")

    _assert_refusal(result, error="story is active", status_code=409)


def test_should_ignore_a_non_dict_store_result_and_use_the_plan_dir(
    plan_dir, repo, store
):
    fake = _FakeRecordStore()
    fake.get_manifest = lambda namespace, plan_name: ["not", "a", "manifest"]
    store(fake)
    _write_manifest(plan_dir, _manifest("in_progress", str(repo)))

    result = apply_patch(PLAN_NAME, STORY_KEY, "wp-not-a-real-id", "whatever")

    _assert_refusal(result, error="story is active", status_code=409)


# --------------------------------------------------------------------------
# fail closed: no manifest, no write
# --------------------------------------------------------------------------


def test_should_fail_closed_when_neither_source_has_the_manifest(
    plan_dir, repo, store, monkeypatch
):
    store(_FakeRecordStore({}))
    calls: list[object] = []
    real = worktree_patch._run_git_apply
    monkeypatch.setattr(
        worktree_patch,
        "_run_git_apply",
        lambda *a, **k: calls.append(a) or real(*a, **k),
    )
    rec = _pending_record(repo)

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    _assert_refusal(result, error="unknown plan or story", status_code=404)
    assert calls == []


def test_should_fail_closed_when_the_store_manifest_lacks_the_story(
    plan_dir, repo, store
):
    """A store manifest that omits the story is authoritative -- no fallback."""
    store(
        _FakeRecordStore(
            {
                PLAN_NAME: {
                    "epics": {},
                    "stories": {OTHER_STORY_KEY: {"status": "failed"}},
                }
            }
        )
    )
    # The disk copy DOES have the story; using it would leak a stale story in.
    _write_manifest(plan_dir, _manifest("failed", str(repo)))

    result = apply_patch(PLAN_NAME, STORY_KEY, "wp-not-a-real-id", "whatever")

    _assert_refusal(result, error="unknown plan or story", status_code=404)
