"""Manifest reads and writes in the namespace bound for the call.

The scheduler advances each namespace inside ``pipeline.actor.bind_namespace``,
so every manifest read and write below a tick has to land in the namespace
bound for that tick instead of in the default partition. The record-store
wrappers in ``pipeline.bound_records`` are where that selection happens.

Two things are pinned here.

The structural scan holds the six production modules that read or write
manifests (``advance``, ``advance_merge``, ``advance_review``, ``merge``,
``merge_reverify`` and ``story_commit``) to naming no default partition
literally any more, and to reaching the bound namespace through the
``bound_records`` wrappers.

The behavioural tests show the selection through the entry points the brief
names: ``story_commit.commit_story`` writes the bound partition and leaves the
default one alone, ``merge``'s namespace guard refuses a bound request only
when the store can hold nothing but the default partition (a record store
proceeds), and ``merge``'s manifest handling reads and writes the bound
partition.
"""

from __future__ import annotations

import contextlib
import re
from pathlib import Path

import pytest

# pipeline.server is imported explicitly and first: importing any pipeline
# submodule before it trips the pre-existing package import cycle.
import pipeline.server
from pipeline import authorization, concurrency, merge, story_commit
from pipeline.actor import bind_namespace, current_namespace
from pipeline.store import FileStore
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import DEFAULT_NAMESPACE
from tests.unit._line_limit_policy import assert_within_line_limit

REPO_ROOT = Path(__file__).resolve().parents[2]

NAMESPACE = "a/b"
PLAN = "alpha"
STORY = "s1"

#: The production modules that read or write manifests by naming a partition.
MODULES = (
    "pipeline/advance.py",
    "pipeline/advance_merge.py",
    "pipeline/advance_review.py",
    "pipeline/merge.py",
    "pipeline/merge_approve.py",
    "pipeline/merge_reverify.py",
    "pipeline/story_commit.py",
)

#: A record-store call that names the default partition literally. The
#: wrappers select the bound namespace instead, so no call may name it.
DEFAULT_PARTITION_CALL = re.compile(
    r"""records\(\)\s*\.\s*(?:get_manifest|put_manifest|list_plans)\(\s*["']default["']"""
)

#: The refusal the merge guard returns for a store that cannot hold the
#: bound namespace.
REFUSAL = {"ok": False, "error": "unknown namespace"}


class _RecordStore:
    """A record-store facade: its record view is one in-memory record store.

    The store-level reads and writes answer for the namespace bound for the
    call, the way a store that can hold every partition does.
    """

    def __init__(self, records: InMemoryRecordStore) -> None:
        self._records = records

    def records(self) -> InMemoryRecordStore:
        return self._records

    def transaction(self, plan_name: str, wait_seconds: float = 0.0):
        return _always_acquired_lock(plan_name)

    def get_manifest(self, plan_name: str) -> dict:
        manifest = self._records.get_manifest(current_namespace(), plan_name)
        if manifest is None:
            raise FileNotFoundError(plan_name)
        return manifest

    def get_manifest_or_none(self, plan_name: str) -> dict | None:
        return self._records.get_manifest(current_namespace(), plan_name)

    def save_manifest(self, plan_name: str, manifest: dict) -> None:
        self._records.put_manifest(current_namespace(), plan_name, manifest)


class _FileStoreStub(FileStore):
    """Stands in for the file store: its layout holds only the default partition."""

    def __init__(self) -> None:
        pass


@contextlib.contextmanager
def _always_acquired_lock(plan_name: str, wait_seconds: float = 0.0):
    """A plan lock that is always acquired."""
    yield True


def _manifest(*stories: str) -> dict:
    """A manifest carrying *stories*, each one in its pre-commit state."""
    return {"stories": {key: {"status": "agent_done"} for key in stories}}


def _deny(monkeypatch: pytest.MonkeyPatch) -> list:
    """Make authorization deny, and record that it was reached."""
    calls: list = []

    def _authorize(*args, **kwargs):
        calls.append((args, kwargs))
        return {"ok": False, "error": "denied"}

    monkeypatch.setattr(authorization, "authorize", _authorize)
    return calls


def _allow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make authorization allow, so the manifest handling is reached."""
    monkeypatch.setattr(authorization, "authorize", lambda *args, **kwargs: None)
    monkeypatch.setattr(merge, "record_audit", lambda *args, **kwargs: None)


@pytest.fixture
def records() -> InMemoryRecordStore:
    """One in-memory record store, holding every partition of one test."""
    return InMemoryRecordStore()


@pytest.fixture
def store(
    records: InMemoryRecordStore, monkeypatch: pytest.MonkeyPatch
) -> _RecordStore:
    """Hand the production paths a record store for the whole test."""
    facade = _RecordStore(records)
    monkeypatch.setattr(pipeline.server, "_store", facade)
    return facade


@pytest.fixture
def plan_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plan lock that is always acquired, so the commit path is hermetic."""
    monkeypatch.setattr(concurrency, "_plan_lock", _always_acquired_lock)


# -- the structural scan ------------------------------------------------------


def test_no_module_names_the_default_partition() -> None:
    """No manifest call in the six modules names the default partition."""
    offenders = {
        relative: DEFAULT_PARTITION_CALL.findall(
            (REPO_ROOT / relative).read_text(encoding="utf-8")
        )
        for relative in MODULES
    }

    assert not {name: hits for name, hits in offenders.items() if hits}, offenders


def test_every_module_reaches_the_bound_namespace() -> None:
    """Each of the six modules selects the namespace through the wrappers."""
    missing = [
        relative
        for relative in MODULES
        if "bound_records" not in (REPO_ROOT / relative).read_text(encoding="utf-8")
    ]

    assert not missing, f"no bound-namespace wrappers in: {missing}"


def test_advance_merge_stays_under_its_line_cap() -> None:
    """``advance_merge`` sits at its 1000-line cap and must not grow past it."""
    source = (REPO_ROOT / "pipeline/advance_merge.py").read_text(encoding="utf-8")

    assert_within_line_limit(len(source.splitlines()), 1000, "pipeline/advance_merge.py")


# -- story commit -------------------------------------------------------------


def test_bound_commit_lands_in_the_bound_partition(
    records: InMemoryRecordStore, store: _RecordStore, plan_lock: None
) -> None:
    """With ``a/b`` bound the commit lands in that partition only."""
    records.put_manifest(DEFAULT_NAMESPACE, PLAN, _manifest(STORY))
    records.put_manifest(NAMESPACE, PLAN, _manifest(STORY))

    with bind_namespace(NAMESPACE):
        story_commit.commit_story(PLAN, STORY, {"status": "reviewed"})

    assert records.get_manifest(NAMESPACE, PLAN)["stories"][STORY] == {
        "status": "reviewed"
    }
    assert records.get_manifest(DEFAULT_NAMESPACE, PLAN)["stories"][STORY] == {
        "status": "agent_done"
    }


def test_unbound_commit_lands_in_the_default_partition(
    records: InMemoryRecordStore, store: _RecordStore, plan_lock: None
) -> None:
    """Unbound, the commit lands in the default partition, as before."""
    records.put_manifest(DEFAULT_NAMESPACE, PLAN, _manifest(STORY))

    story_commit.commit_story(PLAN, STORY, {"status": "reviewed"})

    assert records.get_manifest(DEFAULT_NAMESPACE, PLAN)["stories"][STORY] == {
        "status": "reviewed"
    }


def test_bound_commit_without_a_bound_manifest_raises(
    records: InMemoryRecordStore, store: _RecordStore, plan_lock: None
) -> None:
    """A manifest that exists only in the default partition is not used."""
    records.put_manifest(DEFAULT_NAMESPACE, PLAN, _manifest(STORY))

    with (
        bind_namespace(NAMESPACE),
        pytest.raises(FileNotFoundError, match="manifest not found"),
    ):
        story_commit.commit_story(PLAN, STORY, {"status": "reviewed"})

    assert records.get_manifest(DEFAULT_NAMESPACE, PLAN)["stories"][STORY] == {
        "status": "agent_done"
    }


def test_bound_commit_of_an_absent_story_writes_nothing(
    records: InMemoryRecordStore, store: _RecordStore, plan_lock: None
) -> None:
    """A story the bound manifest does not hold is not written anywhere."""
    records.put_manifest(NAMESPACE, PLAN, _manifest())
    records.put_manifest(DEFAULT_NAMESPACE, PLAN, _manifest(STORY))

    with bind_namespace(NAMESPACE):
        story_commit.commit_story(PLAN, STORY, {"status": "reviewed"})

    assert records.get_manifest(NAMESPACE, PLAN)["stories"] == {}
    assert records.get_manifest(DEFAULT_NAMESPACE, PLAN)["stories"][STORY] == {
        "status": "agent_done"
    }


# -- the merge namespace guard ------------------------------------------------


def test_bound_approve_on_the_file_store_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The file layout holds only the default partition: the request is refused."""
    monkeypatch.setattr(pipeline.server, "_store", _FileStoreStub())
    calls = _deny(monkeypatch)

    with bind_namespace(NAMESPACE):
        result = merge._approve_merge_impl(PLAN, STORY)

    assert result == REFUSAL
    assert calls == []


def test_bound_approve_on_a_record_store_proceeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A record store holds the bound namespace: the request is not refused."""
    monkeypatch.setattr(pipeline.server, "_store", _RecordStore(InMemoryRecordStore()))
    _deny(monkeypatch)

    with bind_namespace(NAMESPACE):
        result = merge._approve_merge_impl(PLAN, STORY)

    assert result == {"ok": False, "error": "denied"}


def test_bound_pause_on_the_file_store_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The file layout holds only the default partition: the request is refused."""
    monkeypatch.setattr(pipeline.server, "_store", _FileStoreStub())
    calls = _deny(monkeypatch)

    with bind_namespace(NAMESPACE):
        result = merge._set_plan_paused(PLAN, True)

    assert result == REFUSAL
    assert calls == []


def test_bound_pause_on_a_record_store_proceeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A record store holds the bound namespace: the request is not refused."""
    monkeypatch.setattr(pipeline.server, "_store", _RecordStore(InMemoryRecordStore()))
    _deny(monkeypatch)

    with bind_namespace(NAMESPACE):
        result = merge._set_plan_paused(PLAN, True)

    assert result == {"ok": False, "error": "denied"}


def test_unbound_pause_is_not_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """With nothing bound the request is never refused as an unknown namespace."""
    monkeypatch.setattr(pipeline.server, "_store", _FileStoreStub())
    _deny(monkeypatch)

    result = merge._set_plan_paused(PLAN, True)

    assert result == {"ok": False, "error": "denied"}


# -- the merge manifest selection ---------------------------------------------


def test_bound_pause_reads_the_bound_partition(
    records: InMemoryRecordStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manifest that exists only in the default partition is not used."""
    monkeypatch.setattr(pipeline.server, "_store", _RecordStore(records))
    _allow(monkeypatch)
    records.put_manifest(DEFAULT_NAMESPACE, PLAN, _manifest(STORY))

    with bind_namespace(NAMESPACE):
        result = merge._set_plan_paused(PLAN, True)

    assert result["ok"] is False
    assert "manifest" in result["error"].lower()
    assert "paused" not in records.get_manifest(DEFAULT_NAMESPACE, PLAN)


def test_bound_pause_writes_the_bound_partition(
    records: InMemoryRecordStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``a/b`` bound the pause is recorded in that partition only."""
    monkeypatch.setattr(pipeline.server, "_store", _RecordStore(records))
    _allow(monkeypatch)
    records.put_manifest(NAMESPACE, PLAN, _manifest(STORY))
    records.put_manifest(DEFAULT_NAMESPACE, PLAN, _manifest(STORY))

    with bind_namespace(NAMESPACE):
        result = merge._set_plan_paused(PLAN, True)

    assert result["ok"] is True
    assert records.get_manifest(NAMESPACE, PLAN)["paused"] is True
    assert "paused" not in records.get_manifest(DEFAULT_NAMESPACE, PLAN)
