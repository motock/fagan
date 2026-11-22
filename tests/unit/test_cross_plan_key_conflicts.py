import json

import pytest

from pipeline import cross_plan
from pipeline.cross_plan import key_conflicts, load_manifests


def _write(plan_dir, name, stories):
    (plan_dir / f"{name}.manifest.json").write_text(json.dumps({"stories": stories}))


def _manifests(tmp_path, plans):
    for name, stories in plans.items():
        _write(tmp_path, name, stories)
    return load_manifests(tmp_path)


@pytest.fixture
def worktrees(tmp_path):
    root = tmp_path / "worktrees"
    root.mkdir()
    return root


def test_key_conflicts_is_exported():
    assert "key_conflicts" in cross_plan.__all__


@pytest.mark.parametrize("status", ["todo", "in_progress", "parked", "failed"])
def test_unsatisfied_status_conflicts(tmp_path, worktrees, status):
    m = _manifests(tmp_path, {"other": {"K-1": {"status": status}}})
    assert key_conflicts(["K-1"], m, worktrees) == [
        {"key": "K-1", "plan": "other", "other_key": "K-1", "status": status}
    ]


def test_key_match_is_case_insensitive(tmp_path, worktrees):
    m = _manifests(tmp_path, {"other": {"setup-01": {"status": "todo"}}})
    assert key_conflicts(["SETUP-01"], m, worktrees) == [
        {"key": "SETUP-01", "plan": "other", "other_key": "setup-01", "status": "todo"}
    ]


@pytest.mark.parametrize("status", ["done", "merged"])
def test_satisfied_story_without_worktree_does_not_conflict(tmp_path, worktrees, status):
    m = _manifests(tmp_path, {"other": {"K-1": {"status": status}}})
    assert key_conflicts(["K-1"], m, worktrees) == []


def test_done_story_with_lingering_worktree_conflicts(tmp_path, worktrees):
    (worktrees / "K-1").mkdir()
    m = _manifests(tmp_path, {"other": {"K-1": {"status": "done"}}})
    assert key_conflicts(["K-1"], m, worktrees) == [
        {"key": "K-1", "plan": "other", "other_key": "K-1", "status": "done"}
    ]


def test_missing_status_conflicts(tmp_path, worktrees):
    m = _manifests(tmp_path, {"other": {"K-1": {}}})
    assert key_conflicts(["K-1"], m, worktrees) == [
        {"key": "K-1", "plan": "other", "other_key": "K-1", "status": None}
    ]


def test_different_key_does_not_conflict(tmp_path, worktrees):
    m = _manifests(tmp_path, {"other": {"K-2": {"status": "todo"}}})
    assert key_conflicts(["K-1"], m, worktrees) == []


def test_empty_keys_yield_no_conflicts(tmp_path, worktrees):
    m = _manifests(tmp_path, {"other": {"K-1": {"status": "todo"}}})
    assert key_conflicts([], m, worktrees) == []


def test_empty_manifests_yield_no_conflicts(worktrees):
    assert key_conflicts(["K-1"], {}, worktrees) == []


def test_non_dict_story_entry_is_skipped(worktrees):
    m = {"other": {"stories": {"K-1": "todo", "K-2": ["x"]}}}
    assert key_conflicts(["K-1", "K-2"], m, worktrees) == []


def test_manifest_without_stories_is_skipped(worktrees):
    assert key_conflicts(["K-1"], {"other": {}}, worktrees) == []


def test_manifest_with_list_stories_is_skipped(worktrees):
    assert key_conflicts(["K-1"], {"other": {"stories": [{"status": "todo"}]}}, worktrees) == []


def test_non_string_and_empty_keys_are_ignored(tmp_path, worktrees):
    m = _manifests(tmp_path, {"other": {"K-1": {"status": "todo"}}})
    assert key_conflicts([None, 5, "", "K-1"], m, worktrees) == [
        {"key": "K-1", "plan": "other", "other_key": "K-1", "status": "todo"}
    ]


def test_results_are_ordered_by_plan_then_other_key(worktrees):
    m = {
        "zeta": {"stories": {"k-1": {"status": "todo"}}},
        "alpha": {"stories": {"K-1": {"status": "todo"}, "k-1": {"status": "todo"}}},
    }
    result = key_conflicts(["K-1"], m, worktrees)
    assert [(c["plan"], c["other_key"]) for c in result] == [
        ("alpha", "K-1"),
        ("alpha", "k-1"),
        ("zeta", "k-1"),
    ]


@pytest.mark.parametrize("status", ["done", "merged"])
def test_resuming_key_with_satisfied_neighbour_and_worktree_does_not_conflict(
    tmp_path, worktrees, status,
):
    (worktrees / "K-1").mkdir()
    m = _manifests(tmp_path, {"other": {"K-1": {"status": status}}})
    assert key_conflicts(["K-1"], m, worktrees, resuming=["K-1"]) == []


@pytest.mark.parametrize("status", ["in_progress", "interrupted", "todo"])
def test_resuming_key_with_unsatisfied_neighbour_conflicts(tmp_path, worktrees, status):
    m = _manifests(tmp_path, {"other": {"K-1": {"status": status}}})
    assert key_conflicts(["K-1"], m, worktrees, resuming=["K-1"]) == [
        {"key": "K-1", "plan": "other", "other_key": "K-1", "status": status}
    ]


def test_resuming_key_with_missing_status_conflicts(tmp_path, worktrees):
    m = _manifests(tmp_path, {"other": {"K-1": {}}})
    assert key_conflicts(["K-1"], m, worktrees, resuming=["K-1"]) == [
        {"key": "K-1", "plan": "other", "other_key": "K-1", "status": None}
    ]


def test_key_not_resuming_with_done_neighbour_and_worktree_still_conflicts(
    tmp_path, worktrees,
):
    (worktrees / "K-1").mkdir()
    m = _manifests(tmp_path, {"other": {"K-1": {"status": "done"}}})
    assert key_conflicts(["K-1"], m, worktrees, resuming=["K-2"]) == [
        {"key": "K-1", "plan": "other", "other_key": "K-1", "status": "done"}
    ]


def test_resuming_matches_key_case_insensitively(tmp_path, worktrees):
    (worktrees / "setup-01").mkdir()
    m = _manifests(tmp_path, {"other": {"setup-01": {"status": "done"}}})
    assert key_conflicts(["SETUP-01"], m, worktrees, resuming=["setup-01"]) == []


def test_empty_resuming_is_identical_to_default(tmp_path, worktrees):
    (worktrees / "K-1").mkdir()
    m = _manifests(
        tmp_path,
        {"other": {"K-1": {"status": "done"}, "K-2": {"status": "todo"}}},
    )
    keys = ["K-1", "K-2"]
    assert key_conflicts(keys, m, worktrees, resuming=()) == key_conflicts(
        keys, m, worktrees
    )
