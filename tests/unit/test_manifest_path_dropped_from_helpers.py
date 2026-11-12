"""The no-progress, plan-conflict and review-deferral helpers take no
``manifest_path``: every caller reads and writes the plan through the record
store, so the path is dead weight (ADR 0012 stage C)."""

import inspect

import pytest

from pipeline import no_progress
from pipeline import plan_conflict_ruling as pcr
from pipeline import review_deferral as rd


def _params(fn):
    return list(inspect.signature(fn).parameters)


def test_should_drop_manifest_path_from_classify_no_progress_signature():
    assert _params(no_progress.classify_no_progress) == [
        "plan_name", "story_key", "story", "worktree", "manifest",
    ]


def test_should_drop_manifest_path_from_wait_for_capacity_signature():
    assert _params(no_progress._wait_for_capacity) == [
        "plan_name", "story_key", "story", "manifest",
    ]


def test_should_drop_manifest_path_from_retry_transport_signature():
    assert _params(no_progress._retry_transport) == [
        "plan_name", "story_key", "story", "manifest",
    ]


def test_should_drop_manifest_path_from_plan_conflict_intercept_signature():
    assert _params(pcr._plan_conflict_intercept) == [
        "plan_name", "story_key", "story", "test_result", "worktree", "manifest", "pid",
    ]


def test_should_drop_manifest_path_from_park_rate_limited_review_signature():
    assert _params(rd.park_rate_limited_review) == [
        "plan_name", "story_key", "story", "manifest", "cid_kwargs",
    ]


def test_should_reject_old_classify_no_progress_call_with_manifest_path():
    old_args = ("plan", "S1", {}, "/wt", {}, "/plans/plan.manifest.json")
    with pytest.raises(TypeError):
        inspect.signature(no_progress.classify_no_progress).bind(*old_args)


def test_should_reject_old_plan_conflict_intercept_call_with_manifest_path():
    old_args = ("plan", "S1", {}, object(), "/wt", {}, "/plans/plan.manifest.json", 4242)
    with pytest.raises(TypeError):
        inspect.signature(pcr._plan_conflict_intercept).bind(*old_args)


def test_should_reject_old_park_rate_limited_review_call_with_manifest_path():
    old_args = ("plan", "S1", {}, {}, "/plans/plan.manifest.json")
    with pytest.raises(TypeError):
        inspect.signature(rd.park_rate_limited_review).bind(*old_args)


def test_should_accept_the_new_classify_no_progress_call_shape():
    inspect.signature(no_progress.classify_no_progress).bind("plan", "S1", {}, "/wt", {})
