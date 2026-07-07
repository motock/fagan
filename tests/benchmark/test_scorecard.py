"""Tests for the TDD-skip yellow flag in scorecard.render.

The T2 ratelimiter_bugfix task explicitly asks the agent to add a regression
test, but a green scorecard today rewards an agent that fixes the impl and
skips the test. The scorecard must surface a yellow flag per (task, model)
group: any T2 cell where the agent's diff touched the impl but no test file
counts as a TDD-skip. The flag is a footnote column (mirroring
merged_wrong), NOT a failure of _is_success - TDD-skip is a process signal,
not a correctness one.
"""
import scorecard


def _cell(task, model, *, status="done", gt=True, impl_changed=True,
          test_changed=False, tier="T2", trial=0, merged=True, **overrides):
    base = {
        "task": task, "model": model, "trial": trial,
        "final_status": status, "merged": merged,
        "groundtruth_passed": gt, "groundtruth_ran": True,
        "review_verdict": "APPROVE", "rework_attempts": 0,
        "dispatch_attempts": 1, "dispatched_model": model,
        "task_tier": tier, "impl_changed": impl_changed,
        "test_changed": test_changed, "elapsed_s": 60.0, "ticks": 5,
        "timed_out": False, "groundtruth_where": "master",
    }
    base.update(overrides)
    return base


def test_scorecard_flags_t2_cell_with_impl_change_but_no_test_change():
    # The TDD-skip pattern: T2 ratelimiter_bugfix cell with impl changed and
    # no test changed. Rendered output must mention a TDD-skip annotation
    # so the human reviewing the scorecard notices the process violation.
    cells = [_cell("ratelimiter_bugfix", "gptoss",
                   impl_changed=True, test_changed=False)]
    md = scorecard.render(cells)
    assert "TDD-skip" in md, f"expected TDD-skip annotation in:\n{md}"


def test_scorecard_does_not_flag_t2_cell_with_both_changes():
    # T2 cell that wrote both impl and test (the compliant path) must NOT
    # be flagged - the TDD-skip count for that (task, model) group is 0.
    cells = [_cell("ratelimiter_bugfix", "gptoss",
                   impl_changed=True, test_changed=True)]
    stats = scorecard.aggregate(cells)
    assert stats[("ratelimiter_bugfix", "gptoss")]["tdd_skip"] == 0


def test_scorecard_does_not_flag_non_t2_cell_even_if_impl_only():
    # T1 (greenfield) task has no test-write expectation - impl-only is the
    # whole point. Must not be flagged as a TDD-skip.
    cells = [_cell("lru_cache", "gptoss", tier="T1",
                   impl_changed=True, test_changed=False)]
    stats = scorecard.aggregate(cells)
    assert stats[("lru_cache", "gptoss")]["tdd_skip"] == 0


def test_scorecard_tdd_skip_does_not_demote_success():
    # TDD-skip is a yellow flag, not a failure: the cell's success count
    # (in the per-model rollup) must still reflect done + gt-pass even
    # when tdd_skip is flagged. Otherwise the flag silently regresses the
    # model's headline number.
    cells = [_cell("ratelimiter_bugfix", "gptoss",
                   impl_changed=True, test_changed=False)]
    stats = scorecard.aggregate(cells)
    assert stats[("ratelimiter_bugfix", "gptoss")]["success"] == 1
    assert stats[("ratelimiter_bugfix", "gptoss")]["tdd_skip"] == 1


def test_aggregate_counts_tdd_skip_per_model():
    # aggregate() exposes tdd_skip as a stat so render() can pull it. Pin
    # the count directly: 2 T2 cells with impl_only + 1 compliant cell.
    cells = [
        _cell("ratelimiter_bugfix", "gptoss", trial=0,
              impl_changed=True, test_changed=False),
        _cell("ratelimiter_bugfix", "gptoss", trial=1,
              impl_changed=True, test_changed=False),
        _cell("ratelimiter_bugfix", "gptoss", trial=2,
              impl_changed=True, test_changed=True),
    ]
    stats = scorecard.aggregate(cells)
    assert stats[("ratelimiter_bugfix", "gptoss")]["tdd_skip"] == 2
