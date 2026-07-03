"""Hidden acceptance oracle for the ratelimiter_bugfix task.

Materialized read-only into the worktree before the agent runs. The agent is
told the reported bug but cannot edit this file; the pipeline grades the run
on whether the fix makes this pass. Deliberately narrower than groundtruth.py
(only the literally reported scenario, not the deeper 3-call variant) - same
tiered-oracle structure as the other tasks in this suite.
"""
from ratelimiter import RateLimiter


def test_reported_bug_two_consecutive_successes_then_check_at_same_time():
    rl = RateLimiter(2, 2, now=0.0)
    assert rl.allow(2, now=0.0) is True
    assert rl.allow(1, now=0.5) is True
    assert rl.allow(0.5, now=0.5) is False
