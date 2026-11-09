"""Wedge-scan journal activity must be measured through the record store.

The journal half of ``collect_story_wedge_signals`` used to build
``<plan>.<story>.journal.json`` from ``PLAN_DIR`` and ``stat`` it, globbing the
plans directory for a key-less dispatched story.  ADR 0012 stage B moves
journal knowledge into the record store, so ``pipeline/wedge_io.py`` must ask
``get_journal_updated_at`` / ``list_journal_keys`` instead of touching the
plans directory itself.

Every test pins wedge_io's clock so ``activity_age_seconds`` is an exact
number, and asserts the recorded ``updated_at`` -- never ``now`` -- is the
activity source: a read must not bump a journal, or a stalled wedge would be
silently un-flagged on the next tick.
"""

from __future__ import annotations

import pytest

from pipeline import server as p
from pipeline import wedge_io

PLAN = "p"
NOW = 1000.0


class _FakeStore:
    """Journal-capable store stand-in (the ADR 0012 stage B records view).

    ``journals`` maps ``(plan_name, journal_key)`` to the recorded activity
    time; ``listed`` is what ``list_journal_keys`` reports per plan, so a test
    can model "the journal exists but the plan does not list that key" (the
    manifest/story-key fallbacks) and "this plan lists exactly one journal".
    ``records()`` returns self so the implementation may reach either the
    store's records view (pipeline/stall_scan.py's pattern) or the methods
    directly; both are graded the same way.
    """

    def __init__(self, journals=None, listed=None):
        self.journals = dict(journals or {})
        self.listed = {plan: list(keys) for plan, keys in (listed or {}).items()}
        self.calls: list[tuple] = []

    def records(self):
        return self

    def list_journal_keys(self, namespace, plan_name):
        self.calls.append(("list_journal_keys", namespace, plan_name))
        return list(self.listed.get(plan_name, []))

    def get_journal_updated_at(self, namespace, plan_name, journal_key):
        self.calls.append(
            ("get_journal_updated_at", namespace, plan_name, journal_key)
        )
        return self.journals.get((plan_name, journal_key))


class _Clock:
    """Stand-in for wedge_io's ``time`` module with a settable now."""

    def __init__(self, now=NOW):
        self.now = now

    def time(self):
        return self.now

    def monotonic(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    """Pin wedge_io's clock and skip the liveness probe's subprocess."""
    clock = _Clock()
    monkeypatch.setattr(wedge_io, "time", clock)
    monkeypatch.setattr(wedge_io, "_pid_is_alive", lambda pid: True)
    return clock


def _install(monkeypatch, journals=None, listed=None):
    store = _FakeStore(journals, listed)
    monkeypatch.setattr(p, "_store", store)
    return store


def _age(plan_name, story_key, story):
    """The ``activity_age_seconds`` the wedge scan measures for one story."""
    signals = wedge_io.collect_story_wedge_signals(plan_name, story_key, story)
    return signals["activity_age_seconds"]


def test_should_use_the_manifest_key_journal_time(clock, monkeypatch):
    """The manifest key wins over ``story["key"]`` and over the plan's list."""
    store = _install(
        monkeypatch,
        {("p", "k1"): 100.0, ("p", "k2"): 200.0, ("p", "k3"): 300.0},
        {"p": ["k3"]},
    )
    story = {"key": "k2", "pid": 7}

    assert _age(PLAN, "k1", story) == pytest.approx(NOW - 100.0)
    # story-dict-first would have measured 200, list-first 300.
    assert store.calls == [("get_journal_updated_at", "default", PLAN, "k1")]


def test_should_fall_back_to_the_story_dict_key(clock, monkeypatch):
    """No manifest key: ``story["key"]`` is consulted before the plan's list."""
    store = _install(
        monkeypatch,
        {("p", "k2"): 200.0, ("p", "k3"): 300.0},
        {"p": ["k3"]},
    )
    story = {"key": "k2", "pid": 7}

    assert _age(PLAN, "", story) == pytest.approx(NOW - 200.0)
    assert ("get_journal_updated_at", "default", PLAN, "k2") in store.calls
    assert not any(call[0] == "list_journal_keys" for call in store.calls)


def test_should_use_the_only_journal_of_the_plan_for_a_keyless_dispatched_story(
    clock, monkeypatch
):
    """One journal in the plan and an int pid: that journal is the story's."""
    store = _install(monkeypatch, {("p", "s1"): 50.0}, {"p": ["s1"]})
    story = {"pid": 7}

    assert _age(PLAN, None, story) == pytest.approx(NOW - 50.0)

    # A read must not bump the journal: with a later clock the activity time
    # is still 50, so the age grows by exactly the tick, not back to ~0.
    clock.now = NOW + 1.0
    assert _age(PLAN, None, story) == pytest.approx(NOW + 1.0 - 50.0)
    assert store.journals == {("p", "s1"): 50.0}
    assert [call[0] for call in store.calls] == [
        "list_journal_keys",
        "get_journal_updated_at",
        "list_journal_keys",
        "get_journal_updated_at",
    ]


def test_should_ignore_journals_when_the_plan_has_several_and_the_story_has_no_key(
    clock, monkeypatch
):
    """Several journals and no key: none can be attributed, so no signal."""
    store = _install(
        monkeypatch, {("p", "a"): 10.0, ("p", "b"): 20.0}, {"p": ["a", "b"]}
    )
    story = {"pid": 7}

    assert _age(PLAN, None, story) is None
    # keys[0] would have measured the "a" journal (age NOW - 10).
    assert not any(call[0] == "get_journal_updated_at" for call in store.calls)


@pytest.mark.parametrize("pid", [True, "12", None])
def test_should_ignore_journals_for_a_keyless_story_without_an_int_pid(
    clock, monkeypatch, pid
):
    """Never dispatched: an int pid is required before listing journals.

    ``True`` is an ``int`` subclass, so a bare ``isinstance(pid, int)`` would
    accept it and hand back the plan's single journal time.
    """
    store = _install(monkeypatch, {("p", "s1"): 50.0}, {"p": ["s1"]})
    story = {"pid": pid}

    assert _age(PLAN, None, story) is None
    assert store.calls == []


def test_should_not_count_another_plan_whose_name_extends_this_one(
    plan_dir, clock, monkeypatch
):
    """A journal of plan ``p.x`` is not a journal of plan ``p``.

    The decoy file is real, so a plans-directory glob (``p.*.journal.json``)
    would match ``p.x.s1.journal.json`` and attribute its activity to ``p``.
    """
    decoy = plan_dir / "p.x.s1.journal.json"
    decoy.write_text("[]", encoding="utf-8")

    store = _install(monkeypatch, {("p.x", "s1"): 70.0}, {"p": [], "p.x": ["s1"]})
    story = {"pid": 7}

    assert _age(PLAN, None, story) is None
    assert not any(call[0] == "get_journal_updated_at" for call in store.calls)
    assert decoy.exists()
