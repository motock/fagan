import json

import pytest

from pipeline.cross_plan import (
    ACTIVE_STATUSES,
    blocked_by_gaps,
    file_overlaps,
    in_flight_stories,
    load_manifests,
)


def _write(plan_dir, name, stories):
    (plan_dir / f"{name}.manifest.json").write_text(json.dumps({"stories": stories}))


def test_active_statuses_are_the_states_that_may_still_push():
    assert ACTIVE_STATUSES == {
        "in_progress", "interrupted", "changes_requested", "tests_passed", "pr_open",
    }


def test_load_manifests_reads_every_plan_keyed_by_name(tmp_path):
    _write(tmp_path, "a", {})
    _write(tmp_path, "b", {})
    assert set(load_manifests(tmp_path)) == {"a", "b"}


def test_load_manifests_honours_exclude_plan(tmp_path):
    _write(tmp_path, "a", {})
    _write(tmp_path, "b", {})
    assert set(load_manifests(tmp_path, exclude_plan="a")) == {"b"}


def test_load_manifests_skips_malformed_json_and_keeps_others(tmp_path, caplog):
    _write(tmp_path, "good", {})
    (tmp_path / "bad.manifest.json").write_text("{not json")
    with caplog.at_level("WARNING", logger="pipeline"):
        result = load_manifests(tmp_path)
    assert set(result) == {"good"}
    assert any("bad" in r.getMessage() or "bad" in str(r.__dict__) for r in caplog.records)


def test_load_manifests_skips_json_list(tmp_path):
    (tmp_path / "lst.manifest.json").write_text("[1, 2]")
    _write(tmp_path, "good", {})
    assert set(load_manifests(tmp_path)) == {"good"}


def test_load_manifests_empty_dir(tmp_path):
    assert load_manifests(tmp_path) == {}


@pytest.mark.parametrize("status", sorted(ACTIVE_STATUSES))
def test_in_flight_includes_active_status(status):
    m = {"p": {"stories": {"k": {"status": status, "files": ["a.py"]}}}}
    assert in_flight_stories(m) == [{"plan": "p", "key": "k", "files": ["a.py"]}]


@pytest.mark.parametrize("status", ["done", "merged", "parked", "failed", "todo"])
def test_in_flight_excludes_inactive_status(status):
    m = {"p": {"stories": {"k": {"status": status, "files": ["a.py"]}}}}
    assert in_flight_stories(m) == []


def test_in_flight_missing_files_becomes_empty():
    m = {"p": {"stories": {"k": {"status": "in_progress"}}}}
    assert in_flight_stories(m)[0]["files"] == []


def test_in_flight_non_list_files_becomes_empty():
    m = {"p": {"stories": {"k": {"status": "in_progress", "files": "a.py"}}}}
    assert in_flight_stories(m)[0]["files"] == []


def test_in_flight_drops_non_string_entries():
    m = {"p": {"stories": {"k": {"status": "in_progress", "files": ["a.py", 3, None]}}}}
    assert in_flight_stories(m)[0]["files"] == ["a.py"]


def test_overlaps_returns_story_sharing_a_path():
    others = [{"plan": "p", "key": "k", "files": ["a.py", "b.py"]}]
    assert file_overlaps(["b.py"], others) == others


def test_overlaps_empty_when_no_shared_path():
    assert file_overlaps(["x.py"], [{"plan": "p", "key": "k", "files": ["a.py"]}]) == []


def test_overlaps_empty_candidate_files_skips_others():
    assert file_overlaps([], object()) == []


def test_overlaps_ignores_story_declaring_no_files():
    assert file_overlaps(["a.py"], [{"plan": "p", "key": "k", "files": []}]) == []


def test_overlaps_own_plan_excluded_by_caller_filtering():
    others = [
        {"plan": "me", "key": "k1", "files": ["a.py"]},
        {"plan": "you", "key": "k2", "files": ["a.py"]},
    ]
    mine = [o for o in others if o["plan"] != "me"]
    assert [o["key"] for o in file_overlaps(["a.py"], mine)] == ["k2"]


def _ms(status):
    return {"other": {"stories": {"k": {"status": status}}}}


@pytest.mark.parametrize("status", ["done", "merged"])
def test_blocked_by_satisfied(status):
    assert blocked_by_gaps({"blocked_by": ["other:k"]}, _ms(status)) == []


@pytest.mark.parametrize("status", ["todo", "in_progress"])
def test_blocked_by_unsatisfied_status_is_a_gap(status):
    assert blocked_by_gaps({"blocked_by": ["other:k"]}, _ms(status)) == ["other:k"]


def test_blocked_by_unknown_plan_is_a_gap():
    assert blocked_by_gaps({"blocked_by": ["nope:k"]}, _ms("done")) == ["nope:k"]


def test_blocked_by_unknown_key_is_a_gap():
    assert blocked_by_gaps({"blocked_by": ["other:zzz"]}, _ms("done")) == ["other:zzz"]


def test_blocked_by_malformed_entry_is_a_gap():
    assert blocked_by_gaps({"blocked_by": ["no-colon"]}, _ms("done")) == ["no-colon"]


def test_blocked_by_non_string_entry_is_a_gap():
    assert blocked_by_gaps({"blocked_by": [5]}, _ms("done")) == [5]


def test_blocked_by_splits_on_first_colon():
    m = {"p": {"stories": {"a:b": {"status": "done"}}}}
    assert blocked_by_gaps({"blocked_by": ["p:a:b"]}, m) == []


def test_blocked_by_self_reference_is_a_gap_while_unfinished():
    m = {"p": {"stories": {"k": {"status": "todo"}}}}
    assert blocked_by_gaps({"blocked_by": ["p:k"]}, m) == ["p:k"]


@pytest.mark.parametrize("story", [{}, {"blocked_by": []}, {"blocked_by": None}])
def test_blocked_by_empty_or_absent(story):
    assert blocked_by_gaps(story, _ms("todo")) == []

