"""commit_story persists through the record store, not the caller's path.

``commit_story`` used to re-read the manifest from the caller's
``manifest_path`` and write it back with ``server._atomic_write_json``. It now
reads with ``_store.records().get_manifest("default", plan_name)`` and writes
with ``put_manifest``, so the file it touches is always
``<PLAN_DIR>/<plan>.manifest.json`` (the store's own path) no matter what path
the caller threads through. The plan-lock transaction around it is unchanged:
a reentrant hold when this thread already owns the plan lock, otherwise a
bounded retry of the non-blocking ``concurrency._plan_lock`` that raises
``PlanLockReacquireTimeout`` without writing.

The tests below pin that seam: the decoy path stays byte-identical while the
store's path gains the update, a busy lock still writes nothing, a missing
manifest still raises (with no filesystem path in the message), and a plan
name the store rejects fails closed without leaving the plan lock held.

The seeded manifests always carry both stories: ``commit_story`` replaces an
existing story and refuses (warning, no write) to resurrect one the manifest no
longer has, so a "s1" that is absent would legitimately write nothing.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

# pipeline.server is imported explicitly and first: importing any pipeline
# submodule before it trips the pre-existing package import cycle.
import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import story_commit

PLAN = "alpha"

#: Wall-clock bound for "the busy-lock call must not block". The re-acquire
#: budget is pinned to 0 through the env var, so the call fails immediately.
_NO_BLOCK_SECONDS = 30.0


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """The store's plan directory (S), patched onto every binding that reads it."""
    s = tmp_path / "S"
    s.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", s)
    # persistence and concurrency bind PLAN_DIR by value at their own module
    # load, so the patch has to land on those bindings too.
    monkeypatch.setattr(ppers, "PLAN_DIR", s)
    monkeypatch.setattr(pcon, "PLAN_DIR", s)
    return s


def _seed(plan_dir: Path, stories: list[dict], plan_name: str = PLAN) -> Path:
    """Write a manifest carrying both stories, s1 with a stale status."""
    manifest = {
        "plan": {"name": plan_name},
        "wave": 3,
        "stories": {
            "s0": stories[0],
            "s1": stories[1],
        },
    }
    path = plan_dir / f"{plan_name}.manifest.json"
    path.write_text(json.dumps(manifest))
    return path


def _stale() -> list[dict]:
    return [
        {"id": "s0", "status": "agent_done"},
        {"id": "s1", "status": "tests_passed"},
    ]


def _updated() -> dict:
    return {"id": "s1", "status": "reviewed"}


def test_should_write_the_story_through_the_record_store_when_the_lock_is_held(
    plan_dir, tmp_path
):
    """Reentrant hold: the store's path is written, the caller's is untouched."""
    decoy = tmp_path / "decoy" / f"{PLAN}.manifest.json"
    decoy.parent.mkdir()
    _seed(decoy.parent, _stale())
    decoy_before = decoy.read_bytes()
    store_path = _seed(plan_dir, _stale())

    with pcon._plan_lock(PLAN) as acquired:
        assert acquired, "test could not take the plan lock"
        story_commit.commit_story(PLAN, "s1", _updated(), decoy)

    assert decoy.read_bytes() == decoy_before, (
        "the write followed the caller's manifest_path instead of the store"
    )
    written = json.loads(store_path.read_text())
    assert written["stories"]["s0"] == _stale()[0], "other story was clobbered"
    assert written["stories"]["s1"] == _updated(), "story was not persisted"
    assert written["wave"] == 3, "plan-level field was clobbered"


def test_should_write_the_story_through_the_record_store_after_taking_the_lock(
    plan_dir, tmp_path
):
    """No outer hold: commit_story takes the lock itself, then writes at S."""
    decoy = tmp_path / "decoy" / f"{PLAN}.manifest.json"
    decoy.parent.mkdir()
    _seed(decoy.parent, _stale())
    decoy_before = decoy.read_bytes()
    store_path = _seed(plan_dir, _stale())

    story_commit.commit_story(PLAN, "s1", _updated(), decoy)

    assert decoy.read_bytes() == decoy_before, (
        "the write followed the caller's manifest_path instead of the store"
    )
    written = json.loads(store_path.read_text())
    assert written["stories"]["s0"] == _stale()[0], "other story was clobbered"
    assert written["stories"]["s1"] == _updated(), "story was not persisted"


def test_should_not_write_when_the_lock_is_busy(plan_dir, monkeypatch):
    """Existing contract: a busy plan lock raises and writes nothing."""
    # Pin the re-acquire budget to 0 so the contention path cannot spin for the
    # 300s default; the call must fail on its first attempt, not block.
    monkeypatch.setenv("PIPELINE_PLAN_LOCK_REACQUIRE_TIMEOUT_SECONDS", "0")
    store_path = _seed(plan_dir, _stale())
    before = store_path.read_bytes()

    holding = threading.Event()
    release = threading.Event()

    def hold_the_lock() -> None:
        with pcon._plan_lock(PLAN) as acquired:
            assert acquired, "holder thread could not take the plan lock"
            holding.set()
            release.wait(_NO_BLOCK_SECONDS)

    holder = threading.Thread(target=hold_the_lock)
    holder.start()
    try:
        assert holding.wait(_NO_BLOCK_SECONDS), "holder never took the lock"
        started = time.monotonic()
        with pytest.raises(pcon.PlanLockReacquireTimeout):
            story_commit.commit_story(PLAN, "s1", _updated(), store_path)
        elapsed = time.monotonic() - started
        assert elapsed < _NO_BLOCK_SECONDS, "commit_story blocked on a busy lock"
        assert store_path.read_bytes() == before, "a busy lock must not write"
        assert json.loads(store_path.read_text())["stories"]["s1"] == _stale()[1]
    finally:
        release.set()
        holder.join(_NO_BLOCK_SECONDS)
    assert not holder.is_alive(), "holder thread did not exit"

    # The losing caller must have left the lock state intact: the same commit
    # succeeds once the holder is gone.
    story_commit.commit_story(PLAN, "s1", _updated(), store_path)
    written = json.loads(store_path.read_text())
    assert set(written["stories"]) == {"s0", "s1"}
    assert written["stories"]["s1"] == _updated()


def test_should_raise_and_not_write_when_the_manifest_is_missing(
    plan_dir, tmp_path
):
    """No manifest: raise without a path in the message and write nothing."""
    store_path = plan_dir / f"{PLAN}.manifest.json"
    assert not store_path.exists()

    with pytest.raises(FileNotFoundError) as caught:
        story_commit.commit_story(PLAN, "s1", _updated(), store_path)

    message = str(caught.value)
    assert str(tmp_path) not in message, "message leaked a filesystem path"
    assert str(plan_dir) not in message, "message leaked the plan directory"
    assert ".json" not in message, "message leaked a file name"
    assert "Traceback" not in message
    assert not store_path.exists(), "a missing manifest must not be created"


def test_should_not_write_for_a_plan_name_the_store_rejects(plan_dir, tmp_path):
    """A key the store refuses fails closed, then leaves the lock free."""
    rejected = "a..b"
    store_path = plan_dir / f"{PLAN}.manifest.json"
    _seed(plan_dir, _stale())
    before = store_path.read_bytes()

    with pytest.raises(Exception) as caught:
        story_commit.commit_story(
            rejected, "s1", _updated(), plan_dir / f"{rejected}.manifest.json"
        )

    assert isinstance(caught.value, ValueError), (
        f"expected a ValueError, got {type(caught.value).__name__}"
    )
    message = str(caught.value)
    assert "Traceback" not in message, "a traceback leaked to the caller"
    assert str(tmp_path) not in message, "message leaked a filesystem path"
    assert not list(plan_dir.glob("*manifest.json"))[1:], "a file was written"
    assert not (plan_dir / f"{rejected}.manifest.json").exists()
    assert store_path.read_bytes() == before, "the alpha manifest was rewritten"

    # The plan lock taken for the rejected name must have been released: a
    # later commit of a valid plan still writes.
    story_commit.commit_story(PLAN, "s1", _updated(), store_path)
    written = json.loads(store_path.read_text())
    assert written["stories"]["s1"] == _updated(), (
        "the rejected call left the plan lock held"
    )
