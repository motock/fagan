"""The escalated story's journal is cleared through the record store.

``_escalate_to_claude`` / ``_escalate_to_local_fallback_model`` used to unlink
``PLAN_DIR / f"{plan_name}.{story_key}.journal.json"`` at the call site, which
spelled out the plan-directory layout in the escalation module and - under
``PIPELINE_STORE=sqlite`` - removed only the file projection, leaving the
database row (and therefore the journal the next dispatch reads back) intact.

What these tests pin down:

* the clear goes through the store's namespaced record view -
  ``_store.records().clear_journal("default", plan_name, story_key)`` - in both
  escalation paths;
* the arguments are ``(plan_name, story_key)`` in that order, so a plan holding
  two journals ends with only the escalated story's journal gone and a second
  escalation clears the second one (the state that persists across calls);
* escalating a story that has no journal is not an error.

The file store keeps the on-disk layout (``<plan>.<story>.journal.json``), so
a real file store rooted at ``tmp_path`` is the same seam the legacy unlink
used.
"""

from __future__ import annotations

import pytest

import pipeline.server as p
from pipeline import escalation, pr
from pipeline.store import FileStore

PLAN = "demo"
STORY_KEY = "demo-1"
OTHER_STORY_KEY = "demo-2"
FALLBACK_MODEL = "gpt-oss-20b-high:latest"


class _FakeSubprocess:
    """Stand-in for the git boundary (the real one is never shelled out)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, cmd, **kwargs):
        self.calls.append(cmd)


class _RecordingStore(FileStore):
    """A file store that records every ``clear_journal`` the reset performs."""

    def __init__(self) -> None:
        super().__init__()
        self.cleared: list[tuple[str, str, str]] = []

    def records(self):
        view = super().records()
        original = view.clear_journal

        def clear_journal(namespace, plan_name, story_key):
            self.cleared.append((namespace, plan_name, story_key))
            return original(namespace, plan_name, story_key)

        view.clear_journal = clear_journal
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


def _manifest() -> dict:
    return {
        "plan_name": PLAN,
        "repo_root": "",
        "local_model_fallback": FALLBACK_MODEL,
        "stories": {
            key: {
                "id": key,
                "status": "failed",
                "backend": "local",
                "model": "qwen",
                "worktree": "",
                "log": "/tmp/agent.log",
                "pid": 4321,
                "dispatch_attempts": 2,
                "dispatch_error": "boom",
                "agent_instructions": "make the test pass",
            }
            for key in (STORY_KEY, OTHER_STORY_KEY)
        },
    }


def _seed_journal(store, story_key: str) -> None:
    store.records().append_journal("default", PLAN, story_key, {"step": "boom"})


def _journal(store, story_key: str) -> list[dict]:
    return store.records().get_journal("default", PLAN, story_key)


def test_should_clear_the_journal_when_escalating_to_claude(store):
    _seed_journal(store, STORY_KEY)
    assert _journal(store, STORY_KEY), "the journal must exist before the escalation"

    escalation._escalate_to_claude(
        _manifest(), PLAN, STORY_KEY
    )

    assert store.cleared == [("default", PLAN, STORY_KEY)], (
        "the escalation must clear the journal through the record store"
    )
    assert _journal(store, STORY_KEY) == [], (
        "the escalated story's journal must be gone"
    )


def test_should_clear_the_journal_when_escalating_to_the_fallback_model(store):
    _seed_journal(store, STORY_KEY)
    assert _journal(store, STORY_KEY), "the journal must exist before the escalation"

    escalation._escalate_to_local_fallback_model(
        _manifest(), PLAN, STORY_KEY, FALLBACK_MODEL
    )

    assert store.cleared == [("default", PLAN, STORY_KEY)], (
        "the fallback escalation must clear the journal through the record store"
    )
    assert _journal(store, STORY_KEY) == [], (
        "the escalated story's journal must be gone"
    )


def test_should_escalate_when_no_journal_exists(store):
    assert _journal(store, STORY_KEY) == [], "no journal is seeded for this test"

    escalation._escalate_to_claude(
        _manifest(), PLAN, STORY_KEY
    )

    stored = store.records().get_manifest("default", PLAN)
    assert stored["stories"][STORY_KEY]["escalated"] is True
    assert _journal(store, STORY_KEY) == []


def test_should_not_clear_another_story_journal(store):
    _seed_journal(store, STORY_KEY)
    _seed_journal(store, OTHER_STORY_KEY)

    escalation._escalate_to_claude(
        _manifest(), PLAN, STORY_KEY
    )
    assert _journal(store, OTHER_STORY_KEY), (
        "another story's journal must survive the escalation"
    )

    # Second call: the other story's journal must STILL be findable before it is
    # cleared - a swapped key or a hardcoded plan name would silently clear
    # nothing here while the first call looked correct.
    escalation._escalate_to_claude(
        _manifest(), PLAN, OTHER_STORY_KEY
    )

    assert store.cleared == [
        ("default", PLAN, STORY_KEY),
        ("default", PLAN, OTHER_STORY_KEY),
    ]
    assert _journal(store, OTHER_STORY_KEY) == []
    assert _journal(store, STORY_KEY) == []
