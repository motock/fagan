"""The standalone smoke must accept a status a file:// repo story can reach.

The smoke's repository is a local bare ``file://`` repo: there is no GitHub
remote, so a story can never open a PR or merge, and ``done`` is unreachable.
The furthest it gets is ``tests_passed`` (the agent ran in the container, the
clone worked and the grading job passed). ``poll_for_terminal_state`` used to
accept only ``done | merged | reviewed | completed | success``, of which only
``done`` is a real pipeline status, so a healthy run timed out after 600s with
``last: tests_passed``.

These are static text checks on ``deploy/standalone/smoke.sh``: no Docker, no
network.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE = REPO_ROOT / "deploy" / "standalone" / "smoke.sh"

# The pipeline's real story statuses (set_story_status accepts exactly these).
NOT_PIPELINE_STATUSES = ("merged", "reviewed", "completed", "success")
FAILURE_STATUSES = ("failed", "error", "interrupted", "blocked")


def poll_case_arms() -> tuple[set[str], set[str]]:
    """Return (success, failure) status names from poll_for_terminal_state's case."""
    text = SMOKE.read_text(encoding="utf-8")
    body = re.search(
        r"^poll_for_terminal_state\(\) \{\n(.*?)^\}", text, re.MULTILINE | re.DOTALL
    )
    assert body, "poll_for_terminal_state() not found in smoke.sh"
    arms = re.findall(
        r"^\s*([a-z_ |]+)\)\n(.*?);;", body.group(1), re.MULTILINE | re.DOTALL
    )
    assert len(arms) == 2, (
        f"expected a success arm and a failure arm, found {len(arms)}"
    )
    (success_pattern, _), (failure_pattern, _) = arms
    return (
        {name.strip() for name in success_pattern.split("|")},
        {name.strip() for name in failure_pattern.split("|")},
    )


def test_tests_passed_counts_as_the_smoke_succeeding() -> None:
    success, _ = poll_case_arms()

    assert "tests_passed" in success


def test_done_still_counts_as_the_smoke_succeeding() -> None:
    success, _ = poll_case_arms()

    assert "done" in success


def test_names_that_are_not_pipeline_statuses_are_not_accepted() -> None:
    success, _ = poll_case_arms()

    assert success.isdisjoint(NOT_PIPELINE_STATUSES)


def test_tests_passed_is_not_also_a_failure() -> None:
    _, failure = poll_case_arms()

    assert "tests_passed" not in failure


def test_failure_statuses_still_fail_the_smoke() -> None:
    _, failure = poll_case_arms()

    assert set(FAILURE_STATUSES) <= failure
