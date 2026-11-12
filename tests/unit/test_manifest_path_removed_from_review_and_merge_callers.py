"""The review-collection and merge re-verify callers no longer take a manifest path.

ADR 0012 stage C removes ``manifest_path`` from the Store Protocol. Every caller
reads and writes the plan through ``records()`` instead, so the path parameter
threaded through the review and merge helpers is vestigial and must go. These
tests fail while the parameter exists and pin the new signatures.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from pipeline import advance_merge, advance_review

_REPO_ROOT = Path(__file__).resolve().parents[2]

_SCANNED_FILES = (
    "pipeline/advance.py",
    "pipeline/advance_review.py",
    "pipeline/advance_merge.py",
    "pipeline/review_orchestrator.py",
    "pipeline/story_status.py",
)


def _params(func) -> list[str]:
    return list(inspect.signature(func).parameters)


class TestSignaturesDropManifestPath:
    def test_should_take_plan_story_story_and_summary_in_review_or_collect(self):
        assert _params(advance_review.review_or_collect) == [
            "plan_name",
            "story_key",
            "story",
            "summary",
        ]

    def test_should_take_plan_story_story_and_summary_in_start(self):
        assert _params(advance_review._start) == [
            "plan_name",
            "story_key",
            "story",
            "summary",
        ]

    def test_should_take_plan_story_story_summary_and_result_in_apply(self):
        assert _params(advance_review._apply) == [
            "plan_name",
            "story_key",
            "story",
            "summary",
            "result",
        ]

    def test_should_take_plan_and_story_key_in_drop_job(self):
        assert _params(advance_review._drop_job) == ["plan_name", "story_key"]

    def test_should_take_plan_story_key_and_mutate_in_persist(self):
        assert _params(advance_review._persist) == [
            "plan_name",
            "story_key",
            "mutate",
        ]

    def test_should_take_no_manifest_path_in_merge_reverify_gate(self):
        assert _params(advance_merge._merge_reverify_gate) == [
            "plan_name",
            "key",
            "story",
            "worktree",
            "pushed_sha",
            "summary",
        ]


class TestOldManifestPathArgumentIsRejected:
    def test_should_raise_type_error_when_review_or_collect_gets_a_manifest_path(
        self,
    ):
        signature = inspect.signature(advance_review.review_or_collect)

        with pytest.raises(TypeError):
            signature.bind("plan", "s1", {}, Path("plan.manifest.json"), {})

    def test_should_raise_type_error_when_persist_gets_a_manifest_path(self):
        signature = inspect.signature(advance_review._persist)

        with pytest.raises(TypeError):
            signature.bind("plan", Path("plan.manifest.json"), "s1", lambda s: None)

    def test_should_raise_type_error_when_merge_reverify_gate_gets_a_manifest_path(
        self,
    ):
        signature = inspect.signature(advance_merge._merge_reverify_gate)

        with pytest.raises(TypeError):
            signature.bind(
                "plan",
                "s1",
                {},
                "worktree",
                "abc123",
                Path("plan.manifest.json"),
                {},
            )


class TestSourceNoLongerNamesManifestPath:
    @pytest.mark.parametrize("relative_path", _SCANNED_FILES)
    def test_should_not_mention_manifest_path_in_production_source(
        self, relative_path
    ):
        source = (_REPO_ROOT / relative_path).read_text(encoding="utf-8")

        assert "manifest_path" not in source
