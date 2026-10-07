"""Claude spend accounting for a benchmark cell (cell_cost + harness wiring).

Two cost sources exist on disk per cell:

* review spend - app/backend_claude.py's complete() appends one JSON object per
  Claude call (carrying "total_cost_usd") to <cell>/worktrees/review_token_costs.jsonl;
* dispatch spend - ClaudeCliDriver.dispatch() mirrors every raw stream-json
  line to <worktree>/agent.log.raw, whose terminal {"type": "result", ...}
  line carries the run's "total_cost_usd". The harness's _merge_pr_stub then
  runs `git worktree remove --force`, so the raw log must be copied out to
  <worktrees>/<story_key>.agent.log.raw BEFORE the worktree is removed or every
  successful cell silently loses its dispatch cost.

Local (Ollama) dispatches write no cost lines at all, so a cell with no
artifacts must score 0.0 rather than raise.
"""
import inspect
import re
import shutil
from pathlib import Path

from tests.benchmark import cell_cost, harness


def _write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(line + "\n" for line in lines))


def _assert_zeros(spend: dict) -> None:
    assert set(spend) >= {"review_usd", "dispatch_usd", "total_usd"}
    for key in ("review_usd", "dispatch_usd", "total_usd"):
        assert isinstance(spend[key], float), f"{key} must be a float"
        assert spend[key] == 0.0, f"{key} must be 0.0 for a costless cell"


def test_review_jsonl_sums_into_review_usd(tmp_path):
    """review_token_costs.jsonl records carry no "type" field at all, so the
    review sum must not be filtered on it - unlike the dispatch raw log."""
    cell = tmp_path / "cell"
    _write_lines(
        cell / "worktrees" / "review_token_costs.jsonl",
        ['{"total_cost_usd": 0.01}', '{"total_cost_usd": 0.02}'],
    )
    _write_lines(
        cell / "worktrees" / "STORY-9" / "review_token_costs.jsonl",
        ['{"type": "assistant", "total_cost_usd": 0.04}'],
    )
    spend = cell_cost.claude_spend_usd(cell)
    assert spend["review_usd"] == 0.07
    assert spend["dispatch_usd"] == 0.0
    assert spend["total_usd"] == 0.07


def test_dispatch_raw_log_counts_only_result_events(tmp_path):
    """A raw log holds every stream-json line; only the terminal result event
    carries the run's cost, and a bool cost must not sneak in as an int."""
    cell = tmp_path / "cell"
    _write_lines(
        cell / "worktrees" / "STORY-1" / "agent.log.raw",
        [
            '{"type": "assistant", "total_cost_usd": 9.0}',
            '{"type": "result", "total_cost_usd": true}',
            '{"type": "result", "total_cost_usd": 0.5}',
        ],
    )
    spend = cell_cost.claude_spend_usd(cell)
    assert spend["dispatch_usd"] == 0.5
    assert spend["review_usd"] == 0.0
    assert spend["total_usd"] == 0.5


def test_cost_files_are_discovered_two_directories_deep(tmp_path):
    cell = tmp_path / "cell"
    _write_lines(
        cell / "runs" / "r1" / "review_token_costs.jsonl",
        ['{"total_cost_usd": 0.01}'],
    )
    _write_lines(
        cell / "runs" / "r1" / "wt" / "agent.log.raw",
        ['{"type": "result", "total_cost_usd": 0.02}'],
    )
    spend = cell_cost.claude_spend_usd(cell)
    assert spend["review_usd"] == 0.01
    assert spend["dispatch_usd"] == 0.02
    assert spend["total_usd"] == round(spend["review_usd"] + spend["dispatch_usd"], 6)


def test_malformed_and_non_numeric_cost_lines_are_skipped(tmp_path):
    cell = tmp_path / "cell"
    _write_lines(
        cell / "worktrees" / "review_token_costs.jsonl",
        [
            "not json at all",
            "[1, 2, 3]",
            '"a bare json string"',
            '{"total_cost_usd": null}',
            '{"total_cost_usd": "0.4"}',
            '{"total_cost_usd": true}',
            '{"type": "result"}',
            '{"total_cost_usd": 0.25}',
        ],
    )
    spend = cell_cost.claude_spend_usd(cell)
    assert spend["review_usd"] == 0.25
    assert spend["total_usd"] == 0.25


def test_empty_and_missing_cell_dirs_return_all_zeros(tmp_path):
    empty_cell = tmp_path / "empty"
    _write_lines(empty_cell / "worktrees" / "review_token_costs.jsonl", [])
    _assert_zeros(cell_cost.claude_spend_usd(empty_cell))
    _assert_zeros(cell_cost.claude_spend_usd(tmp_path / "does-not-exist"))
    # pipeline/review.py hands the cell dir over as a str, not a Path.
    spend = cell_cost.claude_spend_usd(str(tmp_path / "does-not-exist"))
    assert spend["review_usd"] == 0.0
    assert spend["dispatch_usd"] == 0.0
    assert spend["total_usd"] == 0.0


def test_unreadable_cost_files_contribute_zero(tmp_path):
    """A cost path pre-empted by a directory (a real hazard the review writer
    already guards for) is an OSError, not a crash."""
    cell = tmp_path / "cell"
    (cell / "worktrees").mkdir(parents=True)
    (cell / "worktrees" / "review_token_costs.jsonl").mkdir()
    (cell / "worktrees" / "broken.agent.log.raw").mkdir()
    _assert_zeros(cell_cost.claude_spend_usd(cell))


def test_amounts_are_rounded_to_six_decimal_places(tmp_path):
    cell = tmp_path / "cell"
    _write_lines(
        cell / "worktrees" / "review_token_costs.jsonl",
        ['{"total_cost_usd": 0.1234567}'],
    )
    spend = cell_cost.claude_spend_usd(cell)
    assert spend["review_usd"] == 0.123457
    assert spend["total_usd"] == 0.123457


def test_preserve_dispatch_log_copies_raw_log_next_to_worktree(tmp_path):
    worktree = tmp_path / "worktrees" / "STORY-1"
    raw = worktree / "agent.log.raw"
    _write_lines(raw, ['{"type": "result", "total_cost_usd": 0.5}'])

    dest = harness._preserve_dispatch_log(worktree, "STORY-1")

    assert dest == worktree.parent / "STORY-1.agent.log.raw"
    assert dest.read_text() == '{"type": "result", "total_cost_usd": 0.5}\n'
    # The point of the copy: the spend survives `git worktree remove --force`.
    raw.unlink()
    shutil.rmtree(worktree)
    assert cell_cost.claude_spend_usd(worktree.parent)["dispatch_usd"] == 0.5


def test_preserve_dispatch_log_appends_when_destination_already_exists(tmp_path):
    worktree = tmp_path / "worktrees" / "STORY-2"
    raw = worktree / "agent.log.raw"
    _write_lines(raw, ["first"])
    dest = harness._preserve_dispatch_log(worktree, "STORY-2")

    _write_lines(raw, ["second"])
    assert harness._preserve_dispatch_log(worktree, "STORY-2") == dest
    kept = dest.read_text()
    assert "first" in kept, "a later merge of the same key must not overwrite"
    assert "second" in kept


def test_preserve_dispatch_log_returns_none_without_a_raw_log(tmp_path):
    worktree = tmp_path / "worktrees" / "STORY-3"
    worktree.mkdir(parents=True)
    assert harness._preserve_dispatch_log(worktree, "STORY-3") is None
    # A worktree that is gone entirely, and a raw path that is not a file.
    assert harness._preserve_dispatch_log(tmp_path / "gone", "STORY-3") is None
    (worktree / "agent.log.raw").mkdir()
    assert harness._preserve_dispatch_log(worktree, "STORY-3") is None


def test_preserve_dispatch_log_is_module_level_above_install_merge_stubs():
    assert callable(harness._preserve_dispatch_log)
    helper_line = inspect.getsourcelines(harness._preserve_dispatch_log)[1]
    stubs_line = inspect.getsourcelines(harness.install_merge_stubs)[1]
    assert helper_line < stubs_line


def test_merge_stub_preserves_dispatch_log_before_worktree_removal():
    """The unit tests above still pass if the call is simply missing from
    _merge_pr_stub, so grade the wiring: the copy must happen before the
    `git worktree remove --force` that destroys the source log."""
    src = inspect.getsource(harness.install_merge_stubs)
    preserve_idx = src.find("_preserve_dispatch_log(")
    assert preserve_idx != -1, "_merge_pr_stub must call _preserve_dispatch_log"
    match = re.search(r'["\']worktree["\'],\s*["\']remove["\']', src)
    assert match is not None, "worktree removal call not found in the stubs"
    assert preserve_idx < match.start(), (
        "_preserve_dispatch_log must run before the worktree is removed"
    )