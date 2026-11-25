"""Precedent: recent paired human answers to the same park class, read per namespace."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import pipeline.server  # noqa: F401  (pre-existing import cycle: load server first)
from pipeline import overlord_precedent as precedent
from pipeline import server
from pipeline import store as store_module
from pipeline.actor import bind_namespace
from pipeline.store import FileStore
from pipeline.store_memory import InMemoryRecordStore
from pipeline.triage_outcomes import HIGH_RISK_MERGE_CLASS

PLAN = "PREC-PLAN"
CLASS = "scope_gate"


def _ruling(story, action, decided_at, park_class=CLASS, **extra):
    return {
        "question": "failure triage",
        "story_key": story,
        "action": action,
        "park_class": park_class,
        "decided_at": decided_at,
        **extra,
    }


def _merge_ruling(story, ruling, decided_at):
    return {
        "question": "high-risk merge adjudication",
        "story_key": story,
        "ruling": ruling,
        "decided_at": decided_at,
    }


def _answer(story, ruling_ref, human_action, rationale="", **extra):
    return {
        "question": "human resolution",
        "story_key": story,
        "ruling_ref": ruling_ref,
        "human_action": human_action,
        "rationale": rationale,
        **extra,
    }


@pytest.fixture(autouse=True)
def _fresh_cache():
    precedent.clear_cache()
    yield
    precedent.clear_cache()


@pytest.fixture
def store(monkeypatch):
    memory = InMemoryRecordStore()
    monkeypatch.setattr(server, "_store", SimpleNamespace(records=lambda: memory))
    return memory


def _seed(store, namespace, plan, records):
    store.put_manifest(namespace, plan, {"stories": {}})
    for record in records:
        store.append_decision(namespace, plan, record)


# ---------------------------------------------------------------------------
# pairing a ruling with the human resolution that answers it
# ---------------------------------------------------------------------------


def test_should_pair_a_ruling_with_the_resolution_that_names_its_decided_at():
    records = [_ruling("S-1", "park_for_human", "t1"), _answer("S-1", "t1", "park")]
    pairs = precedent.pair_rulings(PLAN, records)
    assert [(p["story_key"], p["park_class"], p["overlord_action"], p["human_action"]) for p in pairs] == [
        ("S-1", CLASS, "park_for_human", "park"),
    ]


def test_should_skip_a_ruling_that_no_resolution_answers():
    assert precedent.pair_rulings(PLAN, [_ruling("S-1", "park_for_human", "t1")]) == []


def test_should_not_pair_a_resolution_for_another_story_with_the_same_ref():
    records = [_ruling("S-1", "park_for_human", "t1"), _answer("S-2", "t1", "park")]
    assert precedent.pair_rulings(PLAN, records) == []


def test_should_skip_a_failed_open_ruling_because_no_overlord_ruled():
    records = [_ruling("S-1", "park_for_human", "t1", failed_open=True), _answer("S-1", "t1", "retry")]
    assert precedent.pair_rulings(PLAN, records) == []


def test_should_pair_a_high_risk_merge_proceed_under_the_high_risk_class():
    records = [_merge_ruling("S-1", "proceed", "t1"), _answer("S-1", "t1", "retry")]
    (pair,) = precedent.pair_rulings(PLAN, records)
    assert pair["park_class"] == HIGH_RISK_MERGE_CLASS
    assert pair["overlord_action"] == "proceed"
    assert pair["agree"] is False


def test_should_skip_an_unparseable_merge_adjudication_that_ruled_nothing():
    records = [_merge_ruling("S-1", "", "t1"), _answer("S-1", "t1", "merge_held")]
    assert precedent.pair_rulings(PLAN, records) == []


def test_should_truncate_the_rationale_to_500_characters():
    records = [_ruling("S-1", "park_for_human", "t1"), _answer("S-1", "t1", "park", "x" * 900)]
    (pair,) = precedent.pair_rulings(PLAN, records)
    assert len(pair["rationale"]) == 500


# ---------------------------------------------------------------------------
# agreement: the same rule readiness scores with
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "human", "extra", "agree"),
    [
        ("park_for_human", "park", {}, True),
        ("park_for_human", "rebrief", {}, True),
        ("park_for_human", "amend_scope", {}, True),
        ("park_for_human", "retry", {}, False),
        ("park_for_human", "other", {}, False),
        ("rebrief", "park", {}, False),
        ("rebrief", "rebrief", {}, True),
        ("retry_transient", "retry", {}, True),
    ],
)
def test_should_judge_agreement_as_readiness_does(action, human, extra, agree):
    records = [_ruling("S-1", action, "t1"), _answer("S-1", "t1", human, **extra)]
    (pair,) = precedent.pair_rulings(PLAN, records)
    assert pair["agree"] is agree


def test_should_count_a_park_answered_with_files_added_as_agreement():
    records = [_ruling("S-1", "park_for_human", "t1"), _answer("S-1", "t1", "retry", files_added=["a.py"])]
    (pair,) = precedent.pair_rulings(PLAN, records)
    assert pair["agree"] is True


@pytest.mark.parametrize(
    ("ruling", "human", "agree"),
    [("proceed", "merge_held", True), ("proceed", "retry", False), ("park", "merge_held", False), ("park", "retry", True)],
)
def test_should_judge_a_merge_adjudication_as_readiness_does(ruling, human, agree):
    records = [_merge_ruling("S-1", ruling, "t1"), _answer("S-1", "t1", human)]
    (pair,) = precedent.pair_rulings(PLAN, records)
    assert pair["agree"] is agree


# ---------------------------------------------------------------------------
# namespace: only the bound namespace's plans are read
# ---------------------------------------------------------------------------


def test_should_return_only_paired_rulings_of_the_requested_class(store):
    _seed(
        store,
        "tenant-a",
        PLAN,
        [
            _ruling("S-1", "park_for_human", "t1"),
            _answer("S-1", "t1", "retry"),
            _ruling("S-2", "park_for_human", "t2", park_class="unknown"),
            _answer("S-2", "t2", "retry"),
            _ruling("S-3", "park_for_human", "t3"),
        ],
    )
    with bind_namespace("tenant-a"):
        entries = precedent.precedent_entries(CLASS, 8)
    assert [e["story_key"] for e in entries] == ["S-1"]


def test_should_read_only_the_bound_namespace(store):
    _seed(store, "tenant-a", "A-PLAN", [_ruling("S-1", "park_for_human", "t1"), _answer("S-1", "t1", "retry")])
    _seed(store, "tenant-b", "B-PLAN", [_ruling("S-9", "park_for_human", "t9"), _answer("S-9", "t9", "retry")])
    with bind_namespace("tenant-a"):
        entries = precedent.precedent_entries(CLASS, 8)
    assert {(e["plan"], e["story_key"]) for e in entries} == {("A-PLAN", "S-1")}


def test_should_return_no_entries_when_nothing_is_paired(store):
    _seed(store, "tenant-a", PLAN, [_ruling("S-1", "park_for_human", "t1")])
    with bind_namespace("tenant-a"):
        assert precedent.precedent_entries(CLASS, 8) == []


def test_should_read_nothing_on_the_file_layout_outside_the_default_namespace(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(server, "_store", FileStore())
    (tmp_path / f"{PLAN}.manifest.json").write_text("{}")
    with bind_namespace("tenant-a"):
        assert precedent.precedent_entries(CLASS, 8) == []


# ---------------------------------------------------------------------------
# order and limit
# ---------------------------------------------------------------------------


def test_should_list_disagreements_first_and_keep_newest_first_within_each(store):
    _seed(
        store,
        "tenant-a",
        PLAN,
        [
            _ruling("OLD", "park_for_human", "t1"),
            _answer("OLD", "t1", "park"),
            _ruling("MID", "park_for_human", "t2"),
            _answer("MID", "t2", "retry"),
            _ruling("NEW", "park_for_human", "t3"),
            _answer("NEW", "t3", "park"),
            _ruling("NEWEST-DISAGREE", "park_for_human", "t4"),
            _answer("NEWEST-DISAGREE", "t4", "retry"),
        ],
    )
    with bind_namespace("tenant-a"):
        entries = precedent.precedent_entries(CLASS, 8)
    assert [e["story_key"] for e in entries] == ["NEWEST-DISAGREE", "MID", "NEW", "OLD"]
    assert [e["agree"] for e in entries] == [False, False, True, True]


def test_should_return_at_most_limit_of_the_most_recent_paired_rulings(store):
    records = []
    for index in range(1, 11):
        records += [_ruling(f"S-{index}", "park_for_human", f"2026-11-{index:02d}"), _answer(f"S-{index}", f"2026-11-{index:02d}", "park")]
    _seed(store, "tenant-a", PLAN, records)
    with bind_namespace("tenant-a"):
        entries = precedent.precedent_entries(CLASS, 3)
    assert [e["story_key"] for e in entries] == ["S-10", "S-9", "S-8"]


def test_should_read_nothing_when_the_limit_is_zero(monkeypatch):
    def unreachable():
        raise AssertionError("a zero limit must not read the store")

    monkeypatch.setattr(server, "_store", SimpleNamespace(records=unreachable))
    assert precedent.precedent_block(CLASS, 0) == ("", None)


# ---------------------------------------------------------------------------
# bounded cost: one namespace read per cache window, not one per ruling
# ---------------------------------------------------------------------------


def test_should_reuse_one_namespace_read_within_the_cache_window(store, monkeypatch):
    _seed(store, "tenant-a", PLAN, [_ruling("S-1", "park_for_human", "t1"), _answer("S-1", "t1", "retry")])
    reads = []
    real = store.get_decisions

    def counting(namespace, plan_name):
        reads.append(plan_name)
        return real(namespace, plan_name)

    monkeypatch.setattr(store, "get_decisions", counting)
    with bind_namespace("tenant-a"):
        precedent.precedent_entries(CLASS, 8)
        precedent.precedent_entries("unknown", 8)
    assert reads == [PLAN]


def test_should_read_again_once_the_cache_window_has_passed(store, monkeypatch):
    _seed(store, "tenant-a", PLAN, [_ruling("S-1", "park_for_human", "t1"), _answer("S-1", "t1", "retry")])
    clock = {"now": 1000.0}
    monkeypatch.setattr(precedent, "_clock", lambda: clock["now"])
    with bind_namespace("tenant-a"):
        assert len(precedent.precedent_entries(CLASS, 8)) == 1
        store.append_decision("tenant-a", PLAN, _ruling("S-2", "park_for_human", "t2"))
        store.append_decision("tenant-a", PLAN, _answer("S-2", "t2", "retry"))
        assert len(precedent.precedent_entries(CLASS, 8)) == 1
        clock["now"] += precedent.PRECEDENT_CACHE_SECONDS + 1
        assert len(precedent.precedent_entries(CLASS, 8)) == 2


def test_should_not_serve_a_cached_read_taken_from_a_different_store(monkeypatch):
    first = InMemoryRecordStore()
    _seed(first, "tenant-a", PLAN, [_ruling("S-1", "park_for_human", "t1"), _answer("S-1", "t1", "retry")])
    monkeypatch.setattr(server, "_store", SimpleNamespace(records=lambda: first))
    with bind_namespace("tenant-a"):
        assert [e["story_key"] for e in precedent.precedent_entries(CLASS, 8)] == ["S-1"]
    second = InMemoryRecordStore()
    _seed(second, "tenant-a", PLAN, [_ruling("S-2", "park_for_human", "t2"), _answer("S-2", "t2", "retry")])
    monkeypatch.setattr(server, "_store", SimpleNamespace(records=lambda: second))
    with bind_namespace("tenant-a"):
        assert [e["story_key"] for e in precedent.precedent_entries(CLASS, 8)] == ["S-2"]


# ---------------------------------------------------------------------------
# rendering: labelled as guidance, one line per entry
# ---------------------------------------------------------------------------


def test_should_render_guidance_label_and_one_line_per_entry():
    entries = [
        {
            "plan": PLAN,
            "story_key": "S-1",
            "park_class": CLASS,
            "overlord_action": "park_for_human",
            "human_action": "retry",
            "agree": False,
            "rationale": "the gate\nwas wrong",
        }
    ]
    text = precedent.render_precedent(entries)
    assert "guidance" in text.lower()
    assert "the policy and the authority matrix" in text
    assert f"plan {PLAN} story S-1" in text
    assert "overlord park_for_human" in text and "human retry" in text
    assert "disagree" in text
    assert "the gate was wrong" in text
    assert len([line for line in text.splitlines() if line.startswith("- ")]) == 1
