"""TDD tests for ``pipeline.plan_conflict`` (LD90 W1b: plan-conflict classifier).

A "plan conflict" is a red suite whose failing test files are ALL pre-existing
at the merge base and untouched by the branch: the branch cannot have broken
them, so the failures belong to the plan's rework path, not to this story.
Any ambiguous input (empty failure list, a failing file the branch changed, a
failing file that does not exist at base) yields ``None`` -- "not a conflict"
-- so the existing rework path stays the default.

The pure classifier tests use synthetic sets; the git-adapter tests mock
``subprocess.run`` inside ``pipeline.plan_conflict`` (git is the external
boundary) with canned ``CompletedProcess`` objects keyed on argv.
"""

import subprocess
import typing

# pipeline.server is imported explicitly; import order is not required for
# cold-importability (every pipeline module imports cold, see
# tests/unit/test_hub_satellite_cold_imports.py).
import pipeline.server
from pipeline import plan_conflict as plan_conflict_mod

# Sanity check that the server module resolved.
assert pipeline.server.__name__ == "pipeline.server"

# ---------------------------------------------------------------------------
# failing_test_files
# ---------------------------------------------------------------------------


class TestFailingTestFiles:
    def test_empty_list_gives_empty_list(self):
        assert plan_conflict_mod.failing_test_files([]) == []

    def test_plain_node_id_maps_to_file_part(self):
        node = "tests/unit/test_x.py::TestA::test_y"
        assert plan_conflict_mod.failing_test_files([node]) == [
            "tests/unit/test_x.py"
        ]

    def test_parametrized_node_id_maps_to_file_part(self):
        node = "tests/unit/test_x.py::TestA::test_y[param-1]"
        assert plan_conflict_mod.failing_test_files([node]) == [
            "tests/unit/test_x.py"
        ]

    def test_duplicates_collapse_first_seen_order_kept(self):
        nodes = [
            "tests/unit/test_b.py::TestB::test_2",
            "tests/unit/test_a.py::TestA::test_1",
            "tests/unit/test_b.py::TestB::test_3[case]",
            "tests/unit/test_a.py::TestA::test_1",
        ]
        assert plan_conflict_mod.failing_test_files(nodes) == [
            "tests/unit/test_b.py",
            "tests/unit/test_a.py",
        ]

    def test_empty_string_entries_are_ignored(self):
        nodes = ["", "tests/unit/test_x.py::TestA::test_y", ""]
        assert plan_conflict_mod.failing_test_files(nodes) == [
            "tests/unit/test_x.py"
        ]

    def test_non_py_file_part_is_ignored(self):
        nodes = [
            "README.md::section",
            "tests/unit/test_x.py::TestA::test_y",
            "notes.txt",
        ]
        assert plan_conflict_mod.failing_test_files(nodes) == [
            "tests/unit/test_x.py"
        ]

    def test_id_without_colon_maps_to_itself_if_py(self):
        assert plan_conflict_mod.failing_test_files(
            ["tests/unit/test_x.py"]
        ) == ["tests/unit/test_x.py"]

    def test_id_without_colon_and_not_py_is_ignored(self):
        assert plan_conflict_mod.failing_test_files(["README.md"]) == []


# ---------------------------------------------------------------------------
# classify_plan_conflict (pure)
# ---------------------------------------------------------------------------


class TestClassifyPlanConflict:
    BASE: typing.ClassVar[set[str]] = {"tests/unit/test_a.py", "tests/unit/test_b.py"}
    CHANGED: typing.ClassVar[set[str]] = set()

    def test_all_failing_at_base_and_untouched_returns_sorted(self):
        result = plan_conflict_mod.classify_plan_conflict(
            ["tests/unit/test_b.py", "tests/unit/test_a.py"],
            self.BASE,
            self.CHANGED,
        )
        assert result == ["tests/unit/test_a.py", "tests/unit/test_b.py"]

    def test_empty_failing_list_returns_none(self):
        assert (
            plan_conflict_mod.classify_plan_conflict([], self.BASE, self.CHANGED)
            is None
        )

    def test_failing_file_changed_on_branch_returns_none(self):
        result = plan_conflict_mod.classify_plan_conflict(
            ["tests/unit/test_a.py"],
            self.BASE,
            {"tests/unit/test_a.py"},
        )
        assert result is None

    def test_failing_file_not_at_base_returns_none(self):
        # The story itself added this test file: its failure is the branch's
        # own, not a plan conflict.
        result = plan_conflict_mod.classify_plan_conflict(
            ["tests/unit/test_new.py"],
            self.BASE,
            self.CHANGED,
        )
        assert result is None

    def test_failing_file_both_new_and_changed_returns_none(self):
        result = plan_conflict_mod.classify_plan_conflict(
            ["tests/unit/test_new.py"],
            self.BASE,
            {"tests/unit/test_new.py"},
        )
        assert result is None

    def test_one_bad_file_poisons_the_whole_set(self):
        # Mixed: one clean file + one the branch changed -> not a conflict.
        result = plan_conflict_mod.classify_plan_conflict(
            ["tests/unit/test_a.py", "tests/unit/test_b.py"],
            self.BASE,
            {"tests/unit/test_b.py"},
        )
        assert result is None


# ---------------------------------------------------------------------------
# branch_file_sets (git adapter; subprocess.run mocked at the boundary)
# ---------------------------------------------------------------------------


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


class TestBranchFileSets:
    def test_success_parses_both_sets_ignoring_blank_lines(self, monkeypatch):
        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if argv[:3] == ["git", "ls-tree", "-r"]:
                return _completed("pipeline/advance.py\ntests/unit/test_a.py\n\n")
            return _completed("tests/unit/test_a.py\n\npipeline/triage.py\n")

        monkeypatch.setattr(plan_conflict_mod.subprocess, "run", fake_run)

        result = plan_conflict_mod.branch_file_sets("/tmp/wt", "origin/master")

        assert result == (
            {"pipeline/advance.py", "tests/unit/test_a.py"},
            {"tests/unit/test_a.py", "pipeline/triage.py"},
        )
        assert calls[0][:4] == ["git", "ls-tree", "-r", "--name-only"]
        assert calls[0][-1] == "origin/master"
        assert calls[1][:4] == ["git", "diff", "--name-only", "--no-renames"]
        assert calls[1][-1] == "origin/master..HEAD"

    def test_success_passes_required_run_kwargs(self, monkeypatch):
        seen_kwargs: list[dict] = []

        def fake_run(argv, **kwargs):
            seen_kwargs.append(kwargs)
            return _completed("")

        monkeypatch.setattr(plan_conflict_mod.subprocess, "run", fake_run)

        plan_conflict_mod.branch_file_sets("/tmp/wt", "base")

        for kwargs in seen_kwargs:
            assert kwargs["cwd"] == "/tmp/wt"
            assert kwargs["capture_output"] is True
            assert kwargs["text"] is True
            assert kwargs["check"] is False
            assert isinstance(kwargs["timeout"], (int, float))

    def test_ls_tree_nonzero_exit_returns_none(self, monkeypatch):
        def fake_run(argv, **kwargs):
            if argv[:3] == ["git", "ls-tree", "-r"]:
                return _completed("", returncode=128)
            return _completed("pipeline/advance.py\n")

        monkeypatch.setattr(plan_conflict_mod.subprocess, "run", fake_run)

        assert plan_conflict_mod.branch_file_sets("/tmp/wt", "base") is None

    def test_diff_nonzero_exit_returns_none(self, monkeypatch):
        def fake_run(argv, **kwargs):
            if argv[:3] == ["git", "ls-tree", "-r"]:
                return _completed("pipeline/advance.py\n")
            return _completed("", returncode=128)

        monkeypatch.setattr(plan_conflict_mod.subprocess, "run", fake_run)

        assert plan_conflict_mod.branch_file_sets("/tmp/wt", "base") is None

    def test_git_missing_returns_none(self, monkeypatch):
        def fake_run(argv, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", "git")

        monkeypatch.setattr(plan_conflict_mod.subprocess, "run", fake_run)

        assert plan_conflict_mod.branch_file_sets("/tmp/wt", "base") is None

    def test_timeout_returns_none(self, monkeypatch):
        def fake_run(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=30)

        monkeypatch.setattr(plan_conflict_mod.subprocess, "run", fake_run)

        assert plan_conflict_mod.branch_file_sets("/tmp/wt", "base") is None


# ---------------------------------------------------------------------------
# Integration of the two layers
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_classify_consumes_adapter_output(self):
        files_at_base = {"tests/unit/test_a.py", "tests/unit/test_b.py"}
        changed = {"pipeline/advance.py"}
        failing = plan_conflict_mod.failing_test_files(
            [
                "tests/unit/test_b.py::TestB::test_x",
                "tests/unit/test_a.py::TestA::test_y[p]",
                "tests/unit/test_b.py::TestB::test_x",
            ]
        )
        assert plan_conflict_mod.classify_plan_conflict(
            failing, files_at_base, changed
        ) == ["tests/unit/test_a.py", "tests/unit/test_b.py"]