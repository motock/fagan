"""STORE-B1c: escalation resets are persisted through the record store.

``escalation._escalate_to_claude`` and ``_escalate_to_local_fallback_model``
wrote the caller's manifest to the caller's path instead of through the plan's
record store. The escalation write therefore spelled out the plan-directory
layout at the call site and, under ``PIPELINE_STORE=sqlite``, wrote only the file
projection while bypassing the database row: an escalated story read back as
un-escalated.

What these tests pin down:

* the reset is persisted through the store's namespaced record view -
  ``_store.records().put_manifest(DEFAULT_NAMESPACE, plan_name, manifest)`` -
  so both store kinds persist it;
* nothing writes the ``manifest_path`` argument directly any more (the
  argument itself stays: removing it is ADR 0012 stage C);
* a plan name the store rejects (``"a..b"``: accepted by the caller-side key
  check, refused by ``store_records.validate_plan_name``)
  fails closed - no file, no record, and the refusal surfaces as a ``ValueError``
  instead of being swallowed or papered over with the old writer;
* a following, valid escalation still persists, and the rejected plan stays
  absent.

The file store keeps the on-disk layout (``<plan>.manifest.json``), so the
plan-directory-rooted store fixture below is the same seam the legacy writer
used.
"""

from __future__ import annotations

import pytest

import pipeline.server as p
from pipeline import escalation, pr
from pipeline.store import FileStore

PLAN = "demo"
STORY_KEY = "demo-1"
SENTINEL = "abc123"
FALLBACK_MODEL = "gpt-oss-20b-high:latest"


class _FakeSubprocess:
    """Stand-in for the git boundary (the real one is never shelled out)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, cmd, **kwargs):
        self.calls.append(cmd)


class _RecordingStore(FileStore):
    """A file store that records every manifest the reset persists.

    The spy is the store's own record view, not the legacy writer: the point of
    these tests is that the reset reaches ``records().put_manifest`` at all.
    """

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[tuple[str, str, dict]] = []

    def records(self):
        view = super().records()
        original = view.put_manifest

        def put_manifest(namespace, plan_name, manifest):
            self.writes.append((namespace, plan_name, manifest))
            return original(namespace, plan_name, manifest)

        view.put_manifest = put_manifest
        return view


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A file store rooted at ``tmp_path``, resolved by the escalation module.

    ``pipeline.server.PLAN_DIR`` is the path every ``_store`` implementation
    derives its targets from, and ``pipeline.server._store`` is the live store
    binding the escalation module resolves at call time.
    """
    monkeypatch.setattr(p, "PLAN_DIR", tmp_path)
    file_store = _RecordingStore()
    monkeypatch.setattr(p, "_store", file_store)
    monkeypatch.setattr(escalation, "subprocess", _FakeSubprocess())
    monkeypatch.setattr(pr, "_resolve_story_branch", lambda worktree, key: f"agent/{key}")
    return file_store


def _manifest(repo_root: str = "") -> dict:
    return {
        "plan_name": PLAN,
        "repo_root": repo_root,
        "local_model_fallback": FALLBACK_MODEL,
        "_test_sentinel": SENTINEL,
        "stories": {
            STORY_KEY: {
                "id": STORY_KEY,
                "status": "failed",
                "backend": "local",
                "model": "qwen",
                "worktree": "",
                "log": "/tmp/agent.log",
                "pid": 4321,
                "dispatch_attempts": 2,
                "dispatch_error": "boom",
                "step_cap_streak": 3,
                "infra_failure_streak": 2,
                "agent_instructions": "make the test pass",
            }
        },
    }


def test_should_persist_the_claude_reset_through_the_record_store(store, tmp_path):
    manifest = _manifest()

    escalation._escalate_to_claude(
        manifest, PLAN, STORY_KEY, store.manifest_path(PLAN)
    )

    assert [w[:2] for w in store.writes] == [("default", PLAN)], (
        "the reset must be persisted through the record store"
    )
    stored = store.records().get_manifest("default", PLAN)
    assert stored is not None, "the reset must be persisted through the store"
    assert stored["_test_sentinel"] == SENTINEL
    assert stored["stories"][STORY_KEY]["escalated"] is True
    assert stored["stories"][STORY_KEY]["status"] == "todo"
    # Persisted to disk by the store, not by the caller's fabricated path.
    assert (tmp_path / f"{PLAN}.manifest.json").exists()


def test_should_persist_the_fallback_reset_through_the_record_store(
    store, tmp_path
):
    manifest = _manifest()

    escalation._escalate_to_local_fallback_model(
        manifest, PLAN, STORY_KEY, store.manifest_path(PLAN), FALLBACK_MODEL
    )

    assert [w[:2] for w in store.writes] == [("default", PLAN)], (
        "the reset must be persisted through the record store"
    )
    stored = store.records().get_manifest("default", PLAN)
    assert stored is not None, "the reset must be persisted through the store"
    assert stored["_test_sentinel"] == SENTINEL
    assert stored["stories"][STORY_KEY]["tried_fallback_model"] is True
    assert (tmp_path / f"{PLAN}.manifest.json").exists()


def test_should_not_write_for_a_plan_name_the_store_rejects(store, tmp_path):
    plan_name = "a..b"
    manifest_path = tmp_path / f"{plan_name}.manifest.json"
    manifest = _manifest()
    manifest["plan_name"] = plan_name

    with pytest.raises(ValueError):
        escalation._escalate_to_claude(manifest, plan_name, STORY_KEY, manifest_path)

    # InvalidRecordKeyError is raised before any bytes are written: the caller's
    # fabricated path stays absent, and the store's own path is the same file.
    assert not manifest_path.exists()
    assert not store.manifest_path(plan_name).exists()

    # A later, valid escalation still persists, and the rejected plan stays absent.
    escalation._escalate_to_claude(
        _manifest(), PLAN, STORY_KEY, tmp_path / f"{plan_name}.manifest.json"
    )
    stored = store.records().get_manifest("default", PLAN)
    assert stored is not None
    assert stored["_test_sentinel"] == SENTINEL
    assert not manifest_path.exists()
    assert not store.manifest_path(plan_name).exists()
