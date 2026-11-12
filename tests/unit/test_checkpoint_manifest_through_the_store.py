"""The interrupt checkpoint writes its manifest through the record store.

``_terminate_and_checkpoint`` used to finish with a direct manifest FILE write
(``_atomic_write_json(manifest_path, ...)``). Under ``PIPELINE_STORE=sqlite``
that write never reached the database, so an interrupted story was never
recorded as interrupted. It now persists via
``_store.records().put_manifest("default", plan_name, manifest)`` and takes
no ``manifest_path`` argument.
"""

import inspect
import json
import subprocess
from pathlib import Path

import pytest

from pipeline import server as p
from pipeline.checkpoint import _terminate_and_checkpoint
from pipeline.store_sqlite import SqliteStore

_PLAN = "P-SQL"
_STORY = "S-SQL"


def _init_repo(tmp: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.test"], cwd=tmp, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp, check=True)
    (tmp / "foo.txt").write_text("original\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=tmp, check=True)


@pytest.fixture
def sqlite_store(tmp_path, monkeypatch):
    """A SqliteStore with no manifest file on disk, wired in as the server's store."""
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", plan_dir)
    store = SqliteStore(str(tmp_path / "records.db"))
    monkeypatch.setattr(p, "_store", store)
    return store


def _seed_running_story(store, worktree: Path) -> dict:
    manifest = {
        "stories": {
            _STORY: {
                "worktree": str(worktree),
                "status": "running",
                "pid": 999999,  # nonexistent pid -> os.kill raises ProcessLookupError
                "last_commit": "",
            },
        },
    }
    store.records().put_manifest("default", _PLAN, manifest)
    return manifest


class _RecordingStore:
    """Delegates to the real store and records every record-view manifest write."""

    def __init__(self, inner):
        self._inner = inner
        self.manifest_writes = []

    def records(self):
        inner_records = self._inner.records()
        writes = self.manifest_writes

        class _Records:
            def put_manifest(self, namespace, plan_name, manifest):
                writes.append((namespace, plan_name, json.loads(json.dumps(manifest))))
                return inner_records.put_manifest(namespace, plan_name, manifest)

            def __getattr__(self, name):
                return getattr(inner_records, name)

        return _Records()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_interrupt_story_records_interrupted_status_under_sqlite(sqlite_store, tmp_path, monkeypatch):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _init_repo(worktree)
    (worktree / "foo.txt").write_text("changed\n")  # real WIP so a commit is made
    _seed_running_story(sqlite_store, worktree)
    recorder = _RecordingStore(sqlite_store)
    monkeypatch.setattr(p, "_store", recorder)

    result = p.interrupt_story(_PLAN, _STORY)

    assert result["ok"] is True
    assert len(recorder.manifest_writes) == 1
    namespace, plan_name, written = recorder.manifest_writes[0]
    assert (namespace, plan_name) == ("default", _PLAN)
    assert written["stories"][_STORY]["status"] == "interrupted"
    assert written["stories"][_STORY]["last_commit"] == result["commit"]
    assert written["stories"][_STORY]["interrupted_at"]
    stored = sqlite_store.records().get_manifest("default", _PLAN)["stories"][_STORY]
    assert stored["status"] == "interrupted"


def test_terminate_and_checkpoint_has_no_manifest_path_parameter():
    parameters = inspect.signature(_terminate_and_checkpoint).parameters

    assert "manifest_path" not in parameters


def test_terminate_and_checkpoint_rejected_story_key_still_terminates(sqlite_store, tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _init_repo(worktree)
    bad_key = "bad/key"
    story = {
        "worktree": str(worktree),
        "status": "running",
        "pid": 999999,
        "last_commit": "",
    }
    manifest = {"stories": {bad_key: story}}
    sqlite_store.records().put_manifest("default", _PLAN, manifest)

    sha = _terminate_and_checkpoint(
        manifest, _PLAN, bad_key, story,
        pid=999999, step="interrupted", summary="rejected key",
    )

    assert sha
    stored = sqlite_store.records().get_manifest("default", _PLAN)["stories"][bad_key]
    assert stored["status"] == "interrupted"
